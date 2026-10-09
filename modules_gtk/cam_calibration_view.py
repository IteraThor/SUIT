"""Camera Calibration GTK4 / Libadwaita View.

Implements a streamlined multi-step fullscreen wizard:
1. Dashboard (Table of standard vs calibrated values + master toggle switch + start button)
2. Pre-Check (Simultaneous 3-camera live focus check on furthest double field)
3. Baseline (Untouched auto-exposure snapshot capture)
4. Setup (Scope: all vs single, Precision: standard vs deep)
5. Sweep (Simultaneous 3-camera live calibration preview with progress and abort)
6. Review (Simultaneous 3-camera A/B comparison with edge & contrast filters)
"""

from __future__ import annotations
import logging
import threading
import time
from typing import Dict, List, Optional, Tuple
from pathlib import Path
import math
import cv2
import numpy as np

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk

from core.board_detector import BoardDetector
from core.diagnostics import HardwareDiagnostics, DiagnosticResult
from core.optimizer import CameraOptimizer, OptimizationResult
from core.persistence_service import PersistenceService
from core.systemd_service import SystemdManager
from core.v4l2_device import CameraSettings, V4L2Device
from core.v4l2_topology import CameraDevice, V4L2Topology

logger = logging.getLogger("cam_librator.view")


def frame_to_pixbuf(
    frame: np.ndarray,
    zoom_factor: float = 1.0,
    center_x: Optional[int] = None,
    center_y: Optional[int] = None,
    max_w: int = 560,
    max_h: int = 380,
) -> Optional[GdkPixbuf.Pixbuf]:
    """Convert an OpenCV BGR frame to a scaled GdkPixbuf with optional ROI zoom."""
    if frame is None or frame.size == 0:
        return None
    h, w = frame.shape[:2]

    if zoom_factor > 1.05 and center_x is not None and center_y is not None:
        crop_w = int(w / zoom_factor)
        crop_h = int(h / zoom_factor)
        x1 = max(0, min(w - crop_w, int(center_x - crop_w / 2)))
        y1 = max(0, min(h - crop_h, int(center_y - crop_h / 2)))
        x2 = min(w, x1 + crop_w)
        y2 = min(h, y1 + crop_h)
        cropped = frame[y1:y2, x1:x2]
    else:
        cropped = frame

    ch, cw = cropped.shape[:2]
    scale = min(max_w / cw, max_h / ch)
    nw, nh = max(1, int(cw * scale)), max(1, int(ch * scale))
    resized = cv2.resize(cropped, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

    return GdkPixbuf.Pixbuf.new_from_data(
        rgb.tobytes(),
        GdkPixbuf.Colorspace.RGB,
        False,
        8,
        nw,
        nh,
        nw * 3,
    )


def extract_magnified_pixbuf(
    frame: np.ndarray,
    center: Optional[Tuple[int, int]] = None,
    zoom_factor: float = 2.5,
    max_w: int = 560,
    max_h: int = 380,
    filter_mode: str = "normal",
    show_reticle: bool = True,
    diff_reference_frame: Optional[np.ndarray] = None,
) -> Tuple[Optional[GdkPixbuf.Pixbuf], Tuple[int, int, int, int]]:
    """Extract a magnified ROI crop, apply optional filter, and convert to Pixbuf.
    
    Returns:
        (pixbuf, (crop_x1, crop_y1, crop_w, crop_h)) in original frame coordinates.
    """
    if frame is None or frame.size == 0:
        return None, (0, 0, 0, 0)
    fh, fw = frame.shape[:2]

    if zoom_factor > 1.05 and center is not None:
        crop_w = max(20, int(fw / zoom_factor))
        crop_h = max(20, int(fh / zoom_factor))
        cx, cy = center
        x1 = max(0, min(fw - crop_w, int(cx - crop_w / 2)))
        y1 = max(0, min(fh - crop_h, int(cy - crop_h / 2)))
        x2 = min(fw, x1 + crop_w)
        y2 = min(fh, y1 + crop_h)
        cropped = frame[y1:y2, x1:x2].copy()
        diff_ref_cropped = diff_reference_frame[y1:y2, x1:x2].copy() if (diff_reference_frame is not None and diff_reference_frame.size > 0) else None
    else:
        x1, y1, crop_w, crop_h = 0, 0, fw, fh
        cropped = frame.copy()
        diff_ref_cropped = diff_reference_frame.copy() if (diff_reference_frame is not None and diff_reference_frame.size > 0) else None

    if filter_mode == "diff" and diff_ref_cropped is not None:
        filtered = apply_difference_filter(diff_ref_cropped, cropped)
    elif filter_mode == "edge":
        filtered = apply_edge_contrast_filter(cropped)
    elif filter_mode == "noise":
        filtered = apply_noise_inspector_filter(cropped)
    else:
        filtered = cropped

    ch, cw = filtered.shape[:2]
    scale = min(max_w / cw, max_h / ch)
    nw, nh = max(1, int(cw * scale)), max(1, int(ch * scale))
    resized = cv2.resize(filtered, (nw, nh), interpolation=cv2.INTER_LINEAR if zoom_factor > 1.05 else cv2.INTER_AREA)

    if show_reticle and zoom_factor > 1.05:
        rcx, rcy = nw // 2, nh // 2
        cv2.drawMarker(resized, (rcx, rcy), (18, 20, 24), markerType=cv2.MARKER_CROSS, markerSize=20, thickness=3, line_type=cv2.LINE_AA)
        cv2.drawMarker(resized, (rcx, rcy), (0, 255, 200), markerType=cv2.MARKER_CROSS, markerSize=18, thickness=1, line_type=cv2.LINE_AA)

    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
    pix = GdkPixbuf.Pixbuf.new_from_data(
        rgb.tobytes(),
        GdkPixbuf.Colorspace.RGB,
        False,
        8,
        nw,
        nh,
        nw * 3,
    )
    return pix, (x1, y1, crop_w, crop_h)


def apply_difference_filter(empty_frame: np.ndarray, dart_frame: np.ndarray) -> np.ndarray:
    """Isolate dart difference signal and colorize with high-contrast heatmap."""
    if empty_frame is None or dart_frame is None or empty_frame.size == 0 or dart_frame.size == 0:
        return dart_frame if dart_frame is not None else empty_frame
    
    if empty_frame.shape[:2] != dart_frame.shape[:2]:
        empty_frame = cv2.resize(empty_frame, (dart_frame.shape[1], dart_frame.shape[0]))

    g_empty = cv2.cvtColor(empty_frame, cv2.COLOR_BGR2GRAY) if len(empty_frame.shape) == 3 else empty_frame
    g_dart = cv2.cvtColor(dart_frame, cv2.COLOR_BGR2GRAY) if len(dart_frame.shape) == 3 else dart_frame
    
    diff = cv2.absdiff(g_empty, g_dart)
    amplified = np.clip(diff.astype(np.float32) * 3.5, 0, 255).astype(np.uint8)
    heatmap = cv2.applyColorMap(amplified, cv2.COLORMAP_TURBO)
    
    dart_rgb = cv2.cvtColor(g_dart, cv2.COLOR_GRAY2BGR)
    blended = cv2.addWeighted(heatmap, 0.65, dart_rgb, 0.35, 0)
    return blended


def detect_dart_tip(
    empty_frame: np.ndarray,
    dart_frame: np.ndarray,
    roi: Optional[BoardROI] = None,
) -> Optional[Tuple[int, int]]:
    """Locate the exact pixel coordinate of the inserted dart tip using background subtraction."""
    if empty_frame is None or dart_frame is None or empty_frame.size == 0 or dart_frame.size == 0:
        return None
    
    if empty_frame.shape[:2] != dart_frame.shape[:2]:
        empty_frame = cv2.resize(empty_frame, (dart_frame.shape[1], dart_frame.shape[0]))

    g_empty = cv2.cvtColor(empty_frame, cv2.COLOR_BGR2GRAY) if len(empty_frame.shape) == 3 else empty_frame
    g_dart = cv2.cvtColor(dart_frame, cv2.COLOR_BGR2GRAY) if len(dart_frame.shape) == 3 else dart_frame
    
    diff = cv2.absdiff(g_empty, g_dart)
    if roi and roi.board_mask is not None:
        diff[roi.board_mask == 0] = 0

    _, thresh = cv2.threshold(diff, 18, 255, cv2.THRESH_BINARY)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)
    cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    valid_contours = [c for c in contours if cv2.contourArea(c) > 60]
    if not valid_contours:
        return None

    largest_cnt = max(valid_contours, key=cv2.contourArea)
    pts = largest_cnt.reshape(-1, 2)
    
    if roi and roi.center:
        bcx, bcy = roi.center
        tip_pt = min(pts, key=lambda p: (p[0] - bcx) ** 2 + (p[1] - bcy) ** 2)
        return (int(tip_pt[0]), int(tip_pt[1]))
    else:
        M = cv2.moments(largest_cnt)
        if M["m00"] > 0:
            return (int(M["m10"] / M["m00"]), int(M["m01"] / M["m00"]))
        return (int(pts[0][0]), int(pts[0][1]))


def apply_edge_contrast_filter(frame: np.ndarray) -> np.ndarray:
    """Generate colorized Sobel edge gradient map highlighting wire steepness."""
    if frame is None or frame.size == 0:
        return frame
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    mag = np.clip(mag * 2.2, 0, 255).astype(np.uint8)
    return cv2.applyColorMap(mag, cv2.COLORMAP_VIRIDIS)


def apply_noise_inspector_filter(frame: np.ndarray) -> np.ndarray:
    """Isolate high-frequency sensor grain / noise by subtracting low-pass structure and amplifying 7x."""
    if frame is None or frame.size == 0:
        return frame
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
    blurred = cv2.GaussianBlur(gray, (5, 5), 1.2)
    residual = gray.astype(np.float32) - blurred.astype(np.float32)
    amplified = np.clip(128.0 + residual * 7.0, 0, 255).astype(np.uint8)
    return cv2.cvtColor(amplified, cv2.COLOR_GRAY2BGR)


def compute_suit_sharpness(patch: np.ndarray) -> float:
    """Compute normalized sharpness score (0-100) tuned for dartboard blade wire optics."""
    if patch is None or patch.size == 0:
        return 0.0
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY) if len(patch.shape) == 3 else patch
    lap = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    ten = float(np.mean(gx**2 + gy**2))
    log_lap = math.log10(lap + 1e-6)
    log_ten = math.log10(ten + 1e-6)
    norm_lap = np.clip((log_lap - 1.2) / (3.35 - 1.2), 0.0, 1.0)
    norm_ten = np.clip((log_ten - 2.2) / (4.70 - 2.2), 0.0, 1.0)
    blended = 0.5 * norm_lap + 0.5 * norm_ten
    return float(np.clip(blended * 100.0, 0.0, 100.0))


