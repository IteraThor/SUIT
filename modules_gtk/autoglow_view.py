import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
from core.logger import get_logger
from core.autoglow_service import AutoGlowService, DEFAULT_DIR, WEB_URL
from modules_gtk.async_utils import run_async, open_browser_url
from modules_gtk.dialogs.community_dialog import DISCORD_URL

from modules_gtk.ui_helpers import build_page_shell, create_button_with_icon
from core.i18n import tr

logger = get_logger("autoglow_view")

PILL_CLASSES = ("status-pill-running", "status-pill-stopped", "status-pill-pending", "status-pill-checking")


def _set_pill(label: Gtk.Label, text: str, css: str):
    label.set_label(tr(text))
    for c in PILL_CLASSES:
        label.remove_css_class(c)
    label.add_css_class(css)


def _compact_button(label: str, css: str) -> Gtk.Button:
    btn = Gtk.Button(label=tr(label))
    btn.set_valign(Gtk.Align.CENTER)
    for c in (css, "compact-btn"):
        btn.add_css_class(c)
    return btn


class AutoGlowView(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title=tr("AutoGlow 2 Manager"), tag="autoglow")
        self.window = window
        self._status = {}
        self._busy = False
        self._loading = False
        self._update_info = None

        scrolled, main_box = build_page_shell()
        self.set_child(scrolled)

        # Beta notice
        beta_card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        beta_card.add_css_class("warning-card")
        beta_card.add_css_class("warning-card-compact")
        main_box.append(beta_card)

        beta_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        beta_icon.set_pixel_size(18)
        beta_icon.set_valign(Gtk.Align.CENTER)
        beta_card.append(beta_icon)

        beta_lbl = Gtk.Label(label=tr("AutoGlow 2 is in beta and experimental. Stay updated on Discord."), xalign=0, wrap=True)
        beta_lbl.add_css_class("warning-text")
        beta_lbl.set_hexpand(True)
        beta_lbl.set_valign(Gtk.Align.CENTER)
        beta_card.append(beta_lbl)

        btn_discord = _compact_button("Discord", "secondary-btn")
        btn_discord.set_tooltip_text(tr("Open the IteraThor Discord"))
        btn_discord.connect("clicked", lambda b: open_browser_url(self.window, DISCORD_URL))
        beta_card.append(btn_discord)

        # AutoGlow: state, web interface and service in one group
        grp_main = Adw.PreferencesGroup(title=tr("AutoGlow 2"))
        main_box.append(grp_main)

        self.row_status = Adw.ActionRow(title=tr("Status"), subtitle=tr("Checking..."))
        box_status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box_status.set_valign(Gtk.Align.CENTER)
        self.spinner = Gtk.Spinner()
        box_status.append(self.spinner)
        self.lbl_pill = Gtk.Label(label=tr("Checking..."))
        self.lbl_pill.add_css_class("status-pill-checking")
        self.lbl_pill.set_valign(Gtk.Align.CENTER)
        box_status.append(self.lbl_pill)
        self.btn_open = create_button_with_icon(
            "web-browser-symbolic", "Open", "suggested-action compact-btn", height=36)
        self.btn_open.set_valign(Gtk.Align.CENTER)
        self.btn_open.connect("clicked", lambda b: open_browser_url(self.window, WEB_URL))
        box_status.append(self.btn_open)
        self.row_status.add_suffix(box_status)
        grp_main.add(self.row_status)

        self.row_install = Adw.ActionRow(
            title=tr("Install AutoGlow 2"),
            subtitle=tr("WLED lighting driven by your live games, with the AutoGlow Bridge browser extension."))
        self.btn_install = _compact_button("Install", "suggested-action")
        self.btn_install.connect("clicked", self._on_install_clicked)
        self.row_install.add_suffix(self.btn_install)
        grp_main.add(self.row_install)

        self.row_service = Adw.ActionRow(title=tr("Service"))
        box_service = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box_service.set_valign(Gtk.Align.CENTER)
        self.btn_start_stop = _compact_button("Stop", "secondary-btn")
        self.btn_start_stop.set_size_request(80, -1)
        self.btn_start_stop.connect("clicked", self._on_start_stop_clicked)
        box_service.append(self.btn_start_stop)
        self.btn_restart = _compact_button("Restart", "secondary-btn")
        self.btn_restart.connect("clicked", self._on_restart_clicked)
        box_service.append(self.btn_restart)
        self.row_service.add_suffix(box_service)
        grp_main.add(self.row_service)

        self.row_autostart = Adw.SwitchRow(title=tr("Start on Boot"))
        self.row_autostart.connect("notify::active", self._on_autostart_toggled)
        grp_main.add(self.row_autostart)

        # Software: updates, repair and removal
        self.grp_software = Adw.PreferencesGroup(title=tr("Software"))
        main_box.append(self.grp_software)

        self.row_update = Adw.ActionRow(title=tr("Updates"), subtitle=tr("Not checked yet."))
        self.btn_update = _compact_button("Check", "secondary-btn")
        self.btn_update.connect("clicked", self._on_update_clicked)
        self.row_update.add_suffix(self.btn_update)
        self.grp_software.add(self.row_update)

        self.row_maintenance = Adw.ActionRow(title=tr("Maintenance"))
        box_maint = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box_maint.set_valign(Gtk.Align.CENTER)
        self.btn_repair = _compact_button("Repair", "secondary-btn")
        self.btn_repair.set_tooltip_text(tr("Reinstall packages, the browser extension and the service. Settings are kept."))
        self.btn_repair.connect("clicked", self._on_repair_clicked)
        box_maint.append(self.btn_repair)
        self.btn_uninstall = _compact_button("Uninstall", "secondary-btn-destructive")
        self.btn_uninstall.set_tooltip_text(tr("Remove the service and the browser extension"))
        self.btn_uninstall.connect("clicked", self._on_uninstall_clicked)
        box_maint.append(self.btn_uninstall)
        self.row_maintenance.add_suffix(box_maint)
        self.grp_software.add(self.row_maintenance)

        self.connect("map", lambda w: self.refresh(check_updates=True))

    # ----------------------------------------------------------------- state

    def refresh(self, check_updates: bool = False):
        def on_done(status):
            self._status = status
            self._apply_status()
            if check_updates and status.get("is_git") and not self._busy:
                self._check_updates(quiet=True)

        run_async(AutoGlowService.get_status, on_done=on_done)

    def _apply_status(self):
        s = self._status
        installed = s.get("installed", False)

        if not installed:
            _set_pill(self.lbl_pill, "Not Installed", "status-pill-checking")
            self.row_status.set_subtitle(
                tr("An AutoGlow 1 service was found. Installing replaces it.") if s.get("legacy")
                else tr("Installs into {path}.").format(path=self._short_path(DEFAULT_DIR)))
        elif not s.get("service_present"):
            _set_pill(self.lbl_pill, "No Service", "status-pill-pending")
        elif s.get("running"):
            _set_pill(self.lbl_pill, "Running", "status-pill-running")
        elif s.get("active_state") in ("activating", "reloading"):
            _set_pill(self.lbl_pill, "Starting", "status-pill-pending")
        else:
            _set_pill(self.lbl_pill, "Failed" if s.get("active_state") == "failed" else "Stopped", "status-pill-stopped")

        running = s.get("running", False)
        if installed:
            version = s.get("version")
            parts = [tr("Version {version}").format(version=version) if version else "", self._short_path(s["install_dir"])]
            self.row_status.set_subtitle(" · ".join(p for p in parts if p))

        self.row_install.set_visible(not installed)
        self.btn_open.set_visible(installed)
        self.btn_open.set_sensitive(running and not self._busy)
        self.btn_open.set_tooltip_text(
            tr("Open the web interface ({url})").format(url=WEB_URL) if running
            else tr("Start the AutoGlow service to open it"))
        has_service = installed and s.get("service_present", False)
        self.row_service.set_visible(has_service)
        self.row_autostart.set_visible(has_service)
        self.grp_software.set_visible(installed)
        self.row_update.set_visible(s.get("is_git", False))

        self.btn_start_stop.set_label(tr("Stop") if s.get("running") else tr("Start"))
        self.btn_restart.set_sensitive(s.get("running", False))
        self._loading = True
        try:
            self.row_autostart.set_active(s.get("enabled", False))
        finally:
            self._loading = False


    @staticmethod
    def _short_path(path) -> str:
        home = str(DEFAULT_DIR.parent)
        p = str(path)
        return "~" + p[len(home):] if p.startswith(home + "/") else p

    def _set_busy(self, busy: bool, text: str = ""):
        self._busy = busy
        for w in (self.btn_install, self.row_service, self.row_autostart, self.grp_software, self.btn_open):
            w.set_sensitive(not busy)
        if busy:
            self.spinner.start()
            _set_pill(self.lbl_pill, text, "status-pill-pending")
        else:
            self.spinner.stop()

    def _run_task(self, worker, busy_text: str, on_success=None):
        """Run a long action with the page locked, then toast its message and refresh."""
        self._set_busy(True, busy_text)

        def on_done(res):
            self._set_busy(False)
            ok, msg = res
            self.window.show_toast(msg)
            if ok and on_success:
                on_success()
            self.refresh()

        def on_error(e):
            on_done((False, tr("AutoGlow action failed: {error}").format(error=e)))

        run_async(worker, on_done=on_done, on_error=on_error)

    # ------------------------------------------------------------- actions

    def _on_install_clicked(self, btn):
        self.window.show_toast(tr("Installing AutoGlow 2. This takes a few minutes..."))
        self._run_task(AutoGlowService.install, "Installing...", on_success=self._show_browser_hint)

    def _show_browser_hint(self):
        dialog = Adw.AlertDialog(
            heading=tr("AutoGlow 2 Installed"),
            body=tr("AutoGlow gets your live games from the AutoGlow Bridge extension, which installs itself in "
                 "Chrome and Chromium.\n\nRestart the browser, play on play.autodarts.com and set up your "
                 "WLED lights in the AutoGlow web interface."))
        dialog.add_response("close", tr("Later"))
        dialog.add_response("open", tr("Open AutoGlow"))
        dialog.set_response_appearance("open", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda d, r: r == "open" and open_browser_url(self.window, WEB_URL))
        dialog.present(self.window)

    def _on_repair_clicked(self, btn):
        self._run_task(AutoGlowService.install, "Repairing...")

    def _on_start_stop_clicked(self, btn):
        running = self._status.get("running", False)

        def worker():
            ok = AutoGlowService.stop() if running else AutoGlowService.start()
            if not ok:
                return False, tr("Failed changing the AutoGlow service state.")
            return True, tr("AutoGlow stopped.") if running else tr("AutoGlow started.")

        self._run_task(worker, "Stopping..." if running else "Starting...")

    def _on_restart_clicked(self, btn):
        def worker():
            ok = AutoGlowService.restart()
            return ok, tr("AutoGlow restarted.") if ok else tr("Failed restarting AutoGlow.")

        self._run_task(worker, "Restarting...")

    def _on_autostart_toggled(self, row, param):
        if self._loading:
            return
        enabled = row.get_active()

        def on_done(ok):
            if ok:
                self.window.show_toast(tr("AutoGlow starts on boot.") if enabled else tr("AutoGlow no longer starts on boot."))
            else:
                self.window.show_toast(tr("Failed changing AutoGlow autostart."))
            self.refresh()

        run_async(lambda: AutoGlowService.set_enabled(enabled), on_done=on_done)

    def _check_updates(self, quiet: bool = False):
        self.row_update.set_subtitle(tr("Checking for updates..."))
        self.btn_update.set_sensitive(False)

        def on_done(info):
            self.btn_update.set_sensitive(True)
            self._update_info = info
            if info.get("error"):
                self.row_update.set_subtitle(tr(info["error"]))
                self._set_update_button(False)
                if not quiet:
                    self.window.show_toast(info["error"])
                return
            if info.get("has_update"):
                count = info.get("commits_behind", 1)
                latest = info.get("latest_message", "")
                text = tr("1 update available.") if count == 1 else tr("{count} updates available.").format(count=count)
                if latest:
                    text += " " + tr("Latest: {msg}").format(msg=latest)
                self.row_update.set_subtitle(text)
            else:
                self.row_update.set_subtitle(tr("AutoGlow 2 is up to date."))
                if not quiet:
                    self.window.show_toast(tr("AutoGlow 2 is up to date."))
            self._set_update_button(info.get("has_update", False))

        run_async(AutoGlowService.check_update, on_done=on_done)

    def _set_update_button(self, has_update: bool):
        self.btn_update.set_label(tr("Update") if has_update else tr("Check"))
        if has_update:
            self.btn_update.remove_css_class("secondary-btn")
            self.btn_update.add_css_class("suggested-action")
        else:
            self.btn_update.remove_css_class("suggested-action")
            self.btn_update.add_css_class("secondary-btn")

    def _on_update_clicked(self, btn):
        if not (self._update_info or {}).get("has_update"):
            self._check_updates()
            return

        def on_success():
            self._update_info = None
            self._set_update_button(False)
            self.row_update.set_subtitle(tr("AutoGlow 2 is up to date."))

        self.window.show_toast(tr("Updating AutoGlow 2. Settings, flows and profiles are kept..."))
        self._run_task(AutoGlowService.update, "Updating...", on_success=on_success)

    def _on_uninstall_clicked(self, btn):
        own_folder = self._status.get("install_dir") == DEFAULT_DIR
        body = tr("This stops AutoGlow and removes its service and the AutoGlow Bridge browser extension.")
        if own_folder:
            body += "\n\n" + tr("Delete the AutoGlow folder ({path}) as well? "
                                 "This also deletes your AutoGlow settings, flows and profiles.").format(
                path=self._short_path(DEFAULT_DIR))
        dialog = Adw.AlertDialog(heading=tr("Uninstall AutoGlow 2?"), body=body)
        dialog.add_response("cancel", tr("Cancel"))
        if own_folder:
            dialog.add_response("keep", tr("Keep Settings"))
            dialog.add_response("delete", tr("Delete Everything"))
            dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        else:
            dialog.add_response("keep", tr("Uninstall"))
            dialog.set_response_appearance("keep", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(d, response):
            if response in ("keep", "delete"):
                remove_files = response == "delete"
                self._run_task(lambda: AutoGlowService.uninstall(remove_files), "Removing...")

        dialog.connect("response", on_response)
        dialog.present(self.window)
