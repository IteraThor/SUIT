"""Camera Controls page: live view and every control of each camera, for any UVC camera (core.hbvcal.live).

Camera strip on top (shared with the Camera Optimizer), live view with metric chips and overlays on the left,
collapsible control sections on the right. Every row has - / + steppers, a tappable value and a revert button.

Changes apply immediately. A camera's settings are kept (re-applied by the keeper service after Autodarts
restarts and at boot) once it has a profile: from the Camera Optimizer, from "Keep these settings", or from a
loaded preset. On confirmed HBV OV2710 cameras the hidden JPEG quality and sensor gain are adjustable too.
"""

from __future__ import annotations
import time
from typing import Dict, List, Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango

import cv2
import numpy as np

from core.hbvcal import board, live, oneclick, profiles
from core.hbvcal.daemon import cameras
from core.logger import get_logger
from modules_gtk.async_utils import run_async
from modules_gtk.camera_widgets import (AutodartsStatus, CameraStrip, ThumbPoller, ask_rename, caption, cam_name, chip,
                                        port_no, segmented, set_chip, to_texture)

logger = get_logger("camera_controls_view")

# Sensor analog gain register 0x350B: bits 4-6 = x2 steps, bits 0-3 = n/16
SENSOR_STEPS = [0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x30, 0x38, 0x70]
GROUPS = [("exposure", "Exposure"), ("tone", "Tone"), ("detail", "Detail"), ("color", "Color"), ("stream", "Stream")]
SEGMENT_MAX = 4  # a select with this many options or fewer is shown as one-tap segments, else as a dropdown
UNDO_MAX = 20
# why a control is greyed out
OFF_REASON = {"exposure_time_absolute": "Set Mode to Manual to change the exposure time.",
              "sensor_gain": "Set Mode to Manual to change the sensor gain.",
              "white_balance_temperature": "Turn off Auto white balance to set a temperature."}
# a camera takes these first, so the dependent controls accept their values
APPLY_FIRST = ("auto_exposure", "white_balance_automatic")
# name -> (group, label, kind, options/formatter, disabled-when)
SPEC = {
    "auto_exposure": ("exposure", "Mode", "select", {1: "Manual", 3: "Automatic"}, None),
    "exposure_time_absolute": ("exposure", "Exposure time", "range", None,
                               lambda vals: vals.get("auto_exposure") != 1),
    "gain": ("exposure", "Digital gain", "range", None, None),
    "sensor_gain": ("exposure", "Sensor gain", "sensor", None, lambda vals: vals.get("auto_exposure") != 1),
    "brightness": ("tone", "Brightness", "range", None, None),
    "contrast": ("tone", "Contrast", "range", None, None),
    "gamma": ("tone", "Gamma", "range", None, None),
    "saturation": ("tone", "Saturation", "range", None, None),
    "hue": ("tone", "Hue", "range", None, None),
    "sharpness": ("detail", "Sharpening", "range", None, None),
    "backlight_compensation": ("detail", "Backlight", "range", None, None),
    "white_balance_automatic": ("color", "Auto white balance", "switch", None, None),
    "white_balance_temperature": ("color", "Temperature", "range", lambda v: f"{v} K",
                                  lambda vals: vals.get("white_balance_automatic") == 1),
    "q": ("stream", "JPEG compression", "q", None, None),
    "power_line_frequency": ("stream", "Anti-flicker", "select", {0: "Off", 1: "50 Hz", 2: "60 Hz"}, None),
}


def sensor_gain(code: int) -> float:
    return (1 << sum(1 for b in (1, 2, 4) if (code >> 4) & b)) * (1 + (code & 15) / 16)


def gain_label(code: int) -> str:
    return f"{sensor_gain(code):.2f}".rstrip("0").rstrip(".") + "×"


def ask_save_preset(window, name: str = "", on_saved=None) -> None:
    """Ask for a name and save every camera's settings as a preset (also used after an optimization)."""
    d = Adw.AlertDialog(heading="Save as Preset",
                        body="Stores the settings of every camera under this name. Load it later from "
                             "Camera Controls → Presets.")
    e = Gtk.Entry(text=name, activates_default=True, placeholder_text="e.g. Evening light")
    d.set_extra_child(e)
    d.add_response("cancel", "Cancel")
    d.add_response("save", "Save")
    d.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
    d.set_default_response("save")

    def on_resp(_d, resp):
        new = e.get_text().strip()
        if resp != "save" or not new:
            return
        replaced = new in profiles.load_presets()

        def done(_r):
            window.show_toast(f"Preset “{new}” {'overwritten' if replaced else 'saved'}")
            if on_saved:
                on_saved()

        run_async(live.save_preset, done, lambda err: window.show_toast(str(err)), new)

    d.connect("response", on_resp)
    d.present(window)


def calibrated_value(info: dict, name: str):
    k = info.get("calibrated")
    if not k:
        return None
    if name == "q":
        return k.get("mjpeg_q")
    if name == "sensor_gain":
        return (k.get("sensor") or {}).get("0x350B")
    return (k.get("uvc") or {}).get(name)