def process_zoom_crop_suit(
    frame: np.ndarray,
    target_roi: Tuple[int, int, int, int],
    accum_crop: Optional[np.ndarray] = None,
    zoom_factor: float = 2.5,
    out_size: Tuple[int, int] = (440, 275),
    score: float = 0.0,
) -> Tuple[np.ndarray, np.ndarray]:
    """Extract digital zoom crop with temporal smoothing, neon-green wire focus peaking, and reticle."""
    fh, fw = frame.shape[:2]
    tx, ty, tw, th = target_roi
    if tw > 0 and th > 0:
        cx = tx + tw // 2
        cy = ty + th // 2
    else:
        cx = fw // 2
        cy = fh // 4

    target_w, target_h = out_size
    cw = max(20, int(target_w / zoom_factor))
    ch = max(20, int(target_h / zoom_factor))

    x1 = cx - cw // 2
    y1 = cy - ch // 2
    x2 = x1 + cw
    y2 = y1 + ch

    pad_top = max(0, -y1)
    pad_bottom = max(0, y2 - fh)
    pad_left = max(0, -x1)
    pad_right = max(0, x2 - fw)

    if pad_top > 0 or pad_bottom > 0 or pad_left > 0 or pad_right > 0:
        padded = cv2.copyMakeBorder(
            frame, pad_top, pad_bottom, pad_left, pad_right, cv2.BORDER_CONSTANT, value=[10, 12, 15]
        )
        crop = padded[y1 + pad_top : y2 + pad_top, x1 + pad_left : x2 + pad_left]
    else:
        crop = frame[y1:y2, x1:x2]

    if crop.size == 0:
        return frame, accum_crop

    # Temporal smoothing to stabilize sensor noise
    if accum_crop is None or accum_crop.shape != crop.shape:
        accum_crop = crop.astype(np.float32)
    else:
        accum_crop = cv2.addWeighted(
            crop.astype(np.float32), 0.35, accum_crop, 0.65, 0
        )
    clean_crop = np.clip(accum_crop, 0, 255).astype(np.uint8)

    # Focus peaking edge filter
    gray_crop = cv2.cvtColor(clean_crop, cv2.COLOR_BGR2GRAY)
    gx = cv2.Sobel(gray_crop, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray_crop, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    peak_mask = mag > 260.0

    if np.any(peak_mask):
        peaked_crop = clean_crop.copy()
        # Highlight sharp wire edges in bright neon green [40, 255, 60] (BGR)
        peaked_crop[peak_mask] = cv2.addWeighted(
            clean_crop[peak_mask], 0.35, np.full_like(clean_crop[peak_mask], [60, 255, 40]), 0.65, 0
        )
        render_crop = peaked_crop
    else:
        render_crop = clean_crop

    resized = cv2.resize(render_crop, out_size, interpolation=cv2.INTER_LINEAR)

    # Draw center reticle / crosshairs
    rw, rh = out_size
    rcx, rcy = rw // 2, rh // 2
    cv2.line(resized, (rcx - 14, rcy), (rcx - 4, rcy), (255, 255, 255), 1)
    cv2.line(resized, (rcx + 4, rcy), (rcx + 14, rcy), (255, 255, 255), 1)
    cv2.line(resized, (rcx, rcy - 14), (rcx, rcy - 4), (255, 255, 255), 1)
    cv2.line(resized, (rcx, rcy + 4), (rcx, rcy + 14), (255, 255, 255), 1)

    # Status border indicator
    if score >= 85.0:
        border_color = (46, 194, 78)  # Emerald green
        thickness = 3
    elif score >= 55.0:
        border_color = (10, 165, 229)  # Amber
        thickness = 2
    else:
        border_color = (36, 28, 224)  # Red
        thickness = 1
    cv2.rectangle(resized, (1, 1), (rw - 2, rh - 2), border_color, thickness)

    return resized, accum_crop


def draw_sweet_spot_overlay_suit(
    frame: np.ndarray,
    target_roi: Tuple[int, int, int, int],
    score: float,
) -> np.ndarray:
    """Draw SUIT-style Sweet-Spot Target Box and Reticle on the full frame."""
    out = frame.copy()
    tx, ty, tw, th = target_roi
    is_locked = score >= 85.0

    if is_locked:
        color = (46, 194, 78)  # Emerald green
        thickness = 3
    elif score >= 55.0:
        color = (10, 165, 229)  # Amber
        thickness = 2
    else:
        color = (229, 132, 53)  # Blue
        thickness = 2

    # Bounding box
    cv2.rectangle(out, (tx, ty), (tx + tw, ty + th), color, thickness)

    # Reticle crosshairs
    tcx, tcy = tx + tw // 2, ty + th // 2
    cv2.line(out, (tcx - 16, tcy), (tcx + 16, tcy), color, 1)
    cv2.line(out, (tcx, tcy - 12), (tcx, tcy + 12), color, 1)

    # Label box
    lbl = "SWEET SPOT" if is_locked else f"TARGET ({score:.0f}%)"
    font = cv2.FONT_HERSHEY_SIMPLEX
    (tw_t, th_t), _ = cv2.getTextSize(lbl, font, 0.45, 1)
    ly1 = max(0, ty - th_t - 8)
    ly2 = ty
    lx1 = tx
    lx2 = tx + tw_t + 12
    cv2.rectangle(out, (lx1, ly1), (lx2, ly2), (18, 20, 24), -1)
    cv2.putText(out, lbl, (lx1 + 6, ly2 - 4), font, 0.45, color, 1, cv2.LINE_AA)

    return out


class CamCalibrationView(Adw.Bin):
    """Multi-Step Fullscreen Camera Calibration Wizard."""

    def __init__(self, on_back_callback=None):
        super().__init__()
        self.on_back = on_back_callback
        self.topology = V4L2Topology()
        self.persistence = PersistenceService()
        self.detector = BoardDetector()
        self.diagnostics = HardwareDiagnostics(self.detector)

        self.cameras: List[CameraDevice] = []
        self.baseline_frames: Dict[str, np.ndarray] = {}
        self.results: Dict[str, OptimizationResult] = {}

        # Wizard state
        self.selected_scope: str = "all"  # "all" or dev_node
        self.selected_precision: str = "deep"  # "standard" or "deep"
        self.focus_zoom_all: bool = False
        self.focus_per_cam_zoom: Dict[str, bool] = {}
        self.accum_crops: Dict[str, np.ndarray] = {}

        # Review & Comparison state
        self.review_selected_cam: Optional[str] = None  # None for Overview, or dev_node for Magnified Single Cam
        self.review_source_mode: str = "empty"  # "empty" or "dart"
        self.review_zoom_zone: str = "sweet_spot"  # "dart_tip", "sweet_spot", "bullseye", "treble", "center", "custom"
        self.review_custom_coord: Optional[Tuple[int, int]] = None
        self.review_zoom_factor: float = 2.5
        self.active_review_mode: str = "after"  # "before" or "after" for overview
        self.active_review_filter: str = "normal"  # "normal", "edge", "diff", "noise"
        self._magnifier_crop_coords: Dict[str, Tuple[int, int, int, int]] = {}

        # Dart Test Verification state
        self.dart_frames_before: Dict[str, np.ndarray] = {}
        self.dart_frames_after: Dict[str, np.ndarray] = {}
        self.dart_tips: Dict[str, Tuple[int, int]] = {}
        self.dart_capturing: bool = False

        # Background worker state
        self.focus_running: bool = False
        self.focus_thread: Optional[threading.Thread] = None
        self.focus_stop_event: threading.Event = threading.Event()
        self.sweep_running: bool = False
        self.cancel_event: Optional[threading.Event] = None
        self._autodarts_was_running: bool = False

        # Build UI layout
        self._build_ui()
        self.refresh_cameras()

    def _build_ui(self):
        """Construct the Adw.ViewStack and all wizard pages."""
        self.stack = Adw.ViewStack()
        self.stack.set_vexpand(True)
        self.stack.set_hexpand(True)

        # 1. Dashboard Page
        self.page_dashboard = self._build_dashboard_page()
        self.stack.add_named(self.page_dashboard, "dashboard")

        # 2. Focus Check Page (Voranalyse)
        self.page_focus = self._build_focus_page()
        self.stack.add_named(self.page_focus, "focus")

        # 3. Baseline Snapshot Page
        self.page_baseline = self._build_baseline_page()
        self.stack.add_named(self.page_baseline, "baseline")

        # 4. Setup Options Page
        self.page_setup = self._build_setup_page()
        self.stack.add_named(self.page_setup, "setup")

        # 5. Live Calibration Sweep Page
        self.page_sweep = self._build_sweep_page()
        self.stack.add_named(self.page_sweep, "sweep")

        # 6. Review / Comparison Page
        self.page_review = self._build_review_page()
        self.stack.add_named(self.page_review, "review")

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(self.stack)
        self.set_child(self.toast_overlay)
        self.stack.set_visible_child_name("dashboard")

    # =========================================================================
    # PAGE 1: STARTMENU / DASHBOARD
    # =========================================================================
    def _build_dashboard_page(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        box.set_margin_top(24)
        box.set_margin_bottom(32)
        box.set_margin_start(32)
        box.set_margin_end(32)

        # Top Header
        header_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title = Gtk.Label(label="Camera Overview & Profiles", xalign=0.0)
        title.add_css_class("wizard-title")
        header_box.append(title)

        subtitle = Gtk.Label(
            label="Hardware-level calibration for maximum Autodarts accuracy at 0% CPU load.",
            xalign=0.0,
        )
        subtitle.add_css_class("wizard-subtitle")
        header_box.append(subtitle)
        box.append(header_box)

        # 3-Camera Status Cards (Side-by-Side in Fullscreen)
        self.dash_cards_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16, homogeneous=True)
        self.dash_cards_box.set_hexpand(True)
        box.append(self.dash_cards_box)

        # Activation Control Group
        pref_group = Adw.PreferencesGroup()
        pref_group.set_title("Hardware Profile Control")

        self.dash_active_switch = Adw.SwitchRow(
            title="Enable Calibrated Profiles",
            subtitle="Applies calibrated hardware values at boot and during runtime",
        )
        self.dash_active_switch.connect("notify::active", self._on_dash_switch_toggled)
        pref_group.add(self.dash_active_switch)

        self.dash_reset_row = Adw.ActionRow(
            title="Reset Calibrated Values",
            subtitle="Deletes saved hardware profiles and restores all cameras to defaults",
        )
        self.dash_reset_btn = Gtk.Button(label="Reset")
        self.dash_reset_btn.add_css_class("destructive-action")
        self.dash_reset_btn.add_css_class("pill")
        self.dash_reset_btn.set_valign(Gtk.Align.CENTER)
        self.dash_reset_btn.connect("clicked", self._on_dash_reset_clicked)
        self.dash_reset_row.add_suffix(self.dash_reset_btn)
        pref_group.add(self.dash_reset_row)

        box.append(pref_group)

        # Big Next CTA Button (Bottom Center)
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        btn_box.set_halign(Gtk.Align.CENTER)
        btn_box.set_margin_top(16)

        start_calib_btn = Gtk.Button(label="Start Calibration")
        start_calib_btn.add_css_class("suggested-action")
        start_calib_btn.add_css_class("pill")
        start_calib_btn.add_css_class("big-cta-button")
        start_calib_btn.connect("clicked", lambda _: self._go_to_focus())
        btn_box.append(start_calib_btn)
        box.append(btn_box)

        scroll.set_child(box)
        return scroll

    def _update_dashboard(self):
        """Update dashboard table with current calibrated vs standard values."""
        # Clear existing cards
        while child := self.dash_cards_box.get_first_child():
            self.dash_cards_box.remove(child)

        calibrations = self.persistence.load_calibrations()
        has_any_calib = len(calibrations) > 0

        # Update Master Switch and Reset Button sensitivity
        self.dash_active_switch.handler_block_by_func(self._on_dash_switch_toggled)
        if has_any_calib:
            self.dash_active_switch.set_sensitive(True)
            self.dash_active_switch.set_active(self.persistence.is_calibration_enabled())
            if self.persistence.is_calibration_enabled():
                self.dash_active_switch.set_subtitle("Optimized profiles are active and enabled in boot service.")
            else:
                self.dash_active_switch.set_subtitle("Cameras are running in default automatic mode.")
        else:
            self.dash_active_switch.set_sensitive(False)
            self.dash_active_switch.set_active(False)
            self.dash_active_switch.set_subtitle("No calibration found — Please start calibration.")
        self.dash_active_switch.handler_unblock_by_func(self._on_dash_switch_toggled)

        # Always allow resetting hardware to defaults
        self.dash_reset_row.set_sensitive(True)
        self.dash_reset_btn.set_sensitive(True)

        for cam in self.cameras:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            card.add_css_class("cam-column-card")

            hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            title = Gtk.Label(label=f"Camera {cam.index}", xalign=0.0)
            title.add_css_class("heading")
            hdr.append(title)

            calib = calibrations.get(cam.by_path)
            badge = Gtk.Label()
            if calib:
                badge.set_text("Calibrated")
                badge.set_css_classes(["status-pill-ok"])
            else:
                badge.set_text("Default")
                badge.set_css_classes(["status-pill-warn"])
            badge.set_hexpand(True)
            badge.set_xalign(1.0)
            hdr.append(badge)
            card.append(hdr)

            # Node label
            node_lbl = Gtk.Label(label=f"Node: {cam.dev_node}", xalign=0.0)
            node_lbl.add_css_class("dim-label")
            card.append(node_lbl)

            # Parameter Table: Before vs. After
            grid = Gtk.Grid()
            grid.set_column_spacing(12)
            grid.set_row_spacing(6)
            grid.set_margin_top(8)

            def add_row(r: int, name: str, std_val: str, cal_val: str):
                nl = Gtk.Label(label=name, xalign=0.0)
                nl.add_css_class("dim-label")
                grid.attach(nl, 0, r, 1, 1)

                vl = Gtk.Label(label=f"{std_val}  →  {cal_val}", xalign=1.0)
                vl.set_hexpand(True)
                if calib:
                    vl.add_css_class("accent")
                grid.attach(vl, 1, r, 1, 1)

            if calib:
                exp_str = f"{calib.exposure_absolute * 0.1:.1f}ms" if calib.exposure_auto == 1 else "Auto"
                add_row(0, "Exposure:", "Auto", exp_str)
                add_row(1, "Sensor Gain:", "64", str(calib.gain))
                add_row(2, "Contrast:", "34", str(calib.contrast))
                add_row(3, "Sharpness:", "40", str(calib.sharpness))
                add_row(4, "White Balance:", "Auto", "Auto")
            else:
                add_row(0, "Exposure:", "Auto", "—")
                add_row(1, "Sensor Gain:", "Auto / 64", "—")
                add_row(2, "Contrast:", "34", "—")
                add_row(3, "Sharpness:", "40", "—")
                add_row(4, "White Balance:", "Auto", "Auto")

            card.append(grid)
            self.dash_cards_box.append(card)

    def _on_dash_switch_toggled(self, switch, _):
        """Enable or disable calibrations system-wide."""
        is_active = switch.get_active()
        self.persistence.set_calibration_enabled(is_active)
        # Apply to live devices immediately
        calibrations = self.persistence.load_calibrations()
        for cam in self.cameras:
            try:
                vdev = V4L2Device(cam.dev_node)
                if is_active and cam.by_path in calibrations:
                    vdev.apply_settings(calibrations[cam.by_path])
                else:
                    vdev.reset_to_defaults()
            except Exception as e:
                logger.warning("Error applying switch toggle on %s: %s", cam.dev_node, e)
        self._update_dashboard()

    def _on_dash_reset_clicked(self, _btn):
        """Reset all camera hardware settings to factory defaults and clear stored calibrations."""
        for cam in self.cameras:
            try:
                dev = V4L2Device(cam.dev_node)
                dev.reset_to_defaults()
            except Exception as e:
                logger.warning("Failed to reset camera %s to defaults: %s", cam.dev_node, e)

        self.persistence.clear_calibrations()
        self.results.clear()
        self._update_dashboard()
        if hasattr(self, "toast_overlay"):
            self.toast_overlay.add_toast(Adw.Toast(title="Calibrated profiles deleted — Defaults active"))

    # =========================================================================
    # PAGE 2: VORANALYSE (FOKUS-CHECK)
    # =========================================================================
    def _build_focus_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(16)
        box.set_margin_bottom(20)
        box.set_margin_start(24)
        box.set_margin_end(24)

        # Header
        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        top_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label="Step 1 of 5: Focus Check", xalign=0.0)
        title.add_css_class("wizard-title")
        top_txt.append(title)
        sub = Gtk.Label(
            label="Rotate lenses manually until spider wires glow green and the sweet spot is sharp.",
            xalign=0.0,
        )
        sub.add_css_class("wizard-subtitle")
        top_txt.append(sub)
        top_bar.append(top_txt)

        # Global Zoom & Peaking Toggle button on the right
        self.focus_zoom_btn = Gtk.Button(label="3x Zoom & Peaking (All)")
        self.focus_zoom_btn.add_css_class("pill")
        self.focus_zoom_btn.set_valign(Gtk.Align.CENTER)
        self.focus_zoom_btn.set_hexpand(True)
        self.focus_zoom_btn.set_halign(Gtk.Align.END)
        self.focus_zoom_btn.connect("clicked", self._on_toggle_focus_zoom)
        top_bar.append(self.focus_zoom_btn)
        box.append(top_bar)

        # 3 Camera Columns (Side-by-Side)
        self.focus_columns_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14, homogeneous=True)
        self.focus_columns_box.set_vexpand(True)
        box.append(self.focus_columns_box)

        # Bottom Bar: Back and Next
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        bottom_bar.set_margin_top(10)

        back_btn = Gtk.Button(label="Back to Dashboard")
        back_btn.add_css_class("pill")
        back_btn.connect("clicked", lambda _: self._go_to_dashboard())
        bottom_bar.append(back_btn)

        self.focus_next_btn = Gtk.Button(label="Continue to Baseline")
        self.focus_next_btn.add_css_class("suggested-action")
        self.focus_next_btn.add_css_class("pill")
        self.focus_next_btn.set_hexpand(True)
        self.focus_next_btn.set_halign(Gtk.Align.END)
        self.focus_next_btn.connect("clicked", lambda _: self._go_to_baseline())
        bottom_bar.append(self.focus_next_btn)
        box.append(bottom_bar)

        return box

    def _setup_focus_columns(self):
        """Create rich SUIT focus assistant cards for each camera."""
        while child := self.focus_columns_box.get_first_child():
            self.focus_columns_box.remove(child)

        self.focus_cam_widgets = {}
        self.accum_crops = {}

        for cam in self.cameras:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            card.add_css_class("cam-column-card")

            # Header: Cam Title (left) + Live Gauge Score (right)
            hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            title = Gtk.Label(label=f"Camera {cam.index}", xalign=0.0)
            title.add_css_class("heading")
            hdr.append(title)

            score_lbl = Gtk.Label(label="0.0", xalign=1.0)
            score_lbl.add_css_class("focus-gauge-score")
            score_lbl.add_css_class("score-poor")
            score_lbl.set_hexpand(True)
            hdr.append(score_lbl)
            card.append(hdr)

            # Subheader: Trend Badge (left), Peak Badge (right)
            meta_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            trend_badge = Gtk.Label(label="Adjusting...")
            trend_badge.add_css_class("focus-trend-badge")
            trend_badge.add_css_class("trend-tuning")
            meta_box.append(trend_badge)

            peak_btn = Gtk.Button(label="Peak: 0.0  ↺")
            peak_btn.add_css_class("flat")
            peak_btn.add_css_class("peak-badge")
            peak_btn.set_tooltip_text("Click to reset peak value")
            peak_btn.set_hexpand(True)
            peak_btn.set_halign(Gtk.Align.END)
            peak_btn.connect("clicked", self._make_reset_peak_handler(cam.dev_node))
            meta_box.append(peak_btn)
            card.append(meta_box)

            # Progress bar
            pbar = Gtk.ProgressBar()
            pbar.set_fraction(0.0)
            card.append(pbar)

            # Video Picture
            pic = Gtk.Picture()
            pic.set_can_shrink(True)
            pic.set_size_request(160, 110)
            pic.set_hexpand(True)
            pic.set_vexpand(True)
            card.append(pic)

            # Controls row under picture: Zoom & Peaking toggle + Reset button
            ctrls_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            mode_btn = Gtk.Button(label="Zoom & Peaking")
            mode_btn.add_css_class("pill")
            mode_btn.connect("clicked", self._make_cam_zoom_handler(cam.dev_node))
            ctrls_row.append(mode_btn)

            reset_btn = Gtk.Button(label="Reset Peak")
            reset_btn.add_css_class("pill")
            reset_btn.set_hexpand(True)
            reset_btn.set_halign(Gtk.Align.END)
            reset_btn.connect("clicked", self._make_reset_peak_handler(cam.dev_node))
            ctrls_row.append(reset_btn)

            card.append(ctrls_row)

            self.focus_cam_widgets[cam.dev_node] = {
                "score_lbl": score_lbl,
                "trend_badge": trend_badge,
                "peak_btn": peak_btn,
                "pbar": pbar,
                "picture": pic,
                "mode_btn": mode_btn,
                "peak_score": 0.0,
                "last_score": 0.0,
            }
            self.focus_columns_box.append(card)

    def _make_reset_peak_handler(self, dev_node: str):
        def _handler(_btn):
            if dev_node in self.focus_cam_widgets:
                self.focus_cam_widgets[dev_node]["peak_score"] = 0.0
                self.focus_cam_widgets[dev_node]["peak_btn"].set_label("Peak: 0.0  ↺")
            self.detector.unlock_target(dev_node)
            self.accum_crops[dev_node] = None
        return _handler

    def _make_cam_zoom_handler(self, dev_node: str):
        def _handler(btn):
            cur = self.focus_per_cam_zoom.get(dev_node, self.focus_zoom_all)
            new_val = not cur
            self.focus_per_cam_zoom[dev_node] = new_val
            btn.set_label("Full Frame" if new_val else "Zoom & Peaking")
        return _handler

    def _on_toggle_focus_zoom(self, btn):
        self.focus_zoom_all = not self.focus_zoom_all
        btn.set_label("Full Frame (All)" if self.focus_zoom_all else "3x Zoom & Peaking (All)")
        for node in self.focus_cam_widgets:
            self.focus_per_cam_zoom[node] = self.focus_zoom_all
            w = self.focus_cam_widgets[node]
            w["mode_btn"].set_label("Full Frame" if self.focus_zoom_all else "Zoom & Peaking")

    def _start_focus_stream(self):
        """Start background capture thread for 3-camera live focus check."""
        self._stop_focus_stream()
        self.focus_stop_event = threading.Event()
        self.focus_running = True
        self.focus_thread = threading.Thread(
            target=self._focus_stream_worker,
            args=(self.focus_stop_event,),
            daemon=True,
        )
        self.focus_thread.start()

    def _stop_focus_stream(self):
        self.focus_running = False
        if hasattr(self, "focus_stop_event") and self.focus_stop_event is not None:
            self.focus_stop_event.set()
        if hasattr(self, "focus_thread") and self.focus_thread and self.focus_thread.is_alive():
            self.focus_thread.join(timeout=4.0)
        self.focus_thread = None
        self.accum_crops.clear()

    def _focus_stream_worker(self, stop_event: threading.Event):
        """Round-robin capture across all active cameras with SUIT focus peaking & target reticle."""
        caps: Dict[str, cv2.VideoCapture] = {}

        try:
            for cam in self.cameras:
                if stop_event.is_set():
                    break
                try:
                    vdev = V4L2Device(cam.dev_node)
                    cap = vdev._open_capture(width=1280, height=960)
                    if cap is not None and cap.isOpened():
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                        caps[cam.dev_node] = cap
                except Exception as e:
                    logger.warning("Could not open cap for %s: %s", cam.dev_node, e)

            while not stop_event.is_set():
                for cam in self.cameras:
                    if stop_event.is_set():
                        break
                    cap = caps.get(cam.dev_node)
                    if not cap or not cap.isOpened():
                        continue

                    ret, frame = cap.read()
                    if not ret or frame is None or frame.size == 0:
                        continue

                    # Cache untouched auto-exposure frame for Baseline Reference
                    self.baseline_frames[cam.dev_node] = frame.copy()

                    target_roi = self.detector.get_furthest_double_roi(
                        frame,
                        dev_node=cam.dev_node,
                        by_path=cam.by_path,
                        cam_idx=cam.index,
                    )

                    tx, ty, tw, th = target_roi
                    patch = frame[ty : ty + th, tx : tx + tw]
                    score = compute_suit_sharpness(patch)

                    is_cam_zoom = self.focus_per_cam_zoom.get(cam.dev_node, self.focus_zoom_all)

                    if is_cam_zoom:
                        rendered, self.accum_crops[cam.dev_node] = process_zoom_crop_suit(
                            frame,
                            target_roi,
                            accum_crop=self.accum_crops.get(cam.dev_node),
                            zoom_factor=2.5,
                            out_size=(440, 275),
                            score=score,
                        )
                    else:
                        rendered = draw_sweet_spot_overlay_suit(frame, target_roi, score=score)

                    pix = frame_to_pixbuf(rendered, max_w=480, max_h=300)
                    GLib.idle_add(self._update_focus_ui, cam.dev_node, score, pix)

                if not stop_event.is_set():
                    time.sleep(0.035)  # ~28 FPS composite rate
        finally:
            for cap in list(caps.values()):
                try:
                    cap.release()
                except Exception:
                    pass
            caps.clear()

    def _update_focus_ui(self, dev_node: str, score: float, pixbuf: Optional[GdkPixbuf.Pixbuf]):
        if not self.focus_running or dev_node not in self.focus_cam_widgets:
            return False

        w = self.focus_cam_widgets[dev_node]
        last_s = w["last_score"]
        w["last_score"] = score

        # Peak update
        if score > w["peak_score"]:
            w["peak_score"] = score
        w["peak_btn"].set_label(f"Peak: {w['peak_score']:.1f}  ↺")

        # Gauge score label & styling
        w["score_lbl"].set_label(f"{score:.1f}")
        w["score_lbl"].remove_css_class("score-good")
        w["score_lbl"].remove_css_class("score-fair")
        w["score_lbl"].remove_css_class("score-poor")
        if score >= 85.0:
            w["score_lbl"].add_css_class("score-good")
        elif score >= 55.0:
            w["score_lbl"].add_css_class("score-fair")
        else:
            w["score_lbl"].add_css_class("score-poor")

        # Progress bar
        w["pbar"].set_fraction(score / 100.0)

        # Trend badge
        tb = w["trend_badge"]
        tb.remove_css_class("trend-sharpening")
        tb.remove_css_class("trend-infocus")
        tb.remove_css_class("trend-overshot")
        tb.remove_css_class("trend-tuning")

        if score > last_s and (score - last_s) >= 1.0:
            tb.set_text("Sharpening...")
            tb.add_css_class("trend-sharpening")
        elif w["peak_score"] > 0 and (w["peak_score"] - score) >= 4.0:
            tb.set_text("Overshot — Turn Back!")
            tb.add_css_class("trend-overshot")
        elif score >= 85.0:
            tb.set_text("In Focus")
            tb.add_css_class("trend-infocus")
        elif abs(score - last_s) <= 1.0:
            tb.set_text("Stable")
            tb.add_css_class("trend-tuning")
        else:
            tb.set_text("Adjusting...")
            tb.add_css_class("trend-tuning")

        if pixbuf:
            w["picture"].set_pixbuf(pixbuf)

        # Check overall state for Next button
        all_sharp = all(
            cw["last_score"] >= 50.0 for cw in self.focus_cam_widgets.values() if cw["last_score"] > 0
        )
        if all_sharp and len(self.focus_cam_widgets) > 0:
            self.focus_next_btn.set_label("Continue to Baseline")
            self.focus_next_btn.set_css_classes(["suggested-action", "pill"])
        else:
            self.focus_next_btn.set_label("Continue Anyway")
            self.focus_next_btn.set_css_classes(["pill"])

        return False

    # =========================================================================
    # PAGE 3: BASELINE SNAPSHOT
    # =========================================================================
    def _build_baseline_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(16)
        box.set_margin_bottom(20)
        box.set_margin_start(24)
        box.set_margin_end(24)

        # Header
        top_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label="Step 2 of 5: Baseline Reference", xalign=0.0)
        title.add_css_class("wizard-title")
        top_txt.append(title)
        sub = Gtk.Label(
            label="Untouched baseline image captured in default automatic mode across all cameras.",
            xalign=0.0,
        )
        sub.add_css_class("wizard-subtitle")
        top_txt.append(sub)
        box.append(top_txt)

        # 3 Columns Side-by-Side
        self.baseline_columns_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14, homogeneous=True)
        self.baseline_columns_box.set_vexpand(True)
        box.append(self.baseline_columns_box)

        # Bottom Bar
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        bottom_bar.set_margin_top(10)

        back_btn = Gtk.Button(label="Back to Focus Check")
        back_btn.add_css_class("pill")
        back_btn.connect("clicked", lambda _: self._go_to_focus())
        bottom_bar.append(back_btn)

        next_btn = Gtk.Button(label="Continue to Setup")
        next_btn.add_css_class("suggested-action")
        next_btn.add_css_class("pill")
        next_btn.set_hexpand(True)
        next_btn.set_halign(Gtk.Align.END)
        next_btn.connect("clicked", lambda _: self._go_to_setup())
        bottom_bar.append(next_btn)
        box.append(bottom_bar)

        return box

    def _capture_and_display_baseline(self):
        """Display untouched auto-exposure reference frames across all 3 cameras."""
        while child := self.baseline_columns_box.get_first_child():
            self.baseline_columns_box.remove(child)

        for cam in self.cameras:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            card.add_css_class("cam-column-card")

            hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            title = Gtk.Label(label=f"Camera {cam.index}", xalign=0.0)
            title.add_css_class("heading")
            hdr.append(title)

            badge = Gtk.Label(label="Reference Captured")
            badge.set_css_classes(["status-pill-ok"])
            badge.set_hexpand(True)
            badge.set_xalign(1.0)
            hdr.append(badge)
            card.append(hdr)

            pic = Gtk.Picture()
            pic.set_can_shrink(True)
            pic.set_size_request(160, 110)
            pic.set_hexpand(True)
            pic.set_vexpand(True)
            card.append(pic)

            # Use cached frame from Focus Check, or fallback to direct capture
            frame = self.baseline_frames.get(cam.dev_node)
            if frame is None:
                try:
                    vdev = V4L2Device(cam.dev_node)
                    frame = vdev.capture_frame(warmup_frames=2)
                    if frame is not None:
                        self.baseline_frames[cam.dev_node] = frame
                except Exception as e:
                    logger.error("Failed to capture baseline on %s: %s", cam.dev_node, e)

            if frame is not None:
                pix = frame_to_pixbuf(frame, max_w=480, max_h=300)
                if pix:
                    pic.set_pixbuf(pix)

            info_lbl = Gtk.Label(label="Mode: Auto Exposure & Auto WB", xalign=0.5)
            info_lbl.add_css_class("caption")
            info_lbl.add_css_class("dim-label")
            card.append(info_lbl)

            self.baseline_columns_box.append(card)

    # =========================================================================
    # PAGE 4: SETUP & OPTIONEN
    # =========================================================================
    def _build_setup_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(40)
        box.set_margin_end(40)

        # Header
        top_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title = Gtk.Label(label="Step 3 of 5: Calibration Setup", xalign=0.0)
        title.add_css_class("wizard-title")
        top_txt.append(title)
        sub = Gtk.Label(
            label="Select camera scope and measurement precision for optimization.",
            xalign=0.0,
        )
        sub.add_css_class("wizard-subtitle")
        top_txt.append(sub)
        box.append(top_txt)

        # Options Card Group
        pref_group = Adw.PreferencesGroup()
        pref_group.set_title("Calibration Parameters")

        # Row 1: Scope (All vs Single)
        self.scope_row = Adw.ActionRow(
            title="Camera Scope",
            subtitle="Calibrate all cameras sequentially or target a specific camera",
        )
        self.scope_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.scope_box.set_valign(Gtk.Align.CENTER)
        self.scope_buttons = {}
        self.scope_row.add_suffix(self.scope_box)
        pref_group.add(self.scope_row)

        # Row 2: Precision (Standard vs Deep)
        self.prec_row = Adw.ActionRow(
            title="Precision Mode",
            subtitle="Standard (~30s) for quick shutter/gain scan, Deep (~2 min) for fine 2D grid",
        )
        self.prec_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.prec_box.set_valign(Gtk.Align.CENTER)
        self.prec_buttons = {}

        for val, lbl in [("standard", "Standard (~30s)"), ("deep", "Deep (~2 min)")]:
            btn = Gtk.Button(label=lbl)
            btn.add_css_class("pill")
            btn.connect("clicked", self._make_precision_handler(val))
            self.prec_box.append(btn)
            self.prec_buttons[val] = btn

        self._update_precision_buttons("deep")
        self.prec_row.add_suffix(self.prec_box)
        pref_group.add(self.prec_row)
        box.append(pref_group)

        # Bottom Bar
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        bottom_bar.set_margin_top(20)

        back_btn = Gtk.Button(label="Back to Baseline")
        back_btn.add_css_class("pill")
        back_btn.connect("clicked", lambda _: self._go_to_baseline())
        bottom_bar.append(back_btn)

        start_btn = Gtk.Button(label="Start Optimization Now")
        start_btn.add_css_class("suggested-action")
        start_btn.add_css_class("pill")
        start_btn.add_css_class("big-cta-button")
        start_btn.set_hexpand(True)
        start_btn.set_halign(Gtk.Align.END)
        start_btn.connect("clicked", lambda _: self._start_calibration_sweep())
        bottom_bar.append(start_btn)
        box.append(bottom_bar)

        return box

    def _setup_scope_buttons(self):
        while child := self.scope_box.get_first_child():
            self.scope_box.remove(child)

        self.scope_buttons.clear()

        all_btn = Gtk.Button(label="All Cameras")
        all_btn.add_css_class("pill")
        all_btn.connect("clicked", lambda _: self._set_scope("all"))
        self.scope_box.append(all_btn)
        self.scope_buttons["all"] = all_btn

        for cam in self.cameras:
            btn = Gtk.Button(label=f"Camera {cam.index}")
            btn.add_css_class("pill")
            btn.connect("clicked", self._make_scope_handler(cam.dev_node))
            self.scope_box.append(btn)
            self.scope_buttons[cam.dev_node] = btn

        self._set_scope("all")

    def _make_scope_handler(self, dev_node: str):
        return lambda _: self._set_scope(dev_node)

    def _set_scope(self, scope: str):
        self.selected_scope = scope
        for k, b in self.scope_buttons.items():
            if k == scope:
                b.add_css_class("suggested-action")
            else:
                b.remove_css_class("suggested-action")

    def _make_precision_handler(self, val: str):
        return lambda _: self._update_precision_buttons(val)

    def _update_precision_buttons(self, val: str):
        self.selected_precision = val
        for k, b in self.prec_buttons.items():
            if k == val:
                b.add_css_class("suggested-action")
            else:
                b.remove_css_class("suggested-action")

    # =========================================================================
    # PAGE 5: LIVE KALIBRIERUNGS-DURCHLAUF (SWEEP)
    # =========================================================================
    def _build_sweep_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(16)
        box.set_margin_bottom(20)
        box.set_margin_start(24)
        box.set_margin_end(24)

        # Header
        top_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.sweep_title_lbl = Gtk.Label(label="Step 4 of 5: Calibration in Progress...", xalign=0.0)
        self.sweep_title_lbl.add_css_class("wizard-title")
        top_txt.append(self.sweep_title_lbl)
        self.sweep_sub_lbl = Gtk.Label(
            label="Calibrating cameras and balancing exposure...",
            xalign=0.0,
        )
        self.sweep_sub_lbl.add_css_class("wizard-subtitle")
        top_txt.append(self.sweep_sub_lbl)
        box.append(top_txt)

        # 3 Camera Columns Side-by-Side
        self.sweep_columns_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14, homogeneous=True)
        self.sweep_columns_box.set_vexpand(True)
        box.append(self.sweep_columns_box)

        # Progress Bar & Status
        prog_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.sweep_progress_bar = Gtk.ProgressBar()
        self.sweep_progress_bar.set_show_text(True)
        self.sweep_progress_bar.set_fraction(0.0)
        prog_box.append(self.sweep_progress_bar)
        box.append(prog_box)

        # Bottom Bar: Cancel and Continue
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        bottom_bar.set_margin_top(8)

        self.sweep_cancel_btn = Gtk.Button(label="Cancel Calibration")
        self.sweep_cancel_btn.add_css_class("destructive-action")
        self.sweep_cancel_btn.add_css_class("pill")
        self.sweep_cancel_btn.connect("clicked", lambda _: self._cancel_sweep())
        bottom_bar.append(self.sweep_cancel_btn)

        self.sweep_finish_btn = Gtk.Button(label="Continue to Review")
        self.sweep_finish_btn.add_css_class("suggested-action")
        self.sweep_finish_btn.add_css_class("pill")
        self.sweep_finish_btn.add_css_class("big-cta-button")
        self.sweep_finish_btn.set_hexpand(True)
        self.sweep_finish_btn.set_halign(Gtk.Align.END)
        self.sweep_finish_btn.set_visible(False)
        self.sweep_finish_btn.connect("clicked", lambda _: self._go_to_review())
        bottom_bar.append(self.sweep_finish_btn)
        box.append(bottom_bar)

        return box

    def _setup_sweep_columns(self):
        while child := self.sweep_columns_box.get_first_child():
            self.sweep_columns_box.remove(child)

        self.sweep_cam_widgets = {}
        for cam in self.cameras:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            card.add_css_class("cam-column-card")

            hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            title = Gtk.Label(label=f"Camera {cam.index}", xalign=0.0)
            title.add_css_class("heading")
            hdr.append(title)

            badge = Gtk.Label(label="Waiting...")
            badge.set_css_classes(["status-pill-active"])
            badge.set_hexpand(True)
            badge.set_xalign(1.0)
            hdr.append(badge)
            card.append(hdr)

            pic = Gtk.Picture()
            pic.set_can_shrink(True)
            pic.set_size_request(160, 110)
            pic.set_hexpand(True)
            pic.set_vexpand(True)
            card.append(pic)

            # Preload baseline frame if available
            if cam.dev_node in self.baseline_frames:
                pix = frame_to_pixbuf(self.baseline_frames[cam.dev_node], max_w=480, max_h=300)
                if pix:
                    pic.set_pixbuf(pix)

            status_lbl = Gtk.Label(label="Ready", xalign=0.5)
            status_lbl.add_css_class("caption")
            card.append(status_lbl)

            self.sweep_cam_widgets[cam.dev_node] = {
                "card": card,
                "badge": badge,
                "picture": pic,
                "status_lbl": status_lbl,
            }
            self.sweep_columns_box.append(card)

    def _start_calibration_sweep(self):
        """Transition to sweep view and run optimization in background thread."""
        self._stop_focus_stream()
        if SystemdManager.is_autodarts_active():
            self._autodarts_was_running = True
            SystemdManager.stop_autodarts()

        self.stack.set_visible_child_name("sweep")
        self._setup_sweep_columns()
        self.sweep_progress_bar.set_fraction(0.0)
        self.sweep_finish_btn.set_visible(False)
        self.sweep_cancel_btn.set_sensitive(True)

        self.sweep_running = True
        self.cancel_event = threading.Event()
        self.results.clear()

        thread = threading.Thread(target=self._sweep_worker, daemon=True)
        thread.start()

    def _cancel_sweep(self):
        if self.cancel_event:
            self.cancel_event.set()
        self.sweep_cancel_btn.set_sensitive(False)
        self.sweep_sub_lbl.set_text("Cancelling... Restoring initial state.")

    def _sweep_worker(self):
        """Sequentially optimize cameras according to selected scope."""
        target_cams = (
            self.cameras
            if self.selected_scope == "all"
            else [c for c in self.cameras if c.dev_node == self.selected_scope]
        )
        total = len(target_cams)

        for idx, cam in enumerate(target_cams):
            if self.cancel_event.is_set():
                break

            cam_num = idx + 1
            GLib.idle_add(self._on_cam_sweep_start, cam.dev_node, cam_num, total)

            try:
                vdev = V4L2Device(cam.dev_node)
                optimizer = CameraOptimizer(vdev)

                def make_progress_cb(node: str, base_frac: float, span_frac: float):
                    def cb(stage: str, progress: float, details: str, frame: Optional[np.ndarray] = None):
                        overall = base_frac + (progress * span_frac)
                        GLib.idle_add(self._on_cam_sweep_progress, node, stage, details, overall, frame)
                    return cb

                cb = make_progress_cb(cam.dev_node, idx / total, 1.0 / total)
                result = optimizer.run_optimization(
                    precision_mode=self.selected_precision,
                    progress_cb=cb,
                    cancel_event=self.cancel_event,
                )

                if result:
                    self.results[cam.dev_node] = result
                    GLib.idle_add(self._on_cam_sweep_done, cam.dev_node, result)

            except Exception as e:
                logger.exception("Error during sweep on %s: %s", cam.dev_node, e)

        GLib.idle_add(self._on_all_sweeps_finished)

    def _on_cam_sweep_start(self, dev_node: str, cam_num: int, total: int):
        self.sweep_title_lbl.set_text(f"Step 4 of 5: Calibrating (Camera {cam_num} of {total})...")
        for node, w in self.sweep_cam_widgets.items():
            if node == dev_node:
                w["card"].add_css_class("active-calibrating")
                w["badge"].set_text("Optimizing...")
                w["badge"].set_css_classes(["status-pill-active"])
            else:
                w["card"].remove_css_class("active-calibrating")
        return False

    def _on_cam_sweep_progress(self, dev_node: str, stage: str, details: str, overall: float, frame: Optional[np.ndarray]):
        self.sweep_progress_bar.set_fraction(min(1.0, max(0.0, overall)))
        self.sweep_sub_lbl.set_text(details)
        if dev_node in self.sweep_cam_widgets:
            w = self.sweep_cam_widgets[dev_node]
            w["status_lbl"].set_text(stage)
            if frame is not None:
                pix = frame_to_pixbuf(frame, max_w=480, max_h=300)
                if pix:
                    w["picture"].set_pixbuf(pix)
        return False

    def _on_cam_sweep_done(self, dev_node: str, result: OptimizationResult):
        if dev_node in self.sweep_cam_widgets:
            w = self.sweep_cam_widgets[dev_node]
            w["card"].remove_css_class("active-calibrating")
            sign = "+" if result.improvement_percentage >= 0 else ""
            badge_text = f"{sign}{result.improvement_percentage:.0f}% Sharper"
            badge_cls = "status-pill-ok" if result.improvement_percentage >= 0 else "status-pill-warn"
            w["badge"].set_text(badge_text)
            w["badge"].set_css_classes([badge_cls])
            w["status_lbl"].set_text(f"Optimal Gain: {result.optimized_settings.gain} | Exposure: {result.optimized_settings.exposure_absolute * 0.1:.1f}ms")
            pix = frame_to_pixbuf(result.optimized_frame, max_w=480, max_h=300)
            if pix:
                w["picture"].set_pixbuf(pix)
        return False

    def _on_all_sweeps_finished(self):
        self.sweep_running = False
        if self.cancel_event and self.cancel_event.is_set():
            self.sweep_title_lbl.set_text("Calibration Cancelled")
            self.sweep_sub_lbl.set_text("Cameras have been restored to initial state.")
            GLib.timeout_add(1500, lambda: self._go_to_dashboard())
        else:
            self.sweep_progress_bar.set_fraction(1.0)
            self.sweep_title_lbl.set_text("Calibration Successfully Completed!")
            self.sweep_sub_lbl.set_text("All values calculated. Click 'Continue to Review'.")
            self.sweep_finish_btn.set_visible(True)
            self.sweep_cancel_btn.set_sensitive(False)

            # Auto-export Vorher/Nachher sample images and metrics to disk
            try:
                samples_dir = Path.home() / "Fedora-CAM-librator" / "samples"
                samples_dir.mkdir(parents=True, exist_ok=True)
                for dev_node, res in self.results.items():
                    cam = next((c for c in self.cameras if c.dev_node == dev_node), None)
                    if cam:
                        cam_id = f"cam{cam.index}"
                        if res.initial_frame is not None:
                            cv2.imwrite(str(samples_dir / f"{cam_id}_before.jpg"), res.initial_frame)
                        if res.optimized_frame is not None:
                            cv2.imwrite(str(samples_dir / f"{cam_id}_after.jpg"), res.optimized_frame)
                        with open(str(samples_dir / f"{cam_id}_metrics.txt"), "w", encoding="utf-8") as fp:
                            fp.write(f"Camera: {dev_node}\n")
                            fp.write(f"Gain: {res.optimized_settings.gain}\n")
                            fp.write(f"Contrast: {res.optimized_settings.contrast}\n")
                            fp.write(f"Sharpness: {res.optimized_settings.sharpness}\n")
                            fp.write(f"Exposure: {res.optimized_settings.exposure_absolute}\n")
                            fp.write(f"Quality: {res.optimized_metrics.composite_score}/100\n")
                            fp.write(f"Improvement: +{res.improvement_percentage}%\n")
                logger.info("Saved sweep sample images and metrics to %s", samples_dir)
            except Exception as e:
                logger.warning("Failed to auto-save sweep samples: %s", e)
        return False

    # =========================================================================
    # PAGE 6: REVIEW & VORHER/NACHHER-VERGLEICH
    # =========================================================================
    def _build_review_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(16)
        box.set_margin_bottom(20)
        box.set_margin_start(24)
        box.set_margin_end(24)

        # Header & Controls Row
        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        
        top_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label="Step 5 of 5: Review & Comparison", xalign=0.0)
        title.add_css_class("wizard-title")
        top_txt.append(title)
        self.review_sub_lbl = Gtk.Label(
            label="Direct A/B comparison and magnified inspection.",
            xalign=0.0,
        )
        self.review_sub_lbl.add_css_class("wizard-subtitle")
        top_txt.append(self.review_sub_lbl)
        top_bar.append(top_txt)

        # Top Right: Camera Selector Buttons (Overview vs Cam 1 vs Cam 2 vs Cam 3)
        self.rev_cam_selector_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.rev_cam_selector_box.set_hexpand(True)
        self.rev_cam_selector_box.set_halign(Gtk.Align.END)
        self.rev_cam_selector_box.set_valign(Gtk.Align.CENTER)
        top_bar.append(self.rev_cam_selector_box)
        box.append(top_bar)

        # View Stack: Switch between "overview" (3 columns) and "detail" (side-by-side magnified)
        self.review_view_stack = Gtk.Stack()
        self.review_view_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.review_view_stack.set_vexpand(True)

        # -------------------------------------------------------------
        # 1. OVERVIEW VIEW (3 Columns)
        # -------------------------------------------------------------
        self.review_overview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        
        # Overview Top Controls (Source + A/B Toggle + Filters)
        ov_ctrls_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        ov_ctrls_box.set_halign(Gtk.Align.END)

        ov_src_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.rev_src_empty_btn = Gtk.Button(label="Empty Board")
        self.rev_src_empty_btn.add_css_class("pill")
        self.rev_src_empty_btn.connect("clicked", lambda _: self._set_review_source("empty"))
        ov_src_box.append(self.rev_src_empty_btn)

        self.rev_src_dart_btn = Gtk.Button(label="With Dart")
        self.rev_src_dart_btn.add_css_class("pill")
        self.rev_src_dart_btn.connect("clicked", lambda _: self._set_review_source("dart"))
        ov_src_box.append(self.rev_src_dart_btn)
        ov_ctrls_box.append(ov_src_box)

        ov_sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        ov_ctrls_box.append(ov_sep)
        
        ov_ab_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.rev_btn_after = Gtk.Button(label="After (Calibrated)")
        self.rev_btn_after.add_css_class("pill")
        self.rev_btn_after.connect("clicked", lambda _: self._set_review_mode("after"))
        ov_ab_box.append(self.rev_btn_after)

        self.rev_btn_before = Gtk.Button(label="Before (Default)")
        self.rev_btn_before.add_css_class("pill")
        self.rev_btn_before.connect("clicked", lambda _: self._set_review_mode("before"))
        ov_ab_box.append(self.rev_btn_before)
        ov_ctrls_box.append(ov_ab_box)

        # Filter Buttons in Overview
        ov_filt_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.ov_filt_buttons = {}
        for f_val, f_lbl in [
            ("normal", "Normal"),
            ("edge", "Edges & Wire"),
            ("diff", "CV Difference"),
            ("noise", "Noise Analysis"),
        ]:
            btn = Gtk.Button(label=f_lbl)
            btn.add_css_class("pill")
            btn.connect("clicked", self._make_filter_handler(f_val))
            ov_filt_box.append(btn)
            self.ov_filt_buttons[f_val] = btn
        ov_ctrls_box.append(ov_filt_box)
        self.review_overview_box.append(ov_ctrls_box)

        # 3 Columns Container
        self.review_columns_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14, homogeneous=True)
        self.review_columns_box.set_vexpand(True)
        self.review_overview_box.append(self.review_columns_box)
        self.review_view_stack.add_named(self.review_overview_box, "overview")

        # -------------------------------------------------------------
        # 2. DETAIL VIEW (Side-by-Side Magnifier Before vs After)
        # -------------------------------------------------------------
        self.review_detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.review_detail_box.set_vexpand(True)

        # Detail Toolbar (Source, Zones, Zoom Level, Filters)
        detail_tb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        detail_tb.set_valign(Gtk.Align.CENTER)

        # Source Selector in Detail Mode
        det_src_lbl = Gtk.Label(label="Source:")
        det_src_lbl.add_css_class("caption")
        detail_tb.append(det_src_lbl)

        det_src_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.det_src_empty_btn = Gtk.Button(label="Empty Board")
        self.det_src_empty_btn.add_css_class("pill")
        self.det_src_empty_btn.connect("clicked", lambda _: self._set_review_source("empty"))
        det_src_box.append(self.det_src_empty_btn)

        self.det_src_dart_btn = Gtk.Button(label="With Dart")
        self.det_src_dart_btn.add_css_class("pill")
        self.det_src_dart_btn.connect("clicked", lambda _: self._set_review_source("dart"))
        det_src_box.append(self.det_src_dart_btn)
        detail_tb.append(det_src_box)

        sep0 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        detail_tb.append(sep0)

        # Target Zone Group
        zone_lbl = Gtk.Label(label="Target:")
        zone_lbl.add_css_class("caption")
        detail_tb.append(zone_lbl)

        zone_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.rev_zone_buttons = {}
        for z_val, z_lbl in [
            ("dart_tip", "Dart Tip"),
            ("sweet_spot", "Sweet Spot"),
            ("bullseye", "Bullseye"),
            ("treble", "Treble Ring"),
            ("center", "Full Board"),
        ]:
            z_btn = Gtk.Button(label=z_lbl)
            z_btn.add_css_class("pill")
            z_btn.connect("clicked", self._make_zone_handler(z_val))
            zone_box.append(z_btn)
            self.rev_zone_buttons[z_val] = z_btn
        detail_tb.append(zone_box)

        sep1 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        detail_tb.append(sep1)

        # Zoom Factor Group
        zoom_lbl = Gtk.Label(label="Zoom:")
        zoom_lbl.add_css_class("caption")
        detail_tb.append(zoom_lbl)

        zoom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.rev_zoom_buttons = {}
        for zoom_f, z_lbl in [(1.5, "1.5x"), (2.5, "2.5x"), (4.0, "4.0x")]:
            zf_btn = Gtk.Button(label=z_lbl)
            zf_btn.add_css_class("pill")
            zf_btn.connect("clicked", self._make_zoom_factor_handler(zoom_f))
            zoom_box.append(zf_btn)
            self.rev_zoom_buttons[zoom_f] = zf_btn
        detail_tb.append(zoom_box)

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        detail_tb.append(sep2)

        # Filter Group in Detail Mode
        det_filt_lbl = Gtk.Label(label="Filter:")
        det_filt_lbl.add_css_class("caption")
        detail_tb.append(det_filt_lbl)

        det_filt_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.det_filt_buttons = {}
        for f_val, f_lbl in [
            ("normal", "Normal"),
            ("edge", "Edges & Wire"),
            ("diff", "CV Difference"),
            ("noise", "Noise Analysis"),
        ]:
            btn = Gtk.Button(label=f_lbl)
            btn.add_css_class("pill")
            btn.connect("clicked", self._make_filter_handler(f_val))
            det_filt_box.append(btn)
            self.det_filt_buttons[f_val] = btn
        detail_tb.append(det_filt_box)

        self.review_detail_box.append(detail_tb)

        # Side-by-Side Dual Column Box
        side_by_side_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16, homogeneous=True)
        side_by_side_box.set_vexpand(True)

        # Left Column: Before (Default) Card
        self.rev_detail_card_before = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.rev_detail_card_before.add_css_class("cam-column-card")

        hdr_before = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_before = Gtk.Label(label="Before (Default)", xalign=0.0)
        title_before.add_css_class("heading")
        hdr_before.append(title_before)

        self.rev_detail_badge_before = Gtk.Label(label="Default")
        self.rev_detail_badge_before.set_css_classes(["status-pill-warn"])
        self.rev_detail_badge_before.set_hexpand(True)
        self.rev_detail_badge_before.set_xalign(1.0)
        hdr_before.append(self.rev_detail_badge_before)
        self.rev_detail_card_before.append(hdr_before)

        self.rev_detail_pic_before = Gtk.Picture()
        self.rev_detail_pic_before.set_can_shrink(True)
        self.rev_detail_pic_before.set_size_request(200, 140)
        self.rev_detail_pic_before.set_hexpand(True)
        self.rev_detail_pic_before.set_vexpand(True)
        click_before = Gtk.GestureClick()
        click_before.connect("pressed", lambda g, n, x, y: self._on_detail_picture_clicked(g, n, x, y, "before"))
        self.rev_detail_pic_before.add_controller(click_before)
        self.rev_detail_card_before.append(self.rev_detail_pic_before)

        self.rev_detail_lbl_before = Gtk.Label(label="Default values", xalign=0.0)
        self.rev_detail_lbl_before.add_css_class("caption")
        self.rev_detail_card_before.append(self.rev_detail_lbl_before)
        side_by_side_box.append(self.rev_detail_card_before)

        # Right Column: After (Calibrated) Card
        self.rev_detail_card_after = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.rev_detail_card_after.add_css_class("cam-column-card")

        hdr_after = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.rev_detail_title_after = Gtk.Label(label="After (Calibrated)", xalign=0.0)
        self.rev_detail_title_after.add_css_class("heading")
        hdr_after.append(self.rev_detail_title_after)

        self.rev_detail_badge_after = Gtk.Label(label="Calibrated")
        self.rev_detail_badge_after.set_css_classes(["status-pill-ok"])
        self.rev_detail_badge_after.set_hexpand(True)
        self.rev_detail_badge_after.set_xalign(1.0)
        hdr_after.append(self.rev_detail_badge_after)
        self.rev_detail_card_after.append(hdr_after)

        self.rev_detail_pic_after = Gtk.Picture()
        self.rev_detail_pic_after.set_can_shrink(True)
        self.rev_detail_pic_after.set_size_request(200, 140)
        self.rev_detail_pic_after.set_hexpand(True)
        self.rev_detail_pic_after.set_vexpand(True)
        click_after = Gtk.GestureClick()
        click_after.connect("pressed", lambda g, n, x, y: self._on_detail_picture_clicked(g, n, x, y, "after"))
        self.rev_detail_pic_after.add_controller(click_after)
        self.rev_detail_card_after.append(self.rev_detail_pic_after)

        self.rev_detail_lbl_after = Gtk.Label(label="Calibrated parameters", xalign=0.0)
        self.rev_detail_lbl_after.add_css_class("caption")
        self.rev_detail_lbl_after.add_css_class("accent")
        self.rev_detail_card_after.append(self.rev_detail_lbl_after)
        side_by_side_box.append(self.rev_detail_card_after)

        self.review_detail_box.append(side_by_side_box)

        # Helper note
        hint_lbl = Gtk.Label(label="Click anywhere on the image to pan the magnifying glass.", xalign=0.5)
        hint_lbl.add_css_class("caption")
        hint_lbl.set_opacity(0.7)
        self.review_detail_box.append(hint_lbl)

        self.review_view_stack.add_named(self.review_detail_box, "detail")
        box.append(self.review_view_stack)

        # Bottom Bar: Discard vs Test with Dart vs Apply
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        bottom_bar.set_margin_top(10)

        discard_btn = Gtk.Button(label="Discard")
        discard_btn.add_css_class("pill")
        discard_btn.connect("clicked", lambda _: self._go_to_dashboard())
        bottom_bar.append(discard_btn)

        self.rev_btn_test_dart = Gtk.Button(label="Test with Dart")
        self.rev_btn_test_dart.add_css_class("pill")
        self.rev_btn_test_dart.connect("clicked", lambda _: self._show_dart_test_prompt())
        bottom_bar.append(self.rev_btn_test_dart)

        apply_btn = Gtk.Button(label="Save & Apply Settings")
        apply_btn.add_css_class("suggested-action")
        apply_btn.add_css_class("pill")
        apply_btn.add_css_class("big-cta-button")
        apply_btn.set_hexpand(True)
        apply_btn.set_halign(Gtk.Align.END)
        apply_btn.connect("clicked", lambda _: self._apply_and_finish())
        bottom_bar.append(apply_btn)
        box.append(bottom_bar)

        return box

    def _setup_review_columns(self):
        # 1. Rebuild Top Camera Selector
        while child := self.rev_cam_selector_box.get_first_child():
            self.rev_cam_selector_box.remove(child)

        self.rev_cam_nav_buttons = {}

        btn_all = Gtk.Button(label="All Cameras")
        btn_all.add_css_class("pill")
        btn_all.connect("clicked", lambda _: self._select_review_camera(None))
        self.rev_cam_selector_box.append(btn_all)
        self.rev_cam_nav_buttons[None] = btn_all

        for cam in self.cameras:
            c_btn = Gtk.Button(label=f"Camera {cam.index}")
            c_btn.add_css_class("pill")
            c_btn.connect("clicked", self._make_cam_select_handler(cam.dev_node))
            self.rev_cam_selector_box.append(c_btn)
            self.rev_cam_nav_buttons[cam.dev_node] = c_btn

        # 2. Rebuild 3 Overview Column Cards
        while child := self.review_columns_box.get_first_child():
            self.review_columns_box.remove(child)

        self.review_cam_widgets = {}

        for cam in self.cameras:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            card.add_css_class("cam-column-card")

            hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            title = Gtk.Label(label=f"Camera {cam.index}", xalign=0.0)
            title.add_css_class("heading")
            hdr.append(title)

            badge = Gtk.Label(label="Calibrated")
            badge.set_css_classes(["status-pill-ok"])
            badge.set_hexpand(True)
            badge.set_xalign(1.0)
            hdr.append(badge)
            card.append(hdr)

            pic = Gtk.Picture()
            pic.set_can_shrink(True)
            pic.set_size_request(160, 110)
            pic.set_hexpand(True)
            pic.set_vexpand(True)
            card.append(pic)

            # Metric Box under picture
            metric_grid = Gtk.Grid()
            metric_grid.set_column_spacing(12)
            metric_grid.set_row_spacing(4)

            res = self.results.get(cam.dev_node)
            if res:
                s_opt = res.optimized_settings
                m_init = res.initial_metrics
                m_opt = res.optimized_metrics
                exp_lbl = Gtk.Label(label=f"Exposure: {s_opt.exposure_absolute * 0.1:.1f}ms", xalign=0.0)
                exp_lbl.add_css_class("caption")
                gain_lbl = Gtk.Label(label=f"Gain: {s_opt.gain}", xalign=0.0)
                gain_lbl.add_css_class("caption")

                noise_delta = ((m_init.temporal_noise - m_opt.temporal_noise) / max(0.01, m_init.temporal_noise)) * 100.0 if m_init else 0.0
                noise_sign = "-" if noise_delta >= 0 else "+"
                noise_lbl = Gtk.Label(label=f"Noise: {noise_sign}{abs(noise_delta):.0f}%", xalign=0.0)
                noise_lbl.add_css_class("caption")

                contrast_sign = "+" if res.improvement_percentage >= 0 else ""
                contrast_lbl = Gtk.Label(label=f"Wire Contrast: {contrast_sign}{res.improvement_percentage:.0f}%", xalign=0.0)
                contrast_lbl.add_css_class("caption")
                contrast_lbl.add_css_class("accent")

                metric_grid.attach(exp_lbl, 0, 0, 1, 1)
                metric_grid.attach(gain_lbl, 1, 0, 1, 1)
                metric_grid.attach(noise_lbl, 0, 1, 1, 1)
                metric_grid.attach(contrast_lbl, 1, 1, 1, 1)
            else:
                info = Gtk.Label(label="Default values unchanged", xalign=0.0)
                info.add_css_class("caption")
                metric_grid.attach(info, 0, 0, 2, 1)

            card.append(metric_grid)

            inspect_btn = Gtk.Button(label="Inspect Magnified")
            inspect_btn.add_css_class("pill")
            inspect_btn.add_css_class("suggested-action")
            inspect_btn.connect("clicked", self._make_cam_select_handler(cam.dev_node))
            card.append(inspect_btn)

            self.review_cam_widgets[cam.dev_node] = {
                "badge": badge,
                "picture": pic,
            }
            self.review_columns_box.append(card)

        # Default to overview or previously selected
        if self.review_selected_cam is None:
            self._select_review_camera(None)
        else:
            self._select_review_camera(self.review_selected_cam)

        self._update_source_button_styles()
        self._update_zone_button_styles()
        self._update_zoom_button_styles()
        self._set_review_filter(self.active_review_filter)

    def _make_cam_select_handler(self, dev_node: Optional[str]):
        return lambda _: self._select_review_camera(dev_node)

    def _select_review_camera(self, dev_node: Optional[str]):
        self.review_selected_cam = dev_node
        self.review_custom_coord = None
        if self.review_source_mode == "dart" and len(self.dart_frames_after) > 0:
            self.review_zoom_zone = "dart_tip"
        else:
            self.review_zoom_zone = "sweet_spot"

        for node, btn in self.rev_cam_nav_buttons.items():
            if node == dev_node:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")

        if dev_node is None:
            self.review_view_stack.set_visible_child_name("overview")
            self.review_sub_lbl.set_text("Direct A/B comparison across all cameras.")
        else:
            self.review_view_stack.set_visible_child_name("detail")
            cam = next((c for c in self.cameras if c.dev_node == dev_node), None)
            cam_title = f"Camera {cam.index}" if cam else dev_node
            self.review_sub_lbl.set_text(f"Magnified Side-by-Side Inspection ({cam_title}).")

        self._update_zone_button_styles()
        self._refresh_review_pictures()

    def _set_review_source(self, source: str):
        if source == "dart" and not self.dart_frames_after:
            self._show_dart_test_prompt()
            return
        self.review_source_mode = source
        if source == "dart":
            self.review_zoom_zone = "dart_tip"
        elif self.review_zoom_zone == "dart_tip":
            self.review_zoom_zone = "sweet_spot"
        self._update_source_button_styles()
        self._update_zone_button_styles()
        self._refresh_review_pictures()

    def _update_source_button_styles(self):
        is_dart = self.review_source_mode == "dart"
        if hasattr(self, "rev_src_empty_btn"):
            if not is_dart:
                self.rev_src_empty_btn.add_css_class("suggested-action")
                self.rev_src_dart_btn.remove_css_class("suggested-action")
            else:
                self.rev_src_dart_btn.add_css_class("suggested-action")
                self.rev_src_empty_btn.remove_css_class("suggested-action")
        if hasattr(self, "det_src_empty_btn"):
            if not is_dart:
                self.det_src_empty_btn.add_css_class("suggested-action")
                self.det_src_dart_btn.remove_css_class("suggested-action")
            else:
                self.det_src_dart_btn.add_css_class("suggested-action")
                self.det_src_empty_btn.remove_css_class("suggested-action")

    def _show_dart_test_prompt(self):
        root = self.get_root()
        dialog = Adw.MessageDialog(
            transient_for=root if isinstance(root, Gtk.Window) else None,
            heading="Insert Dart for Verification",
            body="Place or throw a dart into any board sector (e.g. Treble 20 or Bullseye). SUIT will capture before vs after snapshots across all cameras to verify dart tip edge contrast and CV difference signal.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("capture", "Capture Snapshot")
        dialog.set_response_appearance("capture", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._on_dart_dialog_response)
        dialog.present()

    def _on_dart_dialog_response(self, dialog, response: str):
        if response == "capture":
            self._start_dart_capture()

    def _start_dart_capture(self):
        if self.dart_capturing:
            return
        self.dart_capturing = True
        if hasattr(self, "rev_btn_test_dart"):
            self.rev_btn_test_dart.set_sensitive(False)
        self.review_sub_lbl.set_text("Capturing dart test frames across all cameras...")
        thread = threading.Thread(target=self._dart_capture_worker, daemon=True)
        thread.start()

    def _dart_capture_worker(self):
        try:
            for cam in self.cameras:
                res = self.results.get(cam.dev_node)
                vdev = V4L2Device(cam.dev_node)

                # 1. Capture Before Frame with initial settings
                if res and res.initial_settings:
                    vdev.apply_settings(res.initial_settings)
                time.sleep(0.12)

                frame_before = None
                for _ in range(4):
                    frame_before = vdev.grab_single_frame()
                    time.sleep(0.04)

                # 2. Capture After Frame with optimized settings
                if res and res.optimized_settings:
                    vdev.apply_settings(res.optimized_settings)
                time.sleep(0.12)

                frame_after = None
                for _ in range(4):
                    frame_after = vdev.grab_single_frame()
                    time.sleep(0.04)

                if frame_before is not None:
                    self.dart_frames_before[cam.dev_node] = frame_before
                if frame_after is not None:
                    self.dart_frames_after[cam.dev_node] = frame_after

                # Detect dart tip coordinate
                empty_frame = (
                    res.initial_frame
                    if (res and res.initial_frame is not None)
                    else self.baseline_frames.get(cam.dev_node)
                )
                if empty_frame is not None and frame_after is not None:
                    roi = res.roi if res else None
                    tip = detect_dart_tip(empty_frame, frame_after, roi=roi)
                    if tip:
                        self.dart_tips[cam.dev_node] = tip
        except Exception as e:
            logger.exception("Error capturing dart frames: %s", e)
        finally:
            GLib.idle_add(self._on_dart_capture_complete)

    def _on_dart_capture_complete(self):
        self.dart_capturing = False
        if hasattr(self, "rev_btn_test_dart"):
            self.rev_btn_test_dart.set_sensitive(True)
        self.review_source_mode = "dart"
        self.review_zoom_zone = "dart_tip"
        self._update_source_button_styles()
        self._update_zone_button_styles()
        self._set_review_filter("diff")
        self._refresh_review_pictures()

        if self.review_selected_cam:
            cam = next((c for c in self.cameras if c.dev_node == self.review_selected_cam), None)
            cam_title = f"Camera {cam.index}" if cam else self.review_selected_cam
            self.review_sub_lbl.set_text(f"Dart Test Inspection ({cam_title}) - Dart tip locked.")
        else:
            self.review_sub_lbl.set_text("Dart test captured. Inspect magnified dart tip and difference signal.")
        return False

    def _make_zone_handler(self, zone: str):
        return lambda _: self._set_zoom_zone(zone)

    def _set_zoom_zone(self, zone: str):
        self.review_zoom_zone = zone
        self.review_custom_coord = None
        if zone == "center":
            self.review_zoom_factor = 1.0
        elif self.review_zoom_factor < 1.1:
            self.review_zoom_factor = 2.5
        self._update_zone_button_styles()
        self._update_zoom_button_styles()
        self._refresh_review_pictures()

    def _update_zone_button_styles(self):
        has_dart = len(self.dart_frames_after) > 0
        if "dart_tip" in self.rev_zone_buttons:
            self.rev_zone_buttons["dart_tip"].set_sensitive(has_dart)

        for z_val, btn in self.rev_zone_buttons.items():
            if z_val == self.review_zoom_zone:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")

    def _make_zoom_factor_handler(self, factor: float):
        return lambda _: self._set_zoom_factor(factor)

    def _set_zoom_factor(self, factor: float):
        self.review_zoom_factor = factor
        if self.review_zoom_zone == "center" and factor > 1.05:
            self.review_zoom_zone = "sweet_spot"
            self._update_zone_button_styles()
    def _update_zoom_button_styles(self):
        for z_f, btn in self.rev_zoom_buttons.items():
            if abs(z_f - self.review_zoom_factor) < 0.1:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")

    def _on_detail_picture_clicked(self, gesture, n_press, x, y, frame_key: str):
        widget = gesture.get_widget()
        ww = widget.get_width()
        wh = widget.get_height()
        if ww <= 0 or wh <= 0:
            return
        crop_box = self._magnifier_crop_coords.get(frame_key)
        if not crop_box:
            return
        x1, y1, cw, ch = crop_box
        if cw <= 0 or ch <= 0:
            return
        rel_x = max(0.0, min(1.0, x / ww))
        rel_y = max(0.0, min(1.0, y / wh))
        target_x = int(x1 + rel_x * cw)
        target_y = int(y1 + rel_y * ch)
        self.review_custom_coord = (target_x, target_y)
        self.review_zoom_zone = "custom"
        self._update_zone_button_styles()
        self._refresh_review_pictures()

    def _set_review_mode(self, mode: str):
        self.active_review_mode = mode
        if mode == "after":
            self.rev_btn_after.add_css_class("suggested-action")
            self.rev_btn_before.remove_css_class("suggested-action")
        else:
            self.rev_btn_before.add_css_class("suggested-action")
            self.rev_btn_after.remove_css_class("suggested-action")
        self._refresh_review_pictures()

    def _make_filter_handler(self, f_val: str):
        return lambda _: self._set_review_filter(f_val)

    def _set_review_filter(self, f_val: str):
        self.active_review_filter = f_val
        for k, b in self.ov_filt_buttons.items():
            if k == f_val:
                b.add_css_class("suggested-action")
            else:
                b.remove_css_class("suggested-action")
        for k, b in self.det_filt_buttons.items():
            if k == f_val:
                b.add_css_class("suggested-action")
            else:
                b.remove_css_class("suggested-action")
        self._refresh_review_pictures()

    def _refresh_review_pictures(self):
        """Render and display frames according to active view (overview vs detail)."""
        # 1. Update Overview Columns
        for cam in self.cameras:
            if cam.dev_node not in self.review_cam_widgets:
                continue

            res = self.results.get(cam.dev_node)
            base_frame = self.baseline_frames.get(cam.dev_node)

            if self.review_source_mode == "dart":
                frame = (
                    self.dart_frames_after.get(cam.dev_node)
                    if self.active_review_mode == "after"
                    else self.dart_frames_before.get(cam.dev_node)
                )
                if frame is None:
                    frame = res.optimized_frame if (self.active_review_mode == "after" and res) else (res.initial_frame if res else base_frame)
                badge_txt = "Dart Snapshot"
                badge_cls = "status-pill-ok" if self.active_review_mode == "after" else "status-pill-warn"
                empty_ref = self.baseline_frames.get(cam.dev_node) or (res.initial_frame if res else None)
            else:
                if self.active_review_mode == "after" and res:
                    frame = res.optimized_frame
                    badge_sign = "+" if res.improvement_percentage >= 0 else ""
                    badge_txt = f"{badge_sign}{res.improvement_percentage:.0f}% Sharper"
                    badge_cls = "status-pill-ok" if res.improvement_percentage >= 0 else "status-pill-warn"
                else:
                    frame = res.initial_frame if res else base_frame
                    badge_txt = "Default"
                    badge_cls = "status-pill-warn"
                empty_ref = None

            if frame is None:
                continue

            if self.active_review_filter == "diff" and empty_ref is not None:
                filtered = apply_difference_filter(empty_ref, frame)
            elif self.active_review_filter == "edge":
                filtered = apply_edge_contrast_filter(frame)
            elif self.active_review_filter == "noise":
                filtered = apply_noise_inspector_filter(frame)
            else:
                filtered = frame

            pix = frame_to_pixbuf(filtered, max_w=480, max_h=300)
            w = self.review_cam_widgets[cam.dev_node]
            if pix:
                w["picture"].set_pixbuf(pix)
            w["badge"].set_text(badge_txt)
            w["badge"].set_css_classes([badge_cls])

        # 2. Update Detail Side-by-Side View if active
        if self.review_selected_cam:
            dev_node = self.review_selected_cam
            res = self.results.get(dev_node)
            base_frame = self.baseline_frames.get(dev_node)

            if self.review_source_mode == "dart":
                before_frame = self.dart_frames_before.get(dev_node)
                after_frame = self.dart_frames_after.get(dev_node)
                empty_ref = self.baseline_frames.get(dev_node) or (res.initial_frame if res else None)
            else:
                before_frame = res.initial_frame if (res and res.initial_frame is not None) else base_frame
                after_frame = res.optimized_frame if (res and res.optimized_frame is not None) else base_frame
                empty_ref = None

            ref_frame = after_frame if after_frame is not None else before_frame
            if ref_frame is not None and ref_frame.size > 0:
                fh, fw = ref_frame.shape[:2]

                # Determine center coordinate
                if self.review_zoom_zone == "dart_tip":
                    if dev_node in self.dart_tips:
                        center = self.dart_tips[dev_node]
                    elif res and res.roi and res.roi.target_roi:
                        tx, ty, tw, th = res.roi.target_roi
                        center = (tx + tw // 2, ty + th // 2)
                    else:
                        center = (fw // 2, int(fh * 0.25))
                elif self.review_zoom_zone == "custom" and self.review_custom_coord:
                    center = self.review_custom_coord
                elif self.review_zoom_zone == "bullseye":
                    if res and res.roi and res.roi.center:
                        center = res.roi.center
                    else:
                        center = (fw // 2, fh // 2)
                elif self.review_zoom_zone == "treble":
                    if res and res.roi and res.roi.center:
                        center = (res.roi.center[0], max(0, res.roi.center[1] - int(res.roi.radius * 0.55)))
                    else:
                        center = (fw // 2, int(fh * 0.35))
                elif self.review_zoom_zone == "center":
                    center = (fw // 2, fh // 2)
                else:  # "sweet_spot"
                    if res and res.roi and res.roi.target_roi:
                        tx, ty, tw, th = res.roi.target_roi
                        center = (tx + tw // 2, ty + th // 2)
                    else:
                        center = (fw // 2, int(fh * 0.25))

                # Render Before
                if before_frame is not None:
                    pix_b, box_b = extract_magnified_pixbuf(
                        before_frame,
                        center=center,
                        zoom_factor=self.review_zoom_factor,
                        max_w=580,
                        max_h=380,
                        filter_mode=self.active_review_filter,
                        show_reticle=True,
                        diff_reference_frame=empty_ref,
                    )
                    if pix_b:
                        self.rev_detail_pic_before.set_pixbuf(pix_b)
                    self._magnifier_crop_coords["before"] = box_b

                # Render After
                if after_frame is not None:
                    pix_a, box_a = extract_magnified_pixbuf(
                        after_frame,
                        center=center,
                        zoom_factor=self.review_zoom_factor,
                        max_w=580,
                        max_h=380,
                        filter_mode=self.active_review_filter,
                        show_reticle=True,
                        diff_reference_frame=empty_ref,
                    )
                    if pix_a:
                        self.rev_detail_pic_after.set_pixbuf(pix_a)
                    self._magnifier_crop_coords["after"] = box_a

                # Update Labels and Badges
                if res:
                    s_init = res.initial_settings
                    s_opt = res.optimized_settings
                    m_init = res.initial_metrics
                    m_opt = res.optimized_metrics
                    sign = "+" if res.improvement_percentage >= 0 else ""

                    noise_delta = ((m_init.temporal_noise - m_opt.temporal_noise) / max(0.01, m_init.temporal_noise)) * 100.0 if m_init else 0.0
                    noise_sign = "-" if noise_delta >= 0 else "+"

                    self.rev_detail_badge_before.set_text("Default")
                    self.rev_detail_badge_before.set_css_classes(["status-pill-warn"])
                    self.rev_detail_lbl_before.set_text(
                        f"Exposure: {s_init.exposure_absolute * 0.1:.1f}ms | Gain: {s_init.gain} | Contrast: {s_init.contrast} | Sharpness: {s_init.sharpness}"
                    )

                    self.rev_detail_badge_after.set_text(f"{sign}{res.improvement_percentage:.0f}% Sharper")
                    self.rev_detail_badge_after.set_css_classes(["status-pill-ok" if res.improvement_percentage >= 0 else "status-pill-warn"])
                    self.rev_detail_lbl_after.set_text(
                        f"Exposure: {s_opt.exposure_absolute * 0.1:.1f}ms | Gain: {s_opt.gain} | Contrast: {s_opt.contrast} | Sharpness: {s_opt.sharpness} | Noise: {noise_sign}{abs(noise_delta):.0f}%"
                    )
                else:
                    self.rev_detail_lbl_before.set_text("Default values unchanged")
                    self.rev_detail_lbl_after.set_text("Calibrated values pending")

    def _apply_and_finish(self):
        """Save calibrated values to persistence, apply to cameras, and return to dashboard."""
        for dev_node, res in self.results.items():
            cam = next((c for c in self.cameras if c.dev_node == dev_node), None)
            if cam:
                self.persistence.save_calibration(cam.by_path, res.optimized_settings)
                try:
                    vdev = V4L2Device(dev_node)
                    vdev.apply_settings(res.optimized_settings)
                except Exception as e:
                    logger.error("Failed to apply settings to %s: %s", dev_node, e)

        self.persistence.set_calibration_enabled(True)
        if getattr(self, "_autodarts_was_running", False):
            SystemdManager.start_autodarts()
            self._autodarts_was_running = False
        self._go_to_dashboard()

    # =========================================================================
    # NAVIGATION HANDLERS
    # =========================================================================
    def _go_to_dashboard(self):
        self._stop_focus_stream()
        calibrations = self.persistence.load_calibrations()
        is_enabled = self.persistence.is_calibration_enabled()
        for cam in self.cameras:
            try:
                vdev = V4L2Device(cam.dev_node)
                if is_enabled and cam.by_path in calibrations:
                    vdev.apply_settings(calibrations[cam.by_path])
                else:
                    vdev.reset_to_defaults()
            except Exception as e:
                logger.warning("Failed to restore camera %s on return to dashboard: %s", cam.dev_node, e)
        self.stack.set_visible_child_name("dashboard")
        self._update_dashboard()

    def _go_to_focus(self):
        if SystemdManager.is_autodarts_active():
            self._autodarts_was_running = True
            SystemdManager.stop_autodarts()
        self.detector.unlock_target()
        self.accum_crops.clear()
        # Reset all cameras to standard factory auto mode for Focus Check & Baseline
        for cam in self.cameras:
            try:
                vdev = V4L2Device(cam.dev_node)
                vdev.reset_to_defaults()
            except Exception as e:
                logger.warning("Failed to reset camera %s on focus start: %s", cam.dev_node, e)
        self.stack.set_visible_child_name("focus")
        self._setup_focus_columns()
        self._start_focus_stream()

    def _go_to_baseline(self):
        if SystemdManager.is_autodarts_active():
            self._autodarts_was_running = True
            SystemdManager.stop_autodarts()
        self._stop_focus_stream()
        self.stack.set_visible_child_name("baseline")
        self._capture_and_display_baseline()

    def _go_to_setup(self):
        self._stop_focus_stream()
        self.stack.set_visible_child_name("setup")
        self._setup_scope_buttons()

    def _go_to_review(self):
        self._stop_focus_stream()
        self.stack.set_visible_child_name("review")
        self._setup_review_columns()

    # =========================================================================
    # PUBLIC API / LIFECYCLE
    # =========================================================================
    def refresh_cameras(self):
        """Rescan system cameras and update dashboard."""
        self.cameras = self.topology.get_autodarts_cameras()
        # Sort predictably: index 1, 2, 3
        self.cameras.sort(key=lambda c: c.index)
        self._update_dashboard()

    def on_app_close(self):
        """Cleanup running threads on application close."""
        self._stop_focus_stream()
        if self.cancel_event:
            self.cancel_event.set()
        if getattr(self, "_autodarts_was_running", False):
            SystemdManager.start_autodarts()
            self._autodarts_was_running = False


class CamCalibrationPage(Adw.NavigationPage):
    """Navigation page wrapper for CAM-librator inside SUIT."""

    def __init__(self, window):
        super().__init__(title="Camera Optimizer (Experimental)", tag="cam_calibration")
        self.window = window
        self.view = CamCalibrationView(on_back_callback=self._on_back)
        self.set_child(self.view)

    def _on_back(self):
        if hasattr(self.window, "nav_view"):
            self.window.nav_view.pop()

    def on_page_opened(self):
        if hasattr(self.view, "refresh_cameras"):
            self.view.refresh_cameras()

    def on_page_closed(self):
        if hasattr(self.view, "on_app_close"):
            self.view.on_app_close()

