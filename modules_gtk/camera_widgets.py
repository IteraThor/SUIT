"""Widgets shared by the Camera Optimizer and Camera Controls pages: camera strip, chips, Autodarts status."""

from __future__ import annotations
import json
import re
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

import cv2
import numpy as np

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from core.hbvcal import live, oneclick, profiles
from modules_gtk.async_utils import run_async
from core.i18n import tr


def port_no(port: str) -> str:
    """'pci-0000:00:14.0-usb-0:2:1.0' -> '2'."""
    m = re.search(r"usb-0:([\d.]+):", str(port))
    return m.group(1) if m else "?"


def to_texture(bgr: Optional[np.ndarray], max_side: int = 0) -> Optional[Gdk.Texture]:
    if bgr is None or bgr.size == 0:
        return None
    if max_side and max(bgr.shape[:2]) > max_side:
        s = max_side / max(bgr.shape[:2])
        bgr = cv2.resize(bgr, (int(bgr.shape[1] * s), int(bgr.shape[0] * s)),
                         interpolation=cv2.INTER_AREA if s < 0.5 else cv2.INTER_LINEAR)
    bgr = np.ascontiguousarray(bgr)
    h, w = bgr.shape[:2]
    return Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.B8G8R8, GLib.Bytes.new(bgr.tobytes()), w * 3)


def wrap_label(text: str = "", css: Optional[List[str]] = None) -> Gtk.Label:
    lbl = Gtk.Label(label=tr(text), xalign=0, wrap=True)
    for c in css or []:
        lbl.add_css_class(c)
    return lbl


