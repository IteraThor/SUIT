#!/usr/bin/env python3
"""Hand-over from the Python SUIT to the Rust SUIT.

The Python SUIT's update button pulls this tree and then restarts `python3 app_gtk.py`, and its menu launcher
runs the same file. This script builds the Rust SUIT once with install.sh (which also rewrites the launchers),
then replaces itself with it. If the build fails it can go back to the last Python version.
"""
import locale
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
LAUNCH = PROJECT_DIR / "suit-launch.sh"
BINARY = PROJECT_DIR / "target" / "release" / "suit"
LOG = PROJECT_DIR / "suit-update.log"
# Last commit of the Python SUIT on main, for "Back to the Previous Version"
LAST_PYTHON_COMMIT = "a7c9ff6483f29bd3060af824695eaa711a29f97d"

GERMAN = (locale.getlocale()[0] or os.environ.get("LANG", "")).startswith("de")
TEXTS = {
    "title": ("Updating SUIT", "SUIT wird aktualisiert"),
    "body": (
        "SUIT is switching to its new version. This takes a few minutes and needs an internet connection.",
        "SUIT wechselt auf die neue Version. Das dauert ein paar Minuten und braucht eine Internetverbindung.",
    ),
    "failed": (
        "The update could not be finished. See the details below.",
        "Die Aktualisierung konnte nicht abgeschlossen werden. Details siehe unten.",
    ),
    "details": ("Details", "Details"),
    "retry": ("Try Again", "Erneut versuchen"),
    "back": ("Back to the Previous Version", "Zurück zur vorherigen Version"),
}


def tr(key):
    return TEXTS[key][1 if GERMAN else 0]


def binary_works():
    try:
        return subprocess.run([str(BINARY), "--version"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def start_rust_suit():
    os.execv(str(LAUNCH), [str(LAUNCH)] + sys.argv[1:])


def back_to_python():
    """Reset to the last Python commit and start it; returns git's error if the reset fails."""
    res = subprocess.run(["git", "reset", "--hard", LAST_PYTHON_COMMIT], cwd=str(PROJECT_DIR),
                         capture_output=True, text=True)
    if res.returncode != 0:
        return res.stderr
    # install.sh may already have pointed the menu launcher at suit-launch.sh, which the Python tree lacks
    subprocess.run([sys.executable, str(PROJECT_DIR / "create_launcher.py")], cwd=str(PROJECT_DIR))
    os.execv(sys.executable, [sys.executable, str(PROJECT_DIR / "app_gtk.py")] + sys.argv[1:])


def remove_python_leftovers():
    """__pycache__ folders stay behind as untracked files after the update; drop them and emptied folders."""
    for d in ("core", "modules_gtk", "kiosk_extension"):
        root = PROJECT_DIR / d
        if not root.is_dir():
            continue
        for cache in sorted(root.rglob("__pycache__"), reverse=True):
            shutil.rmtree(cache, ignore_errors=True)
        for sub in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True) + [root]:
            try:
                sub.rmdir()
            except OSError:
                pass


def run_window():
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, GLib, Gtk

    GLib.set_prgname("de.iterathor.suit.gtk")
    app = Adw.Application(application_id="de.iterathor.suit.gtk.update", flags=Gio.ApplicationFlags.NON_UNIQUE)

    def on_activate(app):
        win = Adw.ApplicationWindow(application=app, title=tr("title"), default_width=640, default_height=460)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=24, margin_bottom=24,
                      margin_start=24, margin_end=24)
        heading = Gtk.Label(label=tr("title"), css_classes=["title-2"])
        status = Gtk.Label(label=tr("body"), wrap=True, justify=Gtk.Justification.CENTER)
        spinner = Gtk.Spinner(spinning=True, height_request=32)
        view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False, wrap_mode=Gtk.WrapMode.CHAR)
        scroller = Gtk.ScrolledWindow(child=view, vexpand=True, min_content_height=200)
        expander = Gtk.Expander(label=tr("details"), child=scroller, vexpand=True)
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, visible=False)
        retry = Gtk.Button(label=tr("retry"), css_classes=["suggested-action", "pill"])
        back = Gtk.Button(label=tr("back"), css_classes=["pill"])
        buttons.append(retry)
        buttons.append(back)
        for w in (heading, status, spinner, expander, buttons):
            box.append(w)
        win.set_content(box)

        buf = view.get_buffer()

        def append(text):
            buf.insert(buf.get_end_iter(), text)
            view.scroll_to_mark(buf.get_insert(), 0, False, 0, 0)
            return False

        def finished(ok):
            if ok and binary_works():
                start_rust_suit()
            spinner.set_spinning(False)
            status.set_label(tr("failed"))
            expander.set_expanded(True)
            buttons.set_visible(True)
            return False

        def worker():
            remove_python_leftovers()
            with open(LOG, "w", buffering=1) as log:  # line-buffered: complete up to the last line if cut off
                try:
                    proc = subprocess.Popen(["bash", str(PROJECT_DIR / "install.sh")], cwd=str(PROJECT_DIR),
                                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True, errors="replace")
                    for line in proc.stdout:
                        log.write(line)
                        GLib.idle_add(append, line)
                    ok = proc.wait() == 0
                except Exception as e:
                    log.write(f"{e}\n")
                    GLib.idle_add(append, f"{e}\n")
                    ok = False
            GLib.idle_add(finished, ok)

        def start(*_):
            buttons.set_visible(False)
            spinner.set_spinning(True)
            status.set_label(tr("body"))
            threading.Thread(target=worker, daemon=True).start()

        def go_back(*_):
            append(back_to_python())

        retry.connect("clicked", start)
        back.connect("clicked", go_back)
        win.present()
        start()

    app.connect("activate", on_activate)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    # Always run install.sh, even when an older build exists (after going back and updating again): it builds
    # the current code and points the launcher away from this file, so this runs once per switch only
    sys.exit(run_window())
