"""Small helpers shared by the Camera Optimizer and Camera Controls pages."""

from __future__ import annotations
import re
from typing import List, Optional

import cv2
import numpy as np

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango


def port_no(port: str) -> str:
    """'pci-0000:00:14.0-usb-0:2:1.0' -> '2'."""
    m = re.search(r"usb-0:([\d.]+):", str(port))
    return m.group(1) if m else "?"


def to_texture(bgr: Optional[np.ndarray], max_side: int = 0) -> Optional[Gdk.Texture]:
    if bgr is None or bgr.size == 0:
        return None
    if max_side and max(bgr.shape[:2]) > max_side:
        s = max_side / max(bgr.shape[:2])
        bgr = cv2.resize(bgr, (int(bgr.shape[1] * s), int(bgr.shape[0] * s)), interpolation=cv2.INTER_AREA)
    bgr = np.ascontiguousarray(bgr)
    h, w = bgr.shape[:2]
    return Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.B8G8R8, GLib.Bytes.new(bgr.tobytes()), w * 3)


def wrap_label(text: str = "", css: Optional[List[str]] = None) -> Gtk.Label:
    lbl = Gtk.Label(label=text, xalign=0, wrap=True)
    for c in css or []:
        lbl.add_css_class(c)
    return lbl


def caption(text: str = "", *css: str, wrap: bool = False) -> Gtk.Label:
    """Small, non-interactive text."""
    lbl = Gtk.Label(label=text, xalign=0, wrap=wrap,
                    ellipsize=Pango.EllipsizeMode.NONE if wrap else Pango.EllipsizeMode.END)
    if wrap:
        lbl.set_max_width_chars(34)  # wrapped text must not widen its column
    lbl.add_css_class("camopt-small")
    for c in css:
        lbl.add_css_class(c)
    return lbl


def status_dot(text: str, kind: str) -> Gtk.Box:
    """● text, coloured by kind (ok / warn / bad / neutral)."""
    box = Gtk.Box(spacing=6)
    dot = Gtk.Label(label="●", css_classes=["camopt-dot", f"camopt-{kind}"])
    box.append(dot)
    box.append(caption(text))
    return box


class PortToggles:
    """One toggle per USB port (wraps an Adw.ToggleGroup, which is final); keeps the selection when the port is
    still there. `building` is True while the toggles are rebuilt, so handlers can ignore those changes."""

    def __init__(self, on_pick):
        self.widget = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        self.building = False
        self.widget.connect("notify::active-name", lambda *_: None if self.building else on_pick())

    @property
    def active(self) -> Optional[str]:
        return self.widget.get_active_name()

    def sync(self, ports: List[str]) -> None:
        g = self.widget
        have = [g.get_toggle(i).get_name() for i in range(g.get_n_toggles())]
        if have == ports:
            return
        current = g.get_active_name()
        self.building = True
        g.remove_all()
        for p in ports:
            g.add(Adw.Toggle(name=p, label=f"Port {port_no(p)}"))
        if ports:
            g.set_active_name(current if current in ports else ports[0])
        self.building = False
