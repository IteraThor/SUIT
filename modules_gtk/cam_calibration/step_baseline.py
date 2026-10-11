"""Baseline reference snapshot step for CamCalibrationView."""

from __future__ import annotations
import logging
import threading
from typing import TYPE_CHECKING, Dict, Optional
import numpy as np
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
    """Manages untouched auto-exposure reference snapshot display."""

    def __init__(self, view: CamCalibrationView):
        self.view = view
        self.cam_cards: Dict[str, dict] = {}
        self._is_capturing = False

    def build_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(10)
        box.set_margin_bottom(12)
        box.set_margin_start(16)
        box.set_margin_end(16)

        # Header
        top_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label="Step 1 of 4: Baseline Reference", xalign=0.0)
        title.add_css_class("wizard-title")
        top_txt.append(title)
        sub = Gtk.Label(
            label="Untouched factory auto-exposure baseline captured with an empty dartboard.",
            xalign=0.0,
        )
        sub.set_wrap(True)
        sub.add_css_class("wizard-subtitle")
        top_txt.append(sub)
        box.append(top_txt)

        # Dartboard Empty Warning / Requirement Row
        prep_group = Adw.PreferencesGroup()
        prep_group.set_title("Board Preparation")

        self.prep_row = Adw.ActionRow(
            title="Make Sure No Darts Are Inserted",
            subtitle="Remove all darts from the board. An empty board reference is required to detect needle tips and eliminate sisal glow.",
        )
        self.prep_badge = Gtk.Label(label="Empty Board Required")
        self.prep_badge.add_css_class("status-pill-accent")
        self.prep_badge.set_valign(Gtk.Align.CENTER)
        self.prep_row.add_suffix(self.prep_badge)
        prep_group.add(self.prep_row)
        box.append(prep_group)

        # 3 Columns Side-by-Side
        self.baseline_columns_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)
        self.baseline_columns_box.set_vexpand(True)
        box.append(self.baseline_columns_box)

        # Bottom Bar
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        bottom_bar.set_margin_top(6)

        back_btn = Gtk.Button(label="Back to Dashboard")
        back_btn.add_css_class("pill")
        back_btn.connect("clicked", lambda _: self.view._go_to_dashboard())
        bottom_bar.append(back_btn)

        # Retake Button (Secondary, shown after capture)
        self.retake_btn = Gtk.Button(label="Retake Snapshot")
        self.retake_btn.add_css_class("pill")
        self.retake_btn.set_visible(False)
        self.retake_btn.connect("clicked", lambda _: self._on_retake_clicked())
        bottom_bar.append(self.retake_btn)

        # Take Snapshot Button (Primary before capture)
        self.snap_btn = Gtk.Button(label="Board is Empty — Take Baseline Snapshot")
        self.snap_btn.add_css_class("suggested-action")
        self.snap_btn.add_css_class("pill")
        self.snap_btn.add_css_class("big-cta-button")
        self.snap_btn.set_hexpand(True)
        self.snap_btn.set_halign(Gtk.Align.END)
        self.snap_btn.connect("clicked", lambda _: self._on_take_snapshot_clicked())
        bottom_bar.append(self.snap_btn)

        # Continue Button (Primary after capture)
        self.next_btn = Gtk.Button(label="Continue to Setup")
        self.next_btn.add_css_class("suggested-action")
        self.next_btn.add_css_class("pill")
        self.next_btn.add_css_class("big-cta-button")
        self.next_btn.set_hexpand(True)
        self.next_btn.set_halign(Gtk.Align.END)
        self.next_btn.set_visible(False)
        self.next_btn.connect("clicked", lambda _: self.view._go_to_setup())
        bottom_bar.append(self.next_btn)

        box.append(bottom_bar)
        return box

    def prepare_page(self):
        """Prepare baseline page cards and set state based on whether baseline is populated."""
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

            badge = Gtk.Label(label="Awaiting Snapshot")
            badge.set_css_classes(["status-pill-warn"])
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

            info_lbl = Gtk.Label(label="Board must be clear of all darts", xalign=0.5)
            info_lbl.add_css_class("caption")
            info_lbl.add_css_class("dim-label")
            card.append(info_lbl)

            self.cam_cards[cam.dev_node] = {
                "card": card,
                "badge": badge,
                "picture": pic,
                "info_lbl": info_lbl,
            }
            self.baseline_columns_box.append(card)

        has_baseline = bool(self.view.baseline_frames) and all(
            cam.dev_node in self.view.baseline_frames for cam in self.view.cameras
        )

        if has_baseline:
            self._display_captured_baseline()
        else:
            self._display_awaiting_state()

    def capture_and_display_baseline(self):
        """Compatibility wrapper for prepare_page."""
        self.prepare_page()

    def _display_awaiting_state(self):
        self.prep_badge.set_text("Empty Board Required")
        self.prep_badge.set_css_classes(["status-pill-accent"])
        self.snap_btn.set_label("Board is Empty — Take Baseline Snapshot")
        self.snap_btn.set_sensitive(True)
        self.snap_btn.set_visible(True)
        self.retake_btn.set_visible(False)
        self.next_btn.set_visible(False)

        for cam in self.view.cameras:
            if cam.dev_node in self.cam_cards:
                w = self.cam_cards[cam.dev_node]
                w["badge"].set_text("Awaiting Snapshot")
                w["badge"].set_css_classes(["status-pill-warn"])
                w["info_lbl"].set_text("Board must be clear of all darts")
                w["picture"].set_paintable(None)

    def _display_captured_baseline(self):
        self.prep_badge.set_text("Reference Ready")
        self.prep_badge.set_css_classes(["status-pill-ok"])
        self.snap_btn.set_visible(False)
        self.retake_btn.set_visible(True)
        self.next_btn.set_visible(True)

        for cam in self.view.cameras:
            if cam.dev_node in self.cam_cards:
                w = self.cam_cards[cam.dev_node]
                w["badge"].set_text("Reference Captured")
                w["badge"].set_css_classes(["status-pill-ok"])
                w["info_lbl"].set_text("Mode: Auto Exposure & Auto WB")
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
        self.snap_btn.set_label("Capturing Baseline Frames...")

        for cam in self.view.cameras:
            if cam.dev_node in self.cam_cards:
                w = self.cam_cards[cam.dev_node]
                w["badge"].set_text("Capturing...")
                w["badge"].set_css_classes(["status-pill-active"])
                w["info_lbl"].set_text("Resetting camera and capturing frame...")

        def worker():
            captured = {}
            for cam in self.view.cameras:
                try:
                    vdev = V4L2Device(cam.dev_node)
                    vdev.reset_to_defaults()
                    f = vdev.capture_frame(warmup_frames=3)
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
