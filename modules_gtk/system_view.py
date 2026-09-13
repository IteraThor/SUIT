import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk
import getpass
import subprocess
import shutil
from core.logger import get_logger
from core.system_service import SystemService
from modules_gtk.async_utils import run_async
from modules_gtk.ui_helpers import (
    copy_to_clipboard,
    generate_qr_texture,
    create_button_with_icon,
)
from modules_gtk.dialogs.debloat_dialog import DebloatReviewDialog
from modules_gtk.dialogs.tailscale_dialog import TailscaleAuthDialog
from modules_gtk.dialogs.darts_scorer_dialog import DartsScorerInstallDialog
from modules_gtk.advanced_users_view import AdvancedUsersView

__all__ = [
    "SystemView",
    "AdvancedUsersView",
    "DebloatReviewDialog",
    "TailscaleAuthDialog",
    "DartsScorerInstallDialog",
    "copy_to_clipboard",
    "generate_qr_texture",
    "create_button_with_icon",
]

class SystemView(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="System Utilities and Tweaks", tag="system")
        self.window = window
        self.user = getpass.getuser()
        self._updating = False
        self.advanced_view = None

        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_kinetic_scrolling(True)
        self.set_child(scrolled)

        clamp = Adw.Clamp(maximum_size=820, tightening_threshold=640)
        clamp.set_margin_top(12)
        clamp.set_margin_bottom(16)
        clamp.set_margin_start(12)
        clamp.set_margin_end(12)
        scrolled.set_child(clamp)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        clamp.set_child(main_box)

        # =========================================================================
        # Group 1: System Optimization
        # =========================================================================
        self.grp_system = Adw.PreferencesGroup(
            title="System Optimization",
            description="Performance profile, unattended auto-login, and display power settings."
        )
        self.btn_optimize_all = create_button_with_icon(
            "emblem-ok-symbolic",
            "Apply Recommended",
            "suggested-action compact-btn",
            height=34
        )
        self.btn_optimize_all.connect("clicked", self._on_apply_recommended_clicked)
        self.grp_system.set_header_suffix(self.btn_optimize_all)
        main_box.append(self.grp_system)

        # 1. Power Profile Status
        self.row_profile = Adw.ActionRow(
            title="Power Profile",
            subtitle="Locks CPU to maximum performance."
        )
        self.lbl_profile_status = Gtk.Label(label="Checking...")
        self.lbl_profile_status.add_css_class("dim-label")
        self.row_profile.add_suffix(self.lbl_profile_status)
        self.grp_system.add(self.row_profile)

        # 2. Automatic Login Switch
        self.row_autologin = Adw.SwitchRow(
            title="Automatic Login",
            subtitle="Bypass password prompt when the system turns on."
        )
        self.row_autologin.connect("notify::active", self._on_autologin_toggled)
        self.grp_system.add(self.row_autologin)

        # 3. Screen Blanking & Sleep Switch
        self.row_blanking = Adw.SwitchRow(
            title="Display Always On",
            subtitle="Keep screen lit continuously without sleeping."
        )
        self.row_blanking.connect("notify::active", self._on_blanking_toggled)
        self.grp_system.add(self.row_blanking)

        # 4. GNOME Keyring Password Status
        self.row_keyring = Adw.ActionRow(
            title="Keyring Password",
            subtitle="Allow background apps to unlock without password popups."
        )
        self.box_keyring = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.box_keyring.set_valign(Gtk.Align.CENTER)
        self.lbl_keyring_badge = Gtk.Label(label="Checking...")
        self.lbl_keyring_badge.add_css_class("status-pill-checking")
        self.btn_keyring_action = create_button_with_icon(
            "dialog-password-symbolic",
            "Remove Password",
            "suggested-action compact-btn",
            height=38
        )
        self.btn_keyring_action.connect("clicked", self._on_keyring_action_clicked)
        self.box_keyring.append(self.lbl_keyring_badge)
        self.row_keyring.add_suffix(self.box_keyring)
        self.grp_system.add(self.row_keyring)

        # 5. Appearance and Theme
        self.row_visuals = Adw.ActionRow(
            title="Appearance and Theme",
            subtitle="Dark theme, clean wallpaper, app grid clean up."
        )
        self.box_visuals = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.box_visuals.set_valign(Gtk.Align.CENTER)
        self.lbl_visuals_badge = Gtk.Label(label="Checking...")
        self.lbl_visuals_badge.add_css_class("status-pill-checking")
        self.btn_visuals_action = create_button_with_icon(
            "preferences-desktop-wallpaper-symbolic",
            "Apply Theme",
            "suggested-action compact-btn",
            height=38
        )
        self.btn_visuals_action.connect("clicked", self._on_visuals_action_clicked)
        self.box_visuals.append(self.lbl_visuals_badge)
        self.row_visuals.add_suffix(self.box_visuals)
        self.grp_system.add(self.row_visuals)

        # 6. Touchscreen Tweaks Switch
        self.row_touch = Adw.SwitchRow(
            title="Touchscreen Tweaks",
            subtitle="Enlarge top bar, window controls, and on-screen keyboard."
        )
        self.row_touch.connect("notify::active", self._on_touch_scaling_toggled)
        self.grp_system.add(self.row_touch)

        # =========================================================================
        # Group 2: Apps and Maintenance
        # =========================================================================
        self.grp_apps = Adw.PreferencesGroup(
            title="Apps and Maintenance",
            description="Manage essential dartboard software and clean unused default packages."
        )
        main_box.append(self.grp_apps)

        # 1. Bloatware Removal
        self.row_debloat = Adw.ActionRow(
            title="Remove Unused Apps",
            subtitle="Remove default desktop apps like LibreOffice, Maps, and Weather."
        )
        self.box_debloat = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.box_debloat.set_valign(Gtk.Align.CENTER)
        self.lbl_debloat_badge = Gtk.Label(label="Checking...")
        self.lbl_debloat_badge.add_css_class("status-pill-checking")
        self.btn_debloat_action = create_button_with_icon(
            "user-trash-symbolic",
            "Review & Clean",
            "suggested-action compact-btn",
            height=38
        )
        self.btn_debloat_action.connect("clicked", self._on_debloat_action_clicked)
        self.box_debloat.append(self.lbl_debloat_badge)
        self.row_debloat.add_suffix(self.box_debloat)
        self.grp_apps.add(self.row_debloat)

        # 2. Chromium Browser
        self.row_chromium = Adw.ActionRow(
            title="Chromium Web Browser",
            subtitle="Required browser for Autodarts board display and kiosk."
        )
        self.box_chromium = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.box_chromium.set_valign(Gtk.Align.CENTER)
        self.lbl_chromium_badge = Gtk.Label(label="Checking...")
        self.lbl_chromium_badge.add_css_class("status-pill-checking")
        self.btn_chromium = create_button_with_icon(
            "software-update-available-symbolic",
            "Install Chromium",
            "suggested-action compact-btn",
            height=38
        )
        self.btn_chromium.connect("clicked", self._on_install_chromium_clicked)
        self.box_chromium.append(self.lbl_chromium_badge)
        self.row_chromium.add_suffix(self.box_chromium)
        self.grp_apps.add(self.row_chromium)

        # 3. Advanced Users Submenu Row
        self.row_advanced = Adw.ActionRow(
            title="Advanced Users",
            subtitle="Tailscale remote VPN and Android Darts Scorer app."
        )
        self.row_advanced.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        self.row_advanced.set_activatable(True)
        self.row_advanced.connect("activated", self._on_advanced_clicked)
        self.grp_apps.add(self.row_advanced)

        self._refreshing = False
        self._pending_refresh = False

        self.connect("map", lambda w: self.refresh())

    def refresh(self):
        if self._refreshing:
            self._pending_refresh = True
            return

        self._refreshing = True
        self._updating = True

        def worker():
            status = SystemService.get_recommended_status(self.user)
            is_blank, reason = SystemService.is_keyring_blank_or_unlocked()
            seahorse_installed = SystemService.is_seahorse_installed()
            bloat = SystemService.get_installed_bloatware()
            has_chrom = SystemService.get_software_status().get("chromium", False)
            vis_status = SystemService.get_visuals_status()
            is_touch = SystemService.is_touch_scaling_enabled()
            return {
                "status": status,
                "keyring": (is_blank, reason, seahorse_installed),
                "bloat": bloat,
                "chromium": has_chrom,
                "visuals": vis_status,
                "touch": is_touch,
            }

        def on_done(data):
            self._updating = True
            try:
                status = data["status"]
                is_blank, reason, seahorse_installed = data["keyring"]
                bloat = data["bloat"]
                is_chrom = data["chromium"]
                vis_status = data["visuals"]
                is_touch = data["touch"]

                # 1. Recommended Button
                self.btn_optimize_all.set_child(None)
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                box.set_halign(Gtk.Align.CENTER)
                box.set_valign(Gtk.Align.CENTER)

                if status["all_ok"]:
                    box.append(Gtk.Image.new_from_icon_name("emblem-ok-symbolic"))
                    box.append(Gtk.Label(label="All Recommended Applied"))
                    self.btn_optimize_all.set_child(box)
                    self.btn_optimize_all.remove_css_class("suggested-action")
                    self.btn_optimize_all.add_css_class("secondary-btn")
                else:
                    box.append(Gtk.Image.new_from_icon_name("system-run-symbolic"))
                    box.append(Gtk.Label(label="Apply Recommended"))
                    self.btn_optimize_all.set_child(box)
                    self.btn_optimize_all.remove_css_class("secondary-btn")
                    self.btn_optimize_all.add_css_class("suggested-action")

                prof_text = "Active: Performance" if status["profile_ok"] else "Active: Default/Powersave"
                self.lbl_profile_status.set_label(prof_text)
                self.row_autologin.set_active(status["autologin_ok"])
                self.row_blanking.set_active(status["power_ok"])

                # 2. Keyring Row
                self._keyring_is_blank = is_blank
                while child := self.box_keyring.get_first_child():
                    self.box_keyring.remove(child)

                if is_blank:
                    self.lbl_keyring_badge.set_label("Unlocked")
                    self.lbl_keyring_badge.remove_css_class("status-pill-checking")
                    self.lbl_keyring_badge.remove_css_class("status-pill-pending")
                    self.lbl_keyring_badge.add_css_class("status-pill-running")
                    self.box_keyring.append(self.lbl_keyring_badge)
                    self.row_keyring.set_subtitle("Unlocked on boot.")
                else:
                    self.btn_keyring_action.set_child(None)
                    kbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                    kbox.set_halign(Gtk.Align.CENTER)
                    kbox.set_valign(Gtk.Align.CENTER)
                    kbox.append(Gtk.Image.new_from_icon_name("dialog-password-symbolic"))
                    kbox.append(Gtk.Label(label="Remove Password"))
                    self.btn_keyring_action.set_child(kbox)
                    self.btn_keyring_action.remove_css_class("secondary-btn")
                    self.btn_keyring_action.add_css_class("suggested-action")
                    self.box_keyring.append(self.btn_keyring_action)
                    self.row_keyring.set_subtitle("Password required on boot.")

                # 3. Visuals Row
                while child := self.box_visuals.get_first_child():
                    self.box_visuals.remove(child)

                if vis_status.get("all_ok", False):
                    self.lbl_visuals_badge.set_label("Applied")
                    self.lbl_visuals_badge.remove_css_class("status-pill-checking")
                    self.lbl_visuals_badge.remove_css_class("status-pill-pending")
                    self.lbl_visuals_badge.add_css_class("status-pill-running")
                    self.box_visuals.append(self.lbl_visuals_badge)
                    self.row_visuals.set_subtitle("Dark theme and clean desktop active.")
                else:
                    self.btn_visuals_action.set_child(None)
                    vbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                    vbox.set_halign(Gtk.Align.CENTER)
                    vbox.set_valign(Gtk.Align.CENTER)
                    vbox.append(Gtk.Image.new_from_icon_name("preferences-desktop-wallpaper-symbolic"))
                    vbox.append(Gtk.Label(label="Apply Theme"))
                    self.btn_visuals_action.set_child(vbox)
                    self.btn_visuals_action.remove_css_class("secondary-btn")
                    self.btn_visuals_action.add_css_class("suggested-action")
                    self.box_visuals.append(self.btn_visuals_action)
                    self.row_visuals.set_subtitle("Dark theme and wallpaper need setup.")

                # 4. Touchscreen Tweaks Switch
                self.row_touch.set_active(is_touch)

                # 5. Apps: Bloatware Row
                while child := self.box_debloat.get_first_child():
                    self.box_debloat.remove(child)

                if not bloat:
                    self.lbl_debloat_badge.set_label("Clean")
                    self.lbl_debloat_badge.remove_css_class("status-pill-checking")
                    self.lbl_debloat_badge.remove_css_class("status-pill-pending")
                    self.lbl_debloat_badge.add_css_class("status-pill-running")
                    self.box_debloat.append(self.lbl_debloat_badge)
                    self.row_debloat.set_subtitle("No unused apps detected.")
                else:
                    count = len(bloat)
                    self.btn_debloat_action.set_child(None)
                    dbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                    dbox.set_halign(Gtk.Align.CENTER)
                    dbox.set_valign(Gtk.Align.CENTER)
                    dbox.append(Gtk.Image.new_from_icon_name("user-trash-symbolic"))
                    dbox.append(Gtk.Label(label=f"Review ({count})"))
                    self.btn_debloat_action.set_child(dbox)
                    self.btn_debloat_action.remove_css_class("secondary-btn")
                    self.btn_debloat_action.add_css_class("suggested-action")
                    self.box_debloat.append(self.btn_debloat_action)
                    sample = ", ".join(pkg.split("-")[0] for pkg in bloat[:3])
                    self.row_debloat.set_subtitle(f"{count} default packages installed ({sample}).")

                # 6. Apps: Chromium Row
                while child := self.box_chromium.get_first_child():
                    self.box_chromium.remove(child)

                if is_chrom:
                    self.lbl_chromium_badge.set_label("Installed")
                    self.lbl_chromium_badge.remove_css_class("status-pill-checking")
                    self.lbl_chromium_badge.remove_css_class("status-pill-pending")
                    self.lbl_chromium_badge.add_css_class("status-pill-running")
                    self.box_chromium.append(self.lbl_chromium_badge)
                    self.row_chromium.set_subtitle("Installed and ready for kiosk.")
                else:
                    self.btn_chromium.set_child(None)
                    cbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                    cbox.set_halign(Gtk.Align.CENTER)
                    cbox.set_valign(Gtk.Align.CENTER)
                    cbox.append(Gtk.Image.new_from_icon_name("software-update-available-symbolic"))
                    cbox.append(Gtk.Label(label="Install Chromium"))
                    self.btn_chromium.set_child(cbox)
                    self.btn_chromium.remove_css_class("secondary-btn")
                    self.btn_chromium.add_css_class("suggested-action")
                    self.btn_chromium.set_sensitive(True)
                    self.box_chromium.append(self.btn_chromium)
                    self.row_chromium.set_subtitle("Required for board display and kiosk.")

                self.queue_draw()
            finally:
                self._updating = False
                self._refreshing = False
                if self._pending_refresh:
                    self._pending_refresh = False
                    GLib.idle_add(self.refresh)

        def on_error(err):
            self._updating = False
            self._refreshing = False
            if self._pending_refresh:
                self._pending_refresh = False
                GLib.idle_add(self.refresh)

        run_async(worker, on_done=on_done, on_error=on_error)

    # -------------------------------------------------------------------------
    # Callbacks and Action Handlers
    # -------------------------------------------------------------------------
    def _on_apply_recommended_clicked(self, btn):
        self.window.show_toast("Applying all recommended optimizations...")
        btn.set_sensitive(False)

        def worker():
            return SystemService.apply_all_recommended(self.user)

        def on_done(res):
            btn.set_sensitive(True)
            success, msg = res
            self.window.show_toast(msg)
            self.refresh()

        run_async(worker, on_done=on_done)

    def _on_autologin_toggled(self, row, param):
        if self._updating:
            return
        active = row.get_active()
        SystemService.set_auto_login(active, self.user)
        msg = "Automatic login enabled." if active else "Automatic login disabled."
        self.window.show_toast(msg)
        self.refresh()

    def _on_blanking_toggled(self, row, param):
        if self._updating:
            return
        active = row.get_active()
        SystemService.set_screen_blanking(active)
        msg = "Display always-on enabled." if active else "Display always-on disabled."
        self.window.show_toast(msg)
        self.refresh()

    def _open_seahorse_guide(self):
        def worker():
            if not SystemService.is_seahorse_installed():
                SystemService.install_seahorse()
            SystemService.launch_seahorse()

        run_async(worker)

        dialog = Adw.AlertDialog(
            heading="Disable Keyring Password in Seahorse",
            body=(
                "Due to security restrictions, GNOME requires entering your password to unlock the keyring.\n\n"
                "To make the keyring unlock automatically on boot without prompts:\n\n"
                "1. In the opened Passwords and Keys (Seahorse) window, locate the 'Login' keyring under Passwords.\n"
                "2. Right-click on 'Login' and select 'Change Password'.\n"
                "3. Enter your current user password when prompted.\n"
                "4. Leave the new password and confirmation fields completely blank.\n"
                "5. Click Continue and confirm storing unencrypted passwords."
            )
        )
        dialog.add_response("ok", "Got It")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda d, r: self.refresh())
        dialog.present(self.window)

    def _on_keyring_action_clicked(self, btn):
        is_blank = getattr(self, "_keyring_is_blank", False)

        if is_blank:
            dialog = Adw.AlertDialog(
                heading="Reset Keyring to Blank Password?",
                body=(
                    "Your keyring is already unencrypted and unlocks automatically.\n\n"
                    "If you choose to reset it, a fresh blank keyring will be created. "
                    "Existing entries will be backed up to login.keyring.bak."
                )
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("reset", "Reset Keyring")
            dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        else:
            dialog = Adw.AlertDialog(
                heading="Remove Keyring Password?",
                body=(
                    "This automatically sets the login keyring to a blank password so it unlocks "
                    "without prompting on boot.\n\n"
                    "Existing keyring entries will be backed up to login.keyring.bak.\n\n"
                    "If you have personal saved passwords to preserve, choose 'Manual (Seahorse)' instead."
                )
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("seahorse", "Manual (Seahorse)")
            dialog.add_response("reset", "Remove Password")
            dialog.set_response_appearance("reset", Adw.ResponseAppearance.SUGGESTED)

        def on_response(d, resp):
            if resp == "reset":
                self.window.show_toast("Removing keyring password in background...")
                self.btn_keyring_action.set_sensitive(False)

                def worker():
                    return SystemService.reset_keyring_to_blank()

                def on_done(res):
                    self.btn_keyring_action.set_sensitive(True)
                    success, msg = res
                    self.window.show_toast(msg)
                    self.refresh()

                run_async(worker, on_done=on_done)
            elif resp == "seahorse":
                self._open_seahorse_guide()

        dialog.connect("response", on_response)
        dialog.present(self.window)

    def _on_debloat_action_clicked(self, btn):
        details = SystemService.get_installed_bloatware_details(include_all=True)

        def on_finished(success, msg):
            self.window.show_toast(msg)
            self.refresh()

        dialog = DebloatReviewDialog(self.window, details, on_finished)
        dialog.present()

    def _on_install_chromium_clicked(self, btn):
        self.window.show_toast("Installing Chromium in background...")
        btn.set_sensitive(False)

        def worker():
            return SystemService.install_chromium()

        def on_done(res):
            btn.set_sensitive(True)
            success, msg = res
            self.window.show_toast(msg)
            self.refresh()

        run_async(worker, on_done=on_done)

    def _on_advanced_clicked(self, row):
        if not self.advanced_view:
            self.advanced_view = AdvancedUsersView(self.window)
        self.window.nav_view.push(self.advanced_view)
        self.advanced_view.refresh()

    def _on_visuals_action_clicked(self, btn):
        self.window.show_toast("Applying desktop visuals and organizing App Grid...")
        btn.set_sensitive(False)

        def worker():
            return SystemService.apply_desktop_visuals()

        def on_done(res):
            btn.set_sensitive(True)
            success, msg = res
            self.window.show_toast(msg)
            self.refresh()

        run_async(worker, on_done=on_done)

    def _on_touch_scaling_toggled(self, row, param):
        if self._updating:
            return
        active = row.get_active()
        success, msg = SystemService.set_touch_scaling(active)
        self.window.show_toast(msg)
        self.refresh()

