import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib
import getpass
from core.system_service import SystemService
from modules_gtk.async_utils import run_async
from modules_gtk.ui_helpers import (
    copy_to_clipboard,
    generate_qr_texture,
    create_button_with_icon,
    build_page_shell,
)
from modules_gtk.dialogs.debloat_dialog import DebloatReviewDialog
from modules_gtk.dialogs.tailscale_dialog import TailscaleAuthDialog
from modules_gtk.advanced_users_view import AdvancedUsersView
from modules_gtk.hw_overlay_view import HwOverlayView, summarize_hw_overlay
from modules_gtk.fast_boot_view import FastBootView, summarize_fast_boot
from core.i18n import tr

__all__ = [
    "SystemView",
    "AdvancedUsersView",
    "HwOverlayView",
    "DebloatReviewDialog",
    "TailscaleAuthDialog",
    "copy_to_clipboard",
    "generate_qr_texture",
    "create_button_with_icon",
]


def summarize_minimal_desktop(enabled: bool, needs_relogin: bool) -> str:
    """One-line state for the Minimal Desktop switch."""
    if not enabled:
        return tr("Off. Desktop dock and a trimmed top bar and quick settings.")
    if needs_relogin:
        return tr("On. Log out and back in once to apply.")
    return tr("On. Dock on the desktop, top bar and quick settings trimmed.")


