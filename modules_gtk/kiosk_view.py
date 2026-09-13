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
            description="Adds power, restart, and exit buttons directly into the Autodarts web interface in Chromium."
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

        # 3. Dartboard Lighting Group
        grp_light = Adw.PreferencesGroup(
            title="Dartboard Lighting",
            description="Control WLED or smart plug illumination directly from the in-game popover menu."
        )
        main_box.append(grp_light)

        # Enable switch
        self.row_light_enable = Adw.SwitchRow(
            title="Dartboard Light Button",
            subtitle="Show light toggle button inside the in-game kiosk popover."
        )
        img_light = Gtk.Image.new_from_icon_name("display-brightness-symbolic")
        img_light.set_pixel_size(24)
        self.row_light_enable.add_prefix(img_light)
        self.row_light_enable.connect("notify::active", self._on_light_enable_toggled)
        grp_light.add(self.row_light_enable)

        # Device Type combo row
        self.light_type_options = ["WLED (HTTP API)", "Smart Plug (HTTP / Tasmota / Shelly - Placeholder)"]
        self.light_type_model = Gtk.StringList.new(self.light_type_options)
        self.row_light_type = Adw.ComboRow(
            title="Device Type",
            subtitle="WLED controllers use direct HTTP JSON API (/json/state).",
            model=self.light_type_model
        )
        self.row_light_type.connect("notify::selected-item", self._on_light_type_changed)
        grp_light.add(self.row_light_type)

        # IP Entry row
        self.row_light_ip = Adw.EntryRow(title="Device IP Address")
        self.row_light_ip.connect("notify::text", self._on_light_ip_changed)
        grp_light.add(self.row_light_ip)

        # Test Connection action row
        self.row_light_test = Adw.ActionRow(
            title="Test Connection",
            subtitle="Verify network reachability and controller response."
        )
        self.btn_light_test = Gtk.Button(label="Test Connection")
        self.btn_light_test.set_valign(Gtk.Align.CENTER)
        self.btn_light_test.add_css_class("suggested-action")
        self.btn_light_test.set_size_request(140, 38)
        self.btn_light_test.connect("clicked", self._on_light_test_clicked)
        self.row_light_test.add_suffix(self.btn_light_test)
        grp_light.add(self.row_light_test)

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
            self.row_light_enable.set_active(cfg.get("light_enabled", False))
            dev_type = cfg.get("light_device_type", "wled")
            sel_type = 0 if dev_type == "wled" else 1
            self.row_light_type.set_selected(sel_type)
            self.row_light_ip.set_text(cfg.get("light_ip", ""))
            self._update_light_ui_sensitivity(cfg.get("light_enabled", False))
        finally:
            self._loading_light_config = False

    def _update_light_ui_sensitivity(self, enabled: bool):
        self.row_light_type.set_sensitive(enabled)
        self.row_light_ip.set_sensitive(enabled)
        self.row_light_test.set_sensitive(enabled)
        self.btn_light_test.set_sensitive(enabled)
        sel_idx = self.row_light_type.get_selected()
        if sel_idx == 0:
            self.row_light_type.set_subtitle("WLED controllers use direct HTTP JSON API (/json/state).")
        else:
            self.row_light_type.set_subtitle("Placeholder support for Shelly (/relay/0) and Tasmota (/cm).")

    def _save_light_settings(self):
        if getattr(self, "_loading_light_config", False):
            return
        enabled = self.row_light_enable.get_active()
        sel_idx = self.row_light_type.get_selected()
        dev_type = "wled" if sel_idx == 0 else "smart_plug"
        ip = self.row_light_ip.get_text().strip()
        LightService.save_config(enabled=enabled, device_type=dev_type, ip=ip)
        KioskService.sync_extension_files()

    def _on_light_enable_toggled(self, row, param):
        enabled = row.get_active()
        self._update_light_ui_sensitivity(enabled)
        self._save_light_settings()
        if not getattr(self, "_loading_light_config", False):
            msg = "Dartboard light button enabled." if enabled else "Dartboard light button disabled."
            self.window.show_toast(msg)

    def _on_light_type_changed(self, row, param):
        sel_idx = row.get_selected()
        if sel_idx == 0:
            row.set_subtitle("WLED controllers use direct HTTP JSON API (/json/state).")
        else:
            row.set_subtitle("Placeholder support for Shelly (/relay/0) and Tasmota (/cm).")
        self._save_light_settings()

    def _on_light_ip_changed(self, row, param):
        self._save_light_settings()

    def _on_light_test_clicked(self, btn):
        ip = self.row_light_ip.get_text().strip()
        if not ip:
            self.window.show_toast("Please enter a device IP address first.")
            return

        sel_idx = self.row_light_type.get_selected()
        dev_type = "wled" if sel_idx == 0 else "smart_plug"

        btn.set_sensitive(False)
        self.row_light_test.set_subtitle("Testing connection...")

        def worker():
            return LightService.test_connection(ip, device_type=dev_type)

        def on_done(res):
            btn.set_sensitive(True)
            ok, msg = res
            self.row_light_test.set_subtitle(msg)
            self.window.show_toast(msg)

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
