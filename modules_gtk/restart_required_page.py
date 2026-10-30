"""Placeholder for pages that an update removed, shown only by a SUIT process started before that update.

The old process keeps its old menu and imports page modules lazily from disk; the removed modules now point
here (cam_calibration_view.py, camera_manual_view.py), so the old buttons ask for a restart instead of
failing with an import error. Safe to delete together with those two shims in a later release.
"""

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk
from core.i18n import tr


class RestartRequiredPage(Adw.NavigationPage):
    def __init__(self, window, title: str = "Restart Required", tag: str = "restart_required"):
        super().__init__(title=tr(title), tag=tag)
        page = Adw.StatusPage(icon_name="view-refresh-symbolic", title=tr("Restart SUIT to Finish the Update"),
                              description=tr("This tool was replaced by Camera Controls and the new Camera Optimizer."))
        btn = Gtk.Button(label=tr("Restart SUIT"), halign=Gtk.Align.CENTER)
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")
        btn.connect("clicked", lambda *_: self._restart())
        page.set_child(btn)
        self.set_child(page)

    def _restart(self):
        from core.update_service import UpdateService
        UpdateService.restart_application()
