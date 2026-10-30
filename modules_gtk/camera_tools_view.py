import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib
from core.logger import get_logger

from modules_gtk.ui_helpers import build_page_shell

logger = get_logger("camera_tools_view")


class CameraToolsView(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Camera Tools", tag="camera_tools")
        self.window = window

        scrolled, main_box = build_page_shell()
        self.set_child(scrolled)

        # Tools Preferences Group
        self.pref_group = Adw.PreferencesGroup()
        main_box.append(self.pref_group)

        # 1. Focus Calibration Row
        self.row_focus = Adw.ActionRow()
        self.row_focus.set_title(GLib.markup_escape_text("Focus Calibration"))
        self.row_focus.set_subtitle(GLib.markup_escape_text("Tune optical sharpness, target sweet spots, and audio tone guidance."))
        icon_focus = Gtk.Image.new_from_icon_name("camera-photo-symbolic")
        icon_focus.set_pixel_size(24)
        icon_focus.set_margin_end(10)
        icon_focus.set_valign(Gtk.Align.CENTER)
        self.row_focus.add_prefix(icon_focus)
        self.row_focus.set_activatable(True)
        self.row_focus.connect("activated", lambda *args: self._open_focus())
        chevron_focus = Gtk.Image.new_from_icon_name("go-next-symbolic")
        chevron_focus.set_opacity(0.5)
        chevron_focus.set_valign(Gtk.Align.CENTER)
        self.row_focus.add_suffix(chevron_focus)
        self.pref_group.add(self.row_focus)

        # 2. Camera Controls Row
        self.row_manual = Adw.ActionRow()
        self.row_manual.set_title(GLib.markup_escape_text("Camera Controls"))
        self.row_manual.set_subtitle(GLib.markup_escape_text("Live view and every camera setting, kept across restarts, with presets."))
        icon_manual = Gtk.Image.new_from_icon_name("preferences-system-symbolic")
        icon_manual.set_pixel_size(24)
        icon_manual.set_margin_end(10)
        icon_manual.set_valign(Gtk.Align.CENTER)
        self.row_manual.add_prefix(icon_manual)
        self.row_manual.set_activatable(True)
        self.row_manual.connect("activated", lambda *args: self._open_manual())
        chevron_manual = Gtk.Image.new_from_icon_name("go-next-symbolic")
        chevron_manual.set_opacity(0.5)
        chevron_manual.set_valign(Gtk.Align.CENTER)
        self.row_manual.add_suffix(chevron_manual)
        self.pref_group.add(self.row_manual)

        # 3. Camera Optimizer
        self.row_optimizer = Adw.ActionRow()
        self.row_optimizer.set_title(GLib.markup_escape_text("Camera Optimizer"))
        self.row_optimizer.set_subtitle(GLib.markup_escape_text("One-click exposure, gain, and JPEG quality calibration for HBV OV2710 cameras."))
        icon_optimizer = Gtk.Image.new_from_icon_name("applications-science-symbolic")
        icon_optimizer.set_pixel_size(24)
        icon_optimizer.set_margin_end(10)
        icon_optimizer.set_valign(Gtk.Align.CENTER)
        self.row_optimizer.add_prefix(icon_optimizer)
        self.row_optimizer.set_activatable(True)
        self.row_optimizer.connect("activated", lambda *args: self._open_optimizer())

        chevron_optimizer = Gtk.Image.new_from_icon_name("go-next-symbolic")
        chevron_optimizer.set_opacity(0.5)
        chevron_optimizer.set_valign(Gtk.Align.CENTER)
        self.row_optimizer.add_suffix(chevron_optimizer)
        self.pref_group.add(self.row_optimizer)
        self.row_diag = self.row_optimizer


    def _open_focus(self):
        logger.debug("Opening Focus Calibration tool from Camera Tools hub")
        if hasattr(self.window, "open_focus_page"):
            self.window.open_focus_page()

    def _open_manual(self):
        logger.debug("Opening Camera Controls from Camera Tools hub")
        if hasattr(self.window, "open_camera_controls_page"):
            self.window.open_camera_controls_page()

    def _open_optimizer(self):
        logger.debug("Opening Camera Optimizer from Camera Tools hub")
        if hasattr(self.window, "open_camera_optimizer_page"):
            self.window.open_camera_optimizer_page()
