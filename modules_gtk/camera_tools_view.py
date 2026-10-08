import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib
from core.logger import get_logger

logger = get_logger("camera_tools_view")


class CameraToolsView(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Camera Tools", tag="camera_tools")
        self.window = window

        # Scrolled container with kinetic touch scrolling
        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_kinetic_scrolling(True)
        scrolled.set_overlay_scrolling(True)
        self.set_child(scrolled)

        # Responsive clamp
        clamp = Adw.Clamp(maximum_size=880, tightening_threshold=660)
        clamp.set_margin_top(16)
        clamp.set_margin_bottom(24)
        clamp.set_margin_start(16)
        clamp.set_margin_end(16)
        scrolled.set_child(clamp)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        clamp.set_child(main_box)

        # Header description card
        header_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        header_card.add_css_class("card")
        header_card.set_margin_bottom(8)

        inner_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        inner_box.set_margin_top(16)
        inner_box.set_margin_bottom(16)
        inner_box.set_margin_start(20)
        inner_box.set_margin_end(20)

        icon_hub = Gtk.Image.new_from_icon_name("camera-photo-symbolic")
        icon_hub.set_pixel_size(48)
        icon_hub.set_valign(Gtk.Align.CENTER)
        inner_box.append(icon_hub)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_valign(Gtk.Align.CENTER)
        text_box.set_hexpand(True)

        lbl_title = Gtk.Label(label="Camera Tools", xalign=0)
        lbl_title.add_css_class("title-2")
        text_box.append(lbl_title)

        lbl_desc = Gtk.Label(label="Optical calibration, sensor exposure, and diagnostic utilities.", xalign=0)
        lbl_desc.add_css_class("dim-label")
        text_box.append(lbl_desc)

        inner_box.append(text_box)
        header_card.append(inner_box)
        main_box.append(header_card)

        # Tools Preferences Group
        self.pref_group = Adw.PreferencesGroup()
        main_box.append(self.pref_group)

        # 1. Focus Calibration Row
        self.row_focus = Adw.ActionRow()
        self.row_focus.set_title(GLib.markup_escape_text("Focus Calibration"))
        self.row_focus.set_subtitle(GLib.markup_escape_text("Tune optical sharpness, target sweet spots, and audio tone guidance."))
        icon_focus = Gtk.Image.new_from_icon_name("camera-photo-symbolic")
        icon_focus.set_pixel_size(28)
        icon_focus.set_margin_end(12)
        icon_focus.set_valign(Gtk.Align.CENTER)
        self.row_focus.add_prefix(icon_focus)
        self.row_focus.set_activatable(True)
        self.row_focus.connect("activated", lambda *args: self._open_focus())
        chevron_focus = Gtk.Image.new_from_icon_name("go-next-symbolic")
        chevron_focus.set_opacity(0.5)
        chevron_focus.set_valign(Gtk.Align.CENTER)
        self.row_focus.add_suffix(chevron_focus)
        self.pref_group.add(self.row_focus)

        # 2. Manual Controls Row
        self.row_manual = Adw.ActionRow()
        self.row_manual.set_title(GLib.markup_escape_text("Manual Controls"))
        self.row_manual.set_subtitle(GLib.markup_escape_text("Adjust exposure, gain, brightness, and sensor parameters."))
        icon_manual = Gtk.Image.new_from_icon_name("preferences-system-symbolic")
        icon_manual.set_pixel_size(28)
        icon_manual.set_margin_end(12)
        icon_manual.set_valign(Gtk.Align.CENTER)
        self.row_manual.add_prefix(icon_manual)
        self.row_manual.set_activatable(True)
        self.row_manual.connect("activated", lambda *args: self._open_manual())
        chevron_manual = Gtk.Image.new_from_icon_name("go-next-symbolic")
        chevron_manual.set_opacity(0.5)
        chevron_manual.set_valign(Gtk.Align.CENTER)
        self.row_manual.add_suffix(chevron_manual)
        self.pref_group.add(self.row_manual)

        # 3. Future Placeholder: Diagnostics & Alignment
        self.row_diag = Adw.ActionRow()
        self.row_diag.set_title(GLib.markup_escape_text("Camera Alignment & Diagnostics"))
        self.row_diag.set_subtitle(GLib.markup_escape_text("Board triangulation and sensor alignment diagnostics."))
        icon_diag = Gtk.Image.new_from_icon_name("applications-science-symbolic")
        icon_diag.set_pixel_size(28)
        icon_diag.set_margin_end(12)
        icon_diag.set_valign(Gtk.Align.CENTER)
        self.row_diag.add_prefix(icon_diag)
        self.row_diag.set_activatable(False)
        self.row_diag.set_opacity(0.55)
        lbl_badge = Gtk.Label(label="Coming Soon")
        lbl_badge.add_css_class("status-pill-coming-soon")
        lbl_badge.set_valign(Gtk.Align.CENTER)
        self.row_diag.add_suffix(lbl_badge)
        self.pref_group.add(self.row_diag)

    def _open_focus(self):
        logger.debug("Opening Focus Calibration tool from Camera Tools hub")
        if hasattr(self.window, "open_focus_page"):
            self.window.open_focus_page()

    def _open_manual(self):
        logger.debug("Opening Manual Controls tool from Camera Tools hub")
        if hasattr(self.window, "open_camera_manual_page"):
            self.window.open_camera_manual_page()
