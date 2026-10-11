"""Setup step for CamCalibrationView: prominent dart placement notification."""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

if TYPE_CHECKING:
    from modules_gtk.cam_calibration_view import CamCalibrationView

logger = logging.getLogger("cam_librator.setup")


class SetupStep:
    """Manages target dart placement notification with deep multi-camera defaults."""

    def __init__(self, view: CamCalibrationView):
        self.view = view

    def build_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(20)
        box.set_margin_end(20)

        # Header
        top_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label="Step 2 of 4: Dart Placement", xalign=0.0)
        title.add_css_class("wizard-title")
        top_txt.append(title)

        sub = Gtk.Label(
            label="Prepare board targets for optical edge tuning.",
            xalign=0.0,
        )
        sub.add_css_class("wizard-subtitle")
        top_txt.append(sub)
        box.append(top_txt)

        # Spacer to center the notifier vertically
        box.append(Gtk.Box(vexpand=True))

        # Prominent Dartboard Notifier Card
        notifier_clamp = Adw.Clamp(maximum_size=720, tightening_threshold=580)
        notifier_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        notifier_card.add_css_class("card")
        notifier_card.set_margin_top(20)
        notifier_card.set_margin_bottom(20)
        notifier_card.set_margin_start(20)
        notifier_card.set_margin_end(20)

        # Inner container with generous padding
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        inner.set_margin_top(28)
        inner.set_margin_bottom(28)
        inner.set_margin_start(24)
        inner.set_margin_end(24)

        # Target Icon
        icon = Gtk.Image.new_from_icon_name("media-record-symbolic")
        icon.set_pixel_size(48)
        icon.add_css_class("accent-icon")
        icon.set_halign(Gtk.Align.CENTER)
        inner.append(icon)

        # Big Notifier Text
        msg_lbl = Gtk.Label(
            label="Insert 3 darts into T20, and one above and one below into S20.",
            xalign=0.5,
        )
        msg_lbl.add_css_class("title-2")
        msg_lbl.set_wrap(True)
        msg_lbl.set_justify(Gtk.Justification.CENTER)
        inner.append(msg_lbl)

        notifier_card.append(inner)
        notifier_clamp.set_child(notifier_card)
        box.append(notifier_clamp)

        # Spacer to balance vertical centering
        box.append(Gtk.Box(vexpand=True))

        # Bottom Bar
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        bottom_bar.set_margin_top(12)

        back_btn = Gtk.Button(label="Back to Baseline")
        back_btn.add_css_class("pill")
        back_btn.connect("clicked", lambda _: self.view._go_to_baseline())
        bottom_bar.append(back_btn)

        start_btn = Gtk.Button(label="Darts Inserted — Start Calibration")
        start_btn.add_css_class("suggested-action")
        start_btn.add_css_class("pill")
        start_btn.add_css_class("big-cta-button")
        start_btn.set_hexpand(True)
        start_btn.set_halign(Gtk.Align.END)
        start_btn.connect("clicked", lambda _: self.view._start_calibration_sweep())
        bottom_bar.append(start_btn)

        box.append(bottom_bar)
        return box

    def prepare_page(self):
        """Set default parameters: all cameras and deep precision mode."""
        self.view.selected_scope = "all"
        self.view.selected_precision = "deep"

    def setup_scope_buttons(self):
        """Compatibility method for legacy callers."""
        self.prepare_page()