class SystemView(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title=tr("System Utilities and Tweaks"), tag="system")
        self.window = window
        self.user = getpass.getuser()
        self._updating = False
        self.advanced_view = None
        self.hw_overlay_view = None
        self.fast_boot_view = None

        # Root overlay container allowing full-screen modal progress overlay
        self.root_overlay = Gtk.Overlay()
        self.set_child(self.root_overlay)

        # Two tabs so each fits one screen: Essentials (what Apply All covers) and optional Extras
        scrolled_ess, main_box = build_page_shell()
        scrolled_extra, extra_box = build_page_shell()
        self.stack = Adw.ViewStack()
        self.stack.add_titled(scrolled_ess, "essentials", tr("Essentials"))
        self.stack.add_titled(scrolled_extra, "extras", tr("Extras"))
        self.stack.set_vexpand(True)

        switcher = Adw.InlineViewSwitcher(stack=self.stack, display_mode=Adw.InlineViewSwitcherDisplayMode.LABELS)
        switcher.set_halign(Gtk.Align.CENTER)
        switcher.set_margin_top(8)

        page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page_box.append(switcher)
        page_box.append(self.stack)
        self.root_overlay.set_child(page_box)

        # Build Apply All progress overlay widget
        self.apply_overlay_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.apply_overlay_box.add_css_class("apply-all-overlay")
        self.apply_overlay_box.set_valign(Gtk.Align.FILL)
        self.apply_overlay_box.set_halign(Gtk.Align.FILL)
        self.apply_overlay_box.set_hexpand(True)
        self.apply_overlay_box.set_vexpand(True)
        self.apply_overlay_box.set_visible(False)

        # Block background clicks while overlay is active
        click_blocker = Gtk.GestureClick.new()
        click_blocker.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.apply_overlay_box.add_controller(click_blocker)

        # Centered card inside the overlay
        center_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        center_box.add_css_class("apply-all-card")
        center_box.set_valign(Gtk.Align.CENTER)
        center_box.set_halign(Gtk.Align.CENTER)
        center_box.set_size_request(420, -1)

        self.apply_spinner = Gtk.Spinner()
        self.apply_spinner.set_size_request(40, 40)
        self.apply_spinner.set_halign(Gtk.Align.CENTER)
        center_box.append(self.apply_spinner)

        self.lbl_apply_main_title = Gtk.Label(label=tr("Applying System Configurations"))
        self.lbl_apply_main_title.add_css_class("title")
        self.lbl_apply_main_title.set_halign(Gtk.Align.CENTER)
        center_box.append(self.lbl_apply_main_title)

        self.lbl_apply_step_title = Gtk.Label(label=tr("Preparing..."))
        self.lbl_apply_step_title.add_css_class("step-title")
        self.lbl_apply_step_title.set_halign(Gtk.Align.CENTER)
        center_box.append(self.lbl_apply_step_title)

        self.apply_progress_bar = Gtk.ProgressBar()
        self.apply_progress_bar.set_fraction(0.0)
        self.apply_progress_bar.set_size_request(340, -1)
        self.apply_progress_bar.set_halign(Gtk.Align.CENTER)
        center_box.append(self.apply_progress_bar)

        self.lbl_apply_detail = Gtk.Label(label=tr("Please wait..."))
        self.lbl_apply_detail.add_css_class("detail-text")
        self.lbl_apply_detail.set_halign(Gtk.Align.CENTER)
        self.lbl_apply_detail.set_wrap(True)
        self.lbl_apply_detail.set_max_width_chars(45)
        center_box.append(self.lbl_apply_detail)

        self.apply_overlay_box.append(center_box)
        self.root_overlay.add_overlay(self.apply_overlay_box)

        # =========================================================================
        # Group 1: System Optimization
        # =========================================================================
        self.grp_system = Adw.PreferencesGroup(
            title=tr("Essentials")
        )
        self.btn_optimize_all = create_button_with_icon(
            "system-run-symbolic",
            "Apply All",
            "suggested-action compact-btn",
            height=36
        )
        self.btn_optimize_all.connect("clicked", self._on_apply_all_clicked)
        self.grp_system.set_header_suffix(self.btn_optimize_all)
        main_box.append(self.grp_system)

        self.grp_extras = Adw.PreferencesGroup(title=tr("Extras"))
        extra_box.append(self.grp_extras)

        # 1. Power Profile Status
        self.row_profile = Adw.ActionRow(
            title=tr("Power Profile"),
            subtitle=tr("Locks CPU to maximum performance.")
        )
        self.lbl_profile_status = Gtk.Label(label=tr("Checking..."))
        self.lbl_profile_status.add_css_class("dim-label")
        self.row_profile.add_suffix(self.lbl_profile_status)
        self.grp_system.add(self.row_profile)

        # 2. Automatic Login Switch
        self.row_autologin = Adw.SwitchRow(
            title=tr("Automatic Login"),
            subtitle=tr("Bypass password prompt when turning on.")
        )
        self.row_autologin.connect("notify::active", self._on_autologin_toggled)
        self.grp_system.add(self.row_autologin)

        # 3. Screen Blanking & Sleep Switch
        self.row_blanking = Adw.SwitchRow(
            title=tr("Display Always On"),
            subtitle=tr("Keep screen lit continuously.")
        )
        self.row_blanking.connect("notify::active", self._on_blanking_toggled)
        self.grp_system.add(self.row_blanking)

        # 4. GNOME Keyring Password Status
        self.row_keyring = Adw.ActionRow(
            title=tr("Keyring Password"),
            subtitle=tr("Unlock apps without password popups.")
        )
        self.box_keyring = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.box_keyring.set_valign(Gtk.Align.CENTER)
        self.lbl_keyring_badge = Gtk.Label(label=tr("Checking..."))
        self.lbl_keyring_badge.add_css_class("status-pill-checking")
        self.btn_keyring_action = create_button_with_icon(
            "dialog-password-symbolic",
            "Remove Password",
            "suggested-action compact-btn",
            height=36
        )
        self.btn_keyring_action.connect("clicked", self._on_keyring_action_clicked)
        self.box_keyring.append(self.lbl_keyring_badge)
        self.row_keyring.add_suffix(self.box_keyring)
        self.grp_system.add(self.row_keyring)

        # 5. Appearance and Theme
        self.row_visuals = Adw.ActionRow(
            title=tr("Appearance and Theme"),
            subtitle=tr("Dark theme and clean wallpaper.")
        )
        self.box_visuals = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.box_visuals.set_valign(Gtk.Align.CENTER)
        self.lbl_visuals_badge = Gtk.Label(label=tr("Checking..."))
        self.lbl_visuals_badge.add_css_class("status-pill-checking")
        self.btn_visuals_action = create_button_with_icon(
            "preferences-desktop-wallpaper-symbolic",
            "Apply Theme",
            "suggested-action compact-btn",
            height=36
        )
        self.btn_visuals_action.connect("clicked", self._on_visuals_action_clicked)
        self.box_visuals.append(self.lbl_visuals_badge)
        self.row_visuals.add_suffix(self.box_visuals)
        self.grp_system.add(self.row_visuals)

        # 6. Touchscreen Tweaks Switch
        self.row_touch = Adw.SwitchRow(
            title=tr("Touchscreen Tweaks"),
            subtitle=tr("Enlarge top bar, controls, and keyboard.")
        )
        self.row_touch.connect("notify::active", self._on_touch_scaling_toggled)
        self.grp_extras.add(self.row_touch)

        # 6b. Minimal Desktop Switch (desktop dock, trimmed top bar and quick settings)
        self.row_minimal_desktop = Adw.SwitchRow(
            title=tr("Minimal Desktop"),
            subtitle=tr("Checking...")
        )
        self.row_minimal_desktop.connect("notify::active", self._on_minimal_desktop_toggled)
        lbl_experimental = Gtk.Label(label=tr("EXPERIMENTAL"))
        lbl_experimental.add_css_class("status-pill-experimental")
        lbl_experimental.set_valign(Gtk.Align.CENTER)
        self.row_minimal_desktop.add_suffix(lbl_experimental)
        self.grp_extras.add(self.row_minimal_desktop)

        # 7. Fast Boot submenu (not part of Apply All)
        self.row_fast_boot = Adw.ActionRow(
            title=tr("Fast Boot"),
            subtitle=tr("Pick boot-time tweaks one by one."),
            activatable=True
        )
        self.row_fast_boot.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        self.row_fast_boot.connect("activated", self._on_fast_boot_clicked)
        self.grp_extras.add(self.row_fast_boot)

        # 8. Hardware Overlay submenu (not part of Apply All)
        self.row_hw_overlay = Adw.ActionRow(
            title=tr("Hardware Overlay"),
            subtitle=tr("Show CPU, temperature, RAM and camera FPS on the wallpaper."),
            activatable=True
        )
        self.row_hw_overlay.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        self.row_hw_overlay.connect("activated", self._on_hw_overlay_clicked)
        self.grp_extras.add(self.row_hw_overlay)

        # =========================================================================
        # (Apps and maintenance rows live in the Essentials group above)
        # =========================================================================
        # 1. Bloatware Removal
        self.row_debloat = Adw.ActionRow(
            title=tr("Remove Unused Apps"),
            subtitle=tr("Remove default desktop apps like LibreOffice, Maps, and Weather.")
        )
        self.box_debloat = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.box_debloat.set_valign(Gtk.Align.CENTER)
        self.lbl_debloat_badge = Gtk.Label(label=tr("Checking..."))
        self.lbl_debloat_badge.add_css_class("status-pill-checking")
        self.btn_debloat_action = create_button_with_icon(
            "user-trash-symbolic",
            "Review & Clean",
            "suggested-action compact-btn",
            height=36
        )
        self.btn_debloat_action.connect("clicked", self._on_debloat_action_clicked)
        self.box_debloat.append(self.lbl_debloat_badge)
        self.row_debloat.add_suffix(self.box_debloat)
        self.grp_system.add(self.row_debloat)

        # 2. Chromium Browser
        self.row_chromium = Adw.ActionRow(
            title=tr("Chromium Web Browser"),
            subtitle=tr("Required browser for Autodarts board display and kiosk.")
        )
        self.box_chromium = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.box_chromium.set_valign(Gtk.Align.CENTER)
        self.lbl_chromium_badge = Gtk.Label(label=tr("Checking..."))
        self.lbl_chromium_badge.add_css_class("status-pill-checking")
        self.btn_chromium = create_button_with_icon(
            "software-update-available-symbolic",
            "Install Chromium",
            "suggested-action compact-btn",
            height=36
        )
        self.btn_chromium.connect("clicked", self._on_install_chromium_clicked)
        self.box_chromium.append(self.lbl_chromium_badge)
        self.row_chromium.add_suffix(self.box_chromium)
        self.grp_system.add(self.row_chromium)

        # 3. Discord (IteraThor community, AutoGlow and SUIT news)
        self.row_discord = Adw.ActionRow(
            title=tr("Discord"),
            subtitle=tr("Join the official Autodarts and IteraThor community servers for quick support and updates.")
        )
        self.box_discord = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.box_discord.set_valign(Gtk.Align.CENTER)
        self.lbl_discord_badge = Gtk.Label(label=tr("Checking..."))
        self.lbl_discord_badge.add_css_class("status-pill-checking")
        self.btn_discord = create_button_with_icon(
            "software-update-available-symbolic",
            "Install Discord",
            "suggested-action compact-btn",
            height=36
        )
        self.btn_discord.connect("clicked", self._on_install_discord_clicked)
        self.box_discord.append(self.lbl_discord_badge)
        self.row_discord.add_suffix(self.box_discord)
        self.grp_extras.add(self.row_discord)

        # 4. Advanced Users Submenu Row
        self.row_advanced = Adw.ActionRow(
            title=tr("Advanced Users"),
            subtitle=tr("Tailscale remote VPN and Android Darts Scorer app.")
        )
        self.row_advanced.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        self.row_advanced.set_activatable(True)
        self.row_advanced.connect("activated", self._on_advanced_clicked)
        self.grp_extras.add(self.row_advanced)

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
            has_discord = SystemService.is_discord_installed()
            vis_status = SystemService.get_visuals_status()
            is_touch = SystemService.is_touch_scaling_enabled()
            minimal_desktop = (SystemService.is_minimal_desktop_enabled(),
                               SystemService.minimal_desktop_needs_relogin())
            fast_boot = SystemService.get_boot_tweak_counts()
            boot_time = SystemService.get_last_boot_time()
            hw_overlay = (SystemService.is_hw_overlay_enabled(), SystemService.get_hw_overlay_config())
            return {
                "status": status,
                "keyring": (is_blank, reason, seahorse_installed),
                "bloat": bloat,
                "chromium": has_chrom,
                "discord": has_discord,
                "visuals": vis_status,
                "touch": is_touch,
                "minimal_desktop": minimal_desktop,
                "fast_boot": (fast_boot, boot_time),
                "hw_overlay": hw_overlay,
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

                # 1. Apply All Button Visibility and State
                all_configured = (
                    status["profile_ok"]
                    and status["autologin_ok"]
                    and status["power_ok"]
                    and is_blank
                    and vis_status.get("all_ok", False)
                    and (not bloat)
                    and is_chrom
                )

                if all_configured:
                    self.btn_optimize_all.set_visible(False)
                else:
                    self.btn_optimize_all.set_visible(True)
                    self.btn_optimize_all.set_child(None)
                    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                    box.set_halign(Gtk.Align.CENTER)
                    box.set_valign(Gtk.Align.CENTER)
                    box.append(Gtk.Image.new_from_icon_name("system-run-symbolic"))
                    box.append(Gtk.Label(label=tr("Apply All")))
                    self.btn_optimize_all.set_child(box)
                    self.btn_optimize_all.remove_css_class("secondary-btn")
                    self.btn_optimize_all.add_css_class("suggested-action")

                prof_text = tr("Active: Performance") if status["profile_ok"] else tr("Active: Default/Powersave")
                self.lbl_profile_status.set_label(prof_text)
                self.row_autologin.set_active(status["autologin_ok"])
                self.row_blanking.set_active(status["power_ok"])

                # 2. Keyring Row
                self._keyring_is_blank = is_blank
                while child := self.box_keyring.get_first_child():
                    self.box_keyring.remove(child)

                if is_blank:
                    self.lbl_keyring_badge.set_label(tr("Unlocked"))
                    self.lbl_keyring_badge.remove_css_class("status-pill-checking")
                    self.lbl_keyring_badge.remove_css_class("status-pill-pending")
                    self.lbl_keyring_badge.add_css_class("status-pill-running")
                    self.box_keyring.append(self.lbl_keyring_badge)
                    self.row_keyring.set_subtitle(tr("Unlocked on boot."))
                else:
                    self.btn_keyring_action.set_child(None)
                    kbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                    kbox.set_halign(Gtk.Align.CENTER)
                    kbox.set_valign(Gtk.Align.CENTER)
                    kbox.append(Gtk.Image.new_from_icon_name("dialog-password-symbolic"))
                    kbox.append(Gtk.Label(label=tr("Remove Password")))
                    self.btn_keyring_action.set_child(kbox)
                    self.btn_keyring_action.remove_css_class("secondary-btn")
                    self.btn_keyring_action.add_css_class("suggested-action")
                    self.box_keyring.append(self.btn_keyring_action)
                    self.row_keyring.set_subtitle(tr("Password required on boot."))

                # 3. Visuals Row
                while child := self.box_visuals.get_first_child():
                    self.box_visuals.remove(child)

                if vis_status.get("all_ok", False):
                    self.lbl_visuals_badge.set_label(tr("Applied"))
                    self.lbl_visuals_badge.remove_css_class("status-pill-checking")
                    self.lbl_visuals_badge.remove_css_class("status-pill-pending")
                    self.lbl_visuals_badge.add_css_class("status-pill-running")
                    self.box_visuals.append(self.lbl_visuals_badge)
                    self.row_visuals.set_subtitle(tr("Dark theme and clean desktop active."))
                else:
                    self.btn_visuals_action.set_child(None)
                    vbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                    vbox.set_halign(Gtk.Align.CENTER)
                    vbox.set_valign(Gtk.Align.CENTER)
                    vbox.append(Gtk.Image.new_from_icon_name("preferences-desktop-wallpaper-symbolic"))
                    vbox.append(Gtk.Label(label=tr("Apply Theme")))
                    self.btn_visuals_action.set_child(vbox)
                    self.btn_visuals_action.remove_css_class("secondary-btn")
                    self.btn_visuals_action.add_css_class("suggested-action")
                    self.box_visuals.append(self.btn_visuals_action)
                    self.row_visuals.set_subtitle(tr("Dark theme and wallpaper need setup."))

                # 4. Touchscreen Tweaks Switch
                self.row_touch.set_active(is_touch)

                # 4a. Minimal Desktop Switch
                self.row_minimal_desktop.set_active(data["minimal_desktop"][0])
                self.row_minimal_desktop.set_subtitle(summarize_minimal_desktop(*data["minimal_desktop"]))

                # 4b. Fast Boot submenu row
                (applied, total), boot_time = data["fast_boot"]
                self.row_fast_boot.set_subtitle(summarize_fast_boot(applied, total, boot_time))

                # 4c. Hardware Overlay submenu row
                self.row_hw_overlay.set_subtitle(summarize_hw_overlay(*data["hw_overlay"]))

                # 5. Apps: Bloatware Row
                while child := self.box_debloat.get_first_child():
                    self.box_debloat.remove(child)

                if not bloat:
                    self.lbl_debloat_badge.set_label(tr("Clean"))
                    self.lbl_debloat_badge.remove_css_class("status-pill-checking")
                    self.lbl_debloat_badge.remove_css_class("status-pill-pending")
                    self.lbl_debloat_badge.add_css_class("status-pill-running")
                    self.box_debloat.append(self.lbl_debloat_badge)
                    self.row_debloat.set_subtitle(tr("No unused apps detected."))
                else:
                    count = len(bloat)
                    self.btn_debloat_action.set_child(None)
                    dbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                    dbox.set_halign(Gtk.Align.CENTER)
                    dbox.set_valign(Gtk.Align.CENTER)
                    dbox.append(Gtk.Image.new_from_icon_name("user-trash-symbolic"))
                    dbox.append(Gtk.Label(label=tr("Review ({count})").format(count=count)))
                    self.btn_debloat_action.set_child(dbox)
                    self.btn_debloat_action.remove_css_class("secondary-btn")
                    self.btn_debloat_action.add_css_class("suggested-action")
                    self.box_debloat.append(self.btn_debloat_action)
                    sample = ", ".join(pkg.split("-")[0] for pkg in bloat[:3])
                    self.row_debloat.set_subtitle(tr("{count} default packages installed ({sample}).").format(count=count, sample=sample))

                # 6. Apps: Chromium Row
                while child := self.box_chromium.get_first_child():
                    self.box_chromium.remove(child)

                if is_chrom:
                    self.lbl_chromium_badge.set_label(tr("Installed"))
                    self.lbl_chromium_badge.remove_css_class("status-pill-checking")
                    self.lbl_chromium_badge.remove_css_class("status-pill-pending")
                    self.lbl_chromium_badge.add_css_class("status-pill-running")
                    self.box_chromium.append(self.lbl_chromium_badge)
                    self.row_chromium.set_subtitle(tr("Installed and ready for kiosk."))
                else:
                    self.btn_chromium.set_child(None)
                    cbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                    cbox.set_halign(Gtk.Align.CENTER)
                    cbox.set_valign(Gtk.Align.CENTER)
                    cbox.append(Gtk.Image.new_from_icon_name("software-update-available-symbolic"))
                    cbox.append(Gtk.Label(label=tr("Install Chromium")))
                    self.btn_chromium.set_child(cbox)
                    self.btn_chromium.remove_css_class("secondary-btn")
                    self.btn_chromium.add_css_class("suggested-action")
                    self.btn_chromium.set_sensitive(True)
                    self.box_chromium.append(self.btn_chromium)
                    self.row_chromium.set_subtitle(tr("Required for board display and kiosk."))

                # 7. Extras: Discord Row
                while child := self.box_discord.get_first_child():
                    self.box_discord.remove(child)

                if data["discord"]:
                    self.lbl_discord_badge.set_label(tr("Installed"))
                    self.lbl_discord_badge.remove_css_class("status-pill-checking")
                    self.lbl_discord_badge.add_css_class("status-pill-running")
                    self.box_discord.append(self.lbl_discord_badge)
                else:
                    self.btn_discord.set_sensitive(True)
                    self.box_discord.append(self.btn_discord)

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
    def _on_apply_all_clicked(self, btn):
        dialog = Adw.AlertDialog(
            heading=tr("Use Touchscreen Tweaks?"),
            body=tr(
                "Apply All will configure maximum performance, automatic login, display always-on, "
                "keyring unlock, dark theme, remove unused default apps, and install Chromium.\n\n"
                "Will you be using a touchscreen display on this system?"
            )
        )
        dialog.add_response("cancel", tr("Cancel"))
        dialog.add_response("no", tr("No, Standard Display"))
        dialog.add_response("yes", tr("Yes, Touchscreen"))
        dialog.set_response_appearance("yes", Adw.ResponseAppearance.SUGGESTED)

        def on_response(d, resp):
            if resp not in ("yes", "no"):
                return
            include_touch = (resp == "yes")
            self._start_apply_all(include_touch)

        dialog.connect("response", on_response)
        dialog.present(self.window)

    def _start_apply_all(self, include_touch: bool):
        # Freeze UI & show overlay
        self.btn_optimize_all.set_sensitive(False)
        if hasattr(self.window, "btn_back"):
            self.window.btn_back.set_sensitive(False)

        self.lbl_apply_step_title.set_label(tr("Initializing..."))
        self.lbl_apply_detail.set_label(tr("Starting system optimization queue..."))
        self.apply_progress_bar.set_fraction(0.0)
        self.apply_spinner.start()
        self.apply_overlay_box.set_visible(True)

        def step_cb(cur, tot, title, detail):
            def update_ui():
                frac = (cur / tot) if tot > 0 else 0.0
                self.lbl_apply_step_title.set_label(tr("Step {cur} of {tot}: {title}").format(cur=cur, tot=tot, title=tr(title)))
                self.lbl_apply_detail.set_label(tr(detail))
                self.apply_progress_bar.set_fraction(min(1.0, max(0.0, frac)))
                return False
            GLib.idle_add(update_ui)

        def worker():
            return SystemService.apply_all_system_and_apps(
                self.user,
                include_touch=include_touch,
                step_callback=step_cb
            )

        def on_done(res):
            self.apply_spinner.stop()
            self.apply_overlay_box.set_visible(False)
            self.btn_optimize_all.set_sensitive(True)
            if hasattr(self.window, "btn_back"):
                self.window.btn_back.set_sensitive(True)

            success, summary_msg, _ = res
            self.window.show_toast(summary_msg)
            self.refresh()

        def on_error(err):
            self.apply_spinner.stop()
            self.apply_overlay_box.set_visible(False)
            self.btn_optimize_all.set_sensitive(True)
            if hasattr(self.window, "btn_back"):
                self.window.btn_back.set_sensitive(True)
            self.window.show_toast(tr("Error during Apply All: {err}").format(err=err))
            self.refresh()

        run_async(worker, on_done=on_done, on_error=on_error)

    def _on_autologin_toggled(self, row, param):
        if self._updating:
            return
        active = row.get_active()
        SystemService.set_auto_login(active, self.user)
        msg = tr("Automatic login enabled.") if active else tr("Automatic login disabled.")
        self.window.show_toast(msg)
        self.refresh()

    def _on_blanking_toggled(self, row, param):
        if self._updating:
            return
        active = row.get_active()
        SystemService.set_screen_blanking(active)
        msg = tr("Display always-on enabled.") if active else tr("Display always-on disabled.")
        self.window.show_toast(msg)
        self.refresh()

    def _open_seahorse_guide(self):
        def worker():
            if not SystemService.is_seahorse_installed():
                SystemService.install_seahorse()
            SystemService.launch_seahorse()

        run_async(worker)

        dialog = Adw.AlertDialog(
            heading=tr("Disable Keyring Password in Seahorse"),
            body=tr(
                "Due to security restrictions, GNOME requires entering your password to unlock the keyring.\n\n"
                "To make the keyring unlock automatically on boot without prompts:\n\n"
                "1. In the opened Passwords and Keys (Seahorse) window, locate the 'Login' keyring under Passwords.\n"
                "2. Right-click on 'Login' and select 'Change Password'.\n"
                "3. Enter your current user password when prompted.\n"
                "4. Leave the new password and confirmation fields completely blank.\n"
                "5. Click Continue and confirm storing unencrypted passwords."
            )
        )
        dialog.add_response("ok", tr("Got It"))
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda d, r: self.refresh())
        dialog.present(self.window)

    def _on_keyring_action_clicked(self, btn):
        is_blank = getattr(self, "_keyring_is_blank", False)

        if is_blank:
            dialog = Adw.AlertDialog(
                heading=tr("Reset Keyring to Blank Password?"),
                body=tr(
                    "Your keyring is already unencrypted and unlocks automatically.\n\n"
                    "If you choose to reset it, a fresh blank keyring will be created. "
                    "Existing entries will be backed up to login.keyring.bak."
                )
            )
            dialog.add_response("cancel", tr("Cancel"))
            dialog.add_response("reset", tr("Reset Keyring"))
            dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        else:
            dialog = Adw.AlertDialog(
                heading=tr("Remove Keyring Password?"),
                body=tr(
                    "This automatically sets the login keyring to a blank password so it unlocks "
                    "without prompting on boot.\n\n"
                    "Existing keyring entries will be backed up to login.keyring.bak.\n\n"
                    "If you have personal saved passwords to preserve, choose 'Manual (Seahorse)' instead."
                )
            )
            dialog.add_response("cancel", tr("Cancel"))
            dialog.add_response("seahorse", tr("Manual (Seahorse)"))
            dialog.add_response("reset", tr("Remove Password"))
            dialog.set_response_appearance("reset", Adw.ResponseAppearance.SUGGESTED)

        def on_response(d, resp):
            if resp == "reset":
                self.window.show_toast(tr("Removing keyring password in background..."))
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
        self.window.show_toast(tr("Installing Chromium in background..."))
        btn.set_sensitive(False)

        def worker():
            return SystemService.install_chromium()

        def on_done(res):
            btn.set_sensitive(True)
            success, msg = res
            self.window.show_toast(msg)
            self.refresh()

        run_async(worker, on_done=on_done)

    def _on_install_discord_clicked(self, btn):
        self.window.show_toast(tr("Installing Discord in background..."))
        btn.set_sensitive(False)

        def on_done(res):
            btn.set_sensitive(True)
            success, msg = res
            self.window.show_toast(msg)
            self.refresh()

        run_async(SystemService.install_discord, on_done=on_done)

    def _on_advanced_clicked(self, row):
        if not self.advanced_view:
            self.advanced_view = AdvancedUsersView(self.window)
        self.window.nav_view.push(self.advanced_view)
        self.advanced_view.refresh()

    def _on_visuals_action_clicked(self, btn):
        self.window.show_toast(tr("Applying desktop visuals and organizing App Grid..."))
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

    def _on_minimal_desktop_toggled(self, row, param):
        if self._updating:
            return
        success, msg = SystemService.set_minimal_desktop(row.get_active())
        self.window.show_toast(msg)
        self.refresh()

    def _on_hw_overlay_clicked(self, row):
        if not self.hw_overlay_view:
            self.hw_overlay_view = HwOverlayView(self.window, on_changed=self.refresh)
        self.window.nav_view.push(self.hw_overlay_view)
        self.hw_overlay_view.refresh()

    def _on_fast_boot_clicked(self, row):
        if not self.fast_boot_view:
            self.fast_boot_view = FastBootView(self.window, on_changed=self.refresh)
        self.window.nav_view.push(self.fast_boot_view)
        self.fast_boot_view.refresh()
