"""Dashboard step for CamCalibrationView: camera hardware status and calibration starter."""

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
    """Manages the Dashboard overview, live sensor parameters, and calibration trigger."""

    def __init__(self, view: CamCalibrationView):
        self.view = view

    def build_page(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
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
            label="Automated multi-camera exposure, gain, and sensor calibration wizard.",
            xalign=0.0,
        )
        subtitle.set_wrap(True)
        subtitle.add_css_class("wizard-subtitle")
        header_box.append(subtitle)
        box.append(header_box)

        # 3-Camera Status Cards
        self.dash_cards_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)
        self.dash_cards_box.set_hexpand(True)
        self.dash_cards_box.set_vexpand(True)
        box.append(self.dash_cards_box)

        # Bottom Actions Bar: Reset to Defaults (Left) & Start Calibration (Right)
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        bottom_bar.set_margin_top(10)

        self.dash_reset_btn = Gtk.Button(label="Reset Cameras to Defaults")
        self.dash_reset_btn.add_css_class("destructive-action")
        self.dash_reset_btn.add_css_class("pill")
        self.dash_reset_btn.connect("clicked", self._on_reset_clicked)
        bottom_bar.append(self.dash_reset_btn)

        start_calib_btn = Gtk.Button(label="Start Calibration")
        start_calib_btn.add_css_class("suggested-action")
        start_calib_btn.add_css_class("pill")
        start_calib_btn.add_css_class("big-cta-button")
        start_calib_btn.set_hexpand(True)
        start_calib_btn.set_halign(Gtk.Align.END)
        start_calib_btn.connect("clicked", lambda _: self.view._go_to_baseline(new_session=True))
        bottom_bar.append(start_calib_btn)

        box.append(bottom_bar)

        scroll.set_child(box)
        return scroll

    def update_dashboard(self):
        """Update dashboard table with current hardware values."""
        while child := self.dash_cards_box.get_first_child():
            self.dash_cards_box.remove(child)

        presets = CameraManualService.load_presets() or {}

        for cam in self.view.cameras:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            card.add_css_class("cam-column-card")

            hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            title = Gtk.Label(label=f"Camera {cam.index}", xalign=0.0)
            title.add_css_class("heading")
            hdr.append(title)

            ctrls = CameraManualService.get_camera_controls(cam.dev_node)
            preset = presets.get(cam.dev_node, {})
            has_calib = bool(preset)

            badge = Gtk.Label()
            if has_calib:
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

            def add_row(r: int, name: str, current_val: str, is_custom: bool = False):
                nl = Gtk.Label(label=name, xalign=0.0)
                nl.add_css_class("dim-label")
                grid.attach(nl, 0, r, 1, 1)

                vl = Gtk.Label(label=current_val, xalign=1.0)
                vl.set_hexpand(True)
                if is_custom:
                    vl.add_css_class("accent")
                grid.attach(vl, 1, r, 1, 1)

            if has_calib:
                exp_auto = preset.get("exposure_auto", 1)
                if exp_auto == 1:
                    exp_abs = preset.get("exposure_absolute", preset.get("exposure_time_absolute", 156))
                    exp_str = f"{exp_abs * 0.1:.1f}ms"
                else:
                    exp_str = "Auto"
                gain_val = str(preset.get("gain", "0"))
                contrast_val = str(preset.get("contrast", "32"))
                brightness_val = str(preset.get("brightness", "0"))
                wb_auto = preset.get("wb_auto", 1)
                wb_str = "Auto" if wb_auto == 1 else f"{preset.get('wb_temperature', 4600)}K"

                add_row(0, "Exposure:", exp_str, is_custom=True)
                add_row(1, "Sensor Gain:", gain_val, is_custom=True)
                add_row(2, "Contrast:", contrast_val, is_custom=True)
                add_row(3, "Brightness:", brightness_val, is_custom=True)
                add_row(4, "White Balance:", wb_str, is_custom=True)
            else:
                add_row(0, "Exposure:", "Default", is_custom=False)
                add_row(1, "Sensor Gain:", "Default", is_custom=False)
                add_row(2, "Contrast:", "Default", is_custom=False)
                add_row(3, "Brightness:", "Default", is_custom=False)
                add_row(4, "White Balance:", "Default", is_custom=False)

            card.append(grid)
            self.dash_cards_box.append(card)

    def _on_reset_clicked(self, _btn):
        """Reset all camera hardware settings to factory defaults, clear presets, and disable boot service."""
        for cam in self.view.cameras:
            try:
                CameraManualService.reset_defaults(cam.dev_node)
            except Exception as e:
                logger.warning("Failed to reset camera %s to defaults: %s", cam.dev_node, e)

        CameraManualService.clear_presets()
        CameraManualService.disable_boot_service()
        self.view.results.clear()
        self.update_dashboard()
        if hasattr(self.view, "toast_overlay"):
            self.view.toast_overlay.add_toast(Adw.Toast(title="All cameras reset to factory defaults"))
