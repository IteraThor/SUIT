import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Adw, GLib, Gdk, GdkPixbuf, GObject, Graphene, Gsk
from core.logger import get_logger
from core.update_service import UpdateService
from modules_gtk.async_utils import run_async

from modules_gtk.ui_helpers import build_page_shell
from core.i18n import tr, get_language

logger = get_logger("menu")

FLAG_SIZE = (25, 15)  # 5:3, so the German stripes land on whole pixels
FLAG_OVERSAMPLE = 4


class FlagPaintable(GObject.Object, Gdk.Paintable):
    """An SVG flag rendered at FLAG_OVERSAMPLE x its size and filtered down, so it stays crisp at any scale."""

    def __init__(self, path, width, height):
        super().__init__()
        self._w, self._h = width, height
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            str(path), width * FLAG_OVERSAMPLE, height * FLAG_OVERSAMPLE, False)
        self._texture = Gdk.Texture.new_for_pixbuf(pb)

    def do_get_intrinsic_width(self):
        return self._w

    def do_get_intrinsic_height(self):
        return self._h

    def do_snapshot(self, snapshot, width, height):
        rect = Graphene.Rect().init(0, 0, width, height)
        snapshot.append_scaled_texture(self._texture, Gsk.ScalingFilter.TRILINEAR, rect)

