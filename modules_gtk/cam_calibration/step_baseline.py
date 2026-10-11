"""Baseline reference snapshot step for CamCalibrationView with live feed preview."""

from __future__ import annotations
import logging
import threading
import time
from typing import TYPE_CHECKING, Dict, Optional
import numpy as np
import cv2

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib, Adw

from core.v4l2_device import V4L2Device
from modules_gtk.cv_gtk_utils import frame_to_pixbuf

if TYPE_CHECKING:
    from modules_gtk.cam_calibration_view import CamCalibrationView

logger = logging.getLogger("cam_librator.baseline")


class BaselineStep:
    """Manages untouched reference live feed and snapshot capture for empty dartboard."""

    def __init__(self, view: CamCalibrationView):
        self.view = view
        self.cam_cards: Dict[str, dict] = {}
        self._is_capturing = False
        self._is_streaming = False
        self._stop_stream = threading.Event()
        self._stream_thread: Optional[threading.Thread] = None
        self._latest_live_frames: Dict[str, np.ndarray] = {}
        self._lock = threading.Lock()

    def build_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(8)
        box.set_margin_bottom(10)
        box.set_margin_start(16)
        box.set_margin_end(16)

        # Header - Concise and Minimalist
        top_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label="Step 1 of 4: Baseline Reference", xalign=0.0)
        title.add_css_class("wizard-title")
        top_txt.append(title)

        sub = Gtk.Label(
            label="Ensure no darts are in the board.",
            xalign=0.0,
        )
        sub.add_css_class("wizard-subtitle-danger")
        sub.add_css_class("error")
        top_txt.append(sub)
        box.append(top_txt)

        # 3 Camera Columns Side-by-Side
        self.baseline_columns_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)
        self.baseline_columns_box.set_vexpand(True)
        box.append(self.baseline_columns_box)

        # Bottom Action Bar
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        bottom_bar.set_margin_top(6)

        # Back Button (returns to Camera Tools hub)
        back_btn = Gtk.Button(label="Back")
        back_btn.add_css_class("pill")
        back_btn.connect("clicked", lambda _: self._on_back_clicked())
        bottom_bar.append(back_btn)

        # Retake Button (Secondary, shown after capture)
        self.retake_btn = Gtk.Button(label="Retake Snapshot")
        self.retake_btn.add_css_class("pill")
        self.retake_btn.set_visible(False)
        self.retake_btn.connect("clicked", lambda _: self._on_retake_clicked())
        bottom_bar.append(self.retake_btn)

        # Take Snapshot Button (Primary before capture)
        self.snap_btn = Gtk.Button(label="Capture Reference")
        self.snap_btn.add_css_class("suggested-action")
        self.snap_btn.add_css_class("pill")
        self.snap_btn.add_css_class("big-cta-button")
        self.snap_btn.set_hexpand(True)
        self.snap_btn.set_halign(Gtk.Align.END)
        self.snap_btn.connect("clicked", lambda _: self._on_take_snapshot_clicked())
        bottom_bar.append(self.snap_btn)

        # Continue Button (Primary after capture)
        self.next_btn = Gtk.Button(label="Continue to Step 2")
        self.next_btn.add_css_class("suggested-action")
        self.next_btn.add_css_class("pill")
        self.next_btn.add_css_class("big-cta-button")
        self.next_btn.set_hexpand(True)
        self.next_btn.set_halign(Gtk.Align.END)
        self.next_btn.set_visible(False)
        self.next_btn.connect("clicked", lambda _: self._on_next_clicked())
        bottom_bar.append(self.next_btn)

        box.append(bottom_bar)
        return box

    def _on_back_clicked(self):
        self.stop_live_stream()
        if hasattr(self.view, "on_back") and self.view.on_back:
            self.view.on_back()
        elif hasattr(self.view, "_go_to_dashboard"):
            self.view._go_to_dashboard()

    def _on_next_clicked(self):
        self.stop_live_stream()
        self.view._go_to_setup()

    def prepare_page(self):
        """Rebuild camera columns and enter live preview or captured review."""
        while child := self.baseline_columns_box.get_first_child():
            self.baseline_columns_box.remove(child)

        self.cam_cards.clear()

        for cam in self.view.cameras:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            card.add_css_class("cam-column-card")

            hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            title = Gtk.Label(label=f"Camera {cam.index}", xalign=0.0)
            title.add_css_class("heading")
            hdr.append(title)

            badge = Gtk.Label(label="Live")
            badge.set_css_classes(["status-pill-ok"])
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

            self.cam_cards[cam.dev_node] = {
                "card": card,
                "badge": badge,
                "picture": pic,
            }
            self.baseline_columns_box.append(card)

        has_baseline = bool(self.view.baseline_frames) and all(
            cam.dev_node in self.view.baseline_frames for cam in self.view.cameras
        )

        if has_baseline:
            self.stop_live_stream()
            self._display_captured_baseline()
        else:
            self._display_awaiting_state()
            self.start_live_stream()

    def capture_and_display_baseline(self):
        """Compatibility wrapper for prepare_page."""
        self.prepare_page()

    def start_live_stream(self):
        """Start background live video preview for all available cameras."""
        if self._is_streaming:
            return

        self._stop_stream.clear()
        self._is_streaming = True
        self._stream_thread = threading.Thread(target=self._live_stream_worker, daemon=True)
        self._stream_thread.start()

    def stop_live_stream(self):
        """Stop background live streaming and release camera devices."""
        self._is_streaming = False
        self._stop_stream.set()
        if self._stream_thread and self._stream_thread.is_alive():
            self._stream_thread.join(timeout=2.0)
            self._stream_thread = None
        time.sleep(0.3)

    def _live_stream_worker(self):
        """Capture live preview frames continuously from connected cameras."""
        caps: Dict[str, cv2.VideoCapture] = {}

        for cam in self.view.cameras:
            try:
                vdev = V4L2Device(cam.dev_node)
                cap = vdev.open_stream(width=640, height=480)
                if cap and cap.isOpened():
                    caps[cam.dev_node] = cap
            except Exception as e:
                logger.debug("Failed opening live stream on %s: %s", cam.dev_node, e)

        try:
            while self._is_streaming and not self._stop_stream.is_set():
                for cam_node, cap in list(caps.items()):
                    if self._stop_stream.is_set():
                        break
                    try:
                        ret, frame = cap.read()
                        if ret and frame is not None:
                            with self._lock:
                                self._latest_live_frames[cam_node] = frame
                            GLib.idle_add(self._update_cam_frame, cam_node, frame)
                    except Exception:
                        pass
                time.sleep(0.04)  # ~25 FPS loop
        finally:
            for cap in caps.values():
                try:
                    cap.release()
                except Exception:
                    pass

    def _update_cam_frame(self, dev_node: str, frame: np.ndarray):
        """Update live camera picture on main UI thread."""
        if dev_node in self.cam_cards:
            pic = self.cam_cards[dev_node]["picture"]
            pix = frame_to_pixbuf(frame, max_w=480, max_h=300)
            if pix:
                pic.set_pixbuf(pix)
        return False

    def _display_awaiting_state(self):
        self.snap_btn.set_label("Capture Reference")
        self.snap_btn.set_sensitive(True)
        self.snap_btn.set_visible(True)
        self.retake_btn.set_visible(False)
        self.next_btn.set_visible(False)

        for cam in self.view.cameras:
            if cam.dev_node in self.cam_cards:
                w = self.cam_cards[cam.dev_node]
                w["badge"].set_text("Live")
                w["badge"].set_css_classes(["status-pill-ok"])

    def _display_captured_baseline(self):
        self.snap_btn.set_visible(False)
        self.retake_btn.set_visible(True)
        self.next_btn.set_visible(True)

        for cam in self.view.cameras:
            if cam.dev_node in self.cam_cards:
                w = self.cam_cards[cam.dev_node]
                w["badge"].set_text("Captured")
                w["badge"].set_css_classes(["status-pill-ok"])
                frame = self.view.baseline_frames.get(cam.dev_node)
                if frame is not None:
                    pix = frame_to_pixbuf(frame, max_w=480, max_h=300)
                    if pix:
                        w["picture"].set_pixbuf(pix)

    def _on_take_snapshot_clicked(self):
        if self._is_capturing:
            return
        self._is_capturing = True
        self.snap_btn.set_sensitive(False)
        self.snap_btn.set_label("Capturing Reference...")

        def worker():
            # Stop stream so we can take final clean high-quality snapshots or use latest
            self.stop_live_stream()
            captured = {}

            with self._lock:
                latest = dict(self._latest_live_frames)

            for cam in self.view.cameras:
                if cam.dev_node in latest and latest[cam.dev_node] is not None:
                    f = latest[cam.dev_node]
                    if f.shape[:2] != (960, 1280):
                        f = cv2.resize(f, (1280, 960), interpolation=cv2.INTER_LINEAR)
                    captured[cam.dev_node] = f
                else:
                    try:
                        vdev = V4L2Device(cam.dev_node)
                        f = vdev.capture_frame(width=1280, height=960, warmup_frames=2)
                        if f is not None:
                            captured[cam.dev_node] = f
                    except Exception as e:
                        logger.error("Failed to capture baseline on %s: %s", cam.dev_node, e)

            GLib.idle_add(self._on_capture_finished, captured)

        threading.Thread(target=worker, daemon=True).start()

    def _on_capture_finished(self, captured: Dict[str, np.ndarray]):
        self._is_capturing = False
        self.view.baseline_frames.update(captured)
        self._display_captured_baseline()
        return False

    def _on_retake_clicked(self):
        self.view.baseline_frames.clear()
        self._display_awaiting_state()
        self.start_live_stream()
