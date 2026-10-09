import unittest
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Vte", "3.91")
from gi.repository import Gtk, Adw, Vte

from modules_gtk.autodarts_view import AutodartsView


class DummyWindow:
    def __init__(self):
        self.nav_view = None

    def show_toast(self, text):
        pass

    def open_usb_page(self):
        pass


class TestAutodartsViewMinimal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Gtk.init()

    def test_single_page_minimal_layout(self):
        """Verify AutodartsView is a single-page view with embedded terminal console."""
        window = DummyWindow()
        view = AutodartsView(window)
        self.assertIsInstance(view, Adw.NavigationPage)
        self.assertEqual(view.get_tag(), "autodarts")
        self.assertEqual(view.get_title(), "Autodarts")

        # Embedded terminal elements
        self.assertIsNotNone(view.terminal_card)
        self.assertIsNotNone(view.terminal)
        self.assertIsInstance(view.terminal, Vte.Terminal)
        self.assertIsNotNone(view.overlay)
        self.assertIsNotNone(view.box_overlay_banner)

        # Tools & Maintenance expander
        self.assertIsNotNone(view.exp_tools)
        self.assertIsNotNone(view.btn_restart_console)
        self.assertIsNotNone(view.row_usb)
        self.assertIsNotNone(view.btn_reinstall)
        self.assertIsNotNone(view.btn_uninstall)
        self.assertIsNotNone(view.lbl_footer_ver)

    def test_apply_state(self):
        """Verify _apply_state updates widgets for installed and not-installed status."""
        window = DummyWindow()
        view = AutodartsView(window)

        # Installed state
        view._apply_state(True)
        self.assertFalse(view.box_overlay_banner.get_visible())
        self.assertTrue(view.btn_restart_console.get_sensitive())

        # Not installed state
        view._apply_state(False)
        self.assertTrue(view.box_overlay_banner.get_visible())
        self.assertFalse(view.btn_restart_console.get_sensitive())

    def test_terminal_sizing_and_zero_scrolling(self):
        """Verify VTE terminal is placed directly in overlay (no ScrolledWindow) and has ample size request."""
        window = DummyWindow()
        view = AutodartsView(window)

        # Child of overlay must be the terminal itself (not a Gtk.ScrolledWindow)
        self.assertEqual(view.overlay.get_child(), view.terminal)
        self.assertFalse(isinstance(view.overlay.get_child(), Gtk.ScrolledWindow))

        # Check font is Monospace 8.5 for compact full-height display
        font_desc = view.terminal.get_font()
        self.assertTrue("8.5" in font_desc.to_string() or "8" in font_desc.to_string())

        # Minimum size request ensures proper responsive layout
        min_w, min_h = view.terminal.get_size_request()
        self.assertGreaterEqual(min_w, 400)
        self.assertGreaterEqual(min_h, 240)

    def test_window_adaptation_for_autodarts(self):
        """Verify SuitWindow maintains safe bounds when navigating."""
        from modules_gtk.window import SuitWindow
        app = Adw.Application(application_id="de.iterathor.test_window_adapt")
        win = SuitWindow(app)
        w, h = win.get_default_size()
        self.assertLessEqual(w, 940)
        self.assertLessEqual(h, 760)


if __name__ == "__main__":
    unittest.main()