class MainMenuView(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title=tr("SUIT for Fedora"), tag="main_menu")
        self.window = window
        
        # Scrolled container with kinetic touch scrolling
        scrolled, main_box = build_page_shell()
        self.set_child(scrolled)

        # Header Banner Card with Logo
        banner_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        banner_card.add_css_class("card")
        banner_card.set_margin_bottom(2)

        banner_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        banner_inner.set_margin_top(6)
        banner_inner.set_margin_bottom(6)
        banner_inner.set_margin_start(12)
        banner_inner.set_margin_end(12)

        logo_path = self.window.project_dir / "assets" / "icons" / "suit-icon.png"
        if logo_path.exists():
            logo_img = Gtk.Image.new_from_file(str(logo_path))
            logo_img.set_pixel_size(52)
            logo_img.set_valign(Gtk.Align.CENTER)
            banner_inner.append(logo_img)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        text_box.set_valign(Gtk.Align.CENTER)
        text_box.set_hexpand(True)

        self.lbl_title = Gtk.Label(label=tr("SUIT for Fedora"), xalign=0)
        self.lbl_title.add_css_class("title-3")
        text_box.append(self.lbl_title)

        self.lbl_subtitle = Gtk.Label(label=tr("Setup Utilities by IteraThor for Autodarts"), xalign=0)
        self.lbl_subtitle.add_css_class("dim-label")
        text_box.append(self.lbl_subtitle)

        banner_inner.append(text_box)

        # Language switch (English / German flags), left of the Community button
        banner_inner.append(self._build_language_switch())

        # Community & QR Dialog Button on most right
        self.btn_community = Gtk.Button()
        self.btn_community.set_valign(Gtk.Align.CENTER)
        self.btn_community.add_css_class("compact-btn")
        self.btn_community.add_css_class("secondary-btn")
        self.btn_community.set_tooltip_text(tr("IteraThor Community, Discord & 3D Print Models"))

        btn_comm_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_comm_box.set_halign(Gtk.Align.CENTER)
        btn_comm_box.set_valign(Gtk.Align.CENTER)

        icon_qr = Gtk.Image.new_from_icon_name("qr-code-symbolic")
        icon_qr.set_pixel_size(16)
        btn_comm_box.append(icon_qr)

        lbl_comm = Gtk.Label(label=tr("Community"))
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
            ("btn_system", "preferences-system-symbolic", tr("System Utilities & Tweaks"), tr("Performance, power, and touch tweaks."), self._open_system, True),
            ("btn_autodarts", "darts-symbolic", tr("Autodarts Setup"), tr("Board pairing, service, and camera telemetry."), self._open_autodarts, True),
            ("btn_camera_tools", "camera-photo-symbolic", tr("Camera Tools"), tr("Focus calibration, camera controls, and the camera optimizer."), self._open_camera_tools, True),
            ("btn_touch", "screen-touch-symbolic", tr("Screen & Touch Manager"), tr("Display rotation and touch calibration."), self._open_rotation, True),
            ("btn_kiosk", "view-fullscreen-symbolic", tr("Kiosk Mode"), tr("Fullscreen browser autostart and controls."), self._open_kiosk, True),
            ("btn_autoglow", "display-brightness-symbolic", tr("AutoGlow 2 Manager"), tr("WLED dartboard automation and lighting effects."), self._open_autoglow, True),
        ]
        new_items = {"btn_autoglow"}

        for item in items:
            key, icon_name, title_str, subtitle_str = item[0], item[1], item[2], item[3]
            callback = item[4]
            is_enabled = item[5] if len(item) > 5 else True

            row = Adw.ActionRow()
            row.set_title(GLib.markup_escape_text(title_str))
            row.set_subtitle(GLib.markup_escape_text(subtitle_str))

            # Crisp Native Symbolic Icon (24px for compact touch)
            icon_img = Gtk.Image.new_from_icon_name(icon_name)
            icon_img.set_pixel_size(24)
            icon_img.set_margin_end(10)
            icon_img.set_valign(Gtk.Align.CENTER)
            row.add_prefix(icon_img)

            if is_enabled:
                row.set_activatable(True)
                row.connect("activated", lambda r, cb=callback: cb())
                if key in new_items:
                    lbl_new = Gtk.Label(label=tr("NEW"))
                    lbl_new.add_css_class("status-pill-new")
                    lbl_new.set_valign(Gtk.Align.CENTER)
                    row.add_suffix(lbl_new)
                chevron = Gtk.Image.new_from_icon_name("go-next-symbolic")
                chevron.set_opacity(0.5)
                chevron.set_valign(Gtk.Align.CENTER)
                row.add_suffix(chevron)
            else:
                row.set_activatable(False)
                row.set_opacity(0.55)

                lbl_badge = Gtk.Label(label=tr("Coming Soon"))
                lbl_badge.add_css_class("status-pill-coming-soon")
                lbl_badge.set_valign(Gtk.Align.CENTER)
                row.add_suffix(lbl_badge)

            self.pref_group.add(row)
            self.rows[key] = row

        # Footer Actions (Updates)
        self.update_group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.update_group.set_margin_top(4)
        main_box.append(self.update_group)

        self.btn_update = Gtk.Button()
        self.btn_update.add_css_class("suggested-action")
        self.btn_update.add_css_class("touch-btn")
        self.btn_update.set_margin_top(0)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.CENTER)

        upd_icon = Gtk.Image.new_from_icon_name("software-update-available-symbolic")
        upd_icon.set_pixel_size(18)
        btn_box.append(upd_icon)

        lbl_btn = Gtk.Label(label=tr("Check for Updates"))
        lbl_btn.add_css_class("heading")
        btn_box.append(lbl_btn)

        self.btn_update.set_child(btn_box)
        self.btn_update.connect("clicked", self._on_check_updates)
        self.update_group.append(self.btn_update)

        self._update_available = False
        GLib.idle_add(self._check_updates_on_start)


    def _open_autodarts(self):
        logger.debug("Navigating to Autodarts view")
        self.window.open_autodarts_page()

    def _open_camera_tools(self):
        logger.debug("Navigating to Camera Tools view")
        self.window.open_camera_tools_page()

    def _open_focus(self):
        logger.debug("Navigating to Camera Focus view")
        self.window.open_focus_page()

    def _open_rotation(self):
        logger.debug("Navigating to Screen & Touch view")
        self.window.open_rotation_page()

    def _open_kiosk(self):
        logger.debug("Navigating to Kiosk view")
        self.window.open_kiosk_page()

    def _open_autoglow(self):
        logger.debug("Navigating to AutoGlow 2 Manager view")
        self.window.open_autoglow_page()

    def _open_system(self):
        logger.debug("Navigating to System Utilities view")
        self.window.open_system_page()

    def _set_update_button_loading(self, loading: bool, text: str = ""):
        text = text or tr("Checking for Updates...")
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
            lbl = Gtk.Label(label=tr("Update Available") if self._update_available else tr("Check for Updates"))
            lbl.add_css_class("heading")
            box.append(lbl)

        self.btn_update.set_child(box)

    def _set_update_available(self, available: bool):
        self._update_available = available
        if available:
            self.btn_update.remove_css_class("suggested-action")
            self.btn_update.add_css_class("update-available-btn")
        else:
            self.btn_update.remove_css_class("update-available-btn")
            self.btn_update.add_css_class("suggested-action")
        self._set_update_button_loading(False)

    def _check_updates_on_start(self):
        """Quiet background check at launch; only recolors the button when an update exists."""
        def worker():
            return UpdateService.check_update()

        def on_done(info):
            if info and info.get("has_update"):
                self._set_update_available(True)

        run_async(worker, on_done=on_done)
        return False

    def _on_check_updates(self, btn):
        logger.info("Check for updates and requirements triggered")
        self._set_update_button_loading(True, tr("Checking for Updates..."))

        def worker():
            missing_reqs = UpdateService.check_requirements()
            update_info = UpdateService.check_update()
            return {"missing_reqs": missing_reqs, "update_info": update_info}

        def on_done(res):
            self._set_update_button_loading(False)
            missing_reqs = res.get("missing_reqs", [])
            info = res.get("update_info", {})

            if missing_reqs:
                self._show_missing_requirements_dialog(missing_reqs, next_info=info)
                return

            if not info.get("error"):
                self._set_update_available(bool(info.get("has_update")))
            self._process_update_info(info)

        run_async(worker, on_done=on_done)

    def _show_missing_requirements_dialog(self, missing_reqs: list[str], next_info: dict | None = None):
        pkgs_formatted = "\n".join(f"• {pkg}" for pkg in missing_reqs)
        dialog = Adw.AlertDialog(
            heading=tr("Requirements Not Installed"),
            body=tr("The following required packages are not installed:\n\n{pkgs}\n\nInstall missing requirements now?").format(pkgs=pkgs_formatted)
        )
        dialog.add_response("cancel", tr("Later"))
        dialog.add_response("install", tr("Install"))
        dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)

        def on_response(d, response):
            if response == "install":
                self._perform_install_requirements(missing_reqs, next_info=next_info)

        dialog.connect("response", on_response)
        dialog.present(self.window)

    def _perform_install_requirements(self, missing_reqs: list[str], next_info: dict | None = None):
        self.window.show_toast(tr("Installing requirements..."))
        self._set_update_button_loading(True, tr("Installing..."))

        def worker():
            return UpdateService.install_requirements(missing_reqs)

        def on_done(res):
            self._set_update_button_loading(False)
            ok, msg = res
            self.window.show_toast(msg)
            if ok and next_info:
                self._process_update_info(next_info)

        run_async(worker, on_done=on_done)

    def _process_update_info(self, info: dict):
        if info.get("error"):
            self.window.show_toast(info["error"])
            return

        if not info.get("has_update"):
            self.window.show_toast(tr("SUIT for Fedora is up to date."))
            return

        count = info.get("commits_behind", 1)
        msg = info.get("latest_message", "")

        if count == 1:
            body_text = tr("A new version of SUIT is available (1 update).")
        else:
            body_text = tr("A new version of SUIT is available ({count} updates).").format(count=count)
        if msg:
            body_text += "\n\n" + tr("Latest: {msg}").format(msg=msg)
        body_text += "\n\n" + tr("Install the latest updates now?")

        dialog = Adw.AlertDialog(
            heading=tr("Update Available"),
            body=body_text
        )
        dialog.add_response("cancel", tr("Later"))
        dialog.add_response("update", tr("Update Now"))
        dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)

        def on_response(d, response):
            if response == "update":
                self._perform_update()

        dialog.connect("response", on_response)
        dialog.present(self.window)

    def _perform_update(self):
        self.window.show_toast(tr("Downloading and applying update..."))
        self._set_update_button_loading(True, tr("Updating SUIT..."))

        def worker():
            return UpdateService.apply_update()

        def on_done(res):
            self._set_update_button_loading(False)
            success, msg = res
            if not success:
                self.window.show_toast(msg)
                return

            self._set_update_available(False)
            dialog = Adw.AlertDialog(
                heading=tr("Update Complete"),
                body=tr("SUIT has been successfully updated.\n\nRestart now to apply the changes?")
            )
            dialog.add_response("later", tr("Restart Later"))
            dialog.add_response("restart", tr("Restart SUIT"))
            dialog.set_response_appearance("restart", Adw.ResponseAppearance.SUGGESTED)

            def on_response(d, response):
                if response == "restart":
                    UpdateService.restart_application()

            dialog.connect("response", on_response)
            dialog.present(self.window)

        run_async(worker, on_done=on_done)

    def _build_language_switch(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_end(-8)  # sits right against the Community button
        flags_dir = self.window.project_dir / "assets" / "flags"
        group = None
        for lang, tooltip in (("en", "English"), ("de", "Deutsch")):
            btn = Gtk.ToggleButton()
            btn.add_css_class("flat")
            btn.add_css_class("lang-flag-btn")
            btn.set_valign(Gtk.Align.CENTER)
            btn.set_tooltip_text(tooltip)
            pic = Gtk.Picture.new_for_paintable(FlagPaintable(flags_dir / f"{lang}.svg", *FLAG_SIZE))
            pic.set_can_shrink(False)
            pic.set_halign(Gtk.Align.CENTER)
            pic.set_valign(Gtk.Align.CENTER)
            btn.set_child(pic)
            if group:
                btn.set_group(group)
            else:
                group = btn
            btn.set_active(lang == get_language())
            btn.connect("toggled", self._on_language_toggled, lang)
            box.append(btn)
        return box

    def _on_language_toggled(self, btn, lang):
        if btn.get_active() and lang != get_language():
            logger.info("Switching UI language to %s", lang)
            # Rebuild after the toggle signal returns: the rebuild replaces this page
            GLib.idle_add(lambda: self.window.set_language(lang) or False)

    def _open_community_dialog(self, *args):
        logger.debug("Opening Community & QR dialog")
        from modules_gtk.dialogs.community_dialog import CommunityDialog
        dialog = CommunityDialog(self.window)
        dialog.present()

