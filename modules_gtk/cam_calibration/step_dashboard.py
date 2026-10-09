"""Dashboard step for CamCalibrationView: profile status, toggle, and reset."""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from core.v4l2_device import V4L2Device

if TYPE_CHECKING:
    from modules_gtk.cam_calibration_view import CamCalibrationView

logger = logging.getLogger("cam_librator.dashboard")


class DashboardStep:
    """Manages the Dashboard overview, profile table, and master hardware switch."""

    def __init__(self, view: CamCalibrationView):
        self.view = view

    def build_page(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        box.set_margin_bottom(14)
        box.set_margin_start(16)
        box.set_margin_end(16)

        # Top Header
        header_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label="Camera Overview & Profiles", xalign=0.0)
        title.add_css_class("wizard-title")
        header_box.append(title)

        subtitle = Gtk.Label(
            label="Hardware-level calibration for maximum Autodarts accuracy at 0% CPU load.",
            xalign=0.0,
        )
        subtitle.set_wrap(True)
        subtitle.add_css_class("wizard-subtitle")
        header_box.append(subtitle)
        box.append(header_box)

        # 3-Camera Status Cards
        self.dash_cards_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)
        self.dash_cards_box.set_hexpand(True)
        box.append(self.dash_cards_box)

        # Activation Control Group
        pref_group = Adw.PreferencesGroup()
        pref_group.set_title("Hardware Profile Control")

        self.dash_active_switch = Adw.SwitchRow(
            title="Enable Calibrated Profiles",
            subtitle="Applies calibrated hardware values at boot and during runtime",
        )
        self.dash_active_switch.connect("notify::active", self._on_switch_toggled)
        pref_group.add(self.dash_active_switch)

        self.dash_reset_row = Adw.ActionRow(
            title="Reset Calibrated Values",
            subtitle="Deletes saved hardware profiles and restores all cameras to defaults",
        )
        self.dash_reset_btn = Gtk.Button(label="Reset")
        self.dash_reset_btn.add_css_class("destructive-action")
        self.dash_reset_btn.add_css_class("pill")
        self.dash_reset_btn.set_valign(Gtk.Align.CENTER)
        self.dash_reset_btn.connect("clicked", self._on_reset_clicked)
        self.dash_reset_row.add_suffix(self.dash_reset_btn)
        pref_group.add(self.dash_reset_row)

        box.append(pref_group)

        # Start Calibration CTA Button
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        btn_box.set_halign(Gtk.Align.CENTER)
        btn_box.set_margin_top(8)

        start_calib_btn = Gtk.Button(label="Start Calibration")
        start_calib_btn.add_css_class("suggested-action")
        start_calib_btn.add_css_class("pill")
        start_calib_btn.add_css_class("big-cta-button")
        start_calib_btn.connect("clicked", lambda _: self.view._go_to_focus())
        btn_box.append(start_calib_btn)
        box.append(btn_box)

        scroll.set_child(box)
        return scroll


    def update_dashboard(self):
        """Update dashboard table with current calibrated vs standard values."""
        while child := self.dash_cards_box.get_first_child():
            self.dash_cards_box.remove(child)

        calibrations = self.view.persistence.load_calibrations()
        has_any_calib = len(calibrations) > 0

        self.dash_active_switch.handler_block_by_func(self._on_switch_toggled)
        if has_any_calib:
            self.dash_active_switch.set_sensitive(True)
            self.dash_active_switch.set_active(self.view.persistence.is_calibration_enabled())
            if self.view.persistence.is_calibration_enabled():
                self.dash_active_switch.set_subtitle("Optimized profiles are active and enabled in boot service.")
            else:
                self.dash_active_switch.set_subtitle("Cameras are running in default automatic mode.")
        else:
            self.dash_active_switch.set_sensitive(False)
            self.dash_active_switch.set_active(False)
            self.dash_active_switch.set_subtitle("No calibration found — Please start calibration.")
        self.dash_active_switch.handler_unblock_by_func(self._on_switch_toggled)

        self.dash_reset_row.set_sensitive(True)
        self.dash_reset_btn.set_sensitive(True)

        for cam in self.view.cameras:
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

            node_lbl = Gtk.Label(label=f"Node: {cam.dev_node}", xalign=0.0)
            node_lbl.add_css_class("dim-label")
            card.append(node_lbl)

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

    def _on_switch_toggled(self, switch, _):
        """Enable or disable calibrations system-wide."""
        is_active = switch.get_active()
        self.view.persistence.set_calibration_enabled(is_active)
        calibrations = self.view.persistence.load_calibrations()
        for cam in self.view.cameras:
            try:
                vdev = V4L2Device(cam.dev_node)
                if is_active and cam.by_path in calibrations:
                    vdev.apply_settings(calibrations[cam.by_path])
                else:
                    vdev.reset_to_defaults()
            except Exception as e:
                logger.warning("Error applying switch toggle on %s: %s", cam.dev_node, e)
        self.update_dashboard()

    def _on_reset_clicked(self, _btn):
        """Reset all camera hardware settings to factory defaults and clear stored calibrations."""
        for cam in self.view.cameras:
            try:
                dev = V4L2Device(cam.dev_node)
                dev.reset_to_defaults()
            except Exception as e:
                logger.warning("Failed to reset camera %s to defaults: %s", cam.dev_node, e)

        self.view.persistence.clear_calibrations()
        self.view.results.clear()
        self.update_dashboard()
        if hasattr(self.view, "toast_overlay"):
            self.view.toast_overlay.add_toast(Adw.Toast(title="Calibrated profiles deleted — Defaults active"))