class CameraControlsPage(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Camera Controls", tag="camera_controls")
        self.window = window
        self.cams: List[dict] = []
        self.cur = 0
        self._building = False
        self._pending: Dict[str, tuple] = {}  # debounced slider writes: control -> (GLib source id, args)
        self._origin: Dict[str, int] = {}  # value a slider had before the current drag (for undo)
        self._undo: List[tuple] = []  # (device, layer, name, old value)
        self._diffs: Dict[str, bool] = {}  # control -> differs from the calibration
        self._sec_dots: Dict[str, Gtk.Label] = {}
        self._open = {"exposure"}  # expanded sections survive rebuilds
        self._live_timer = 0
        self._live_tick = 0
        self._scores_busy = False
        self._shown = None
        self._painted = None  # the picture on screen (stays put while frozen)
        self._frozen = False
        self._ident = 0  # distinct pictures seen, for the frame rate of Autodarts' picture
        self._rate = (0, 0, time.time(), 0)  # (frame_count, ident, time, fps)
        self._overlays = {"zebra": False, "hist": False, "mask": False}
        self._masks: Dict[tuple, Optional[np.ndarray]] = {}
        self._tag_text = ""

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_margin_top(8)
        outer.set_margin_bottom(10)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        self.set_child(outer)

        head = Gtk.CenterBox(vexpand=False, valign=Gtk.Align.START)  # explicit: a child must not stretch it
        self.strip = CameraStrip(window, self._on_cam_pick, self._on_cam_rename)
        head.set_start_widget(self.strip.widget)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, halign=Gtk.Align.END)
        nav = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        btn_presets = Gtk.Button()
        btn_presets.set_child(Adw.ButtonContent(icon_name="view-list-symbolic", label="Presets"))
        btn_presets.set_tooltip_text("Save or load the settings of all cameras at once")
        btn_presets.connect("clicked", lambda *_: self._show_presets())
        nav.append(btn_presets)
        btn_opt = Gtk.Button()
        btn_opt.set_child(Adw.ButtonContent(icon_name="applications-science-symbolic", label="Optimizer"))
        btn_opt.set_tooltip_text("One-click calibration of all cameras")
        btn_opt.connect("clicked", lambda *_: self.window.open_camera_optimizer_page())
        nav.append(btn_opt)
        side.append(nav)
        self.autodarts = AutodartsStatus(window)
        self.autodarts.widget.set_halign(Gtk.Align.END)
        side.append(self.autodarts.widget)
        head.set_end_widget(side)
        outer.append(head)
        self.thumbs = ThumbPoller(self.strip, lambda: {c["port"]: c["device"] for c in self.cams},
                                  lambda: (self._cur() or {}).get("port"))

        cols = Gtk.Box(spacing=12, vexpand=True)
        outer.append(cols)

        # --- left: live view with overlays, metric chips, brightness check ---
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        self.live_picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True, vexpand=True)
        self.live_picture.set_size_request(280, 200)
        self.live_picture.add_css_class("camopt-frame")
        ov = Gtk.Overlay(child=self.live_picture, vexpand=True)
        self.lbl_tag = Gtk.Label(label="", halign=Gtk.Align.START, valign=Gtk.Align.START, css_classes=["camopt-tag"])
        self.lbl_tag.set_margin_start(10)
        self.lbl_tag.set_margin_top(10)
        ov.add_overlay(self.lbl_tag)
        tools = Gtk.Box(spacing=6, halign=Gtk.Align.END, valign=Gtk.Align.START)
        tools.set_margin_end(10)
        tools.set_margin_top(10)
        pop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for key, label, tip in (("zebra", "Zebra: mark clipped areas", "Pure white areas are marked in red"),
                                ("hist", "Histogram", "Brightness distribution of the picture"),
                                ("mask", "Board mask", "Dim everything outside the board area used for the scores")):
            cb = Gtk.CheckButton(label=label, tooltip_text=tip)
            cb.connect("toggled", lambda b, k=key: self._set_overlay(k, b.get_active()))
            pop_box.append(cb)
        pop_box.set_margin_top(6)
        pop_box.set_margin_bottom(6)
        pop_box.set_margin_start(6)
        pop_box.set_margin_end(6)
        btn_ov = Gtk.MenuButton(label="Overlays", popover=Gtk.Popover(child=pop_box), css_classes=["osd", "compact-btn"],
                                tooltip_text="Zebra, histogram and board mask on the live picture")
        tools.append(btn_ov)
        self.btn_freeze = Gtk.ToggleButton(icon_name="media-playback-pause-symbolic", tooltip_text="Freeze the picture",
                                           css_classes=["osd", "compact-btn"])
        self.btn_freeze.connect("toggled", lambda b: self._set_frozen(b.get_active()))
        tools.append(self.btn_freeze)
        self.btn_wide = Gtk.ToggleButton(icon_name="view-fullscreen-symbolic", tooltip_text="Hide the controls: bigger picture",
                                         css_classes=["osd", "compact-btn"])
        self.btn_wide.connect("toggled", lambda b: self.panel.set_visible(not b.get_active()))
        tools.append(self.btn_wide)
        ov.add_overlay(tools)
        # the picture's own size must not decide the page height (it would push the strip and panel around)
        left.append(Gtk.ScrolledWindow(child=ov, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                       vscrollbar_policy=Gtk.PolicyType.NEVER))

        info = Gtk.Box(spacing=8)
        self.chips = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, hexpand=True, max_children_per_line=8,
                                 column_spacing=6, row_spacing=4, valign=Gtk.Align.CENTER)
        self.chip_white, self.chip_clip, self.chip_noise, self.chip_kb = (chip() for _ in range(4))
        self.chip_note = chip()  # a message instead of numbers (standby, no calibration…)
        for w in (self.chip_white, self.chip_clip, self.chip_noise, self.chip_kb, self.chip_note):
            w.set_visible(False)
            self.chips.append(w)
        info.append(self.chips)
        self.check_box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.lbl_check = caption("")
        self.check_box.append(self.lbl_check)
        self.btn_trim = Gtk.Button(label="Fix", visible=False, valign=Gtk.Align.CENTER, css_classes=["compact-btn"],
                                   tooltip_text="Trim the digital gain back to the calibrated brightness")
        self.btn_trim.connect("clicked", lambda *_: self._recheck(True))
        self.check_box.append(self.btn_trim)
        self.btn_check = Gtk.Button(label="Check brightness", valign=Gtk.Align.CENTER,
                                    tooltip_text="Compare the brightness with the calibration")
        self.btn_check.add_css_class("compact-btn")
        self.btn_check.connect("clicked", lambda *_: self._recheck(False))
        self.check_box.append(self.btn_check)
        info.append(self.check_box)
        left.append(info)
        cols.append(left)

        # --- right: camera header, collapsible sections, footer ---
        self.panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.panel.set_size_request(500, -1)
        self.panel.set_hexpand(False)
        self.panel.add_css_class("card")
        ph = Gtk.Box(spacing=8)
        ph.set_margin_top(10)
        ph.set_margin_bottom(6)
        ph.set_margin_start(16)
        ph.set_margin_end(10)
        self.lbl_cam = Gtk.Label(label="", xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["title-4"])
        ph.append(self.lbl_cam)
        btn_name = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Name this camera",
                              css_classes=["flat"], valign=Gtk.Align.CENTER)
        btn_name.connect("clicked", lambda *_: self._cur() and ask_rename(self.window, self._cur()["port"],
                                                                          self.strip.refresh_names))
        ph.append(btn_name)
        ph.append(Gtk.Box(hexpand=True))
        self.btn_copy = Gtk.Button(label="Copy to all", valign=Gtk.Align.CENTER, css_classes=["compact-btn"],
                                   tooltip_text="Give the other cameras the same settings")
        self.btn_copy.connect("clicked", lambda *_: self._ask_copy_to_all())
        ph.append(self.btn_copy)
        self.panel.append(ph)

        keep_box = Gtk.Box(spacing=10)
        keep_box.set_margin_start(16)
        keep_box.set_margin_end(16)
        keep_box.set_margin_bottom(8)
        self.sw_keep = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.sw_keep.connect("notify::active", lambda sw, _p: self._on_keep(sw.get_active()))
        keep_box.append(self.sw_keep)
        keep_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        keep_txt.append(Gtk.Label(label="Keep these settings", xalign=0, css_classes=["camctl-label"]))
        self.lbl_keep = caption("")
        keep_txt.append(self.lbl_keep)
        keep_box.append(keep_txt)
        self.panel.append(keep_box)
        self.panel.append(Gtk.Separator())

        # fades and scrolls when the window is too small for every open section
        ctl_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, overlay_scrolling=False)
        ctl_scroll.set_kinetic_scrolling(True)
        self.ctl_scroll = ctl_scroll
        self.controls_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        ctl_scroll.set_child(self.controls_box)
        self.panel.append(ctl_scroll)
        self.panel.append(Gtk.Separator())
        foot = Gtk.Box(spacing=8)
        foot.set_margin_top(8)
        foot.set_margin_bottom(8)
        foot.set_margin_start(16)
        foot.set_margin_end(10)
        self.lbl_marks = caption("")
        self.lbl_marks.set_hexpand(True)
        foot.append(self.lbl_marks)
        self.btn_undo = Gtk.Button(label="Undo", sensitive=False, css_classes=["compact-btn"],
                                   tooltip_text="Take back the last change")
        self.btn_undo.connect("clicked", lambda *_: self._undo_last())
        foot.append(self.btn_undo)
        self.btn_reset = Gtk.Button(label="Reset to defaults", css_classes=["compact-btn"])
        self.btn_reset.connect("clicked", lambda *_: self._reset_cam())
        foot.append(self.btn_reset)
        self.panel.append(foot)
        cols.append(self.panel)

    # ------------------------------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------------------------------

    def on_page_opened(self):
        self._load()
        self.autodarts.refresh()
        self.thumbs.start()

    def on_page_closed(self):
        self._stop_live()
        self.thumbs.stop()

    def _cur(self) -> Optional[dict]:
        return self.cams[self.cur] if self.cams and self.cur < len(self.cams) else None

    def _cur_dev(self) -> Optional[str]:
        c = self._cur()
        return c["device"] if c else None

    def _load(self):
        def done(cams):
            prev = self._cur_dev()
            self.cams = cams
            self.strip.sync([{"port": c["port"], "kind": "bad" if c.get("error") else
                              "ok" if c.get("profile") is not None else "neutral"} for c in cams])
            names = [c["port"] for c in cams]
            self.cur = names.index(self.strip.active) if self.strip.active in names else 0
            if self._cur_dev() != prev:  # another camera: its first picture shows even when frozen
                self._shown = self._painted = None
            self._render_controls()
            self._start_live()
            self.thumbs.kick()

        run_async(lambda: [live.camera_info(d) for d in cameras()], on_done=done)

    def _later(self, ms: int):
        GLib.timeout_add(ms, lambda: self._load() and False)

    def _on_cam_rename(self):
        c = self._cur()
        self.lbl_cam.set_label(cam_name(c["port"]) if c else "")

    def _on_cam_pick(self):
        self._flush_pending()  # slider moves still waiting belong to the camera shown so far
        self.lbl_check.set_label("")
        self.btn_trim.set_visible(False)
        self.live_picture.set_paintable(None)
        self._shown = None
        self._painted = None  # even when frozen: show the new camera's first picture
        self._undo.clear()
        self._origin.clear()
        self._sync_undo()
        self._load()

    # ------------------------------------------------------------------------------------------
    # Controls
    # ------------------------------------------------------------------------------------------

    def _render_controls(self):
        self._building = True
        vadj = self.ctl_scroll.get_vadjustment()
        scroll = vadj.get_value()
        while (child := self.controls_box.get_first_child()) is not None:
            self.controls_box.remove(child)
        self._diffs.clear()
        self._sec_dots.clear()
        c = self._cur()
        if c is None:
            self.controls_box.append(Gtk.Label(label="No cameras connected", css_classes=["camopt-empty"], vexpand=True))
            for w in (self.btn_reset, self.check_box, self.lbl_marks, self.btn_copy):
                w.set_visible(False)
            self.sw_keep.set_sensitive(False)
            self.lbl_keep.set_label("")
            self.lbl_cam.set_label("")
            self._building = False
            return
        self.lbl_cam.set_label(cam_name(c["port"]))
        kept, optimized = c.get("profile") is not None, c.get("optimized")
        self.sw_keep.set_sensitive(not c.get("error"))
        self.sw_keep.set_active(kept)
        if optimized:
            self.lbl_keep.set_label("Optimized · re-applied after restarts")
        elif kept:
            self.lbl_keep.set_label("Re-applied after Autodarts restarts")
        else:
            self.lbl_keep.set_label("Off · Autodarts resets changes on restart")
        self.lbl_keep.remove_css_class("camopt-warn")
        if not kept:
            self.lbl_keep.add_css_class("camopt-warn")
        has_cal = bool(c.get("calibrated"))
        self.btn_reset.set_label("Reset to calibrated" if has_cal else "Reset to defaults")
        self.btn_reset.set_visible(not c.get("error"))
        self.btn_copy.set_visible(len(self.cams) > 1 and not c.get("error"))
        self.lbl_marks.set_visible(True)
        self.check_box.set_visible(bool(optimized))
        if c.get("error"):
            self.controls_box.append(caption(f"Camera not reachable: {c['error']}", wrap=True))
            self.lbl_marks.set_label("")
            self._building = False
            return

        vals = {n: v["value"] for n, v in c["controls"].items()}
        if c.get("q") is not None:
            vals["q"] = c["q"]
        if c.get("sensor_gain") is not None:
            vals["sensor_gain"] = c["sensor_gain"]
        titles = dict(GROUPS)
        for g, _title in GROUPS:
            names = [n for n, s in SPEC.items() if s[0] == g and vals.get(n) is not None]
            hint = self._hidden_hint(c) if g == "stream" else None
            if not names and hint is None:
                continue
            self.controls_box.append(self._section(g, titles[g], names, hint, c, vals))
        self._building = False
        self._update_marks()
        GLib.idle_add(lambda: vadj.set_value(scroll) and False)

    def _summary(self, g: str, c: dict, vals: dict) -> str:
        if g == "exposure":
            if vals.get("auto_exposure") != 1:
                return "Automatic"
            t = vals.get("exposure_time_absolute")
            return "Manual" + (f" · {self._exposure_text(c, t)}" if t is not None else "")
        if g == "color":
            if vals.get("white_balance_automatic") == 1 or "white_balance_temperature" not in vals:
                return "Auto white balance"
            return f"Manual · {vals['white_balance_temperature']} K"
        if g == "stream":
            parts = [f"JPEG {vals['q']}"] if vals.get("q") is not None else []
            pl = SPEC["power_line_frequency"][3].get(vals.get("power_line_frequency"))
            return " · ".join(parts + ([pl] if pl else []))
        return ""

    @staticmethod
    def _exposure_text(c: dict, v: int) -> str:
        return f"{v * 0.05:.1f} ms" if c.get("hbv") else str(v)  # HBV: 50 µs units (measured)

    def _section(self, g: str, title: str, names: List[str], hint, c: dict, vals: dict) -> Gtk.Widget:
        sec = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sec.add_css_class("camctl-sec")
        head = Gtk.Button(css_classes=["flat", "camctl-sechead"])
        hb = Gtk.Box(spacing=8)
        hb.append(Gtk.Label(label=title, xalign=0, css_classes=["heading"]))
        dot = Gtk.Label(label="●", visible=False, css_classes=["camopt-dot", "camopt-warn"],
                        tooltip_text="Differs from the calibration")
        self._sec_dots[g] = dot
        hb.append(dot)
        hb.append(Gtk.Label(label=self._summary(g, c, vals), xalign=1, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                            css_classes=["camctl-sec-sum"]))
        chev = Gtk.Image.new_from_icon_name("pan-down-symbolic" if g in self._open else "pan-end-symbolic")
        hb.append(chev)
        head.set_child(hb)
        sec.append(head)
        rev = Gtk.Revealer(reveal_child=g in self._open, transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.add_css_class("camctl-grid")
        body.set_margin_bottom(6)
        for n in names:
            self._control_row(body, c, n, vals)
        if hint is not None:
            hint.set_margin_start(16)
            hint.set_margin_end(16)
            body.append(hint)
        rev.set_child(body)
        sec.append(rev)

        def toggle(*_):
            opened = not rev.get_reveal_child()
            rev.set_reveal_child(opened)
            chev.set_from_icon_name("pan-down-symbolic" if opened else "pan-end-symbolic")
            (self._open.add if opened else self._open.discard)(g)

        head.connect("clicked", toggle)
        return sec

    def _hidden_hint(self, c: dict) -> Optional[Gtk.Widget]:
        """HBV camera without its hidden controls: say why, and how to get them."""
        if not c.get("hbv") or c.get("hidden"):
            return None
        box = Gtk.Box(spacing=6)
        if c.get("hidden_error"):
            box.append(caption(f"JPEG quality and sensor gain not available: {c['hidden_error']}", wrap=True))
            return box
        lbl = caption("JPEG quality and sensor gain: confirm HBV cameras first", wrap=True)
        lbl.set_hexpand(True)
        box.append(lbl)
        btn = Gtk.Button(label="Optimizer", valign=Gtk.Align.CENTER, css_classes=["compact-btn"])
        btn.connect("clicked", lambda *_: self.window.open_camera_optimizer_page())
        box.append(btn)
        return box

    def _mark(self, n: str, differs: bool):
        self._diffs[n] = differs
        self._update_marks()

    def _update_marks(self):
        """Section dots and the footer count: how many controls differ from the calibration."""
        c = self._cur()
        if not c or not c.get("calibrated"):
            self.lbl_marks.set_label("")
            for d in self._sec_dots.values():
                d.set_visible(False)
            return
        per_group = {g: any(v for n, v in self._diffs.items() if SPEC[n][0] == g) for g, _t in GROUPS}
        for g, dot in self._sec_dots.items():
            dot.set_visible(per_group.get(g, False))
        n = sum(1 for v in self._diffs.values() if v)
        self.lbl_marks.set_label(f"↺ {n} differ{'s' if n == 1 else ''} from the calibration" if n else
                                 "Matches the calibration")

    def _control_row(self, body: Gtk.Box, c: dict, n: str, vals: dict) -> None:
        """One touch-sized line: label | control | value, - / + and revert (ranges). Disabled rows say why."""
        _g, label, kind, opts, off = SPEC[n]
        v = vals[n]
        cal = calibrated_value(c, n)
        sensitive = not (off and off(vals))
        row = Gtk.Box(spacing=8, css_classes=["camctl-row"])
        lbl = Gtk.Label(label=label, xalign=0, css_classes=["camctl-label"], sensitive=sensitive,
                        ellipsize=Pango.EllipsizeMode.END)
        lbl.set_size_request(104, -1)
        if n == "backlight_compensation":
            lbl.set_tooltip_text("Backlight compensation (steers automatic exposure)")
        row.append(lbl)
        body.append(row)
        self._diffs[n] = cal is not None and cal != v
        revert = Gtk.Button(icon_name="edit-undo-symbolic", css_classes=["cam-revert"], valign=Gtk.Align.CENTER,
                            tooltip_text="Back to the calibrated value")

        def set_differs(value):
            d = cal is not None and cal != value
            revert.set_opacity(1 if d else 0)
            revert.set_sensitive(d)
            self._mark(n, d)

        if not sensitive and n in OFF_REASON:
            body.append(Gtk.Label(label=OFF_REASON[n], xalign=0, wrap=True, css_classes=["camctl-hint"]))

        if kind in ("select", "sensor", "switch"):
            st = {"v": v}
            layer, cname = ("sensor", "350B") if kind == "sensor" else ("uvc", n)

            def commit(new, st=st, layer=layer, cname=cname):
                if self._building or new == st["v"]:
                    return
                old, st["v"] = st["v"], new
                self._edit(layer, cname, old, new)
                set_differs(new)

            if kind == "switch":
                w = Gtk.Switch(active=bool(v), valign=Gtk.Align.CENTER, sensitive=sensitive)
                w.connect("notify::active", lambda s, _p: commit(1 if s.get_active() else 0))
                row.append(Gtk.Box(hexpand=True))
                row.append(w)
                set_ctl = lambda x, w=w: w.set_active(bool(x))
            else:
                if kind == "sensor":
                    steps = SENSOR_STEPS if v in SENSOR_STEPS else sorted(SENSOR_STEPS + [v], key=sensor_gain)
                    keys, texts = steps, [gain_label(k) for k in steps]
                else:
                    keys, texts = list(opts), list(opts.values())
                    if v not in keys:
                        keys, texts = keys + [v], texts + [str(v)]
                if len(keys) <= SEGMENT_MAX:
                    w = segmented(list(zip(keys, texts)), v, commit)
                    w.set_sensitive(sensitive)
                    set_ctl = lambda x, w=w: w.set_active_name(str(x))
                else:
                    w = Gtk.DropDown.new_from_strings(texts)
                    w.set_selected(keys.index(v))
                    w.set_sensitive(sensitive)
                    w.set_valign(Gtk.Align.CENTER)
                    w.connect("notify::selected", lambda d, _p, keys=keys: commit(keys[d.get_selected()]))
                    set_ctl = lambda x, w=w, keys=keys: w.set_selected(keys.index(x)) if x in keys else None
                row.append(w)
                row.append(Gtk.Box(hexpand=True))
            row.append(revert)
            revert.connect("clicked", lambda *_: cal is not None and set_ctl(cal))
            set_differs(v)
            return

        # range
        if kind == "q":
            lo, hi, step, fmt = 1, 30, 1, str
            lbl.set_tooltip_text("Lower is sharper and uses more USB bandwidth. Applies when the stream restarts.")
        else:
            ctl = c["controls"][n]
            lo, hi, step = ctl["min"], ctl["max"], ctl.get("step") or 1
            if n == "exposure_time_absolute":
                fmt = lambda x: self._exposure_text(c, x)
            else:
                fmt = opts or str
        layer = "q" if kind == "q" else "uvc"
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
        scale.set_value(v)
        scale.set_draw_value(False)
        scale.set_hexpand(True)
        scale.set_size_request(110, -1)
        scale.set_valign(Gtk.Align.CENTER)
        scale.set_sensitive(sensitive)
        val_btn = Gtk.Button(label=fmt(v), css_classes=["flat", "camctl-value-btn", "numeric"], sensitive=sensitive,
                             tooltip_text="Tap to type a value")
        val_btn.set_size_request(70, 34)
        minus = Gtk.Button(label="−", css_classes=["cam-step"], sensitive=sensitive, valign=Gtk.Align.CENTER)
        plus = Gtk.Button(label="+", css_classes=["cam-step"], sensitive=sensitive, valign=Gtk.Align.CENTER)
        st = {"v": v}

        def changed(s):
            value = int(round(s.get_value()))
            val_btn.set_label(fmt(value))
            if self._building:
                return
            set_differs(value)
            self._origin.setdefault(n, st["v"])
            if n in self._pending:
                GLib.source_remove(self._pending[n][0])
            args = (layer, n, value, st)
            self._pending[n] = (GLib.timeout_add(350, self._flush, *args), args)

        scale.connect("value-changed", changed)
        minus.connect("clicked", lambda *_: scale.set_value(max(lo, scale.get_value() - step)))
        plus.connect("clicked", lambda *_: scale.set_value(min(hi, scale.get_value() + step)))
        revert.connect("clicked", lambda *_: cal is not None and scale.set_value(cal))
        val_btn.connect("clicked", lambda b: self._type_value(b, label, scale, lo, hi, step))
        for w in (scale, val_btn, minus, plus, revert):
            row.append(w)
        set_differs(v)
        if kind == "q" and c.get("q_active") is not None and c["q_active"] != v:
            hint = Gtk.Box(spacing=6)
            hint.set_margin_start(16)
            hint.set_margin_end(16)
            t = caption(f"Running stream still uses {c['q_active']}", "camopt-warn")
            t.set_hexpand(True)
            hint.append(t)
            btn = Gtk.Button(label="Restart Autodarts", valign=Gtk.Align.CENTER, css_classes=["compact-btn"])
            btn.connect("clicked", lambda *_: self._ask_restart_autodarts())
            hint.append(btn)
            body.append(hint)

    def _type_value(self, anchor: Gtk.Widget, label: str, scale: Gtk.Scale, lo: float, hi: float, step: float):
        """Tap a value to type it. Exposure shows milliseconds on HBV cameras but is typed in the raw unit."""
        pop = Gtk.Popover()
        box = Gtk.Box(spacing=6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        spin = Gtk.SpinButton.new_with_range(lo, hi, step)
        spin.set_value(scale.get_value())
        spin.set_numeric(True)
        spin.set_width_chars(7)
        box.append(Gtk.Label(label=f"{label} ({int(lo)}–{int(hi)})", css_classes=["camopt-small"]))
        box.append(spin)
        ok = Gtk.Button(label="Set", css_classes=["suggested-action"])

        def apply(*_):
            scale.set_value(spin.get_value())
            pop.popdown()

        ok.connect("clicked", apply)
        spin.connect("activate", apply)
        box.append(ok)
        pop.set_child(box)
        pop.set_parent(anchor)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()
        spin.grab_focus()

    def _flush_pending(self):
        for sid, args in list(self._pending.values()):
            GLib.source_remove(sid)
            self._flush(*args)

    def _flush(self, layer: str, n: str, value: int, st: dict):
        self._pending.pop(n, None)
        old = self._origin.pop(n, st["v"])
        st["v"] = value
        # a JPEG level change adds/removes the restart hint; other sliders stay put (no rebuild under the finger)
        self._edit(layer, n, old, value, reload=layer == "q")
        return False

    def _edit(self, layer: str, name: str, old, new, reload: bool = True):
        """A change made by the user: remember it for Undo, then write it to the camera."""
        dev = self._cur_dev()
        if dev is not None and old != new:
            self._undo.append((dev, layer, name, old))
            del self._undo[:-UNDO_MAX]
            self._sync_undo()
        self._set(layer, name, new, reload=reload)

    def _sync_undo(self):
        self.btn_undo.set_sensitive(bool(self._undo))

    def _undo_last(self):
        if not self._undo:
            return
        dev, layer, name, old = self._undo.pop()
        self._sync_undo()
        if dev != self._cur_dev():
            return
        shown = {"q": "JPEG compression", "350B": "Sensor gain"}.get(name) or SPEC.get(name, (0, name))[1]
        self.window.show_toast(f"Undone: {shown}")
        self._set(layer, name, old)

    def _set(self, layer: str, name: str, value: int, reload: bool = True):
        if self._building:
            return
        dev = self._cur_dev()
        if dev is None:
            return

        def done(r):
            if r.get("note"):
                self.window.show_toast(r["note"])
            if reload:
                self._load()  # dependent rows (manual exposure, WB temperature) and marks

        def fail(e):
            self.window.show_toast(str(e))
            self._load()

        run_async(live.set_control, done, fail, dev, layer, name, int(value))

    def _ask_copy_to_all(self):
        c = self._cur()
        others = [o for o in self.cams if o is not c and not o.get("error")]
        if c is None or not others:
            self.window.show_toast("No other camera to copy to")
            return
        who = ", ".join(cam_name(o["port"]) for o in others)
        d = Adw.AlertDialog(heading=f"Copy {cam_name(c['port'])} to {who}?",
                            body="Exposure, gain, tone, color, detail and stream settings of this camera are written "
                                 "to the other cameras and kept there. Cameras with their own light or angle may "
                                 "need different values.")
        d.add_response("cancel", "Cancel")
        d.add_response("copy", "Copy")
        d.set_response_appearance("copy", Adw.ResponseAppearance.SUGGESTED)

        def work():
            skipped = 0
            order = [n for n in APPLY_FIRST if n in c["controls"]] + [n for n in c["controls"] if n not in APPLY_FIRST]
            for o in others:
                for n in order:
                    if n not in o["controls"] or o["controls"][n]["value"] == c["controls"][n]["value"]:
                        continue
                    try:
                        live.set_control(o["device"], "uvc", n, c["controls"][n]["value"])
                    except OSError:
                        skipped += 1  # e.g. temperature while automatic white balance is on
                if c.get("hidden") and o.get("hidden"):
                    for layer, name, key in (("q", "q", "q"), ("sensor", "350B", "sensor_gain")):
                        if c.get(key) is not None and o.get(key) != c[key]:
                            try:
                                live.set_control(o["device"], layer, name, c[key])
                            except (OSError, ValueError):
                                skipped += 1
            return skipped

        def done(skipped):
            self.window.show_toast(f"Copied to {who}" + (f" ({skipped} setting(s) refused)" if skipped else ""))
            self._load()

        d.connect("response", lambda _d, r: r == "copy" and run_async(work, done, self._fail))
        d.present(self.window)

    def _on_keep(self, on: bool):
        if self._building:
            return
        c = self._cur()
        if c is None or on == (c.get("profile") is not None):
            return
        if on:
            run_async(live.keep, lambda _r: (self.window.show_toast("Settings kept for this camera"), self._load()),
                      self._fail, c["device"])
            return
        if not c.get("optimized"):
            run_async(live.forget, self._forgotten, self._fail, c["device"])
            return
        d = Adw.AlertDialog(heading="Stop Keeping the Optimized Settings?",
                            body="This camera goes back to its stock JPEG quality and the settings from before the "
                                 "optimization. Autodarts writes its own values when it restarts.")
        d.add_response("cancel", "Cancel")
        d.add_response("stop", "Stop Keeping")
        d.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_resp(_d, r):
            if r == "stop":
                run_async(live.forget, self._forgotten, self._fail, c["device"])
            else:
                self._load()  # puts the switch back

        d.connect("response", on_resp)
        d.present(self.window)

    def _forgotten(self, r: dict):
        self.window.show_toast("Settings no longer kept for this camera")
        if r.get("needs_restart"):
            self._ask_restart_autodarts()
        self._load()

    def _fail(self, e: Exception):
        self.window.show_toast(str(e))
        self._load()

    def _reset_cam(self):
        c = self._cur()
        if c is None:
            return
        if c.get("calibrated"):
            def done(r):
                self.window.show_toast(r.get("note") or "Back to the calibrated values")
                if r.get("needs_restart"):
                    self._ask_restart_autodarts()
                self._later(2500)  # the keeper applies within 2 s

            run_async(live.reset_profile, done, self._fail, c["device"])
        else:
            def done(r):
                self.window.show_toast("Defaults restored" + (f" (refused: {', '.join(r['refused'])})"
                                                              if r.get("refused") else ""))
                self._load()

            run_async(live.reset_defaults, done, self._fail, c["device"])

    def _ask_restart_autodarts(self):
        d = Adw.AlertDialog(heading="Restart Autodarts?",
                            body="The new JPEG level takes effect when the camera stream restarts. "
                                 "Autodarts is back in about 10 seconds.")
        d.add_response("cancel", "Cancel")
        d.add_response("restart", "Restart")
        d.set_response_appearance("restart", Adw.ResponseAppearance.SUGGESTED)

        def on_resp(_d, r):
            if r != "restart":
                return
            self.window.show_toast("Restarting Autodarts…")
            run_async(oneclick.systemctl, lambda _r: (self._later(8000), self.autodarts.refresh()), None, "restart")

        d.connect("response", on_resp)
        d.present(self.window)

    def _recheck(self, trim: bool):
        dev = self._cur_dev()
        if dev is None:
            return
        self.btn_check.set_sensitive(False)
        self.btn_trim.set_visible(False)
        self.lbl_check.set_label("Fixing…" if trim else "Checking…")

        def done(r):
            self.btn_check.set_sensitive(True)
            if r.get("error"):
                self.lbl_check.set_label("No picture yet, try again")
                return
            pct = r["deviation_pct"]
            if "gain" in r:
                self.lbl_check.set_label(f"Gain {r['gain'][0]} → {r['gain'][1]}")
                self._load()
            elif r["verdict"] == "ok":
                self.lbl_check.set_label(f"Brightness OK ({pct:+.0f} %)")
            elif r["verdict"] == "trim gain":
                self.lbl_check.set_label(f"{abs(pct):.0f} % {'brighter' if pct > 0 else 'darker'} than calibrated")
                self.btn_trim.set_visible(True)
            else:
                self.lbl_check.set_label(f"{abs(pct):.0f} % {'brighter' if pct > 0 else 'darker'}: light changed, optimize again")

        def fail(e):
            self.btn_check.set_sensitive(True)
            self.lbl_check.set_label(str(e))

        run_async(live.recheck, done, fail, dev, trim)

    # ------------------------------------------------------------------------------------------
    # Presets
    # ------------------------------------------------------------------------------------------

    def _show_presets(self):
        dialog = Adw.Dialog(title="Presets", content_width=480, content_height=520)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        tv.set_content(page)
        dialog.set_child(tv)

        grp_new = Adw.PreferencesGroup(description="A preset stores the settings of every camera. "
                                                   "Loading it applies them and keeps them.")
        btn_save = Gtk.Button(label="Save Current Settings as Preset…", halign=Gtk.Align.START)
        btn_save.add_css_class("pill")
        grp_new.add(btn_save)
        page.add(grp_new)
        grp = Adw.PreferencesGroup(title="Presets")
        page.add(grp)
        rows: List[Gtk.Widget] = []

        def refresh():
            for r in rows:
                grp.remove(r)
            rows.clear()
            r = Adw.ActionRow(title=live.DEFAULT_PRESET,
                              subtitle="Every camera on its own defaults. Nothing is kept or optimized.")
            load = Gtk.Button(label="Load", valign=Gtk.Align.CENTER)
            load.connect("clicked", lambda _b: confirm_defaults())
            r.add_suffix(load)
            grp.add(r)
            rows.append(r)
            presets = profiles.load_presets()
            default = profiles.default_preset()
            for name in sorted(presets, key=str.lower):
                ports = presets[name]
                r = Adw.ActionRow(title=GLib.markup_escape_text(name))
                r.set_subtitle(", ".join(f"Port {port_no(p)}" for p in sorted(ports)) +
                               (" · loads at every start" if name == default else ""))
                star = Gtk.Button(icon_name="starred-symbolic" if name == default else "non-starred-symbolic",
                                  valign=Gtk.Align.CENTER,
                                  tooltip_text="Default: loads at every start" if name == default
                                  else "Make default: load at every start")
                star.add_css_class("flat")
                star.connect("clicked", lambda _b, n=name, on=name != default: (live.set_default_preset(n, on),
                                                                                refresh()))
                r.add_suffix(star)
                for icon, tip, fn in (("document-edit-symbolic", "Rename", rename),
                                      ("user-trash-symbolic", "Delete", delete)):
                    b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
                    b.add_css_class("flat")
                    b.connect("clicked", lambda _b, n=name, f=fn: f(n))
                    r.add_suffix(b)
                load = Gtk.Button(label="Load", valign=Gtk.Align.CENTER)
                load.add_css_class("suggested-action")
                load.connect("clicked", lambda _b, n=name: do_load(n))
                r.add_suffix(load)
                grp.add(r)
                rows.append(r)

        def confirm_defaults():
            d = Adw.AlertDialog(heading="Reset All Cameras?",
                                body="Every camera goes back to its own default settings. Kept and optimized "
                                     "settings are removed; Autodarts writes its own values when it restarts.")
            d.add_response("cancel", "Cancel")
            d.add_response("reset", "Reset")
            d.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
            d.connect("response", lambda _d, resp: resp == "reset" and do_load(live.DEFAULT_PRESET))
            d.present(self.window)

        def do_load(name: str):
            def done(r):
                dialog.close()
                self.window.show_toast(f"Preset “{name}” loaded on {r['cameras']} camera(s)")
                if r.get("needs_restart"):
                    self._ask_restart_autodarts()
                self._load()

            run_async(live.load_preset, done, lambda e: self.window.show_toast(str(e)), name)

        def rename(name: str):
            d = Adw.AlertDialog(heading="Rename Preset")
            e = Gtk.Entry(text=name, activates_default=True)
            d.set_extra_child(e)
            d.add_response("cancel", "Cancel")
            d.add_response("rename", "Rename")
            d.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
            d.set_default_response("rename")

            def on_resp(_d, resp):
                new = e.get_text().strip()
                if resp != "rename" or not new or new == name:
                    return
                try:
                    live.rename_preset(name, new)
                except ValueError as err:
                    self.window.show_toast(str(err))
                refresh()

            d.connect("response", on_resp)
            d.present(self.window)

        def delete(name: str):
            d = Adw.AlertDialog(heading="Delete Preset?", body=f"“{name}” is deleted. Camera settings stay as they are.")
            d.add_response("cancel", "Cancel")
            d.add_response("delete", "Delete")
            d.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
            d.connect("response", lambda _d, resp: resp == "delete" and (live.delete_preset(name), refresh()))
            d.present(self.window)

        btn_save.connect("clicked", lambda *_: ask_save_preset(self.window, on_saved=refresh))
        refresh()
        dialog.present(self.window)

    # ------------------------------------------------------------------------------------------
    # Live view
    # ------------------------------------------------------------------------------------------

    def _set_overlay(self, key: str, on: bool):
        self._overlays[key] = on
        if self._frozen and self._painted is not None:  # redraw the frozen picture now
            dev = self._cur_dev()
            if dev is not None:
                self.live_picture.set_paintable(to_texture(self._compose(dev, self._painted), 960))
        else:
            self._shown = None  # redraw the current picture now

    def _set_frozen(self, on: bool):
        self._frozen = on
        self._shown = None

    def _start_live(self):
        if not self._live_timer:
            self._live_tick = 0
            self._live_timer = GLib.timeout_add(33, self._tick_live)

    def _stop_live(self):
        if self._live_timer:
            GLib.source_remove(self._live_timer)
            self._live_timer = 0
        live.stop_all()

    def _mask(self, dev: str, shape: tuple, full: tuple) -> Optional[np.ndarray]:
        """Board area of the scores at the size of the shown picture (only known at 1280x960)."""
        key = (dev, shape)
        if key not in self._masks:
            m = None
            if full == (960, 1280):
                H = board.load_homography(dev, 1280, 960)
                if H is not None:
                    full_mask = board.board_mask_image(H, full).astype(np.uint8)
                    m = cv2.resize(full_mask, (shape[1], shape[0]), interpolation=cv2.INTER_NEAREST) > 0
            self._masks[key] = m
        return self._masks[key]

    def _compose(self, dev: str, bgr: np.ndarray) -> np.ndarray:
        """The picture with the overlays the user switched on (zebra, mask, histogram)."""
        if not any(self._overlays.values()):
            return bgr
        s = 960 / max(bgr.shape[:2])
        img = cv2.resize(bgr, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else bgr.copy()
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if self._overlays["mask"]:
            m = self._mask(dev, img.shape[:2], bgr.shape[:2])
            if m is not None:
                img[~m] = (img[~m] * 0.3).astype(np.uint8)
        if self._overlays["zebra"]:
            yy, xx = np.indices(gray.shape)
            img[(gray >= 250) & (((xx + yy) // 6) % 2 == 0)] = (0, 0, 255)
        if self._overlays["hist"]:
            h = cv2.calcHist([gray], [0], None, [64], [0, 256]).ravel()
            h = np.sqrt(h / max(h.max(), 1))
            x0, y1 = 12, img.shape[0] - 12
            cv2.rectangle(img, (x0 - 4, y1 - 84), (x0 + 64 * 4 + 4, y1 + 4), (0, 0, 0), -1)
            for i, v in enumerate(h):
                cv2.rectangle(img, (x0 + i * 4, y1 - int(v * 76)), (x0 + i * 4 + 3, y1),
                              (80, 80, 255) if i == 63 else (230, 230, 230), -1)
        return img

    def _tick_live(self):
        dev = self._cur_dev()
        if dev is None or oneclick.JOB.running or not self.get_mapped():
            self._live_timer = 0
            live.stop_all()
            return False
        lv = live.live(dev)
        for other in live.LIVE.values():
            if other is not lv:
                other.stop()
        lv.touch()
        with lv.lock:
            bgr = lv.bgr
        if bgr is not None and bgr is not self._shown:
            self._ident += 1
            self._shown = bgr
            if not self._frozen or self._painted is None:
                self._painted = bgr
                self.live_picture.set_paintable(to_texture(self._compose(dev, bgr), 960))
        self._live_tick += 1
        if self._live_tick % 30 == 0:  # about once a second
            fc, ident, t0, _fps = self._rate
            now = time.time()
            n = lv.frame_count - fc if lv.source == "own stream" else self._ident - ident
            self._rate = (lv.frame_count, self._ident, now, max(0.0, n / max(now - t0, 0.1)))
            if bgr is not None:
                self.strip.set_thumb(self._cur()["port"], to_texture(bgr, CameraStrip.THUMB_PX))
            if not self._scores_busy:
                self._scores_busy = True
                run_async(lv.scores, self._show_scores, lambda e: setattr(self, "_scores_busy", False))
        return True

    def _show_scores(self, sc: dict):
        self._scores_busy = False
        src = {"own stream": "Live from the camera", "Autodarts": "Picture from Autodarts",
               "waking Autodarts from standby": "Waking Autodarts' cameras from standby…"}.get(sc.get("source"),
                                                                                              sc.get("source", ""))
        fps = self._rate[3]
        self.lbl_tag.set_label(f"● {src}" + (f" · {fps:.0f} fps" if fps >= 0.5 and sc.get("source") in
                                             ("own stream", "Autodarts") else ""))
        self.lbl_tag.set_visible(bool(src))

        def only_note(text, kind="warn"):
            for w in (self.chip_white, self.chip_clip, self.chip_noise, self.chip_kb):
                w.set_visible(False)
            set_chip(self.chip_note, text, kind)
            self.chip_note.set_visible(True)

        if "white" not in sc:
            only_note("Waiting for a picture…", "neutral")
            return
        if sc.get("source") == "Autodarts" and sc["white"] < 2:
            only_note("Autodarts sends a black picture: its cameras are probably in standby")
            return
        c = self._cur() or {}
        self.chip_note.set_visible(False)
        if c.get("optimized"):
            dev_pct = abs(100 * (sc["white"] - sc["white_target"]) / sc["white_target"])
            set_chip(self.chip_white, f"White {sc['white']:.0f} / {sc['white_target']:.0f}",
                     "ok" if dev_pct < 6 else "warn" if dev_pct < 15 else "bad")
        else:
            set_chip(self.chip_white, f"White {sc['white']:.0f}", "neutral")
        set_chip(self.chip_clip, f"Clipped {sc['clip_pct']:.1f} %", "ok" if sc["clip_pct"] < 1 else
                 "warn" if sc["clip_pct"] < 3 else "bad")
        set_chip(self.chip_kb, f"{sc['kb']:.0f} KB/frame", "neutral")
        if "tnoise" in sc:
            set_chip(self.chip_noise, f"Noise {sc['tnoise']:.2f}", "neutral")
        self.chip_noise.set_visible("tnoise" in sc)
        for w in (self.chip_white, self.chip_clip, self.chip_kb):
            w.set_visible(True)
        tip = "" if sc.get("board_mask") else "No Autodarts calibration: the scores use the whole picture"
        self.chips.set_tooltip_text(tip or None)
