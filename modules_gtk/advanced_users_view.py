import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
import subprocess
from core.system_service import SystemService
from modules_gtk.async_utils import run_async
from modules_gtk.ui_helpers import create_button_with_icon, copy_to_clipboard
from modules_gtk.dialogs.tailscale_dialog import TailscaleAuthDialog
from modules_gtk.dialogs.darts_scorer_dialog import DartsScorerInstallDialog

__all__ = ["AdvancedUsersView"]


class AdvancedUsersView(Adw.NavigationPage):
    """Submenu for advanced utilities: Desktop Sharing, Tailscale VPN, and Darts Scorer via Waydroid."""

    def __init__(self, window):
        super().__init__(title="Advanced Users", tag="advanced_users")
        self.window = window
        self._updating = False
        self._updating_rdp = False
        self._current_rdp_info = ""

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
        # Group 1: Desktop Sharing & Remote Control
        # =========================================================================
        self.grp_remote = Adw.PreferencesGroup(
            title="Desktop Sharing",
            description="Share desktop screen and enable remote control via RDP."
        )
        main_box.append(self.grp_remote)

        self.row_sharing = Adw.SwitchRow(
            title="Desktop Sharing",
            subtitle="Allow connecting to this display from another device."
        )
        self.row_sharing.connect("notify::active", self._on_sharing_toggled)
        self.grp_remote.add(self.row_sharing)

        self.row_control = Adw.SwitchRow(
            title="Remote Control",
            subtitle="Allow remote keyboard, mouse, and touch control."
        )
        self.row_control.connect("notify::active", self._on_control_toggled)
        self.grp_remote.add(self.row_control)

        self.row_rdp_info = Adw.ActionRow(
            title="Connection Details",
            subtitle="Checking connection details..."
        )
        self.box_rdp_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.box_rdp_actions.set_valign(Gtk.Align.CENTER)

        self.btn_copy_rdp = create_button_with_icon("edit-copy-symbolic", "Copy Info", "secondary-btn compact-btn", height=38)
        self.btn_copy_rdp.connect("clicked", self._on_copy_rdp_clicked)
        self.box_rdp_actions.append(self.btn_copy_rdp)

        self.btn_gnome_settings = create_button_with_icon("preferences-system-symbolic", "Settings", "secondary-btn compact-btn", height=38)
        self.btn_gnome_settings.connect("clicked", lambda b: SystemService.open_gnome_sharing_settings())
        self.box_rdp_actions.append(self.btn_gnome_settings)

        self.row_rdp_info.add_suffix(self.box_rdp_actions)
        self.grp_remote.add(self.row_rdp_info)

        # =========================================================================
        # Group 2: Remote Access (Tailscale VPN)
        # =========================================================================
        self.grp_tailscale = Adw.PreferencesGroup(
            title="Tailscale VPN",
            description="Secure mesh VPN for connecting to your dartboard from anywhere."
        )
        main_box.append(self.grp_tailscale)

        self.row_tailscale = Adw.ActionRow(
            title="Tailscale VPN",
            subtitle="Remote network access for board management away from home."
        )
        self.box_tailscale = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.box_tailscale.set_valign(Gtk.Align.CENTER)
        self.lbl_tailscale_badge = Gtk.Label(label="Checking...")
        self.lbl_tailscale_badge.add_css_class("status-pill-checking")
        self.btn_tailscale = create_button_with_icon(
            "network-vpn-symbolic",
            "Install Tailscale",
            "suggested-action compact-btn",
            height=38
        )
        self.btn_tailscale.connect("clicked", self._on_tailscale_clicked)
        self.box_tailscale.append(self.lbl_tailscale_badge)
        self.row_tailscale.add_suffix(self.box_tailscale)
        self.grp_tailscale.add(self.row_tailscale)

        # =========================================================================
        # Group 3: Android Darts Scorer (Waydroid)
        # =========================================================================
        self.grp_waydroid = Adw.PreferencesGroup(
            title="Darts Scorer",
            description="Android companion app for live scores, stats, and scoreboard."
        )
        main_box.append(self.grp_waydroid)

        # Primary Darts Scorer Row
        self.row_scorer = Adw.ActionRow(
            title="Darts Scorer App",
            subtitle="Android scoreboard app for local score tracking."
        )
        self.box_scorer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.box_scorer.set_valign(Gtk.Align.CENTER)
        self.lbl_scorer_badge = Gtk.Label(label="Checking...")
        self.lbl_scorer_badge.add_css_class("status-pill-checking")
        self.btn_scorer_action = create_button_with_icon(
            "media-playback-start-symbolic",
            "Launch Scorer",
            "secondary-btn compact-btn",
            height=38
        )
        self.btn_scorer_action.connect("clicked", self._on_scorer_action_clicked)
        self.box_scorer.append(self.lbl_scorer_badge)
        self.row_scorer.add_suffix(self.box_scorer)
        self.grp_waydroid.add(self.row_scorer)

        # Expander for advanced sub-tools
        self.expander_waydroid = Adw.ExpanderRow(
            title="Advanced Waydroid Options",
            subtitle="Diagnostics, debloat, and Android container settings."
        )
        self.grp_waydroid.add(self.expander_waydroid)

        # Sub-row 1: Waydroid Debloat
        self.row_sub_debloat = Adw.ActionRow(
            title="Waydroid Debloat",
            subtitle="Clean unused LineageOS apps to save memory and CPU."
        )
        btn_sub_debloat = create_button_with_icon("view-refresh-symbolic", "Run Debloat", "secondary-btn compact-btn", height=38)
        btn_sub_debloat.connect("clicked", self._on_sub_debloat_clicked)
        self.row_sub_debloat.add_suffix(btn_sub_debloat)
        self.expander_waydroid.add_row(self.row_sub_debloat)

        # Sub-row 2: Android Settings
        self.row_sub_settings = Adw.ActionRow(
            title="Android Settings",
            subtitle="Open native Android container settings."
        )
        btn_sub_settings = create_button_with_icon("emblem-system-symbolic", "Open Settings", "secondary-btn compact-btn", height=38)
        btn_sub_settings.connect("clicked", self._on_sub_settings_clicked)
        self.row_sub_settings.add_suffix(btn_sub_settings)
        self.expander_waydroid.add_row(self.row_sub_settings)

        # Sub-row 3: Aurora Store
        self.row_sub_aurora = Adw.ActionRow(
            title="Aurora Store",
            subtitle="Download and update Android apps without a Google account."
        )
        btn_sub_aurora = create_button_with_icon("software-update-available-symbolic", "Open Store", "secondary-btn compact-btn", height=38)
        btn_sub_aurora.connect("clicked", self._on_sub_aurora_clicked)
        self.row_sub_aurora.add_suffix(btn_sub_aurora)
        self.expander_waydroid.add_row(self.row_sub_aurora)

        self.connect("map", lambda w: self.refresh())

    def refresh(self):
        # 1. Remote Desktop Status Refresh
        def worker_rdp():
            return SystemService.get_remote_desktop_status()

        def on_done_rdp(rdp):
            self._updating_rdp = True
            self.row_sharing.set_active(rdp["enabled"])
            self.row_control.set_active(rdp["remote_control"])
            self.row_control.set_sensitive(rdp["enabled"])

            if rdp["enabled"]:
                ip = rdp.get("ip") or rdp.get("hostname")
                port = rdp.get("port") or "3389"
                user = rdp.get("username") or "autodarts"
                pw = rdp.get("password") or "autodarts"
                self.row_rdp_info.set_visible(True)
                self.row_rdp_info.set_subtitle(f"Connect to {ip}:{port} (User: {user} / Pass: {pw})")
                self._current_rdp_info = f"Host: {ip}:{port}\nUsername: {user}\nPassword: {pw}"
            else:
                self.row_rdp_info.set_visible(False)
                self._current_rdp_info = ""

            self._updating_rdp = False

        run_async(worker_rdp, on_done=on_done_rdp)

        # 2. Tailscale Status Refresh
        def worker_tailscale():
            return SystemService.get_tailscale_status()

        def on_done_tailscale(tail_info):
            is_tail = tail_info.get("installed", False)
            is_running = tail_info.get("running", False)
            tail_state = tail_info.get("state", "Unknown")
            tail_ips = tail_info.get("ips", [])
            tail_host = tail_info.get("hostname", "")

            while child := self.box_tailscale.get_first_child():
                self.box_tailscale.remove(child)

            tbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            tbox.set_halign(Gtk.Align.CENTER)
            tbox.set_valign(Gtk.Align.CENTER)

            if not is_tail:
                self.row_tailscale.set_subtitle("Remote network access for board management away from home.")
                tbox.append(Gtk.Image.new_from_icon_name("software-update-available-symbolic"))
                tbox.append(Gtk.Label(label="Install Tailscale"))
                self.btn_tailscale.set_child(tbox)
                self.btn_tailscale.remove_css_class("secondary-btn")
                self.btn_tailscale.add_css_class("suggested-action")
                self.btn_tailscale.set_sensitive(True)
                self.box_tailscale.append(self.btn_tailscale)
            elif is_running and tail_ips:
                ip_str = tail_ips[0]
                self.lbl_tailscale_badge.set_label(f"Connected: {ip_str}")
                self.lbl_tailscale_badge.remove_css_class("status-pill-checking")
                self.lbl_tailscale_badge.remove_css_class("status-pill-pending")
                self.lbl_tailscale_badge.remove_css_class("status-pill-stopped")
                self.lbl_tailscale_badge.add_css_class("status-pill-running")
                self.row_tailscale.set_subtitle(f"Connected as {tail_host}.")
                self.box_tailscale.append(self.lbl_tailscale_badge)
            elif tail_state == "Stopped":
                self.row_tailscale.set_subtitle("Installed but disconnected.")
                tbox.append(Gtk.Image.new_from_icon_name("network-vpn-symbolic"))
                tbox.append(Gtk.Label(label="Connect"))
                self.btn_tailscale.set_child(tbox)
                self.btn_tailscale.remove_css_class("secondary-btn")
                self.btn_tailscale.add_css_class("suggested-action")
                self.btn_tailscale.set_sensitive(True)
                self.box_tailscale.append(self.btn_tailscale)
            else:
                self.row_tailscale.set_subtitle("Authentication required to connect.")
                tbox.append(Gtk.Image.new_from_icon_name("network-vpn-symbolic"))
                tbox.append(Gtk.Label(label="Sign In"))
                self.btn_tailscale.set_child(tbox)
                self.btn_tailscale.remove_css_class("secondary-btn")
                self.btn_tailscale.add_css_class("suggested-action")
                self.btn_tailscale.set_sensitive(True)
                self.box_tailscale.append(self.btn_tailscale)

        run_async(worker_tailscale, on_done=on_done_tailscale)

        # 3. Waydroid / Darts Scorer Status Refresh
        def worker_waydroid():
            return SystemService.get_waydroid_status()

        def on_done_waydroid(w_status):
            while child := self.box_scorer.get_first_child():
                self.box_scorer.remove(child)

            sbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            sbox.set_halign(Gtk.Align.CENTER)
            sbox.set_valign(Gtk.Align.CENTER)

            if w_status.get("darts_scorer"):
                self.row_scorer.set_subtitle("Android scoreboard app running via Waydroid.")
                sbox.append(Gtk.Image.new_from_icon_name("media-playback-start-symbolic"))
                sbox.append(Gtk.Label(label="Launch Scorer"))
                self.btn_scorer_action.set_child(sbox)
                self.btn_scorer_action.remove_css_class("suggested-action")
                self.btn_scorer_action.add_css_class("secondary-btn")
                self.btn_scorer_action.set_sensitive(True)
                self.btn_scorer_action._is_installed = True
                self.box_scorer.append(self.btn_scorer_action)
            else:
                self.row_scorer.set_subtitle("Android scoreboard app for local score tracking.")
                sbox.append(Gtk.Image.new_from_icon_name("software-update-available-symbolic"))
                sbox.append(Gtk.Label(label="1-Click Install"))
                self.btn_scorer_action.set_child(sbox)
                self.btn_scorer_action.remove_css_class("secondary-btn")
                self.btn_scorer_action.add_css_class("suggested-action")
                self.btn_scorer_action.set_sensitive(True)
                self.btn_scorer_action._is_installed = False
                self.box_scorer.append(self.btn_scorer_action)

            self.expander_waydroid.set_visible(w_status.get("installed", False))

        run_async(worker_waydroid, on_done=on_done_waydroid)

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------
    def _on_sharing_toggled(self, row, param):
        if getattr(self, "_updating_rdp", False):
            return
        active = row.get_active()
        SystemService.set_desktop_sharing(active)
        if active and self.row_control.get_active():
            SystemService.set_remote_control(True)
        msg = "Desktop sharing enabled." if active else "Desktop sharing disabled."
        self.window.show_toast(msg)
        self.refresh()

    def _on_control_toggled(self, row, param):
        if getattr(self, "_updating_rdp", False):
            return
        active = row.get_active()
        SystemService.set_remote_control(active)
        msg = "Remote control enabled." if active else "Remote control disabled (view-only)."
        self.window.show_toast(msg)
        self.refresh()

    def _on_copy_rdp_clicked(self, btn):
        info = getattr(self, "_current_rdp_info", "")
        if info:
            copy_to_clipboard(info)
            self.window.show_toast("Connection details copied to clipboard.")

    def _on_tailscale_clicked(self, btn):
        tail_status = SystemService.get_tailscale_status()
        if not tail_status.get("installed"):
            self.window.show_toast("Installing Tailscale in background...")
            btn.set_sensitive(False)

            def worker():
                return SystemService.install_tailscale()

            def on_done(res):
                btn.set_sensitive(True)
                success, msg = res
                self.window.show_toast(msg)
                self.refresh()

            run_async(worker, on_done=on_done)
            return

        if tail_status.get("running"):
            btn.set_sensitive(False)

            def worker():
                return SystemService.disconnect_tailscale()

            def on_done(res):
                btn.set_sensitive(True)
                self.window.show_toast(res[1])
                self.refresh()

            run_async(worker, on_done=on_done)
            return

        def on_finished(success, msg):
            self.window.show_toast(msg)
            self.refresh()

        dialog = TailscaleAuthDialog(self.window, tail_status, on_finished)
        dialog.present()

    def _on_scorer_action_clicked(self, btn):
        if getattr(btn, "_is_installed", False) or SystemService.is_darts_scorer_installed():
            SystemService.launch_darts_scorer()
            self.window.show_toast("Opening Darts Scorer...")
            return

        def on_finished(success, msg):
            self.window.show_toast(msg)
            self.refresh()

        dialog = DartsScorerInstallDialog(self.window, on_finished)
        dialog.present()

    def _on_sub_debloat_clicked(self, btn):
        self.window.show_toast("Running Waydroid debloat and seamless multi-window setup...")

        def worker():
            return SystemService.waydroid_debloat_and_seamless()

        def on_done(res):
            success, msg = res
            self.window.show_toast(msg)
            self.refresh()

        run_async(worker, on_done=on_done)

    def _on_sub_settings_clicked(self, btn):
        subprocess.Popen(["waydroid", "app", "launch", "com.android.settings"])
        self.window.show_toast("Opening Android Settings...")

    def _on_sub_aurora_clicked(self, btn):
        if SystemService.is_aurora_store_installed():
            subprocess.Popen(["waydroid", "app", "launch", "com.aurora.store"])
            self.window.show_toast("Opening Aurora Store...")
            return
        self.window.show_toast("Downloading and installing Aurora Store APK...")

        def worker():
            return SystemService.install_aurora_store()

        def on_done(res):
            success, msg = res
            self.window.show_toast(msg)
            self.refresh()

        run_async(worker, on_done=on_done)
