"""Camera Controls page: live view and every control of each camera, for any UVC camera (core.hbvcal.live).

Changes apply immediately. A camera's settings are kept (re-applied by the keeper service after Autodarts
restarts and at boot) once it has a profile: from the Camera Optimizer, from "Keep these settings", or from a
loaded preset. On confirmed HBV OV2710 cameras the hidden JPEG quality and sensor gain are adjustable too.
"""

from __future__ import annotations
from typing import Dict, List, Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from core.hbvcal import live, oneclick, profiles
from core.hbvcal.daemon import cameras
from core.logger import get_logger
from modules_gtk.async_utils import run_async
from modules_gtk.camera_widgets import PortToggles, caption, port_no, to_texture

logger = get_logger("camera_controls_view")

# Sensor analog gain register 0x350B: bits 4-6 = x2 steps, bits 0-3 = n/16
SENSOR_STEPS = [0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x30, 0x38, 0x70]
GROUPS = [("exposure", "Exposure"), ("tone", "Tone"), ("detail", "Detail"), ("color", "Color"), ("stream", "Stream")]
COLUMNS = (("exposure", "color", "stream"), ("tone", "detail"))  # two columns: every control fits without scrolling
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
        self._pending: Dict[str, int] = {}  # debounced slider writes: control -> GLib source id
        self._live_timer = 0
        self._live_tick = 0
        self._scores_busy = False
        self._shown = None

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_margin_top(8)
        outer.set_margin_bottom(10)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        self.set_child(outer)

        head = Gtk.Box(spacing=12)
        self.cam_toggle = PortToggles(self._on_cam_pick)
        head.append(self.cam_toggle.widget)
        keep_box = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        self.sw_keep = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.sw_keep.connect("notify::active", lambda sw, _p: self._on_keep(sw.get_active()))
        keep_box.append(self.sw_keep)
        keep_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        keep_txt.append(Gtk.Label(label="Keep these settings", xalign=0, css_classes=["camctl-label"]))
        self.lbl_keep = caption("")
        keep_txt.append(self.lbl_keep)
        keep_box.append(keep_txt)
        head.append(keep_box)
        head.append(Gtk.Box(hexpand=True))
        btn_presets = Gtk.Button()
        btn_presets.set_child(Adw.ButtonContent(icon_name="view-list-symbolic", label="Presets"))
        btn_presets.set_tooltip_text("Save or load the settings of all cameras at once")
        btn_presets.connect("clicked", lambda *_: self._show_presets())
        head.append(btn_presets)
        outer.append(head)

        cols = Gtk.Box(spacing=12, vexpand=True)
        outer.append(cols)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        self.live_picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True, vexpand=True)
        self.live_picture.set_size_request(320, 240)
        self.live_picture.add_css_class("camopt-frame")
        left.append(self.live_picture)
        info = Gtk.Box(spacing=8)
        self.lbl_metrics = caption("", "numeric")
        self.lbl_metrics.set_hexpand(True)
        info.append(self.lbl_metrics)
        self.check_box = Gtk.Box(spacing=6)
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

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right.set_size_request(600, -1)
        right.add_css_class("card")
        right.add_css_class("camopt-panel")
        # fits without scrolling at the page's size; scrolls only in a smaller window
        ctl_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.controls_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        ctl_scroll.set_child(self.controls_box)
        right.append(ctl_scroll)
        foot = Gtk.Box(spacing=8)
        self.lbl_marks = caption("● differs from the calibration")
        self.lbl_marks.set_hexpand(True)
        foot.append(self.lbl_marks)
        self.btn_reset = Gtk.Button(label="Reset to defaults", css_classes=["compact-btn"])
        self.btn_reset.connect("clicked", lambda *_: self._reset_cam())
        foot.append(self.btn_reset)
        right.append(foot)
        cols.append(right)

    # ------------------------------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------------------------------

    def on_page_opened(self):
        self._load()

    def on_page_closed(self):
        self._stop_live()

    def _cur(self) -> Optional[dict]:
        return self.cams[self.cur] if self.cams and self.cur < len(self.cams) else None

    def _cur_dev(self) -> Optional[str]:
        c = self._cur()
        return c["device"] if c else None

    def _load(self):
        def done(cams):
            self.cams = cams
            names = [c["port"] for c in cams]
            self.cam_toggle.sync(names)
            name = self.cam_toggle.active
            self.cur = names.index(name) if name in names else 0
            self._render_controls()
            self._start_live()

        run_async(lambda: [live.camera_info(d) for d in cameras()], on_done=done)

    def _later(self, ms: int):
        GLib.timeout_add(ms, lambda: self._load() and False)

    def _on_cam_pick(self):
        self.lbl_check.set_label("")
        self.btn_trim.set_visible(False)
        self.live_picture.set_paintable(None)
        self._shown = None
        self._load()

    # ------------------------------------------------------------------------------------------
    # Controls
    # ------------------------------------------------------------------------------------------

    def _render_controls(self):
        self._building = True
        while (child := self.controls_box.get_first_child()) is not None:
            self.controls_box.remove(child)
        c = self._cur()
        if c is None:
            self.controls_box.append(Gtk.Label(label="No cameras connected", css_classes=["camopt-empty"], vexpand=True))
            for w in (self.btn_reset, self.check_box, self.lbl_marks):
                w.set_visible(False)
            self.sw_keep.set_sensitive(False)
            self.lbl_keep.set_label("")
            self._building = False
            return
        kept, optimized = c.get("profile") is not None, c.get("optimized")
        self.sw_keep.set_sensitive(not c.get("error"))
        self.sw_keep.set_active(kept)
        if optimized:
            self.lbl_keep.set_label("Optimized · re-applied after restarts")
        elif kept:
            self.lbl_keep.set_label("Re-applied after Autodarts restarts")
        else:
            self.lbl_keep.set_label("Off · Autodarts resets changes on restart")
        for cls in ("camopt-warn",):
            self.lbl_keep.remove_css_class(cls)
        if not kept:
            self.lbl_keep.add_css_class("camopt-warn")
        has_cal = bool(c.get("calibrated"))
        self.btn_reset.set_label("Reset to calibrated" if has_cal else "Reset to defaults")
        self.btn_reset.set_visible(not c.get("error"))
        self.lbl_marks.set_visible(has_cal)
        self.check_box.set_visible(bool(optimized))
        if c.get("error"):
            self.controls_box.append(caption(f"Camera not reachable: {c['error']}", wrap=True))
            self._building = False
            return

        vals = {n: v["value"] for n, v in c["controls"].items()}
        if c.get("q") is not None:
            vals["q"] = c["q"]
        if c.get("sensor_gain") is not None:
            vals["sensor_gain"] = c["sensor_gain"]
        titles = dict(GROUPS)
        columns = Gtk.Box(spacing=20, homogeneous=True)
        for groups in COLUMNS:
            grid = Gtk.Grid(column_spacing=8, row_spacing=0, valign=Gtk.Align.START)
            grid.add_css_class("camctl-grid")
            row = 0
            for g in groups:
                names = [n for n, s in SPEC.items() if s[0] == g and vals.get(n) is not None]
                hint = self._hidden_hint(c) if g == "stream" else None
                if not names and hint is None:
                    continue
                head = Gtk.Label(label=titles[g].upper(), xalign=0, css_classes=["camopt-heading"])
                head.set_margin_top(2 if row == 0 else 14)
                grid.attach(head, 0, row, 3, 1)
                row += 1
                for n in names:
                    row = self._control_row(grid, row, c, n, vals)
                if hint is not None:
                    grid.attach(hint, 0, row, 3, 1)
                    row += 1
            columns.append(grid)
        self.controls_box.append(columns)
        self._building = False

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

    def _control_row(self, grid: Gtk.Grid, row: int, c: dict, n: str, vals: dict) -> int:
        """One compact line: label | control | value. Returns the next free grid row."""
        _g, label, kind, opts, off = SPEC[n]
        v = vals[n]
        cal = calibrated_value(c, n)
        sensitive = not (off and off(vals))
        lbl = Gtk.Label(label=label + ("  ●" if cal is not None and cal != v else ""), xalign=0,
                        css_classes=["camctl-label"], sensitive=sensitive)
        if n == "backlight_compensation":
            lbl.set_tooltip_text("Backlight compensation (steers automatic exposure)")
        grid.attach(lbl, 0, row, 1, 1)

        if kind in ("select", "sensor"):
            if kind == "sensor":
                steps = SENSOR_STEPS if v in SENSOR_STEPS else sorted(SENSOR_STEPS + [v], key=sensor_gain)
                keys, texts = steps, [gain_label(k) for k in steps]
            else:
                keys, texts = list(opts), list(opts.values())
                if v not in keys:
                    keys, texts = keys + [v], texts + [str(v)]
            dd = Gtk.DropDown.new_from_strings(texts)
            dd.set_selected(keys.index(v))
            dd.set_sensitive(sensitive)
            dd.set_halign(Gtk.Align.END)
            layer, cname = ("sensor", "350B") if kind == "sensor" else ("uvc", n)
            dd.connect("notify::selected", lambda d, _p: self._set(layer, cname, keys[d.get_selected()]))
            grid.attach(dd, 1, row, 2, 1)
            return row + 1
        if kind == "switch":
            sw = Gtk.Switch(active=bool(v), halign=Gtk.Align.END, valign=Gtk.Align.CENTER)
            sw.connect("notify::active", lambda w, _p: self._set("uvc", n, 1 if w.get_active() else 0))
            grid.attach(sw, 1, row, 2, 1)
            return row + 1

        if kind == "q":
            lo, hi, step, fmt = 1, 30, 1, str
            lbl.set_tooltip_text("Lower is sharper and uses more USB bandwidth. Applies when the stream restarts.")
        else:
            ctl = c["controls"][n]
            lo, hi, step = ctl["min"], ctl["max"], ctl.get("step") or 1
            if n == "exposure_time_absolute" and c.get("hbv"):
                fmt = lambda x: f"{x * 0.05:.1f} ms"  # HBV: 50 µs units (measured)
            else:
                fmt = opts or str
        val_lbl = Gtk.Label(label=fmt(v), width_chars=6, xalign=1, css_classes=["camctl-value", "numeric"],
                            sensitive=sensitive)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
        scale.set_value(v)
        scale.set_draw_value(False)
        scale.set_hexpand(True)
        scale.set_size_request(100, -1)
        scale.set_valign(Gtk.Align.CENTER)
        scale.set_sensitive(sensitive)
        layer = "q" if kind == "q" else "uvc"

        def changed(s):
            value = int(round(s.get_value()))
            val_lbl.set_label(fmt(value))
            if self._building:
                return
            lbl.set_label(label + ("  ●" if cal is not None and cal != value else ""))
            if n in self._pending:
                GLib.source_remove(self._pending[n])
            self._pending[n] = GLib.timeout_add(350, self._flush, layer, n, value)

        scale.connect("value-changed", changed)
        grid.attach(scale, 1, row, 1, 1)
        grid.attach(val_lbl, 2, row, 1, 1)
        row += 1
        if kind == "q" and c.get("q_active") is not None and c["q_active"] != v:
            hint = Gtk.Box(spacing=6)
            t = caption(f"Running stream still uses {c['q_active']}", "camopt-warn")
            t.set_hexpand(True)
            hint.append(t)
            btn = Gtk.Button(label="Restart Autodarts", valign=Gtk.Align.CENTER, css_classes=["compact-btn"])
            btn.connect("clicked", lambda *_: self._ask_restart_autodarts())
            hint.append(btn)
            grid.attach(hint, 0, row, 3, 1)
            row += 1
        return row

    def _flush(self, layer: str, n: str, value: int):
        self._pending.pop(n, None)
        # a JPEG level change adds/removes the restart hint; other sliders stay put (no rebuild under the finger)
        self._set(layer, n, value, reload=layer == "q")
        return False

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
                self._load()  # dependent rows (manual exposure, WB temperature) and ● marks

        def fail(e):
            self.window.show_toast(str(e))
            self._load()

        run_async(live.set_control, done, fail, dev, layer, name, int(value))

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
            run_async(oneclick.systemctl, lambda _r: self._later(8000), None, "restart")

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
            for name in sorted(presets, key=str.lower):
                ports = presets[name]
                r = Adw.ActionRow(title=GLib.markup_escape_text(name))
                r.set_subtitle(", ".join(f"Port {port_no(p)}" for p in sorted(ports)))
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

    def _start_live(self):
        if not self._live_timer:
            self._live_tick = 0
            self._live_timer = GLib.timeout_add(200, self._tick_live)

    def _stop_live(self):
        if self._live_timer:
            GLib.source_remove(self._live_timer)
            self._live_timer = 0
        live.stop_all()

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
            self._shown = bgr
            self.live_picture.set_paintable(to_texture(bgr, 960))
        self._live_tick += 1
        if self._live_tick % 5 == 0 and not self._scores_busy:
            self._scores_busy = True
            run_async(lv.scores, self._show_scores, lambda e: setattr(self, "_scores_busy", False))
        return True

    def _show_scores(self, sc: dict):
        self._scores_busy = False
        src = {"own stream": "Live from the camera", "Autodarts": "Picture from Autodarts"}.get(sc.get("source"),
                                                                                              sc.get("source", ""))
        if "white" not in sc:
            self.lbl_metrics.set_label(src)
            return
        if sc.get("source") == "Autodarts" and sc["white"] < 2:
            self.lbl_metrics.set_label("Autodarts sends a black picture: its cameras are probably in standby.")
            return
        c = self._cur() or {}
        white = f"White {sc['white']:.0f}" + (f" (target {sc['white_target']:.0f})" if c.get("optimized") else "")
        parts = [src, white, f"Black {sc['black']:.0f}", f"Clipped {sc['clip_pct']:.1f} %"]
        if "tnoise" in sc:
            parts.append(f"Noise {sc['tnoise']:.2f}")
        parts.append(f"{sc['kb']:.0f} KB/frame")
        if not sc.get("board_mask"):
            parts.append("whole picture (no Autodarts calibration)")
        self.lbl_metrics.set_label(" · ".join(parts))
