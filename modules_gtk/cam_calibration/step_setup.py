"""Setup parameters step for CamCalibrationView: scope & precision."""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING, Dict
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

if TYPE_CHECKING:
    from modules_gtk.cam_calibration_view import CamCalibrationView

logger = logging.getLogger("cam_librator.setup")


class SetupStep:
    """Manages calibration scope and precision mode selection."""

    def __init__(self, view: CamCalibrationView):
        self.view = view
        self.scope_buttons: Dict[str, Gtk.Button] = {}
        self.prec_buttons: Dict[str, Gtk.Button] = {}

    def build_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        box.set_margin_bottom(14)
        box.set_margin_start(20)
        box.set_margin_end(20)

        # Header
        top_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label="Step 2 of 4: Calibration Setup", xalign=0.0)
        title.add_css_class("wizard-title")
        top_txt.append(title)
        sub = Gtk.Label(
            label="Select camera scope and measurement precision for optimization.",
            xalign=0.0,
        )
        sub.set_wrap(True)
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
        self.scope_buttons.clear()
        self.scope_row.add_suffix(self.scope_box)
        pref_group.add(self.scope_row)

        # Row 2: Precision (Standard vs Deep)
        self.prec_row = Adw.ActionRow(
            title="Precision Mode",
            subtitle="Standard (~30s) for quick shutter/gain scan, Deep (~2 min) for fine 2D grid",
        )
        self.prec_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.prec_box.set_valign(Gtk.Align.CENTER)
        self.prec_buttons.clear()

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

        # Dart Target Setup Group
        dart_group = Adw.PreferencesGroup()
        dart_group.set_title("Target Setup")

        dart_row = Adw.ActionRow(
            title="Insert Darts in Board",
            subtitle="Insert three darts, one at T20, and then one above and one below into S20.",
        )
        dart_badge = Gtk.Label(label="Required")
        dart_badge.add_css_class("status-pill-accent")
        dart_badge.set_valign(Gtk.Align.CENTER)
        dart_row.add_suffix(dart_badge)
        dart_group.add(dart_row)
        box.append(dart_group)

        # Bottom Bar
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        bottom_bar.set_margin_top(16)

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

    def setup_scope_buttons(self):
        while child := self.scope_box.get_first_child():
            self.scope_box.remove(child)

        self.scope_buttons.clear()

        all_btn = Gtk.Button(label="All Cameras")
        all_btn.add_css_class("pill")
        all_btn.connect("clicked", lambda _: self._set_scope("all"))
        self.scope_box.append(all_btn)
        self.scope_buttons["all"] = all_btn

        for cam in self.view.cameras:
            btn = Gtk.Button(label=f"Camera {cam.index}")
            btn.add_css_class("pill")
            btn.connect("clicked", self._make_scope_handler(cam.dev_node))
            self.scope_box.append(btn)
            self.scope_buttons[cam.dev_node] = btn

        self._set_scope(self.view.selected_scope or "all")

    def _make_scope_handler(self, dev_node: str):
        return lambda _: self._set_scope(dev_node)

    def _set_scope(self, scope: str):
        self.view.selected_scope = scope
        for k, b in self.scope_buttons.items():
            if k == scope:
                b.add_css_class("suggested-action")
            else:
                b.remove_css_class("suggested-action")

    def _make_precision_handler(self, val: str):
        return lambda _: self._update_precision_buttons(val)

    def _update_precision_buttons(self, val: str):
        self.view.selected_precision = val
        for k, b in self.prec_buttons.items():
            if k == val:
                b.add_css_class("suggested-action")
            else:
                b.remove_css_class("suggested-action")
