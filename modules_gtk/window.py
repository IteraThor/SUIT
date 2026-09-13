import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, Gdk, GLib
import time
import threading
from pathlib import Path

from core.logger import get_logger
from core.privileges import check_sudo_privileges, setup_sudoers_pkexec
from modules_gtk.menu import MainMenuView

logger = get_logger("window")

class SuitWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="SUIT for Fedora")
        self.set_default_size(1020, 820)
        self.set_size_request(480, 600)

        self.project_dir = Path(__file__).resolve().parent.parent

        # Register custom icons directory
        icons_dir = self.project_dir / "assets" / "icons"
        if icons_dir.exists():
            display = Gdk.Display.get_default()
            if display:
                icon_theme = Gtk.IconTheme.get_for_display(display)
                icon_theme.add_search_path(str(icons_dir))

        self.set_icon_name("de.iterathor.suit.gtk")

        # Load Touchscreen Optimized CSS
        css_file = self.project_dir / "style_gtk.css"
        if css_file.exists():
            try:
                css_provider = Gtk.CssProvider()
                css_provider.load_from_path(str(css_file))
                display = Gdk.Display.get_default()
                if display:
                    Gtk.StyleContext.add_provider_for_display(
                        display, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                    )
            except Exception:
                logger.exception("Error loading style_gtk.css")

        # Toast Overlay (for modern, non-intrusive touch feedback)
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        # Main vertical container
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toast_overlay.set_child(box)

        # Native Header Bar
        self.header_bar = Adw.HeaderBar()
        box.append(self.header_bar)

        # Touch-Friendly Back Button (visible on subpages)
        self.btn_back = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self.btn_back.add_css_class("flat")
        self.btn_back.set_tooltip_text("Back to Menu")
        self.btn_back.set_size_request(46, 44)
        self.btn_back.set_visible(False)
        self.btn_back.connect("clicked", lambda b: self.nav_view.pop())
        self.header_bar.pack_start(self.btn_back)

        # Title Widget
        self.title_widget = Adw.WindowTitle(title="SUIT for Fedora")
        self.header_bar.set_title_widget(self.title_widget)

        # Navigation View (smooth hardware-accelerated transitions)
        self.nav_view = Adw.NavigationView()
        self.nav_view.set_vexpand(True)
        self.nav_view.connect("notify::visible-page", self._on_nav_changed)
        box.append(self.nav_view)

        # Cached Pages
        self.autodarts_page = None
        self.focus_page = None
        self.rotation_page = None
        self.kiosk_page = None
        self.system_page = None
        self.usb_page = None

        # Push Main Menu
        self.menu_page = MainMenuView(self)
        self._last_visible_page = self.menu_page
        self.nav_view.push(self.menu_page)

        # Check privilege escalation on first run
        GLib.idle_add(self._check_first_run_privileges)

        # Background pre-warm heavy dependencies (OpenCV, GStreamer, CameraFocusView)
        self._start_background_prewarm()

    def _start_background_prewarm(self):
        """Asynchronously pre-warm heavy dependencies (OpenCV, GStreamer, CameraFocusView)
        so that tapping the Camera Focus menu card is instantaneous on the first click."""
        def _bg_prewarm():
            time.sleep(0.4)  # Let main menu render smoothly first
            try:
                # Pre-import heavy native C extensions in background thread
                import cv2  # noqa: F401
                from modules_gtk.camera_focus_view import CameraFocusView

                def _init_page():
                    if not self.focus_page:
                        logger.info("Pre-warming CameraFocusView in background")
                        self.focus_page = CameraFocusView(self)
                    return False

                GLib.idle_add(_init_page)
            except Exception as e:
                logger.debug("Background pre-warm notice: %s", e)

        threading.Thread(target=_bg_prewarm, daemon=True, name="suit-prewarm").start()

    def _check_first_run_privileges(self):
        if not check_sudo_privileges():
            dialog = Adw.AlertDialog(
                heading="Administrator Privileges Required",
                body="SUIT requires administrator rights to configure systemd background services, kiosk mode, and system settings without interrupting you.\n\nAuthorize once to enable seamless passwordless access for future runs?"
            )
            dialog.add_response("cancel", "Not Now")
            dialog.add_response("auth", "Authorize")
            dialog.set_response_appearance("auth", Adw.ResponseAppearance.SUGGESTED)

            def on_response(d, response):
                if response == "auth":
                    def _do_auth():
                        ok, msg = setup_sudoers_pkexec()
                        def _notify():
                            if ok:
                                self.show_toast("Administrator privileges successfully authorized.")
                            else:
                                self.show_toast("Authorization was not completed.")
                        GLib.idle_add(_notify)
                    threading.Thread(target=_do_auth, daemon=True).start()

            dialog.connect("response", on_response)
            dialog.present(self)
        return False

    def _on_nav_changed(self, *args):
        """Updates back button visibility and title according to current page."""
        page = self.nav_view.get_visible_page()
        
        # Cleanup previous page if navigating away
        if hasattr(self, "_last_visible_page") and self._last_visible_page and page != self._last_visible_page:
            if hasattr(self._last_visible_page, "on_page_closed"):
                self._last_visible_page.on_page_closed()

        self._last_visible_page = page

        if page and page != self.menu_page:
            self.btn_back.set_visible(True)
            self.title_widget.set_title(page.get_title() or "SUIT for Fedora")
            self.title_widget.set_subtitle("SUIT for Fedora")
        else:
            self.btn_back.set_visible(False)
            self.title_widget.set_title("SUIT for Fedora")
            self.title_widget.set_subtitle("")

    def show_toast(self, text):
        toast = Adw.Toast(title=text, timeout=3)
        self.toast_overlay.add_toast(toast)

    def open_autodarts_page(self):
        if not self.autodarts_page:
            from modules_gtk.autodarts_view import AutodartsView
            self.autodarts_page = AutodartsView(self)
        self.nav_view.push(self.autodarts_page)
        if hasattr(self.autodarts_page, "refresh"):
            self.autodarts_page.refresh()

    def open_focus_page(self):
        try:
            if not self.focus_page:
                from modules_gtk.camera_focus_view import CameraFocusView
                self.focus_page = CameraFocusView(self)
            self.nav_view.push(self.focus_page)
            if hasattr(self.focus_page, "on_page_opened"):
                self.focus_page.on_page_opened()
        except ImportError as e:
            logger.exception("Camera Focus Tool unavailable due to missing dependency: %s", e)
            dialog = Adw.AlertDialog(
                heading="OpenCV Required",
                body="The Camera Focus Tool requires OpenCV.\n\nInstall with: sudo dnf install python3-opencv"
            )
            dialog.add_response("ok", "OK")
            dialog.present(self)
        except Exception as e:
            logger.exception("Failed to open Camera Focus Tool: %s", e)
            dialog = Adw.AlertDialog(
                heading="Camera Focus Error",
                body="Unable to open Camera Focus Tool. Check system log for details."
            )
            dialog.add_response("ok", "OK")
            dialog.present(self)

    def open_rotation_page(self):
        if not self.rotation_page:
            from modules_gtk.rotation_view import RotationView
            self.rotation_page = RotationView(self)
        self.nav_view.push(self.rotation_page)
        if hasattr(self.rotation_page, "refresh"):
            self.rotation_page.refresh()

    def open_kiosk_page(self):
        if not self.kiosk_page:
            from modules_gtk.kiosk_view import KioskView
            self.kiosk_page = KioskView(self)
        self.nav_view.push(self.kiosk_page)
        if hasattr(self.kiosk_page, "refresh"):
            self.kiosk_page.refresh()

    def open_system_page(self):
        if not self.system_page:
            from modules_gtk.system_view import SystemView
            self.system_page = SystemView(self)
        self.nav_view.push(self.system_page)
        if hasattr(self.system_page, "refresh"):
            self.system_page.refresh()

    def open_usb_page(self):
        if not self.usb_page:
            from modules_gtk.usb_view import UsbView
            self.usb_page = UsbView(self)
        self.nav_view.push(self.usb_page)
        if hasattr(self.usb_page, "refresh"):
            self.usb_page.refresh()
