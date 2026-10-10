"""Dashboard step for CamCalibrationView: profile status, toggle, and reset."""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from core.v4l2_device import V4L2Device
from core.camera_manual_service import CameraManualService

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
            title="Load on Boot",
            subtitle="Applies saved camera settings at system startup before Autodarts launches",
        )
        self.dash_active_switch.connect("notify::active", self._on_switch_toggled)
        pref_group.add(self.dash_active_switch)

        self.dash_reset_row = Adw.ActionRow(
            title="Reset to Factory Defaults",
            subtitle="Restores all camera sensors to default auto settings and disables boot unit",
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
        """Update dashboard table with current hardware vs standard values."""
        while child := self.dash_cards_box.get_first_child():
            self.dash_cards_box.remove(child)

        boot_enabled = CameraManualService.is_boot_service_enabled()
        presets = CameraManualService.load_presets()

        self.dash_active_switch.handler_block_by_func(self._on_switch_toggled)
        self.dash_active_switch.set_sensitive(True)
        self.dash_active_switch.set_active(boot_enabled)
        if boot_enabled:
            self.dash_active_switch.set_subtitle("Saved manual settings are loaded at boot before Autodarts.")
        else:
            self.dash_active_switch.set_subtitle("Boot service is inactive. Cameras use default settings unless configured.")
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

            ctrls = CameraManualService.get_camera_controls(cam.dev_node)
            preset = presets.get(cam.dev_node, {})

            # Check if camera has manual exposure or custom settings
            exp_val = ctrls.get("auto_exposure", ctrls.get("exposure_auto", {})).get("value", 3)
            is_manual_exp = (exp_val == 1)

            badge = Gtk.Label()
            if is_manual_exp or bool(preset):
                badge.set_text("Manual / Calibrated")
                badge.set_css_classes(["status-pill-ok"])
            else:
                badge.set_text("Default Auto")
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

            def add_row(r: int, name: str, std_val: str, current_val: str, is_custom: bool = False):
                nl = Gtk.Label(label=name, xalign=0.0)
                nl.add_css_class("dim-label")
                grid.attach(nl, 0, r, 1, 1)

                vl = Gtk.Label(label=f"{std_val}  →  {current_val}", xalign=1.0)
                vl.set_hexpand(True)
                if is_custom:
                    vl.add_css_class("accent")
                grid.attach(vl, 1, r, 1, 1)

            if is_manual_exp:
                exp_time = ctrls.get("exposure_time_absolute", {}).get("value", 156)
                exp_str = f"{exp_time * 0.1:.1f}ms"
            else:
                exp_str = "Auto"

            gain_val = str(ctrls.get("gain", {}).get("value", "—"))
            contrast_val = str(ctrls.get("contrast", {}).get("value", "32"))
            brightness_val = str(ctrls.get("brightness", {}).get("value", "0"))
            
            wb_val = ctrls.get("white_balance_automatic", ctrls.get("white_balance_temperature_auto", {})).get("value", 1)
            wb_str = "Auto" if wb_val == 1 else f"{ctrls.get('white_balance_temperature', {}).get('value', 4600)}K"

            add_row(0, "Exposure:", "Auto", exp_str, is_custom=is_manual_exp)
            add_row(1, "Sensor Gain:", "0 / Auto", gain_val, is_custom=(gain_val not in ("0", "—")))
            add_row(2, "Contrast:", "32", contrast_val, is_custom=(contrast_val != "32"))
            add_row(3, "Brightness:", "0", brightness_val, is_custom=(brightness_val != "0"))
            add_row(4, "White Balance:", "Auto", wb_str, is_custom=(wb_str != "Auto"))

            card.append(grid)
            self.dash_cards_box.append(card)

    def _on_switch_toggled(self, switch, _):
        """Enable or disable camera boot service."""
        is_active = switch.get_active()
        all_nodes = [c.dev_node for c in self.view.cameras]
        if is_active and all_nodes:
            CameraManualService.enable_boot_service(all_nodes)
        else:
            CameraManualService.disable_boot_service()
        self.update_dashboard()

    def _on_reset_clicked(self, _btn):
        """Reset all camera hardware settings to factory defaults and disable boot service."""
        for cam in self.view.cameras:
            try:
                CameraManualService.reset_defaults(cam.dev_node)
            except Exception as e:
                logger.warning("Failed to reset camera %s to defaults: %s", cam.dev_node, e)

        CameraManualService.disable_boot_service()
        self.view.results.clear()
        self.update_dashboard()
        if hasattr(self.view, "toast_overlay"):
            self.view.toast_overlay.add_toast(Adw.Toast(title="All cameras reset to defaults"))
