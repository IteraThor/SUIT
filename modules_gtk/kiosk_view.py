import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
import subprocess
from core.logger import get_logger
from core.kiosk_service import KioskService
from core.light_service import LightService
from core.system_service import SystemService
from modules_gtk.async_utils import run_async
from modules_gtk.camera_widgets import segmented

from modules_gtk.ui_helpers import build_page_shell, create_button_with_icon
from core.i18n import tr

logger = get_logger("kiosk_view")

DEFAULT_URL = "https://play.autodarts.com/"
LIGHT_TYPES = [("wled", "WLED"), ("tasmota", "Tasmota"), ("shelly", "Shelly")]


class KioskView(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title=tr("Kiosk Mode"), tag="kiosk")
        self.window = window
        self._loading_light_config = False
        self._light_type = "wled"

        scrolled, main_box = build_page_shell()
        self.set_child(scrolled)

        # Kiosk: where it opens and when, with Launch in the group header
        grp_kiosk = Adw.PreferencesGroup(title=tr("Kiosk"))
        self.btn_launch = create_button_with_icon(
            "media-playback-start-symbolic", "Launch", "suggested-action compact-btn", height=36)
        self.btn_launch.set_tooltip_text(tr("Open the kiosk session now"))
        self.btn_launch.connect("clicked", self._launch_kiosk)
        grp_kiosk.set_header_suffix(self.btn_launch)
        main_box.append(grp_kiosk)

        self.row_url = Adw.EntryRow(title=tr("Address"))
        self.row_url.set_text(DEFAULT_URL)
        btn_reset_url = Gtk.Button.new_from_icon_name("edit-undo-symbolic")
        btn_reset_url.set_valign(Gtk.Align.CENTER)
        btn_reset_url.add_css_class("flat")
        btn_reset_url.set_tooltip_text(tr("Reset to play.autodarts.com"))
        btn_reset_url.connect("clicked", lambda b: self.row_url.set_text(DEFAULT_URL))
        self.row_url.add_suffix(btn_reset_url)
        grp_kiosk.add(self.row_url)

        self.row_autostart = Adw.SwitchRow(title=tr("Start on Login"))
        self.row_autostart.connect("notify::active", self._on_autostart_toggled)
        grp_kiosk.add(self.row_autostart)

        # In-page controls: the Chromium extension that adds power and config buttons to Autodarts
        self.grp_ext = Adw.PreferencesGroup(title=tr("In-Page Controls"))
        main_box.append(self.grp_ext)

        self.row_chromium = Adw.ActionRow(title=tr("Chromium Required"), subtitle=tr("The in-page controls run in Chromium."))
        self.btn_install_chromium = Gtk.Button(label=tr("Install"))
        self.btn_install_chromium.set_valign(Gtk.Align.CENTER)
        self.btn_install_chromium.add_css_class("suggested-action")
        self.btn_install_chromium.add_css_class("compact-btn")
        self.btn_install_chromium.connect("clicked", self._on_install_chromium_clicked)
        self.row_chromium.add_suffix(self.btn_install_chromium)
        self.row_chromium.set_visible(False)
        self.grp_ext.add(self.row_chromium)

        self.row_ext = Adw.ActionRow(title=tr("Chromium Extension"), subtitle=tr("Checking..."))
        self.btn_ext_toggle = Gtk.Button(label=tr("Install"))
        self.btn_ext_toggle.set_valign(Gtk.Align.CENTER)
        self.btn_ext_toggle.add_css_class("compact-btn")
        self.btn_ext_toggle.connect("clicked", self._on_ext_toggle_clicked)
        self.row_ext.add_suffix(self.btn_ext_toggle)
        self.grp_ext.add(self.row_ext)

        # Dartboard light: adds a light toggle to the in-page controls
        self.grp_light = Adw.PreferencesGroup(title=tr("Dartboard Light"))
        main_box.append(self.grp_light)

        self.row_light_enable = Adw.SwitchRow(title=tr("Light Button"))
        self.row_light_enable.connect("notify::active", self._on_light_enable_toggled)
        self.grp_light.add(self.row_light_enable)

        self.row_light_device = Adw.ActionRow(title=tr("Device"))
        self.row_light_type = segmented(LIGHT_TYPES, self._light_type, self._on_light_type_changed)
        self.row_light_device.add_suffix(self.row_light_type)
        self.grp_light.add(self.row_light_device)

        self.row_light_ip = Adw.EntryRow(title=tr("IP Address"))
        self.row_light_ip.connect("notify::text", self._on_light_ip_changed)
        self.grp_light.add(self.row_light_ip)

        self.row_light_test = Adw.ActionRow(title=tr("Test"))
        box_test_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box_test_btns.set_valign(Gtk.Align.CENTER)
        self.btn_light_on = Gtk.Button(label=tr("On"))
        self.btn_light_off = Gtk.Button(label=tr("Off"))
        for btn, target in ((self.btn_light_on, True), (self.btn_light_off, False)):
            btn.add_css_class("secondary-btn")
            btn.add_css_class("compact-btn")
            btn.set_size_request(64, -1)
            btn.connect("clicked", lambda b, t=target: self._test_light_power(t))
            box_test_btns.append(btn)
        self.row_light_test.add_suffix(box_test_btns)
        self.grp_light.add(self.row_light_test)

        self.connect("map", lambda w: self.refresh())

    def refresh(self):
        self.row_autostart.set_active(KioskService.is_autostart_enabled())

        if KioskService.is_permanent_extension_installed():
            self.row_ext.set_subtitle(tr("Active in Chromium."))
            self.btn_ext_toggle.set_label(tr("Remove"))
            self.btn_ext_toggle.remove_css_class("suggested-action")
            self.btn_ext_toggle.add_css_class("secondary-btn")
        else:
            self.row_ext.set_subtitle(tr("Adds power, restart and board config to the Autodarts header."))
            self.btn_ext_toggle.set_label(tr("Install"))
            self.btn_ext_toggle.remove_css_class("secondary-btn")
            self.btn_ext_toggle.add_css_class("suggested-action")

        has_chromium = SystemService.is_app_installed("chromium")
        self.row_chromium.set_visible(not has_chromium)
        self.row_ext.set_sensitive(has_chromium)
        self.grp_light.set_sensitive(has_chromium)

        cfg = LightService.read_config()
        self._loading_light_config = True
        try:
            enabled = cfg.get("light_enabled", False)
            self.row_light_enable.set_active(enabled)
            dev_type = cfg.get("light_device_type", "wled")
            self._light_type = "tasmota" if dev_type == "smart_plug" else dev_type
            self.row_light_type.set_active_name(self._light_type)
            self.row_light_ip.set_text(cfg.get("light_ip", ""))
            self._update_light_ui_sensitivity(enabled)
        finally:
            self._loading_light_config = False

    def _on_install_chromium_clicked(self, btn):
        btn.set_sensitive(False)
        btn.set_label(tr("Installing..."))
        self.window.show_toast(tr("Installing Chromium in background..."))

        def on_done(res):
            success, msg = res
            self.window.show_toast(msg)
            btn.set_label(tr("Install"))
            btn.set_sensitive(True)
            self.refresh()

        run_async(SystemService.install_chromium, on_done=on_done)

    def _update_light_ui_sensitivity(self, enabled: bool):
        for row in (self.row_light_device, self.row_light_ip, self.row_light_test):
            row.set_sensitive(enabled)

    def _save_light_settings(self):
        if self._loading_light_config:
            return
        LightService.save_config(enabled=self.row_light_enable.get_active(), device_type=self._light_type,
                                 ip=self.row_light_ip.get_text().strip())
        KioskService.sync_extension_files()

    def _on_light_enable_toggled(self, row, param):
        enabled = row.get_active()
        self._update_light_ui_sensitivity(enabled)
        self._save_light_settings()
        if not self._loading_light_config:
            msg = tr("Dartboard light button enabled.") if enabled else tr("Dartboard light button disabled.")
            self.window.show_toast(msg)

    def _on_light_type_changed(self, dev_type):
        self._light_type = dev_type
        self._save_light_settings()

    def _on_light_ip_changed(self, row, param):
        self._save_light_settings()

    def _test_light_power(self, target_on: bool):
        ip = self.row_light_ip.get_text().strip()
        if not ip:
            self.window.show_toast(tr("Please enter a device IP address first."))
            return

        dev_type = self._light_type
        self.btn_light_on.set_sensitive(False)
        self.btn_light_off.set_sensitive(False)
        self.row_light_test.set_subtitle(tr("Sending..."))

        def worker():
            return LightService.set_power(target_on, config={"light_ip": ip, "light_device_type": dev_type, "light_port": 80})

        def on_done(res):
            self.btn_light_on.set_sensitive(True)
            self.btn_light_off.set_sensitive(True)
            ok, is_on, msg = res
            if ok:
                self.row_light_test.set_subtitle(tr("Connected. Light is on.") if is_on else tr("Connected. Light is off."))
            else:
                self.row_light_test.set_subtitle(tr("Error: {msg}").format(msg=tr(msg)))
                self.window.show_toast(tr("Error: {msg}").format(msg=tr(msg)))

        run_async(worker, on_done=on_done)

    def _on_ext_toggle_clicked(self, btn):
        btn.set_sensitive(False)
        is_installed = KioskService.is_permanent_extension_installed()

        def worker():
            if is_installed:
                return KioskService.uninstall_permanent_extension()
            return KioskService.install_permanent_extension()

        def on_done(res):
            btn.set_sensitive(True)
            success, msg = res
            self.refresh()
            self.window.show_toast(msg)

        run_async(worker, on_done=on_done)

    def _on_autostart_toggled(self, row, param):
        active = row.get_active()
        url = self.row_url.get_text().strip() or DEFAULT_URL
        KioskService.set_autostart(active, url=url, browser=KioskService.resolve_browser(), enable_controls=True)
        msg = tr("Kiosk autostart enabled.") if active else tr("Kiosk autostart disabled.")
        self.window.show_toast(msg)

    def _launch_kiosk(self, btn):
        url = self.row_url.get_text().strip() or DEFAULT_URL
        browser = KioskService.resolve_browser()
        KioskService.ensure_native_messaging_host()
        KioskService.clean_stale_singleton_locks()

        launcher = KioskService.ensure_launcher_script()
        flags = KioskService.get_kiosk_flags(enable_controls=True)
        cmd = f"{launcher} {browser} {' '.join(flags)} '{url}'"
        logger.info(f"Launching Kiosk: {cmd}")
        subprocess.Popen(cmd, shell=True)
        self.window.show_toast(tr("Kiosk session launched."))
