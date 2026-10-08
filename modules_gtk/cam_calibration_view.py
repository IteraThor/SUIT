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
        self.active_review_mode: str = "after"  # "before" or "after"
        self.active_review_filter: str = "normal"  # "normal", "edge", "contrast"

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
            self.dash_reset_row.set_sensitive(True)
            self.dash_reset_btn.set_sensitive(True)
            self.dash_active_switch.set_active(self.persistence.is_calibration_enabled())
            if self.persistence.is_calibration_enabled():
                self.dash_active_switch.set_subtitle("Optimized profiles are active and enabled in boot service.")
            else:
                self.dash_active_switch.set_subtitle("Cameras are running in default automatic mode.")
        else:
            self.dash_active_switch.set_sensitive(False)
            self.dash_reset_row.set_sensitive(False)
            self.dash_reset_btn.set_sensitive(False)
            self.dash_active_switch.set_active(False)
            self.dash_active_switch.set_subtitle("No calibration found — Please start calibration.")
        self.dash_active_switch.handler_unblock_by_func(self._on_dash_switch_toggled)

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
            pic.set_size_request(400, 260)
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
            pic.set_size_request(400, 260)
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
            pic.set_size_request(400, 260)
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
        sub = Gtk.Label(
            label="Direct A/B comparison across all cameras with filter inspection.",
            xalign=0.0,
        )
        sub.add_css_class("wizard-subtitle")
        top_txt.append(sub)
        top_bar.append(top_txt)

        # Controls Container (A/B Toggle + Filters)
        ctrls_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        ctrls_box.set_hexpand(True)
        ctrls_box.set_halign(Gtk.Align.END)
        ctrls_box.set_valign(Gtk.Align.CENTER)

        # A/B Toggle Buttons
        ab_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.rev_btn_after = Gtk.Button(label="After (Calibrated)")
        self.rev_btn_after.add_css_class("pill")
        self.rev_btn_after.connect("clicked", lambda _: self._set_review_mode("after"))
        ab_box.append(self.rev_btn_after)

        self.rev_btn_before = Gtk.Button(label="Before (Default)")
        self.rev_btn_before.add_css_class("pill")
        self.rev_btn_before.connect("clicked", lambda _: self._set_review_mode("before"))
        ab_box.append(self.rev_btn_before)
        ctrls_box.append(ab_box)

        # Filter Buttons
        filt_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.filt_buttons = {}
        for f_val, f_lbl in [("normal", "Normal"), ("edge", "Edges & Wire"), ("noise", "Noise Analysis")]:
            btn = Gtk.Button(label=f_lbl)
            btn.add_css_class("pill")
            btn.connect("clicked", self._make_filter_handler(f_val))
            filt_box.append(btn)
            self.filt_buttons[f_val] = btn
        ctrls_box.append(filt_box)

        top_bar.append(ctrls_box)
        box.append(top_bar)

        # 3 Columns Side-by-Side
        self.review_columns_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14, homogeneous=True)
        self.review_columns_box.set_vexpand(True)
        box.append(self.review_columns_box)

        # Bottom Bar: Discard vs Apply
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        bottom_bar.set_margin_top(10)

        discard_btn = Gtk.Button(label="Discard")
        discard_btn.add_css_class("pill")
        discard_btn.connect("clicked", lambda _: self._go_to_dashboard())
        bottom_bar.append(discard_btn)

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
        while child := self.review_columns_box.get_first_child():
            self.review_columns_box.remove(child)

        self.review_cam_widgets = {}
        self._review_frame_cache = {}

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
            pic.set_size_request(400, 260)
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
                m_opt = res.optimized_metrics
                exp_lbl = Gtk.Label(label=f"Exposure: {s_opt.exposure_absolute * 0.1:.1f}ms", xalign=0.0)
                exp_lbl.add_css_class("caption")
                gain_lbl = Gtk.Label(label=f"Gain: {s_opt.gain}", xalign=0.0)
                gain_lbl.add_css_class("caption")
                noise_lbl = Gtk.Label(label=f"Noise: -{abs(m_opt.temporal_noise * 100):.1f}%", xalign=0.0)
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

            self.review_cam_widgets[cam.dev_node] = {
                "badge": badge,
                "picture": pic,
            }
            self.review_columns_box.append(card)

        self._set_review_mode("after")
        self._set_review_filter("normal")

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
        for k, b in self.filt_buttons.items():
            if k == f_val:
                b.add_css_class("suggested-action")
            else:
                b.remove_css_class("suggested-action")
        self._refresh_review_pictures()

    def _refresh_review_pictures(self):
        """Render and display frames according to active mode (after/before) and active filter."""
        for cam in self.cameras:
            if cam.dev_node not in self.review_cam_widgets:
                continue

            res = self.results.get(cam.dev_node)
            base_frame = self.baseline_frames.get(cam.dev_node)

            if self.active_review_mode == "after" and res:
                frame = res.optimized_frame
                badge_sign = "+" if res.improvement_percentage >= 0 else ""
                badge_txt = f"{badge_sign}{res.improvement_percentage:.0f}% Sharper"
                badge_cls = "status-pill-ok" if res.improvement_percentage >= 0 else "status-pill-warn"
            else:
                frame = res.initial_frame if res else base_frame
                badge_txt = "Default"
                badge_cls = "status-pill-warn"

            if frame is None:
                continue

            # Apply filter
            if self.active_review_filter == "edge":
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
        self.stack.set_visible_child_name("dashboard")
        self._update_dashboard()

    def _go_to_focus(self):
        if SystemdManager.is_autodarts_active():
            self._autodarts_was_running = True
            SystemdManager.stop_autodarts()
        self.detector.unlock_target()
        self.accum_crops.clear()
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

