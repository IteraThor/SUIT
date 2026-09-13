import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib
from pathlib import Path
from core.logger import get_logger
from core.update_service import UpdateService
from modules_gtk.async_utils import run_async

logger = get_logger("menu")

class MainMenuView(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="SUIT for Fedora", tag="main_menu")
        self.window = window
        
        # Scrolled container with kinetic touch scrolling
        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_kinetic_scrolling(True)
        scrolled.set_overlay_scrolling(True)
        self.set_child(scrolled)
        
        # Clamp to center and constrain width on 16" wide screens (up to 1920px)
        clamp = Adw.Clamp(maximum_size=880, tightening_threshold=660)
        clamp.set_margin_top(16)
        clamp.set_margin_bottom(24)
        clamp.set_margin_start(16)
        clamp.set_margin_end(16)
        scrolled.set_child(clamp)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        clamp.set_child(main_box)

        # Header Banner Card with Logo
        banner_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        banner_card.add_css_class("card")
        banner_card.set_margin_bottom(8)
        
        banner_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        banner_inner.set_margin_top(16)
        banner_inner.set_margin_bottom(16)
        banner_inner.set_margin_start(20)
        banner_inner.set_margin_end(20)
        
        logo_path = self.window.project_dir / "assets" / "icons" / "suit-icon.png"
        if logo_path.exists():
            logo_img = Gtk.Image.new_from_file(str(logo_path))
            logo_img.set_pixel_size(64)
            logo_img.set_valign(Gtk.Align.CENTER)
            banner_inner.append(logo_img)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_valign(Gtk.Align.CENTER)
        text_box.set_hexpand(True)

        self.lbl_title = Gtk.Label(label="SUIT for Fedora", xalign=0)
        self.lbl_title.add_css_class("title-1")
        text_box.append(self.lbl_title)
        
        self.lbl_subtitle = Gtk.Label(label="Setup Utilities by IteraThor for Autodarts", xalign=0)
        self.lbl_subtitle.add_css_class("dim-label")
        text_box.append(self.lbl_subtitle)
        
        banner_inner.append(text_box)

        # Community & QR Dialog Button on most right
        self.btn_community = Gtk.Button()
        self.btn_community.set_valign(Gtk.Align.CENTER)
        self.btn_community.add_css_class("compact-btn")
        self.btn_community.add_css_class("secondary-btn")
        self.btn_community.set_tooltip_text("IteraThor Community, Discord & 3D Print Models")

        btn_comm_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_comm_box.set_halign(Gtk.Align.CENTER)
        btn_comm_box.set_valign(Gtk.Align.CENTER)

        icon_qr = Gtk.Image.new_from_icon_name("qr-code-symbolic")
        icon_qr.set_pixel_size(18)
        btn_comm_box.append(icon_qr)

        lbl_comm = Gtk.Label(label="Community")
        lbl_comm.add_css_class("heading")
        btn_comm_box.append(lbl_comm)

        self.btn_community.set_child(btn_comm_box)
        self.btn_community.connect("clicked", self._open_community_dialog)
        banner_inner.append(self.btn_community)

        banner_card.append(banner_inner)
        main_box.append(banner_card)

        # Preferences Group for Modules
        self.pref_group = Adw.PreferencesGroup()
        main_box.append(self.pref_group)

        self.rows = {}
        
        # Native Module Items
        items = [
            ("btn_system", "preferences-system-symbolic", "System Utilities & Tweaks", "Performance, power, and touch tweaks.", self._open_system, True),
            ("btn_autodarts", "darts-symbolic", "Autodarts Setup", "Board pairing, service, and camera telemetry.", self._open_autodarts, True),
            ("btn_autoglow", "display-brightness-symbolic", "AutoGlow 2 Manager", "WLED dartboard automation and lighting effects.", None, False),
            ("btn_focus", "camera-photo-symbolic", "Camera Focus Tool", "Adjust lens sharpness and alignment.", self._open_focus, True),
            ("btn_touch", "screen-touch-symbolic", "Screen & Touch Manager", "Display rotation and touch calibration.", self._open_rotation, True),
            ("btn_kiosk", "view-fullscreen-symbolic", "Kiosk Mode", "Fullscreen browser autostart and controls.", self._open_kiosk, True),
        ]

        for item in items:
            key, icon_name, title_str, subtitle_str = item[0], item[1], item[2], item[3]
            callback = item[4]
            is_enabled = item[5] if len(item) > 5 else True

            row = Adw.ActionRow()
            row.set_title(GLib.markup_escape_text(title_str))
            row.set_subtitle(GLib.markup_escape_text(subtitle_str))
            
            # Crisp Native Symbolic Icon (28px for touch)
            icon_img = Gtk.Image.new_from_icon_name(icon_name)
            icon_img.set_pixel_size(28)
            icon_img.set_margin_end(12)
            icon_img.set_valign(Gtk.Align.CENTER)
            row.add_prefix(icon_img)
            
            if is_enabled:
                row.set_activatable(True)
                row.connect("activated", lambda r, cb=callback: cb())
                chevron = Gtk.Image.new_from_icon_name("go-next-symbolic")
                chevron.set_opacity(0.5)
                chevron.set_valign(Gtk.Align.CENTER)
                row.add_suffix(chevron)
            else:
                row.set_activatable(False)
                row.set_opacity(0.55)
                
                lbl_badge = Gtk.Label(label="Coming Soon")
                lbl_badge.add_css_class("status-pill-coming-soon")
                lbl_badge.set_valign(Gtk.Align.CENTER)
                row.add_suffix(lbl_badge)
            
            self.pref_group.add(row)
            self.rows[key] = row

        # Footer Actions (Updates)
        self.update_group = Adw.PreferencesGroup()
        self.update_group.set_margin_top(12)
        main_box.append(self.update_group)

        self.btn_update = Gtk.Button()
        self.btn_update.add_css_class("suggested-action")
        self.btn_update.add_css_class("touch-btn")
        self.btn_update.set_size_request(-1, 54)
        self.btn_update.set_margin_top(6)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        btn_box.set_halign(Gtk.Align.CENTER)
        
        upd_icon = Gtk.Image.new_from_icon_name("software-update-available-symbolic")
        upd_icon.set_pixel_size(22)
        btn_box.append(upd_icon)
        
        lbl_btn = Gtk.Label(label="Check for Updates")
        lbl_btn.add_css_class("heading")
        btn_box.append(lbl_btn)
        
        self.btn_update.set_child(btn_box)
        self.btn_update.connect("clicked", self._on_check_updates)
        self.update_group.add(self.btn_update)

    def _open_autodarts(self):
        logger.debug("Navigating to Autodarts view")
        self.window.open_autodarts_page()

    def _open_focus(self):
        logger.debug("Navigating to Camera Focus view")
        self.window.open_focus_page()

    def _open_rotation(self):
        logger.debug("Navigating to Screen & Touch view")
        self.window.open_rotation_page()

    def _open_kiosk(self):
        logger.debug("Navigating to Kiosk view")
        self.window.open_kiosk_page()

    def _open_system(self):
        logger.debug("Navigating to System Utilities view")
        self.window.open_system_page()

    def _open_usb(self):
        logger.debug("Navigating to USB Bandwidth view")
        self.window.open_usb_page()

    def _set_update_button_loading(self, loading: bool, text: str = "Checking for Updates..."):
        self.btn_update.set_sensitive(not loading)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)

        if loading:
            spinner = Gtk.Spinner()
            spinner.start()
            box.append(spinner)
            lbl = Gtk.Label(label=text)
            lbl.add_css_class("heading")
            box.append(lbl)
        else:
            upd_icon = Gtk.Image.new_from_icon_name("software-update-available-symbolic")
            upd_icon.set_pixel_size(22)
            box.append(upd_icon)
            lbl = Gtk.Label(label="Check for Updates")
            lbl.add_css_class("heading")
            box.append(lbl)

        self.btn_update.set_child(box)

    def _on_check_updates(self, btn):
        logger.info("Check for updates triggered")
        self._set_update_button_loading(True, "Checking for Updates...")

        def worker():
            return UpdateService.check_update()

        def on_done(info):
            self._set_update_button_loading(False)
            if info.get("error"):
                self.window.show_toast(info["error"])
                return

            if not info.get("has_update"):
                self.window.show_toast("SUIT for Fedora is up to date.")
                return

            count = info.get("commits_behind", 1)
            msg = info.get("latest_message", "")

            body_text = f"A new version of SUIT is available ({count} update{'s' if count != 1 else ''})."
            if msg:
                body_text += f"\n\nLatest: {msg}"
            body_text += "\n\nInstall the latest updates now?"

            dialog = Adw.AlertDialog(
                heading="Update Available",
                body=body_text
            )
            dialog.add_response("cancel", "Later")
            dialog.add_response("update", "Update Now")
            dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)

            def on_response(d, response):
                if response == "update":
                    self._perform_update()

            dialog.connect("response", on_response)
            dialog.present(self.window)

        run_async(worker, on_done=on_done)

    def _perform_update(self):
        self.window.show_toast("Downloading and applying update...")
        self._set_update_button_loading(True, "Updating SUIT...")

        def worker():
            return UpdateService.apply_update()

        def on_done(res):
            self._set_update_button_loading(False)
            success, msg = res
            if not success:
                self.window.show_toast(msg)
                return

            dialog = Adw.AlertDialog(
                heading="Update Complete",
                body="SUIT has been successfully updated.\n\nRestart now to apply the changes?"
            )
            dialog.add_response("later", "Restart Later")
            dialog.add_response("restart", "Restart SUIT")
            dialog.set_response_appearance("restart", Adw.ResponseAppearance.SUGGESTED)

            def on_response(d, response):
                if response == "restart":
                    UpdateService.restart_application()

            dialog.connect("response", on_response)
            dialog.present(self.window)

        run_async(worker, on_done=on_done)

    def _open_community_dialog(self, *args):
        logger.debug("Opening Community & QR dialog")
        from modules_gtk.dialogs.community_dialog import CommunityDialog
        dialog = CommunityDialog(self.window)
        dialog.present()

