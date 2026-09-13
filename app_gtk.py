#!/usr/bin/env python3
import sys
from pathlib import Path

# Add project root to path
BASE_DIR = Path(__file__).resolve().parent
sys.path.append(str(BASE_DIR))

# Setup diagnostic logging and global crash handler
from core.logger import setup_logging, install_global_exception_handler
logger = setup_logging()
install_global_exception_handler()

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib

# Set process and application name for GNOME Shell window/app tracking
GLib.set_prgname("de.iterathor.suit.gtk")
GLib.set_application_name("SUIT for Fedora")
Gtk.Window.set_default_icon_name("de.iterathor.suit.gtk")

from modules_gtk.window import SuitWindow

class SuitGtkApplication(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="de.iterathor.suit.gtk",
            flags=Gio.ApplicationFlags.FLAGS_NONE
        )
        self.window = None

    def do_activate(self):
        logger.info("Activating SUIT for Fedora GTK4 Application")
        # Force/Prefer Dark Mode (matching GNOME native dark palette)
        style_mgr = Adw.StyleManager.get_default()
        style_mgr.set_color_scheme(Adw.ColorScheme.PREFER_DARK)

        if not self.window:
            self.window = SuitWindow(self)
        self.window.present()

def main():
    app = SuitGtkApplication()
    return app.run(sys.argv)

if __name__ == "__main__":
    sys.exit(main())
