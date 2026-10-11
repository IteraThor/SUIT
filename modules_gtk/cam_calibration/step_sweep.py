"""Live calibration sweep step for CamCalibrationView: background optimization worker & UI."""

from __future__ import annotations
import logging
from pathlib import Path
import threading
from typing import TYPE_CHECKING, Dict, Optional
import cv2
import numpy as np

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk

from core.optimizer import CameraOptimizer, OptimizationResult
from core.session_exporter import SessionExporter
from core.systemd_service import SystemdManager
from core.v4l2_device import V4L2Device
from modules_gtk.cv_gtk_utils import frame_to_pixbuf

if TYPE_CHECKING:
    from modules_gtk.cam_calibration_view import CamCalibrationView

logger = logging.getLogger("cam_librator.sweep")


class SweepStep:
    """Manages the calibration sweep progress, worker thread, and optimization callbacks."""

    def __init__(self, view: CamCalibrationView):
        self.view = view
        self.sweep_cam_widgets: Dict[str, dict] = {}
        self.sweep_running: bool = False
        self.cancel_event: Optional[threading.Event] = None

    def build_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(12)
        box.set_margin_start(16)
        box.set_margin_end(16)

        # Header
        top_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.sweep_title_lbl = Gtk.Label(label="Step 3 of 4: Calibration in Progress...", xalign=0.0)
        self.sweep_title_lbl.add_css_class("wizard-title")
        top_txt.append(self.sweep_title_lbl)
        self.sweep_sub_lbl = Gtk.Label(
            label="Calibrating cameras and balancing exposure...",
            xalign=0.0,
        )
        self.sweep_sub_lbl.set_wrap(True)
        self.sweep_sub_lbl.add_css_class("wizard-subtitle")
        top_txt.append(self.sweep_sub_lbl)
        box.append(top_txt)

        # 3 Camera Columns Side-by-Side
        self.sweep_columns_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)
        self.sweep_columns_box.set_vexpand(True)
        box.append(self.sweep_columns_box)

        # Progress Bar & Status
        prog_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.sweep_progress_bar = Gtk.ProgressBar()
        self.sweep_progress_bar.set_show_text(True)
        self.sweep_progress_bar.set_fraction(0.0)
        prog_box.append(self.sweep_progress_bar)
        box.append(prog_box)

        # Bottom Bar: Cancel and Continue
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        bottom_bar.set_margin_top(6)

        self.sweep_cancel_btn = Gtk.Button(label="Cancel Calibration")
        self.sweep_cancel_btn.add_css_class("destructive-action")
        self.sweep_cancel_btn.add_css_class("pill")
        self.sweep_cancel_btn.connect("clicked", lambda _: self.cancel_sweep())
        bottom_bar.append(self.sweep_cancel_btn)

        self.sweep_finish_btn = Gtk.Button(label="Continue to Review")
        self.sweep_finish_btn.add_css_class("suggested-action")
        self.sweep_finish_btn.add_css_class("pill")
        self.sweep_finish_btn.add_css_class("big-cta-button")
        self.sweep_finish_btn.set_hexpand(True)
        self.sweep_finish_btn.set_halign(Gtk.Align.END)
        self.sweep_finish_btn.set_visible(False)
        self.sweep_finish_btn.connect("clicked", lambda _: self.view._go_to_review())
        bottom_bar.append(self.sweep_finish_btn)
        box.append(bottom_bar)

        return box

    def setup_sweep_columns(self):
        while child := self.sweep_columns_box.get_first_child():
            self.sweep_columns_box.remove(child)

        self.sweep_cam_widgets.clear()
        for cam in self.view.cameras:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
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
            pic.set_size_request(140, 95)
            pic.set_hexpand(True)
            pic.set_vexpand(True)
            card.append(pic)


            # Preload baseline frame if available
            if cam.dev_node in self.view.baseline_frames:
                pix = frame_to_pixbuf(self.view.baseline_frames[cam.dev_node], max_w=480, max_h=300)
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

    def start_calibration_sweep(self):
        """Transition to sweep view and run optimization in background thread."""
        if SystemdManager.is_autodarts_active():
            self.view._autodarts_was_running = True
            SystemdManager.stop_autodarts()

        self.view.stack.set_visible_child_name("sweep")
        self.setup_sweep_columns()
        self.sweep_progress_bar.set_fraction(0.0)
        self.sweep_finish_btn.set_visible(False)
        self.sweep_cancel_btn.set_sensitive(True)

        self.sweep_running = True
        self.cancel_event = threading.Event()
        self.view.cancel_event = self.cancel_event
        self.view.results.clear()

        thread = threading.Thread(target=self._sweep_worker, daemon=True)
        thread.start()

    def cancel_sweep(self):
        if self.cancel_event:
            self.cancel_event.set()
        self.sweep_cancel_btn.set_sensitive(False)
        self.sweep_sub_lbl.set_text("Cancelling... Restoring initial state.")

    def _sweep_worker(self):
        """Sequentially optimize cameras according to selected scope."""
        target_cams = (
            self.view.cameras
            if self.view.selected_scope == "all"
            else [c for c in self.view.cameras if c.dev_node == self.view.selected_scope]
        )
        total = len(target_cams)

        for idx, cam in enumerate(target_cams):
            if self.cancel_event and self.cancel_event.is_set():
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
                empty_ref = self.view.baseline_frames.get(cam.dev_node)
                result = optimizer.run_optimization(
                    precision_mode=self.view.selected_precision,
                    progress_cb=cb,
                    cancel_event=self.cancel_event,
                    empty_baseline=empty_ref,
                )

                if result:
                    self.view.results[cam.dev_node] = result
                    GLib.idle_add(self._on_cam_sweep_done, cam.dev_node, result)

            except Exception as e:
                logger.exception("Error during sweep on %s: %s", cam.dev_node, e)

        # Auto-export sample images, crops, edge maps, and JSON metrics in worker thread
        if self.view.results and not (self.cancel_event and self.cancel_event.is_set()):
            try:
                exporter = SessionExporter()
                session_path = exporter.export_session(self.view.results)
                logger.info("Saved calibration session telemetry to %s", session_path)
            except Exception as e:
                logger.warning("Failed to auto-save sweep session telemetry: %s", e)

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
            GLib.timeout_add(1500, lambda: self.view._go_to_dashboard())
        else:
            self.sweep_progress_bar.set_fraction(1.0)
            self.sweep_title_lbl.set_text("Calibration Successfully Completed!")
            self.sweep_sub_lbl.set_text("All values calculated. Click 'Continue to Review'.")
            self.sweep_finish_btn.set_visible(True)
            self.sweep_cancel_btn.set_sensitive(False)
        return False