def caption(text: str = "", *css: str, wrap: bool = False) -> Gtk.Label:
    """Small, non-interactive text."""
    lbl = Gtk.Label(label=tr(text), xalign=0, wrap=wrap,
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


# ----------------------------------------------------------------------------------------------
# Camera names and the selection both camera pages share
# ----------------------------------------------------------------------------------------------

NAMES_FILE = profiles.STORE.with_name("names.json")
SELECTED: Dict[str, Optional[str]] = {"port": None}  # the camera picked on either page


def _load_names() -> Dict[str, str]:
    try:
        return json.loads(NAMES_FILE.read_text())
    except (OSError, ValueError):
        return {}


def cam_name(port: str) -> str:
    """The name the user gave this camera, else 'Port n'."""
    return _load_names().get(port) or tr("Port {n}").format(n=port_no(port))


def set_cam_name(port: str, name: str) -> None:
    names = _load_names()
    if name.strip():
        names[port] = name.strip()[:24]
    else:
        names.pop(port, None)
    NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    NAMES_FILE.write_text(json.dumps(names, indent=1))


def ask_rename(window, port: str, on_done: Callable[[], None]) -> None:
    d = Adw.AlertDialog(heading=tr("Name This Camera"), body=tr("Shown instead of “Port {n}” on the camera pages. "
                                                            "Leave empty to go back to the port number.").format(n=port_no(port)))
    e = Gtk.Entry(text=_load_names().get(port, ""), activates_default=True, placeholder_text=tr("e.g. Left"), max_length=24)
    d.set_extra_child(e)
    d.add_response("cancel", tr("Cancel"))
    d.add_response("save", tr("Save"))
    d.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
    d.set_default_response("save")

    def on_resp(_d, resp):
        if resp == "save":
            set_cam_name(port, e.get_text())
            on_done()

    d.connect("response", on_resp)
    d.present(window)


# ----------------------------------------------------------------------------------------------
# Chips
# ----------------------------------------------------------------------------------------------

def chip(text: str = "", kind: str = "neutral") -> Gtk.Label:
    """Small tinted pill (ok / warn / bad / neutral)."""
    lbl = Gtk.Label(label=tr(text), valign=Gtk.Align.CENTER)
    lbl.add_css_class("status-chip")
    lbl.add_css_class(f"chip-{kind}")
    return lbl


def set_chip(lbl: Gtk.Label, text: str, kind: str) -> None:
    lbl.set_label(tr(text))
    for k in ("ok", "warn", "bad", "neutral"):
        lbl.remove_css_class(f"chip-{k}")
    lbl.add_css_class(f"chip-{kind}")


def segmented(options: List[tuple], active, on_change: Callable[[object], None]) -> Adw.ToggleGroup:
    """One-tap alternative to a dropdown for a few options: [(key, label), ...]. `active` may be a key not in options."""
    g = Adw.ToggleGroup(valign=Gtk.Align.CENTER, halign=Gtk.Align.START)
    g.add_css_class("camctl-seg")
    keys = {}
    for key, label in options:
        g.add(Adw.Toggle(name=str(key), label=tr(label)))
        keys[str(key)] = key
    g.set_active_name(str(active) if str(active) in keys else None)
    g.connect("notify::active-name", lambda *_: g.get_active_name() in keys and on_change(keys[g.get_active_name()]))
    return g


# ----------------------------------------------------------------------------------------------
# Camera strip: one card per camera with a thumbnail, shared by both pages
# ----------------------------------------------------------------------------------------------

class Thumb(Gtk.Widget):
    """A picture of a fixed size. (A Gtk.Picture reports a height that grows with the width it is offered, which
    stretched the strip.)"""
    __gtype_name__ = "SuitThumb"

    def __init__(self, width: int, height: int):
        super().__init__(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, css_classes=["cam-thumb"])
        self.set_size_request(width, height)
        self.texture = None

    def set_texture(self, texture) -> None:
        self.texture = texture
        self.queue_draw()

    def do_snapshot(self, snapshot) -> None:
        if self.texture is not None:
            self.texture.snapshot(snapshot, self.get_width(), self.get_height())


class CameraStrip:
    """Row of camera cards that acts as the camera picker. The pick is shared through SELECTED, so both pages
    open on the same camera. Long-press (or right-click) a card to rename the camera."""

    THUMB_PX = 104  # longest side of a thumbnail

    def __init__(self, window, on_pick: Callable[[], None], on_rename: Optional[Callable[[], None]] = None):
        self.window = window
        self.on_pick = on_pick
        self.on_rename = on_rename or on_pick  # pages show the name in more places
        self.widget = Gtk.Box(spacing=8, valign=Gtk.Align.START, halign=Gtk.Align.START)
        self.building = False
        self.cards: Dict[str, dict] = {}
        self.ports: List[str] = []

    @property
    def active(self) -> Optional[str]:
        return SELECTED["port"] if SELECTED["port"] in self.ports else (self.ports[0] if self.ports else None)

    def sync(self, entries: List[dict]) -> None:
        """entries: [{"port": ..., "kind": ok|warn|bad|neutral}]. Keeps the cards (and thumbnails) when unchanged."""
        ports = [e["port"] for e in entries]
        if ports != self.ports:
            self.building = True
            while (c := self.widget.get_first_child()) is not None:
                self.widget.remove(c)
            old, self.cards = self.cards, {}
            self.ports = ports
            for p in ports:
                self._add(p, old.get(p, {}).get("texture"))
            self.building = False
        if SELECTED["port"] not in ports and ports:
            SELECTED["port"] = ports[0]
        for e in entries:
            self.set_kind(e["port"], e.get("kind", "neutral"))
        self._mark()

    def _add(self, port: str, texture) -> None:
        btn = Gtk.Button(css_classes=["cam-card"], tooltip_text=tr("Long-press to rename"), halign=Gtk.Align.START)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        pic = Thumb(self.THUMB_PX, self.THUMB_PX * 3 // 4)
        pic.set_texture(texture)
        box.append(pic)
        row = Gtk.Box(spacing=6)
        dot = Gtk.Label(label="●", css_classes=["camopt-dot", "camopt-neutral"])
        name = Gtk.Label(label=cam_name(port), xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True,
                         css_classes=["heading"])
        row.append(dot)
        row.append(name)
        box.append(row)
        btn.set_child(box)
        btn.connect("clicked", lambda *_: self._pick(port))
        lp = Gtk.GestureLongPress()
        lp.connect("pressed", lambda *_: ask_rename(self.window, port, self.refresh_names))
        btn.add_controller(lp)
        rc = Gtk.GestureClick(button=3)
        rc.connect("pressed", lambda *_: ask_rename(self.window, port, self.refresh_names))
        btn.add_controller(rc)
        self.widget.append(btn)
        self.cards[port] = {"btn": btn, "pic": pic, "dot": dot, "name": name, "texture": texture}

    def _pick(self, port: str) -> None:
        if port == self.active:
            return
        SELECTED["port"] = port
        self._mark()
        self.on_pick()

    def _mark(self) -> None:
        for p, c in self.cards.items():
            (c["btn"].add_css_class if p == self.active else c["btn"].remove_css_class)("selected")

    def refresh_names(self) -> None:
        for p, c in self.cards.items():
            c["name"].set_label(cam_name(p))
        self.on_rename()

    def set_thumb(self, port: str, texture) -> None:
        c = self.cards.get(port)
        if c and texture is not None:
            c["texture"] = texture
            c["pic"].set_texture(texture)

    def set_kind(self, port: str, kind: str) -> None:
        c = self.cards.get(port)
        if c:
            for k in ("ok", "warn", "bad", "neutral"):
                c["dot"].remove_css_class(f"camopt-{k}")
            c["dot"].add_css_class(f"camopt-{kind}")


def fetch_autodarts_frame(dev: str):
    """One picture of this camera from Autodarts' image endpoint (BGR), or None. Never opens the camera itself."""
    import cv2
    import numpy as np
    idx = live.autodarts_index(dev)
    if idx is None:
        return None
    try:
        with urllib.request.urlopen(f"{live.AUTODARTS_API}/api/img/cams/{idx}", timeout=2) as r:
            img = cv2.imdecode(np.frombuffer(r.read(), np.uint8), cv2.IMREAD_COLOR)
    except OSError:
        return None
    return img if img is not None and img.max() > 0 else None  # all black = camera standby


class ThumbPoller:
    """Refreshes the strip thumbnails from Autodarts' pictures every few seconds while the page is open."""

    def __init__(self, strip: CameraStrip, devices: Callable[[], Dict[str, str]], skip: Callable[[], Optional[str]],
                 every_ms: int = 4000):
        self.strip, self.devices, self.skip, self.every_ms = strip, devices, skip, every_ms
        self.timer = 0
        self.busy = False

    def start(self) -> None:
        if not self.timer:
            self.timer = GLib.timeout_add(self.every_ms, self._tick)
            self._tick()

    def kick(self) -> None:
        """Fetch now (the camera list has just arrived)."""
        if self.timer:
            self._tick()

    def stop(self) -> None:
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0

    def _tick(self) -> bool:
        if self.busy or oneclick.JOB.running or not self.strip.widget.get_mapped():
            return True
        self.busy = True
        targets = {p: d for p, d in self.devices().items() if p != self.skip()}

        def work():
            return {p: to_texture(fetch_autodarts_frame(d), CameraStrip.THUMB_PX) for p, d in targets.items()}

        def done(res):
            self.busy = False
            for p, t in res.items():
                self.strip.set_thumb(p, t)

        run_async(work, done, lambda e: setattr(self, "busy", False))
        return True


# ----------------------------------------------------------------------------------------------
# Autodarts status with start / restart / stop
# ----------------------------------------------------------------------------------------------

class AutodartsStatus:
    """'● Autodarts running ▾': a status pill that opens start / restart / stop."""

    def __init__(self, window):
        self.window = window
        self.state = ""
        self.widget = Gtk.MenuButton(valign=Gtk.Align.CENTER, css_classes=["flat", "compact-btn"])
        self.dot = Gtk.Label(label="●", css_classes=["camopt-dot"])
        self.text = caption("Autodarts…")
        content = Gtk.Box(spacing=6)
        content.append(self.dot)
        content.append(self.text)
        content.append(Gtk.Image.new_from_icon_name("pan-down-symbolic"))
        self.widget.set_child(content)
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.buttons = {}
        for action, label in (("start", "Start Autodarts"), ("restart", "Restart Autodarts"), ("stop", "Stop Autodarts")):
            b = Gtk.Button(label=tr(label), css_classes=["flat"])
            b.connect("clicked", lambda _b, a=action: self._do(a, pop))
            box.append(b)
            self.buttons[action] = b
        pop.set_child(box)
        self.widget.set_popover(pop)

    def refresh(self) -> None:
        run_async(lambda: oneclick.systemctl("is-active"), self.show)

    def show(self, state: str) -> None:
        self.state = state
        ok = state == "active"
        if ok:
            self.text.set_label(tr("Autodarts running"))
        elif state:
            self.text.set_label(tr("Autodarts {state}").format(state=tr(state)))
        else:
            self.text.set_label(tr("Autodarts not found"))
        for k in ("ok", "neutral"):
            self.dot.remove_css_class(f"camopt-{k}")
        self.dot.add_css_class("camopt-ok" if ok else "camopt-neutral")
        self.buttons["start"].set_visible(not ok)
        self.buttons["restart"].set_visible(ok)
        self.buttons["stop"].set_visible(ok)
        self.widget.set_sensitive(not oneclick.JOB.running)

    def _do(self, action: str, pop: Gtk.Popover) -> None:
        pop.popdown()
        self.window.show_toast({"start": "Starting Autodarts…", "restart": "Restarting Autodarts…",
                                "stop": "Stopping Autodarts…"}[action])
        run_async(oneclick.systemctl, lambda _r: GLib.timeout_add(3000, lambda: self.refresh() or False), None, action)
