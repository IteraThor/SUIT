import threading
from gi.repository import GLib
import subprocess
from core.logger import get_logger

logger = get_logger("async")

def run_async(func, on_done=None, on_error=None, *args, **kwargs):
    """Runs a Python function in a background thread and invokes on_done(result) on GTK main loop."""
    def worker():
        try:
            res = func(*args, **kwargs)
            if on_done:
                GLib.idle_add(on_done, res)
        except Exception as e:
            logger.exception("Async background worker failed")
            if on_error:
                GLib.idle_add(on_error, e)
    threading.Thread(target=worker, daemon=True).start()

def run_command_async(cmd, on_done=None, shell=True):
    """Executes a shell command asynchronously and returns the CompletedProcess to on_done."""
    def worker():
        try:
            res = subprocess.run(cmd, shell=shell, capture_output=True, text=True)
            if on_done:
                GLib.idle_add(on_done, res)
        except Exception as e:
            logger.exception(f"Async command failed: {cmd}")
            if on_done:
                GLib.idle_add(on_done, None)
    threading.Thread(target=worker, daemon=True).start()

def open_browser_url(parent_window=None, url: str = "") -> bool:
    """Reliably opens a URL in the browser, explicitly configuring Wayland flags on Linux."""
    if not url:
        return False
    import shutil
    import os

    # 1. Direct browser binary execution with Wayland ozone flags
    try:
        browser = shutil.which("chromium-browser") or shutil.which("chromium") or shutil.which("google-chrome") or shutil.which("firefox")
        if browser:
            env = os.environ.copy()
            if "WAYLAND_DISPLAY" not in env and os.path.exists(f"/run/user/{os.getuid()}/wayland-0"):
                env["WAYLAND_DISPLAY"] = "wayland-0"
            if "XDG_RUNTIME_DIR" not in env:
                env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
            flags = []
            if "chromium" in browser or "chrome" in browser:
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

