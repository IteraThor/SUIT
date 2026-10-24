import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, GLib
import importlib
import sys
import time
import threading
from pathlib import Path

from core.logger import get_logger
from core.privileges import check_sudo_privileges, setup_sudoers_pkexec
from core.update_service import UpdateService
from modules_gtk.async_utils import run_async
from modules_gtk.menu import MainMenuView

logger = get_logger("window")

# Large pages with a live view: they open the window maximized
CAMERA_PAGES = ("camera_controls", "camera_optimizer")

# Longest the window stays open after a close request while a Camera Optimizer job (optimize/restore) runs
JOB_WAIT_S = 360.0

class SuitWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="SUIT for Fedora")
        # Determine monitor size for display-aware default window bounds
        display = Gdk.Display.get_default()
        screen_w, screen_h = 1280, 800
        if display:
            monitors = display.get_monitors()
            if monitors.get_n_items() > 0:
                geom = monitors.get_item(0).get_geometry()
                screen_w, screen_h = geom.width, geom.height

        default_w = min(840, max(560, screen_w - 60))
        default_h = min(720, max(540, screen_h - 90))
        self.set_default_size(default_w, default_h)
        self.set_size_request(380, 420)

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
        self.btn_back.connect("clicked", lambda b: self._on_back_clicked())
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
        self.camera_tools_page = None
        self.camera_controls_page = None
        self.camera_optimizer_page = None
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

        # Handle window close request to cleanly tear down active page & services
        self.connect("close-request", self._on_close_request)

    def _on_back_clicked(self):
        """Header back: a page may ask first."""
        page = self.nav_view.get_visible_page()
        confirm = getattr(page, "confirm_leave", None)
        if callable(confirm) and confirm(self.nav_view.pop):
            return
        self.nav_view.pop()

    def _on_close_request(self, *args):
        """Lifecycle hook when window close is requested: cleanly teardown active page."""
        if getattr(self, "_close_ready", False):
            return False
        logger.info("Window close requested: running active page cleanup")
        page = self.nav_view.get_visible_page()
        if page and hasattr(page, "on_page_closed"):
            try:
                page.on_page_closed()
            except Exception:
                logger.exception("Error executing on_page_closed on window close")

        # A Camera Optimizer job runs in this process with Autodarts stopped. Quitting mid-job would
        # leave Autodarts down, so keep the window (insensitive) until the job is done. Its page may
        # already be popped.
        opt = getattr(self, "camera_optimizer_page", None)
        t = opt.job_thread if opt is not None else None
        if t is None or not t.is_alive():
            return False

        self.set_sensitive(False)
        self.title_widget.set_subtitle("Finishing the camera optimization…")
        deadline = time.monotonic() + JOB_WAIT_S

        def _poll():
            if t.is_alive() and time.monotonic() < deadline:
                return True
            if t.is_alive():
                logger.error("Camera Optimizer job still running after %.0f s; closing anyway", JOB_WAIT_S)
            self._close_ready = True
            self.close()
            return False

        GLib.timeout_add(100, _poll)
        return True

    def _start_background_prewarm(self):
        """Asynchronously pre-warm heavy dependencies (OpenCV, GStreamer, CameraFocusView)
        so that tapping the Camera Focus menu card is instantaneous on the first click."""
        def _bg_prewarm():
            time.sleep(0.4)  # Let main menu render smoothly first
            # Auto-heal boot services (e.g. after CLI git pull); independent of OpenCV availability
            try:
                UpdateService.ensure_camera_keeper()
                UpdateService.ensure_display_autostart()
            except Exception as e:
                logger.debug("Background service heal notice: %s", e)

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

        self._adapt_window_size_for_page(page)

    def _adapt_window_size_for_page(self, page):
        """Dynamically adapts window size for active module without overflowing display bounds."""
        if not page:
            return

        display = Gdk.Display.get_default()
        screen_w, screen_h = 1280, 800
        if display:
            monitors = display.get_monitors()
            if monitors.get_n_items() > 0:
                geom = monitors.get_item(0).get_geometry()
                screen_w, screen_h = geom.width, geom.height

        # Safety clearance for GNOME top bar and dock
        max_allowed_w = max(420, screen_w - 60)
        max_allowed_h = max(420, screen_h - 90)

        tag = getattr(page, "tag", None)
        if not tag and hasattr(page, "get_tag"):
            tag = page.get_tag()

        if tag in CAMERA_PAGES:
            self.maximize()
            return

        if hasattr(self, "is_fullscreen") and self.is_fullscreen():
            self.unfullscreen()

        if self.is_maximized():
            self.unmaximize()

        target_map = {
            "main_menu": (760, 640),
            "camera_tools": (740, 480),
            "camera_focus": (940, 640),
            "autodarts": (840, 680),
            "rotation": (780, 680),
            "kiosk": (780, 680),
            "system": (800, 780),
            "usb": (780, 560),
            "advanced_users": (800, 720),
        }

        ideal_w, ideal_h = target_map.get(tag, (780, 680))
        target_w = min(max_allowed_w, ideal_w)
        target_h = min(max_allowed_h, ideal_h)

        self.set_default_size(target_w, target_h)


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

    def open_camera_tools_page(self):
        if not self.camera_tools_page:
            from modules_gtk.camera_tools_view import CameraToolsView
            self.camera_tools_page = CameraToolsView(self)
        self.nav_view.push(self.camera_tools_page)

    def _show_page(self, page):
        """Push a page, or pop back to it when it is already in the stack (the camera pages link to each other)."""
        stack = self.nav_view.get_navigation_stack()
        if any(stack.get_item(i) is page for i in range(stack.get_n_items())):
            self.nav_view.pop_to_page(page)
        else:
            self.nav_view.push(page)

    def open_camera_controls_page(self):
        try:
            if not self.camera_controls_page:
                from modules_gtk.camera_controls_view import CameraControlsPage
                self.camera_controls_page = CameraControlsPage(self)
            self._show_page(self.camera_controls_page)
            self.camera_controls_page.on_page_opened()
        except ImportError as e:
            logger.exception("Camera Controls unavailable due to missing dependency: %s", e)
            self._prompt_install_opencv("Camera Controls", self.open_camera_controls_page)
        except Exception as e:
            logger.exception("Failed to open Camera Controls: %s", e)
            dialog = Adw.AlertDialog(
                heading="Camera Controls Error",
                body="Unable to open Camera Controls. Check system log for details."
            )
            dialog.add_response("ok", "OK")
            dialog.present(self)

    def _prompt_install_opencv(self, tool_name, retry_cb):
        dialog = Adw.AlertDialog(
            heading="OpenCV Required",
            body=f"{tool_name} requires OpenCV (python3-opencv)."
        )
        dialog.add_response("cancel", "Later")
        dialog.add_response("install", "Install Now")
        dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)

        def on_resp(d, resp):
            if resp != "install":
                return
            self.show_toast("Installing python3-opencv...")

            def worker():
                return UpdateService.install_requirements(["python3-opencv"])

            def on_done(res):
                ok, msg = res
                if not ok:
                    self.show_toast(msg)
                    return
                # Freshly installed system modules need fresh import finders; a partially failed
                # import may also be cached, so verify before retrying to avoid a dialog loop.
                importlib.invalidate_caches()
                sys.modules.pop("cv2", None)
                try:
                    import cv2  # noqa: F401
                except ImportError:
                    self.show_toast("OpenCV installed. Please restart SUIT to use this tool.")
                    return
                self.show_toast(msg)
                retry_cb()

            run_async(worker, on_done=on_done)

        dialog.connect("response", on_resp)
        dialog.present(self)

    def open_camera_optimizer_page(self):
        try:
            if not self.camera_optimizer_page:
                from modules_gtk.camera_optimizer_view import CameraOptimizerPage
                self.camera_optimizer_page = CameraOptimizerPage(self)
            self._show_page(self.camera_optimizer_page)
            self.camera_optimizer_page.on_page_opened()
        except ImportError as e:
            logger.exception("Camera Optimizer unavailable due to missing dependency: %s", e)
            self._prompt_install_opencv("Camera Optimizer", self.open_camera_optimizer_page)
        except Exception as e:
            logger.exception("Failed to open Camera Optimizer: %s", e)
            dialog = Adw.AlertDialog(
                heading="Camera Optimizer Error",
                body="Unable to open Camera Optimizer. Check system log for details."
            )
            dialog.add_response("ok", "OK")
            dialog.present(self)

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
            self._prompt_install_opencv("Camera Focus Tool", self.open_focus_page)
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
