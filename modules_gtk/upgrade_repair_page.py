"""System Repair page: lists what a Fedora upgrade left broken and fixes it step by step ("Fix All")."""

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from core import upgrade_service
from core.i18n import tr
from core.logger import get_logger
from modules_gtk.async_utils import run_async
from modules_gtk.ui_helpers import build_page_shell, create_button_with_icon

logger = get_logger("upgrade_page")

PILL_CLASSES = ("status-pill-pending", "status-pill-checking", "status-pill-running", "status-pill-stopped")


class _StepRow(Adw.ActionRow):
    def __init__(self, problem):
        super().__init__(title=problem.title, subtitle=problem.detail)
        self.problem = problem
        self.set_subtitle_lines(0)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, valign=Gtk.Align.CENTER)
        self.spinner = Adw.Spinner(visible=False)
        self.pill = Gtk.Label()
        box.append(self.spinner)
        box.append(self.pill)
        self.add_suffix(box)
        self.set_state("waiting")

    def set_state(self, state: str, message: str | None = None):
        text, css = {
            "waiting": (tr("To Fix"), "status-pill-pending"),
            "running": (tr("Fixing..."), "status-pill-checking"),
            "done": (tr("Fixed"), "status-pill-running"),
            "failed": (tr("Failed"), "status-pill-stopped"),
        }[state]
        for c in PILL_CLASSES:
            self.pill.remove_css_class(c)
        self.pill.add_css_class(css)
        self.pill.set_label(text)
        self.spinner.set_visible(state == "running")
        if message:
            self.set_subtitle(message)


class UpgradeRepairPage(Adw.NavigationPage):
    def __init__(self, window, problems, changes):
        super().__init__(title=tr("System Repair"), tag="upgrade_repair")
        self.window = window
        self.problems = list(problems)
        self.rows = []
        self.running = False

        scrolled, content = build_page_shell()
        self.set_child(scrolled)

        if changes:
            description = tr("Updated: {changes}. Some SUIT features need repairs to work again.").format(
                changes=" · ".join(changes))
        else:
            description = tr("A system update left some SUIT features broken.")
        self.grp_summary = Adw.PreferencesGroup(title=tr("After the System Update"), description=description)
        self.btn_fix = create_button_with_icon("emblem-system-symbolic", "Fix All", "suggested-action compact-btn",
                                               height=36)
        self.btn_fix.set_valign(Gtk.Align.CENTER)
        self.btn_fix.connect("clicked", lambda *_: self.fix_all())
        self.grp_summary.set_header_suffix(self.btn_fix)
        content.append(self.grp_summary)

        self.grp_steps = Adw.PreferencesGroup(title=tr("Steps"))
        for p in self.problems:
            row = _StepRow(p)
            self.rows.append(row)
            self.grp_steps.add(row)
        content.append(self.grp_steps)

        # Shown when everything ran
        self.grp_done = Adw.PreferencesGroup(visible=False)
        self.row_done = Adw.ActionRow()
        self.row_done.set_subtitle_lines(0)
        self.btn_restart = create_button_with_icon("system-reboot-symbolic", "Restart Now",
                                                   "suggested-action compact-btn", height=36)
        self.btn_restart.set_valign(Gtk.Align.CENTER)
        self.btn_restart.set_visible(False)
        self.btn_restart.connect("clicked", lambda *_: self._restart())
        self.row_done.add_suffix(self.btn_restart)
        self.grp_done.add(self.row_done)
        content.append(self.grp_done)

    # ------------------------------------------------------------------ running

    def fix_all(self):
        if self.running:
            return
        self.running = True
        self.btn_fix.set_sensitive(False)
        self.grp_done.set_visible(False)
        self.results = {}
        self._next(0)

    def _next(self, idx: int):
        # Skip what an earlier "Fix All" run already fixed
        while idx < len(self.rows) and self.rows[idx].problem.id in getattr(self, "_fixed", set()):
            idx += 1
        if idx >= len(self.rows):
            self._finish()
            return
        row = self.rows[idx]
        row.set_state("running", tr("A password prompt opens.") if row.problem.interactive else None)
        logger.info("Repair step %s started", row.problem.id)

        def done(res):
            ok, msg = res if isinstance(res, tuple) else (False, str(res))
            logger.info("Repair step %s: %s (%s)", row.problem.id, "ok" if ok else "failed", msg)
            row.set_state("done" if ok else "failed", msg)
            self.results[row.problem.id] = ok
            self._next(idx + 1)

        def error(e):
            done((False, tr("Error: {msg}").format(msg=e)))

        run_async(row.problem.fix, on_done=done, on_error=error)

    def _finish(self):
        self.running = False
        self._fixed = getattr(self, "_fixed", set()) | {pid for pid, ok in self.results.items() if ok}
        failed = [r.problem for r in self.rows if r.problem.id not in self._fixed]
        relogin = any(r.problem.relogin for r in self.rows if r.problem.id in self._fixed)
        if failed:
            self.row_done.set_title(tr("{count} step(s) could not be fixed").format(count=len(failed)))
            self.row_done.set_subtitle(tr("Check the messages above, then try again. Details are in suit.log."))
            self.btn_fix.set_sensitive(True)
            self._set_fix_label("Try Again")
        else:
            upgrade_service.mark_fixed()
            self.row_done.set_title(tr("Everything is repaired"))
            self.row_done.set_subtitle(
                tr("Restart the computer so GNOME loads the updated extensions.") if relogin else "")
            self.btn_fix.set_visible(False)
        self.btn_restart.set_visible(relogin and not failed)
        self.grp_done.set_visible(True)
        self.window.show_toast(tr("Repair finished.") if not failed else tr("Repair finished with errors."))

    def _set_fix_label(self, text: str):
        box = self.btn_fix.get_child()
        child = box.get_first_child() if box else None
        while child:
            if isinstance(child, Gtk.Label):
                child.set_label(tr(text))
            child = child.get_next_sibling()

    def _restart(self):
        self.btn_restart.set_sensitive(False)

        def done(res):
            ok, msg = res
            if not ok:
                self.btn_restart.set_sensitive(True)
                self.window.show_toast(tr("Restart failed: {error}").format(error=msg))

        run_async(upgrade_service.reboot, on_done=done)
