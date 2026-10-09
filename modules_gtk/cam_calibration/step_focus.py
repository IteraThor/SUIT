"""Focus check step for CamCalibrationView: 3-camera live focus stream & peaking HUD."""

from __future__ import annotations
import logging
import threading
import time
from typing import TYPE_CHECKING, Dict, Optional
import cv2
import numpy as np

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GdkPixbuf, GLib, Gtk

from core.cv_utils import draw_sweet_spot_overlay_suit, process_zoom_crop_suit
from core.metrics import QualityEvaluator
from core.v4l2_device import V4L2Device
from modules_gtk.cv_gtk_utils import frame_to_pixbuf

if TYPE_CHECKING:
    from modules_gtk.cam_calibration_view import CamCalibrationView

logger = logging.getLogger("cam_librator.focus")


class FocusStep:
    """Manages the 3-camera live focus check page, streaming worker, and peaking HUD."""

    def __init__(self, view: CamCalibrationView):
        self.view = view
        self.focus_cam_widgets: Dict[str, dict] = {}
        self.focus_zoom_all: bool = False
        self.focus_per_cam_zoom: Dict[str, bool] = {}
        self.accum_crops: Dict[str, np.ndarray] = {}
        self.focus_running: bool = False
        self.focus_thread: Optional[threading.Thread] = None
        self.focus_stop_event: threading.Event = threading.Event()

    def build_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(12)
        box.set_margin_start(16)
        box.set_margin_end(16)

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
        sub.set_wrap(True)
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
        self.focus_columns_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)
        self.focus_columns_box.set_vexpand(True)
        box.append(self.focus_columns_box)

        # Bottom Bar: Back and Next
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        bottom_bar.set_margin_top(6)

        back_btn = Gtk.Button(label="Back to Dashboard")
        back_btn.add_css_class("pill")
        back_btn.connect("clicked", lambda _: self.view._go_to_dashboard())
        bottom_bar.append(back_btn)

        self.focus_next_btn = Gtk.Button(label="Continue to Baseline")
        self.focus_next_btn.add_css_class("suggested-action")
        self.focus_next_btn.add_css_class("pill")
        self.focus_next_btn.set_hexpand(True)
        self.focus_next_btn.set_halign(Gtk.Align.END)
        self.focus_next_btn.connect("clicked", lambda _: self.view._go_to_baseline())
        bottom_bar.append(self.focus_next_btn)
        box.append(bottom_bar)

        return box

    def setup_focus_columns(self):
        """Create rich SUIT focus assistant cards for each camera."""
        while child := self.focus_columns_box.get_first_child():
            self.focus_columns_box.remove(child)

        self.focus_cam_widgets.clear()
        self.accum_crops.clear()

        for cam in self.view.cameras:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
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
            meta_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
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
            pic.set_size_request(140, 95)
            pic.set_hexpand(True)
            pic.set_vexpand(True)
            card.append(pic)

            # Controls row under picture: Zoom & Peaking toggle + Reset button
            ctrls_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
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
            self.view.detector.unlock_target(dev_node)
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

    def start_focus_stream(self):
        """Start background capture thread for 3-camera live focus check."""
        self.stop_focus_stream()
        self.focus_stop_event = threading.Event()
        self.focus_running = True
        self.focus_thread = threading.Thread(
            target=self._focus_stream_worker,
            args=(self.focus_stop_event,),
            daemon=True,
        )
        self.focus_thread.start()

    def stop_focus_stream(self):
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
            for cam in self.view.cameras:
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
                for cam in self.view.cameras:
                    if stop_event.is_set():
                        break
                    cap = caps.get(cam.dev_node)
                    if not cap or not cap.isOpened():
                        continue

                    ret, frame = cap.read()
                    if not ret or frame is None or frame.size == 0:
                        continue

                    # Cache untouched auto-exposure frame for Baseline Reference
                    self.view.baseline_frames[cam.dev_node] = frame.copy()

                    target_roi = self.view.detector.get_furthest_double_roi(
                        frame,
                        dev_node=cam.dev_node,
                        by_path=cam.by_path,
                        cam_idx=cam.index,
                    )

                    tx, ty, tw, th = target_roi
                    patch = frame[ty : ty + th, tx : tx + tw]
                    score = QualityEvaluator.calculate_focus_score(patch)

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
