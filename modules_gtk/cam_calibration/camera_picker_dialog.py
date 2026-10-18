"""Camera picker: with more than 3 cameras connected, choose the 3 that sit on the board."""

from __future__ import annotations
from typing import Callable, List, Sequence

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from core.v4l2_topology import CameraDevice

REQUIRED_CAMERAS = 3


class CameraPickerDialog(Adw.Dialog):
    """Check exactly 3 cameras; Continue hands them to `on_chosen`, closing otherwise calls `on_cancel`."""

    def __init__(self, cameras: Sequence[CameraDevice],
                 on_chosen: Callable[[List[CameraDevice]], None],
                 on_cancel: Callable[[], None]):
        super().__init__(title="Select cameras", content_width=420)
        self._cameras = list(cameras)
        self._on_chosen = on_chosen
        self._on_cancel = on_cancel
        self._done = False
        self._checks: List[Gtk.CheckButton] = []

        group = Adw.PreferencesGroup(
            title=f"{len(self._cameras)} cameras found",
            description=f"Choose the {REQUIRED_CAMERAS} cameras to calibrate.")
        for i, cam in enumerate(self._cameras):
            check = Gtk.CheckButton(active=i < REQUIRED_CAMERAS, valign=Gtk.Align.CENTER)
            check.connect("toggled", lambda _c: self._update_state())
            row = Adw.ActionRow(title=f"Camera {cam.index}", subtitle=cam.dev_node,
                                activatable_widget=check)
            row.add_prefix(check)
            group.add(row)
            self._checks.append(check)

        self._continue = Gtk.Button(label="Continue", css_classes=["suggested-action"])
        self._continue.connect("clicked", self._on_continue)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())

        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        header.pack_start(cancel)
        header.pack_end(self._continue)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=12, margin_bottom=24,
                       margin_start=24, margin_end=24)
        body.append(group)
        toolbar = Adw.ToolbarView(content=body)
        toolbar.add_top_bar(header)
        self.set_child(toolbar)
        self.connect("closed", self._on_closed)
        self._update_state()

    def selected(self) -> List[CameraDevice]:
        return [c for c, chk in zip(self._cameras, self._checks) if chk.get_active()]

    def _update_state(self):
        self._continue.set_sensitive(len(self.selected()) == REQUIRED_CAMERAS)

    def _on_continue(self, _button):
        chosen = self.selected()
        if len(chosen) != REQUIRED_CAMERAS:
            return
        self._done = True
        self.close()
        self._on_chosen(chosen)

    def _on_closed(self, _dialog):
        if not self._done:
            self._done = True
            self._on_cancel()


def needs_camera_choice(cameras: Sequence[CameraDevice]) -> bool:
    return len(cameras) > REQUIRED_CAMERAS


__all__ = ["CameraPickerDialog", "needs_camera_choice", "REQUIRED_CAMERAS"]
