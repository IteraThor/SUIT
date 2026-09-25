import os
import shutil
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Vte", "3.91")
from gi.repository import Gtk, Adw, Vte, Pango, Gdk, GLib

from core.logger import get_logger
from core.systemd_service import SystemdService
from core.autodarts_service import (
    get_autodarts_cli_binary, is_autodarts_installed,
    install_autodarts, uninstall_autodarts
)
from modules_gtk.async_utils import run_async
from modules_gtk.ui_helpers import create_button_with_icon

logger = get_logger("autodarts_view")

SERVICE_NAME = "autodarts.service"


class AutodartsView(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Autodarts", tag="autodarts")
        self.window = window
        self.poll_source_id = None
        self.is_busy = False
        self._child_pid = None
        self._console_active = False

        # Main layout container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        main_box.set_margin_top(6)
        main_box.set_margin_bottom(6)
        main_box.set_margin_start(12)
        main_box.set_margin_end(12)
        main_box.set_vexpand(True)
        main_box.set_hexpand(True)
        self.set_child(main_box)

        # -------------------------------------------------------------
        # 1. Main Content: Embedded VTE Terminal (No Scrolling Required)
        # -------------------------------------------------------------
        self.terminal_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.terminal_card.add_css_class("terminal-card")
        self.terminal_card.set_vexpand(True)
        self.terminal_card.set_hexpand(True)

        self.overlay = Gtk.Overlay()
        self.overlay.set_vexpand(True)
        self.overlay.set_hexpand(True)
        self.terminal_card.append(self.overlay)

        # VTE Terminal directly inside overlay: full viewport, zero scrolling needed
        self.terminal = Vte.Terminal()
        self.terminal.set_font(Pango.FontDescription.from_string("Monospace 8.5"))
        self.terminal.set_cursor_blink_mode(Vte.CursorBlinkMode.OFF)
        self.terminal.set_scrollback_lines(3000)
        self.terminal.set_mouse_autohide(True)
        self.terminal.set_vexpand(True)
        self.terminal.set_hexpand(True)
        self.terminal.set_size_request(780, 660)

        # Dark theme palette (Catppuccin/Libadwaita dark match)
        bg = Gdk.RGBA()
        bg.parse("#181825")
        fg = Gdk.RGBA()
        fg.parse("#cdd6f4")
        self.terminal.set_colors(fg, bg, [])

        self.terminal.connect("child-exited", self._on_child_exited)
        self.overlay.set_child(self.terminal)

        # Overlay banner for when console is stopped / exited
        self.box_overlay_banner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.box_overlay_banner.add_css_class("terminal-overlay-banner")
        self.box_overlay_banner.set_halign(Gtk.Align.CENTER)
        self.box_overlay_banner.set_valign(Gtk.Align.CENTER)

        self.lbl_overlay_msg = Gtk.Label(label="Console is not running.")
        self.lbl_overlay_msg.add_css_class("title-3")
        self.box_overlay_banner.append(self.lbl_overlay_msg)

        self.btn_overlay_start = Gtk.Button(label="Start Console")
        self.btn_overlay_start.add_css_class("suggested-action")
        self.btn_overlay_start.connect("clicked", lambda b: self._spawn_console(force=True))
        self.box_overlay_banner.append(self.btn_overlay_start)

        self.overlay.add_overlay(self.box_overlay_banner)
        self.box_overlay_banner.set_visible(False)

        main_box.append(self.terminal_card)

        # -------------------------------------------------------------
        # 2. Collapsible Maintenance & Tools (Bottom Expander)
        # -------------------------------------------------------------
        self.exp_tools = Adw.ExpanderRow(title="Tools and Maintenance")
        self.exp_tools.set_expanded(False)

        # Terminal Console Restart Action
        row_console = Adw.ActionRow(
            title="Terminal Console",
            subtitle="Restart the embedded Autodarts session"
        )
        self.btn_restart_console = create_button_with_icon("utilities-terminal-symbolic", "Restart", "secondary-btn", height=36, touch_btn=True)
        self.btn_restart_console.set_tooltip_text("Restart the embedded Autodarts terminal console")
        self.btn_restart_console.connect("clicked", lambda b: self._spawn_console(force=True))
        row_console.add_suffix(self.btn_restart_console)
        self.exp_tools.add_row(row_console)

        # Experimental USB Bandwidth Tester
        self.row_usb = Adw.ActionRow(
            title='USB Bandwidth Tester  <span foreground="#ff9800" weight="bold" size="small">Experimental</span>'
        )
        self.row_usb.set_activatable(True)
        self.row_usb.connect("activated", lambda r: self.window.open_usb_page())
        chevron_usb = Gtk.Image.new_from_icon_name("go-next-symbolic")
        chevron_usb.set_opacity(0.5)
        chevron_usb.set_valign(Gtk.Align.CENTER)
        self.row_usb.add_suffix(chevron_usb)
        self.exp_tools.add_row(self.row_usb)

        # Install / Reinstall & Uninstall Actions
        row_install = Adw.ActionRow(title="Software Management")
        box_install_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box_install_btns.set_valign(Gtk.Align.CENTER)

        self.spinner = Gtk.Spinner()
        self.spinner.set_valign(Gtk.Align.CENTER)
        box_install_btns.append(self.spinner)

        self.btn_reinstall = create_button_with_icon("software-update-available-symbolic", "Reinstall", "secondary-btn", height=36, touch_btn=True)
        self.btn_reinstall.connect("clicked", self._on_install_clicked)
        box_install_btns.append(self.btn_reinstall)

        self.btn_uninstall = create_button_with_icon("user-trash-symbolic", "Uninstall", "secondary-btn-destructive", height=36, touch_btn=True)
        self.btn_uninstall.connect("clicked", self._on_uninstall_clicked)
        box_install_btns.append(self.btn_uninstall)

        row_install.add_suffix(box_install_btns)
        self.exp_tools.add_row(row_install)

        self.list_tools = Gtk.ListBox()
        self.list_tools.add_css_class("boxed-list")
        self.list_tools.append(self.exp_tools)
        self.grp_usb = self.list_tools
        main_box.append(self.list_tools)

        # Footer version label
        self.lbl_footer_ver = Gtk.Label(label="Autodarts Console Wrapper", xalign=0)
        self.lbl_footer_ver.add_css_class("dim-label")
        self.lbl_footer_ver.set_margin_start(4)
        main_box.append(self.lbl_footer_ver)

        # Connect page lifecycle
        self.connect("map", self._on_page_mapped)
        self.connect("unmap", self._on_page_unmapped)

    def _on_page_mapped(self, widget):
        self.refresh()
        if not self._console_active:
            self._spawn_console()

    def on_page_closed(self):
        """Lifecycle hook when navigating away: terminate embedded console so V4L2 handles are released."""
        logger.info("AutodartsView closed: terminating child console process if running")
        if self._child_pid is not None:
            try:
                os.kill(self._child_pid, 15)  # SIGTERM
            except Exception:
                pass
            self._child_pid = None
            self._console_active = False

    def _on_page_unmapped(self, widget):
        self.on_page_closed()

    def refresh(self):
        def worker():
            return is_autodarts_installed()

        def on_done(installed):
            self._apply_state(installed)

        run_async(worker, on_done=on_done)

    def _apply_state(self, status=None):
        if isinstance(status, bool):
            installed = status
        elif isinstance(status, dict):
            installed = is_autodarts_installed()
        else:
            installed = is_autodarts_installed()

        if not installed:
            self.box_overlay_banner.set_visible(True)
            self.lbl_overlay_msg.set_text("Autodarts is not installed.")
            self.btn_overlay_start.set_visible(False)
            if hasattr(self, "btn_restart_console"):
                self.btn_restart_console.set_sensitive(False)
        else:
            if hasattr(self, "btn_restart_console"):
                self.btn_restart_console.set_sensitive(True)

    def _spawn_console(self, force=False):
        binary = get_autodarts_cli_binary()
        if not binary:
            logger.warning("No autodarts binary found to spawn")
            self.box_overlay_banner.set_visible(True)
            self.lbl_overlay_msg.set_text("Autodarts binary not found.")
            self.btn_overlay_start.set_visible(False)
            return

        if self._child_pid is not None and force:
            try:
                os.kill(self._child_pid, 9)
            except Exception:
                pass
            self._child_pid = None
            self._console_active = False

        if self._child_pid is not None and not force:
            return

        self.box_overlay_banner.set_visible(False)
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
            logger.exception("Failed to spawn Autodarts terminal: %s", e)
            self.box_overlay_banner.set_visible(True)
            self.lbl_overlay_msg.set_text(f"Spawn failed: {e}")
            self.btn_overlay_start.set_visible(True)

    def _on_spawn_cb(self, terminal, pid, error, user_data):
        if error:
            logger.error("VTE spawn error: %s", error)
            self._console_active = False
            self._child_pid = None
            self.box_overlay_banner.set_visible(True)
            self.lbl_overlay_msg.set_text("Console launch failed.")
            self.btn_overlay_start.set_visible(True)
        else:
            logger.info("VTE spawned Autodarts console (PID %s)", pid)
            self._child_pid = pid
            self._console_active = True
            self.box_overlay_banner.set_visible(False)
            self.terminal.grab_focus()

    def _on_child_exited(self, terminal, status):
        logger.info("Autodarts console exited with status %s", status)
        self._child_pid = None
        self._console_active = False
        self.box_overlay_banner.set_visible(True)
        self.lbl_overlay_msg.set_text("Console exited.")
        self.btn_overlay_start.set_visible(True)

    def _do_service_action(self, action):
        if self.is_busy:
            return
        self.is_busy = True
        self.spinner.start()

        def worker():
            if action == "start":
                return SystemdService.start_unit(SERVICE_NAME)
            elif action == "stop":
                return SystemdService.stop_unit(SERVICE_NAME)
            elif action == "restart":
                return SystemdService.restart_unit(SERVICE_NAME)
            return False

        def on_done(ok):
            self.is_busy = False
            self.spinner.stop()
            if ok:
                self.window.show_toast(f"Service {action}ed.")
                self.refresh()
                # If started or restarted, also reconnect/restart console
                GLib.timeout_add(1000, lambda: self._spawn_console(force=True) or False)
            else:
                self.window.show_toast(f"Failed to {action} service.")

        run_async(worker, on_done=on_done)

    def _on_install_clicked(self, btn):
        if self.is_busy:
            return
        self.is_busy = True
        self.spinner.start()
        self.window.show_toast("Installing Autodarts...")

        def worker():
            return install_autodarts()

        def on_done(res):
            self.is_busy = False
            self.spinner.stop()
            ok, msg = res if isinstance(res, tuple) else (False, str(res))
            self.window.show_toast(msg)
            self.refresh()
            if ok:
                GLib.timeout_add(1500, lambda: self._spawn_console(force=True) or False)

        run_async(worker, on_done=on_done)

    def _on_uninstall_clicked(self, btn):
        if self.is_busy:
            return
        self.is_busy = True
        self.spinner.start()
        self.window.show_toast("Uninstalling Autodarts...")

        def worker():
            return uninstall_autodarts()

        def on_done(res):
            self.is_busy = False
            self.spinner.stop()
            ok, msg = res if isinstance(res, tuple) else (False, str(res))
            self.window.show_toast(msg)
            if self._child_pid:
                try:
                    os.kill(self._child_pid, 9)
                except Exception:
                    pass
                self._child_pid = None
                self._console_active = False
            self.refresh()

        run_async(worker, on_done=on_done)
