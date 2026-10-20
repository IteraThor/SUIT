"""Camera brand picker: the optimizer's measured numbers differ per camera brand, so ask which one."""

from __future__ import annotations
from typing import Callable, List, Optional, Sequence

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from core.camera_probe import usb_identity
from core.camera_profiles import PROFILES, CameraProfile, load_selected, save_selected
from core.v4l2_topology import CameraDevice


def reported_brands(cameras: Sequence[CameraDevice]) -> List[str]:
    """What the connected cameras report over USB ("HBVCAM Camera (0bdc:2710)"), one entry per kind."""
    seen: List[str] = []
    for cam in cameras:
        try:
            ident = usb_identity(cam.dev_node)
        except Exception:
            continue
        if not ident:
            continue
        label = f"{ident.get('manufacturer') or ident.get('product') or 'Unknown'} ({ident.get('idVendor')}:{ident.get('idProduct')})"
        if label not in seen:
            seen.append(label)
    return seen


class CameraProfileDialog(Adw.Dialog):
    """Pick one camera brand; Continue saves it and hands it to `on_chosen`, closing calls `on_cancel`."""

    def __init__(self, cameras: Sequence[CameraDevice],
                 on_chosen: Callable[[CameraProfile], None],
                 on_cancel: Callable[[], None]):
        super().__init__(title="Camera brand", content_width=420)
        self._on_chosen = on_chosen
        self._on_cancel = on_cancel
        self._done = False
        self._checks: List[tuple] = []

        reported = reported_brands(cameras)
        description = "The optimizer is tuned to each camera brand."
        if reported:
            description += f"\nYour cameras report: {', '.join(reported)}"
        group = Adw.PreferencesGroup(title="Which cameras do you use?", description=description)
        current = load_selected()
        first: Optional[Gtk.CheckButton] = None
        for profile in PROFILES.values():
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            if first is None:
                first = check
            else:
                check.set_group(first)
            check.set_active(current is None and check is first or current is profile)
            row = Adw.ActionRow(title=profile.name, subtitle=profile.description, activatable_widget=check)
            row.add_prefix(check)
            group.add(row)
            self._checks.append((profile, check))

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

    def selected(self) -> Optional[CameraProfile]:
        return next((p for p, chk in self._checks if chk.get_active()), None)

    def _on_continue(self, _button):
        profile = self.selected()
        if profile is None:
            return
        save_selected(profile)
        self._done = True
        self.close()
        self._on_chosen(profile)

    def _on_closed(self, _dialog):
        if not self._done:
            self._done = True
            self._on_cancel()


__all__ = ["CameraProfileDialog", "reported_brands"]
