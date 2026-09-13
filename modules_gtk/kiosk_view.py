import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib
import subprocess
from core.logger import get_logger
from core.kiosk_service import KioskService
from core.light_service import LightService
from modules_gtk.async_utils import run_async

logger = get_logger("kiosk_view")

class KioskView(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Kiosk Mode", tag="kiosk")
        self.window = window

        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_kinetic_scrolling(True)
        self.set_child(scrolled)

        clamp = Adw.Clamp(maximum_size=880, tightening_threshold=660)
        clamp.set_margin_top(16)
        clamp.set_margin_bottom(24)
        clamp.set_margin_start(16)
        clamp.set_margin_end(16)
        scrolled.set_child(clamp)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        clamp.set_child(main_box)

        # 1. Fullscreen Kiosk Group
        grp_kiosk = Adw.PreferencesGroup(
            title="Fullscreen Kiosk",
            description="Opens Autodarts in full-screen mode without desktop distractions."
        )
        main_box.append(grp_kiosk)

        # URL Entry Row with Reset Button
        self.row_url = Adw.EntryRow(title="Target URL")
        self.row_url.set_text("https://play.autodarts.com/")

        btn_reset_url = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        btn_reset_url.set_valign(Gtk.Align.CENTER)
        btn_reset_url.add_css_class("flat")
        btn_reset_url.set_tooltip_text("Reset to default URL")
        btn_reset_url.connect("clicked", lambda b: self.row_url.set_text("https://play.autodarts.com/"))
        self.row_url.add_suffix(btn_reset_url)
        grp_kiosk.add(self.row_url)

        # Autostart Toggle Switch
        self.row_autostart = Adw.SwitchRow(
            title="Autostart on Boot",
            subtitle="Launch kiosk automatically when Fedora logs in."
        )
        self.row_autostart.connect("notify::active", self._on_autostart_toggled)
        grp_kiosk.add(self.row_autostart)

        # Launch Kiosk Touch Button
        self.btn_launch = Gtk.Button(label="Launch Kiosk Session")
        self.btn_launch.add_css_class("suggested-action")
        self.btn_launch.add_css_class("touch-btn")
        self.btn_launch.set_size_request(-1, 50)
        self.btn_launch.set_margin_top(8)
        self.btn_launch.connect("clicked", self._launch_kiosk)
        grp_kiosk.add(self.btn_launch)

        # 2. Browser Integration Group
        grp_ext = Adw.PreferencesGroup(
            title="Browser Integration",
            description="Adds controls and illumination toggles directly into the Autodarts web interface in Chromium."
        )
        main_box.append(grp_ext)

        self.row_ext = Adw.ActionRow(
            title="In-Page Controls in Chromium",
            subtitle="Checking installation status..."
        )
        img_ext = Gtk.Image.new_from_icon_name("web-browser-symbolic")
        img_ext.set_pixel_size(24)
        self.row_ext.add_prefix(img_ext)

        self.lbl_ext_status = Gtk.Label(label="Not Installed")
        self.lbl_ext_status.set_valign(Gtk.Align.CENTER)
        self.row_ext.add_suffix(self.lbl_ext_status)

        self.btn_ext_toggle = Gtk.Button(label="Install to Chromium")
        self.btn_ext_toggle.set_valign(Gtk.Align.CENTER)
        self.btn_ext_toggle.set_size_request(160, 38)
        self.btn_ext_toggle.add_css_class("suggested-action")
        self.btn_ext_toggle.connect("clicked", self._on_ext_toggle_clicked)
        self.row_ext.add_suffix(self.btn_ext_toggle)
        grp_ext.add(self.row_ext)

        # Compact expander row for light controls inside Browser Integration
        self.expander_light = Adw.ExpanderRow(
            title="Dartboard Light Controls"
        )
        img_light = Gtk.Image.new_from_icon_name("display-brightness-symbolic")
        img_light.set_pixel_size(24)
        self.expander_light.add_prefix(img_light)

        self.switch_light = Gtk.Switch()
        self.switch_light.set_valign(Gtk.Align.CENTER)
        self.switch_light.connect("notify::active", self._on_light_enable_toggled)
        self.expander_light.add_suffix(self.switch_light)

        # Alias for backwards compatibility with tests and callers
        self.row_light_enable = self.switch_light

        # Single unified sub-row: Type DropDown + IP Entry + ON/OFF test buttons
        self.row_light_ip = Adw.EntryRow(title="IP Address")
        self.row_light_ip.connect("notify::text", self._on_light_ip_changed)

        self.light_type_options = [
            "WLED",
            "Tasmota",
            "Shelly"
        ]
        self.row_light_type = Gtk.DropDown.new_from_strings(self.light_type_options)
        self.row_light_type.set_valign(Gtk.Align.CENTER)
        self.row_light_type.set_margin_end(6)
        self.row_light_type.connect("notify::selected", self._on_light_type_changed)
        self.row_light_ip.add_prefix(self.row_light_type)

        box_test_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box_test_btns.set_valign(Gtk.Align.CENTER)

        self.btn_light_on = Gtk.Button(label="Turn ON")
        self.btn_light_on.add_css_class("suggested-action")
        self.btn_light_on.set_size_request(80, 36)
        self.btn_light_on.set_valign(Gtk.Align.CENTER)
        self.btn_light_on.connect("clicked", lambda b: self._test_light_power(True))
        box_test_btns.append(self.btn_light_on)

        self.btn_light_off = Gtk.Button(label="Turn OFF")
        self.btn_light_off.add_css_class("secondary-btn")
        self.btn_light_off.set_size_request(80, 36)
        self.btn_light_off.set_valign(Gtk.Align.CENTER)
        self.btn_light_off.connect("clicked", lambda b: self._test_light_power(False))
        box_test_btns.append(self.btn_light_off)

        # Aliases for backwards compatibility with tests
        self.btn_light_test = self.btn_light_on
        self.row_light_test = self.expander_light

        self.row_light_ip.add_suffix(box_test_btns)
        self.expander_light.add_row(self.row_light_ip)

        grp_ext.add(self.expander_light)

        self.connect("map", lambda w: self.refresh())

    def refresh(self):
        is_enabled = KioskService.is_autostart_enabled()
        self.row_autostart.set_active(is_enabled)

        is_ext_installed = KioskService.is_permanent_extension_installed()
        if is_ext_installed:
            self.lbl_ext_status.set_label("Active")
            self.lbl_ext_status.remove_css_class("dim-label")
            self.lbl_ext_status.add_css_class("status-pill-running")
            self.btn_ext_toggle.set_label("Remove from Chromium")
            self.btn_ext_toggle.remove_css_class("suggested-action")
            self.btn_ext_toggle.add_css_class("secondary-btn")
            self.row_ext.set_subtitle("Extension is permanently active in Chromium.")
        else:
            self.lbl_ext_status.set_label("Not Installed")
            self.lbl_ext_status.remove_css_class("status-pill-running")
            self.lbl_ext_status.add_css_class("dim-label")
            self.btn_ext_toggle.set_label("Install to Chromium")
            self.btn_ext_toggle.remove_css_class("secondary-btn")
            self.btn_ext_toggle.add_css_class("suggested-action")
            self.row_ext.set_subtitle("Load shutdown, restart, and session controls on all Chromium launches.")

        # Load light config
        cfg = LightService.read_config()
        self._loading_light_config = True
        try:
            enabled = cfg.get("light_enabled", False)
            self.row_light_enable.set_active(enabled)
            self.expander_light.set_enable_expansion(enabled)
            self.expander_light.set_expanded(enabled)
            dev_type = cfg.get("light_device_type", "wled")
            sel_type = 0
            if dev_type in ("tasmota", "smart_plug"):
                sel_type = 1
            elif dev_type == "shelly":
                sel_type = 2
            self.row_light_type.set_selected(sel_type)
            self.row_light_ip.set_text(cfg.get("light_ip", ""))
            self._update_light_ui_sensitivity(enabled)
        finally:
            self._loading_light_config = False

    def _update_light_ui_sensitivity(self, enabled: bool):
        self.row_light_type.set_sensitive(enabled)
        self.row_light_ip.set_sensitive(enabled)
        self.btn_light_on.set_sensitive(enabled)
        self.btn_light_off.set_sensitive(enabled)

    def _save_light_settings(self):
        if getattr(self, "_loading_light_config", False):
            return
        enabled = self.row_light_enable.get_active()
        sel_idx = self.row_light_type.get_selected()
        type_keys = ["wled", "tasmota", "shelly"]
        dev_type = type_keys[sel_idx] if 0 <= sel_idx < len(type_keys) else "wled"
        ip = self.row_light_ip.get_text().strip()
        LightService.save_config(enabled=enabled, device_type=dev_type, ip=ip)
        KioskService.sync_extension_files()

    def _on_light_enable_toggled(self, row, param):
        enabled = row.get_active()
        self.expander_light.set_enable_expansion(enabled)
        self.expander_light.set_expanded(enabled)
        self._update_light_ui_sensitivity(enabled)
        self._save_light_settings()
        if not getattr(self, "_loading_light_config", False):
            msg = "Dartboard light button enabled." if enabled else "Dartboard light button disabled."
            self.window.show_toast(msg)

    def _on_light_type_changed(self, row, param):
        self._save_light_settings()

    def _on_light_ip_changed(self, row, param):
        self._save_light_settings()

    def _test_light_power(self, target_on: bool):
        ip = self.row_light_ip.get_text().strip()
        if not ip:
            self.window.show_toast("Please enter a device IP address first.")
            return

        sel_idx = self.row_light_type.get_selected()
        type_keys = ["wled", "tasmota", "shelly"]
        dev_type = type_keys[sel_idx] if 0 <= sel_idx < len(type_keys) else "wled"

        self.btn_light_on.set_sensitive(False)
        self.btn_light_off.set_sensitive(False)
        action_name = "ON" if target_on else "OFF"
        self.row_light_test.set_subtitle(f"Sending power {action_name} command...")

        def worker():
            return LightService.set_power(target_on, config={"light_ip": ip, "light_device_type": dev_type, "light_port": 80})

        def on_done(res):
            self.btn_light_on.set_sensitive(True)
            self.btn_light_off.set_sensitive(True)
            ok, is_on, msg = res
            if ok:
                state_str = "ON" if is_on else "OFF"
                self.row_light_test.set_subtitle(f"Connected: Light is currently {state_str}")
                self.window.show_toast(f"Dartboard light turned {state_str}")
            else:
                self.row_light_test.set_subtitle(f"Error: {msg}")
                self.window.show_toast(f"Error: {msg}")

        run_async(worker, on_done=on_done)

    def _on_ext_toggle_clicked(self, btn):
        btn.set_sensitive(False)
        is_installed = KioskService.is_permanent_extension_installed()

        def worker():
            if is_installed:
                return KioskService.uninstall_permanent_extension()
            else:
                return KioskService.install_permanent_extension()

        def on_done(res):
            btn.set_sensitive(True)
            success, msg = res
            self.refresh()
            self.window.show_toast(msg)

        run_async(worker, on_done=on_done)

    def _get_browser_binary(self) -> str:
        return KioskService.resolve_browser()

    def _on_autostart_toggled(self, row, param):
        active = row.get_active()
        url = self.row_url.get_text().strip() or "https://play.autodarts.com/"
        browser = self._get_browser_binary()
        KioskService.set_autostart(active, url=url, browser=browser, enable_controls=True)
        msg = "Kiosk autostart enabled." if active else "Kiosk autostart disabled."
        self.window.show_toast(msg)

    def _launch_kiosk(self, btn):
        url = self.row_url.get_text().strip() or "https://play.autodarts.com/"
        browser = self._get_browser_binary()
        KioskService.ensure_native_messaging_host()
        KioskService.clean_stale_singleton_locks()

        launcher = KioskService.ensure_launcher_script()
        flags = KioskService.get_kiosk_flags(enable_controls=True)
        cmd = f"{launcher} {browser} {' '.join(flags)} '{url}'"
        logger.info(f"Launching Kiosk: {cmd}")
        subprocess.Popen(cmd, shell=True)
        self.window.show_toast("Kiosk session launched.")
