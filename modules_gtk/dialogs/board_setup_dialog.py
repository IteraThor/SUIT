import os
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Vte", "3.91")
from gi.repository import Gtk, Adw, Vte, Pango, Gdk, GLib

from core.logger import get_logger
from core.autodarts_service import get_autodarts_cli_binary

logger = get_logger("board_setup_dialog")


class BoardSetupDialog(Adw.Window):
    """Console dialog embedding the official Autodarts terminal UI ('ad')."""
    def __init__(self, parent_window=None, on_saved_cb=None):
        super().__init__(modal=True, title="Autodarts Console")
        if isinstance(parent_window, Gtk.Window):
            self.set_transient_for(parent_window)
        self.set_default_size(740, 520)
        self.set_size_request(600, 420)
        self.on_saved_cb = on_saved_cb
        self._child_pid = None

        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(True)

        btn_restart = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        btn_restart.set_tooltip_text("Restart Console")
        btn_restart.connect("clicked", lambda b: self._spawn(force=True))
        header.pack_end(btn_restart)

        toolbar_view.add_top_bar(header)

        # Scrolled container with VTE Terminal
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.add_css_class("terminal-card")
        box.set_vexpand(True)
        box.set_hexpand(True)

        self.terminal = Vte.Terminal()
        self.terminal.set_font(Pango.FontDescription.from_string("Monospace 10.5"))
        self.terminal.set_cursor_blink_mode(Vte.CursorBlinkMode.OFF)
        self.terminal.set_scrollback_lines(2000)
        self.terminal.set_mouse_autohide(True)
        self.terminal.set_vexpand(True)
        self.terminal.set_hexpand(True)

        bg = Gdk.RGBA()
        bg.parse("#181825")
        fg = Gdk.RGBA()
        fg.parse("#cdd6f4")
        self.terminal.set_colors(fg, bg, [])

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        scrolled.set_child(self.terminal)
        box.append(scrolled)

        toolbar_view.set_content(box)

        self.connect("close-request", self._on_close)
        GLib.idle_add(self._spawn)

    def _spawn(self, force=False):
        binary = get_autodarts_cli_binary()
        if not binary:
            logger.warning("Autodarts CLI binary not found")
            return

        if self._child_pid and force:
            try:
                os.kill(self._child_pid, 9)
            except Exception:
                pass
            self._child_pid = None

        if self._child_pid and not force:
            return

        self.terminal.reset(True, True)
        try:
            self.terminal.spawn_async(
                pty_flags=Vte.PtyFlags.DEFAULT,
                working_directory=os.path.expanduser("~"),
                argv=[binary],
                envv=[],
                spawn_flags=GLib.SpawnFlags.DEFAULT,
                child_setup=None,
                timeout=-1,
                cancellable=None,
                callback=self._on_spawn_cb,
                user_data=None
            )
        except Exception as e:
            logger.exception("Failed to spawn console dialog: %s", e)

    def _on_spawn_cb(self, terminal, pid, error, user_data):
        if not error:
            self._child_pid = pid
            self.terminal.grab_focus()

    def _on_close(self, widget):
        if self._child_pid:
            try:
                os.kill(self._child_pid, 9)
            except Exception:
                pass
            self._child_pid = None
        if callable(self.on_saved_cb):
            try:
                self.on_saved_cb()
            except Exception:
                pass
        return False
