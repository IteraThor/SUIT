"""Baseline reference snapshot step for CamCalibrationView."""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk

from core.v4l2_device import V4L2Device
from modules_gtk.cv_gtk_utils import frame_to_pixbuf

if TYPE_CHECKING:
    from modules_gtk.cam_calibration_view import CamCalibrationView

logger = logging.getLogger("cam_librator.baseline")


class BaselineStep:
    """Manages untouched auto-exposure reference snapshot display."""

    def __init__(self, view: CamCalibrationView):
        self.view = view

    def build_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(12)
        box.set_margin_start(16)
        box.set_margin_end(16)

        # Header
        top_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label="Step 2 of 5: Baseline Reference", xalign=0.0)
        title.add_css_class("wizard-title")
        top_txt.append(title)
        sub = Gtk.Label(
            label="Untouched baseline image captured in default automatic mode across all cameras.",
            xalign=0.0,
        )
        sub.set_wrap(True)
        sub.add_css_class("wizard-subtitle")
        top_txt.append(sub)
        box.append(top_txt)

        # 3 Columns Side-by-Side
        self.baseline_columns_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)
        self.baseline_columns_box.set_vexpand(True)
        box.append(self.baseline_columns_box)

        # Bottom Bar
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        bottom_bar.set_margin_top(6)

        back_btn = Gtk.Button(label="Back to Focus Check")
        back_btn.add_css_class("pill")
        back_btn.connect("clicked", lambda _: self.view._go_to_focus())
        bottom_bar.append(back_btn)

        next_btn = Gtk.Button(label="Continue to Setup")
        next_btn.add_css_class("suggested-action")
        next_btn.add_css_class("pill")
        next_btn.set_hexpand(True)
        next_btn.set_halign(Gtk.Align.END)
        next_btn.connect("clicked", lambda _: self.view._go_to_setup())
        bottom_bar.append(next_btn)
        box.append(bottom_bar)

        return box

    def capture_and_display_baseline(self):
        """Display untouched auto-exposure reference frames across all 3 cameras."""
        while child := self.baseline_columns_box.get_first_child():
            self.baseline_columns_box.remove(child)

        for cam in self.view.cameras:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
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
            pic.set_size_request(140, 95)
            pic.set_hexpand(True)
            pic.set_vexpand(True)
            card.append(pic)


            # Use cached frame from Focus Check, or fallback to direct capture
            frame = self.view.baseline_frames.get(cam.dev_node)
            if frame is None:
                try:
                    vdev = V4L2Device(cam.dev_node)
                    frame = vdev.capture_frame(warmup_frames=2)
                    if frame is not None:
                        self.view.baseline_frames[cam.dev_node] = frame
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
