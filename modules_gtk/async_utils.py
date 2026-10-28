"""GLib safe thread dispatcher and URL launcher for SUIT."""

from __future__ import annotations
import logging
import os
import shutil
import subprocess
import threading
from typing import Any, Callable
from gi.repository import GLib

from core.logger import get_logger

logger = get_logger("async")


def run_async(
    func: Callable[..., Any],
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
    *args: Any,
    on_success: Callable[[Any], None] | None = None,
    **kwargs: Any,
) -> threading.Thread:
    """Run `func` in a background thread and safely dispatch results on the GTK main loop."""
    callback = on_done or on_success

    def once(cb, value):
        # an idle callback repeats while it returns something truthy: always run it exactly once
        cb(value)
        return False

    def worker():
        try:
            res = func(*args, **kwargs)
            if callback:
                GLib.idle_add(once, callback, res)
        except Exception as e:
            logger.exception("Async background worker failed: %s", e)
            if on_error:
                GLib.idle_add(once, on_error, e)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


def open_browser_url(parent_window=None, url: str = "") -> bool:
    """Reliably opens a URL in the browser, explicitly configuring Wayland flags on Linux."""
    if not url:
        return False

    # 1. Direct browser binary execution with Wayland ozone flags
    try:
        browser = (
            shutil.which("chromium-browser")
            or shutil.which("chromium")
            or shutil.which("google-chrome")
            or shutil.which("brave-browser")
            or shutil.which("firefox")
        )
        if browser:
            env = os.environ.copy()
            if "WAYLAND_DISPLAY" not in env and os.path.exists(f"/run/user/{os.getuid()}/wayland-0"):
                env["WAYLAND_DISPLAY"] = "wayland-0"
            if "XDG_RUNTIME_DIR" not in env:
                env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
            flags = []
            if "chromium" in browser or "chrome" in browser or "brave" in browser:
                # Essential for Wayland on Fedora: prevents Ozone platform X11 crash
                flags = ["--ozone-platform-hint=auto", "--ozone-platform=wayland"]
            subprocess.Popen(
                [browser] + flags + [url],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True
            )
            return True
    except Exception as e:
        logger.debug("Direct browser launch failed: %s", e)

    # 2. GTK UriLauncher if available and window is provided
    try:
        from gi.repository import Gtk
        if hasattr(Gtk, "UriLauncher"):
            launcher = Gtk.UriLauncher.new(url)
            launcher.launch(parent_window, None, None)
            return True
    except Exception as e:
        logger.debug("Gtk.UriLauncher failed: %s", e)

    # 3. Gio AppInfo launch
    try:
        from gi.repository import Gio
        if Gio.AppInfo.launch_default_for_uri(url, None):
            return True
    except Exception as e:
        logger.debug("Gio.AppInfo failed: %s", e)

    # 4. Fallback xdg-open
    try:
        subprocess.Popen(
            ["xdg-open", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True
        )
        return True
    except Exception as e:
        logger.debug("xdg-open failed: %s", e)

    return False
