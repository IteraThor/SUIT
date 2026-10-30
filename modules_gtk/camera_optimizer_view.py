"""Camera Optimizer page: one-click calibration of HBV OV2710 cameras (core.hbvcal).

Two tabs under the shared camera strip. Optimize: a table of the cameras with their calibration, a lighting check,
the run with a progress bar and step checklist. Before & After: the pictures taken by the last run as split,
side-by-side or difference view with zoom, and the measured change. Fine-tuning happens on Camera Controls.
"""

from __future__ import annotations
import time
from typing import Callable, Dict, List, Optional

import cv2
import numpy as np

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango

from core.hbvcal import board, live, oneclick, profiles
from core.hbvcal.autocal import PREVIEW, WHITE_TARGET
from core.hbvcal.daemon import cameras
from core.logger import get_logger
from modules_gtk.async_utils import run_async
from modules_gtk.camera_controls_view import ask_save_preset
from modules_gtk.camera_widgets import (SELECTED, AutodartsStatus, CameraStrip, ThumbPoller, cam_name, caption, chip,
                                        port_no, segmented, set_chip, to_texture, wrap_label)
from core.i18n import tr, get_language

logger = get_logger("camera_optimizer_view")

JOB = oneclick.JOB
BA_VIEW = 960  # longest side of the rendered before/after view (the crop is cut from the full picture)
BA_ZOOMS = (1, 2, 3, 4)
PREVIEW_MS = 250  # live view while optimizing: frames come from the calibration itself, decoded here
PREVIEW_VIEW = 720  # longest side of the live view
ETA_S = {"optimize": 180.0, "apply": 25.0, "restore": 40.0}  # typical run times, for the progress bar
COLS = (130, 96, 52, 118)  # table: camera, white / target, JPEG, last run (the rest is "Now")


def when_text(stamp: Optional[str]) -> str:
    """'2026-10-25 13:38:22' -> 'Today 13:38'."""
    try:
        t = time.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "–"
    day = tr("Today") if time.strftime("%Y-%m-%d", t) == time.strftime("%Y-%m-%d") else time.strftime("%d.%m." if get_language() == "de" else "%b %d", t)
    return f"{day} {time.strftime('%H:%M', t)}"


class CameraOptimizerPage(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title=tr("Camera Optimizer"), tag="camera_optimizer")
        self.window = window
        self.status: Optional[dict] = None
        self._status_busy = False
        self._job_timer = 0
        self._job_t0 = 0.0
        self._preview_timer = 0
        self._preview_seq = -1
        self._preview_dev = ""
        self._consent_asked = False
        self.lighting: Dict[str, dict] = {}  # port -> result of the last "Check lighting"
        self.ba: List[dict] = []
        self.ba_data: Optional[dict] = None
        self.ba_imgs: Optional[tuple] = None
        self.ba_key = ""
        self.ba_zoom = 1
        self.ba_mode = "split"
        self.ba_center = (0.5, 0.5)  # zoom centre, as a fraction of the full picture
        self._ba_comp = (1, 1)  # size of the picture as rendered (letterboxing needs its aspect)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_margin_top(8)
        root.set_margin_bottom(10)
        root.set_margin_start(12)
        root.set_margin_end(12)
        self.set_child(root)

        top = Gtk.CenterBox(vexpand=False, valign=Gtk.Align.START)  # explicit: a child must not stretch it
        self.strip = CameraStrip(window, self._on_cam_pick)
        top.set_start_widget(self.strip.widget)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, halign=Gtk.Align.END)
        nav = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        self.tab_toggle = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        self.tab_toggle.add(Adw.Toggle(name="optimize", label=tr("Optimize")))
        self.tab_toggle.add(Adw.Toggle(name="compare", label=tr("Before & After")))
        self.tab_toggle.set_active_name("optimize")
        self.tab_toggle.connect("notify::active-name", lambda *_: self._show_tab(self.tab_toggle.get_active_name()))
        nav.append(self.tab_toggle)
        btn_controls = Gtk.Button()
        btn_controls.set_child(Adw.ButtonContent(icon_name="preferences-system-symbolic", label=tr("Controls")))
        btn_controls.set_tooltip_text(tr("Fine-tune every camera setting, keep settings, presets"))
        btn_controls.connect("clicked", lambda *_: self.window.open_camera_controls_page())
        nav.append(btn_controls)
        side.append(nav)
        self.autodarts = AutodartsStatus(window)
        self.autodarts.widget.set_halign(Gtk.Align.END)
        side.append(self.autodarts.widget)
        top.set_end_widget(side)
        root.append(top)
        self.thumbs = ThumbPoller(self.strip, lambda: {c["port"]: c["device"] for c in (self.status or {}).get("cameras", [])},
                                  lambda: None)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        root.append(self.stack)
        self.stack.add_named(self._build_declined(), "declined")
        self.stack.add_named(self._build_overview(), "overview")
        self.stack.set_visible_child_name("overview")

    def _show_tab(self, name: Optional[str]):
        self.tabs.set_visible_child_name(name or "optimize")

    # ------------------------------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------------------------------

    def on_page_opened(self):
        self.refresh_status()
        self.thumbs.start()
        if JOB.running:
            self._start_job_poll()

    def on_page_closed(self):
        self.thumbs.stop()
        if self._job_timer:
            GLib.source_remove(self._job_timer)
            self._job_timer = 0
        if self._preview_timer:
            GLib.source_remove(self._preview_timer)
            self._preview_timer = 0

    @property
    def job_thread(self):
        """The running optimize/restore job: the window waits for it before quitting."""
        return JOB.thread if JOB.running else None

    def _cam(self, port: Optional[str] = None) -> Optional[dict]:
        port = port or self.strip.active
        return next((c for c in (self.status or {}).get("cameras", []) if c["port"] == port), None)

    # ------------------------------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------------------------------

    def refresh_status(self):
        if self._status_busy:
            return
        self._status_busy = True

        def work():
            cams = []
            for d in cameras():
                s = oneclick.support(d)
                prof = profiles.get(d)
                if s["supported"] and s.get("optimized") and prof:
                    m = prof.get("meta", {})
                    s["info"] = {"white": m.get("reference", {}).get("white"), "q": m.get("mjpeg_q"),
                                 "when": m.get("calibrated")}
                cams.append(s)
            return {"cameras": cams, "consent": profiles.consent(), "autodarts": oneclick.systemctl("is-active"),
                    "pending": bool(oneclick.load_pending())}

        def done(st):
            self._status_busy = False
            self.status = st
            self._render_status()

        def fail(e):
            self._status_busy = False
            self.hero_title.set_label(tr("Can't read the cameras"))
            self.hero_text.set_label(str(e))

        run_async(work, on_done=done, on_error=fail)

    def _render_status(self):
        st = self.status
        consent = st["consent"]
        on = self._consented()
        if consent is None and not self._consent_asked:
            self._consent_asked = True
            self._ask_consent()
        self.stack.set_visible_child_name("declined" if consent and not on else "overview")
        self.autodarts.show(st["autodarts"])
        self.strip.sync([{"port": c["port"], "kind": "bad" if not c["supported"] else "ok" if c["optimized"] else "warn"}
                         for c in st["cameras"]])
        self._render_cams()
        self._render_job()
        self._load_before_after()
        self.thumbs.kick()

    def _on_cam_pick(self):
        self._render_cams()
        self._on_ba_pick()

    def _render_cams(self):
        cams = self.status["cameras"]
        while (child := self.cam_list.get_first_child()) is not None:
            self.cam_list.remove(child)
        sup = [c for c in cams if c["supported"]]
        if cams:
            head = Gtk.Box(spacing=8)
            head.set_margin_start(14)  # the rows are buttons with this padding
            for text, w in zip(("CAMERA", "WHITE / TARGET", "JPEG", "LAST RUN", "NOW"), COLS + (-1,)):
                h = Gtk.Label(label=tr(text), xalign=0, css_classes=["camopt-table-head"], hexpand=w < 0)
                h.set_size_request(w, -1)
                head.append(h)
            self.cam_list.append(head)
        for c in cams:
            self.cam_list.append(self._cam_row(c))
        if not cams:
            self.cam_list.append(caption("No cameras connected"))

        done = sum(1 for c in sup if c["optimized"])
        bad = [p for p, r in self.lighting.items() if r.get("verdict") not in (None, "ok")]
        if JOB.running:
            pass  # _render_job owns the headline
        elif self.status.get("pending"):
            self.hero_title.set_label(tr("Calibration ready to review"))
            self.hero_text.set_label(tr("Nothing has changed yet. Compare before and after, then apply it or discard it."))
        elif not sup:
            self.hero_title.set_label(tr("No supported camera found"))
            self.hero_text.set_label(tr("Connect your HBV OV2710 cameras, then open this page again."))
        elif bad:
            self.hero_title.set_label(tr("The light has changed"))
            self.hero_text.set_label(tr("Optimize again to match the cameras to the new light."))
        elif done == len(sup):
            self.hero_title.set_label(tr("Your camera is optimized") if len(sup) == 1 else
                                      tr("All {n} cameras are optimized").format(n=len(sup)))
            self.hero_text.set_label(tr("Settings stay applied, also after Autodarts restarts. Run it again after changing "
                                        "the lights or moving the board."))
        else:
            n = len(sup)
            self.hero_title.set_label(tr("Ready to optimize"))
            self.hero_text.set_label(tr("Calibrates exposure, gain and JPEG quality for 1 camera. Takes about 3 minutes.")
                                     if n == 1 else
                                     tr("Calibrates exposure, gain and JPEG quality for {n} cameras "
                                        "and matches them. Takes about 3 minutes.").format(n=n))
        need = bool(sup) and (done < len(sup) or bool(bad))
        self.btn_optimize.set_label((tr("Optimize {n} cameras").format(n=len(sup)) if len(sup) > 1 else tr("Optimize camera"))
                                    + " · " + tr("about 3 min") if need else tr("Optimize again"))
        (self.btn_optimize.add_css_class if need else self.btn_optimize.remove_css_class)("suggested-action")
        self.btn_optimize.set_sensitive(bool(sup) and not JOB.running)
        self.btn_restore.set_visible(any(c.get("optimized") for c in cams) and not JOB.running)
        self.btn_light.set_visible(done > 0 and not JOB.running)
        self.guide.set_visible(not JOB.running)
        self.banner.set_visible(bool(bad) and not JOB.running)
        if bad:
            names = ", ".join(cam_name(p) for p in bad)
            r = self.lighting[bad[0]]
            pct = r["deviation_pct"]
            self.banner_title.set_label((tr("{names}: {pct:.0f} % brighter than calibrated") if pct > 0 else
                                         tr("{names}: {pct:.0f} % darker than calibrated")).format(names=names, pct=abs(pct)))

    def _cam_row(self, c: dict) -> Gtk.Widget:
        """One table line; tapping it picks the camera (for Before & After and Controls)."""
        if not c["supported"]:
            kind, cells, tip = "bad", [tr("Not supported"), "", "", ""], tr(c.get("reason", ""))
        elif c["optimized"]:
            i = c.get("info") or {}
            white = f"{i['white']:.0f} / {WHITE_TARGET:.0f}" if i.get("white") else "–"
            kind, cells, tip = "ok", [white, str(i.get("q") or "–"), when_text(i.get("when"))], tr("Firmware {fw}").format(fw=c.get("firmware"))
        else:
            kind, cells, tip = "warn", [tr("Not optimized"), "", ""], tr("Firmware {fw}").format(fw=c.get("firmware"))
        row = Gtk.Box(spacing=8, tooltip_text=tip)
        name = status_dot_box(cam_name(c["port"]), kind)
        name.set_size_request(COLS[0], -1)
        row.append(name)
        for text, w in zip(cells, COLS[1:]):
            lbl = Gtk.Label(label=text, xalign=0, css_classes=["camopt-cell"], ellipsize=Pango.EllipsizeMode.END)
            lbl.set_size_request(w, -1)
            row.append(lbl)
        r = self.lighting.get(c["port"])
        now = Gtk.Label(label="", xalign=0, hexpand=True, css_classes=["camopt-cell"])
        if r and c["supported"] and c["optimized"]:
            pct = r["deviation_pct"]
            if r["verdict"] == "ok":
                now.set_label(tr("✓ OK ({pct:+.0f} %)").format(pct=pct))
                now.add_css_class("camopt-ok")
            else:
                now.set_label((tr("{pct:.0f} % brighter") if pct > 0 else tr("{pct:.0f} % darker")).format(pct=abs(pct)))
                now.add_css_class("camopt-warn")
        row.append(now)
        btn = Gtk.Button(css_classes=["flat"], child=row)
        btn.set_tooltip_text(tip or None)
        btn.connect("clicked", lambda *_: self._pick(c["port"]))
        return btn

    def _pick(self, port: str):
        if port != self.strip.active:
            SELECTED["port"] = port
            self.strip._mark()
            self._on_cam_pick()

    # ------------------------------------------------------------------------------------------
    # Overview
    # ------------------------------------------------------------------------------------------

    def _build_declined(self) -> Gtk.Widget:
        page = Adw.StatusPage(icon_name="camera-disabled-symbolic", title=tr("Optimization is off"))
        page.set_description(tr("This tool changes hidden settings that were only tested on HBV OV2710 cameras, "
                                "so it stays off for other cameras."))
        btn = Gtk.Button(label=tr("I do use HBV OV2710 cameras"), halign=Gtk.Align.CENTER)
        btn.add_css_class("pill")
        btn.connect("clicked", lambda *_: self._ask_consent())
        page.set_child(btn)
        return page

    def _build_overview(self) -> Gtk.Widget:
        self.tabs = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.tabs.add_named(self._build_optimize_tab(), "optimize")
        self.tabs.add_named(self._build_compare_tab(), "compare")
        return self.tabs

    def _build_optimize_tab(self) -> Gtk.Widget:
        cols = Gtk.Box(spacing=12)

        # left: status, cameras, lighting, actions
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        left.set_size_request(440, -1)
        left.set_hexpand(True)
        left.add_css_class("card")
        left.add_css_class("camopt-panel")
        self.hero_title = wrap_label("Loading…", ["camopt-title"])
        self.hero_title.set_max_width_chars(40)
        self.hero_text = caption("", wrap=True)
        self.hero_text.set_max_width_chars(60)
        left.append(self.hero_title)
        left.append(self.hero_text)

        self.banner = Gtk.Box(spacing=10, visible=False, css_classes=["camopt-note", "camopt-note-warn"])
        bt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.banner_title = Gtk.Label(label="", xalign=0, wrap=True, css_classes=["heading"])
        bt.append(self.banner_title)
        bt.append(caption("The light probably changed."))
        self.banner.append(bt)
        left.append(self.banner)

        self.cam_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        left.append(self.cam_list)

        # while optimizing: what the camera being calibrated sees (takes the place of the table)
        self.preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=False, vexpand=True)
        self.preview_pic = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True, vexpand=True,
                                       css_classes=["camopt-frame"])
        self.preview_pic.set_size_request(-1, 220)
        ov = Gtk.Overlay(child=self.preview_pic)
        self.preview_tag = Gtk.Label(label="", halign=Gtk.Align.START, valign=Gtk.Align.START,
                                     css_classes=["camopt-tag"], visible=False)
        self.preview_tag.set_margin_top(10)
        self.preview_tag.set_margin_start(10)
        ov.add_overlay(self.preview_tag)
        self.preview_box.append(ov)
        self.preview_phase = caption("", wrap=True)
        self.preview_phase.set_max_width_chars(60)
        self.preview_box.append(self.preview_phase)
        left.append(self.preview_box)
        self.btn_light = Gtk.Button(label=tr("Check lighting"), halign=Gtk.Align.START, css_classes=["compact-btn"],
                                    tooltip_text=tr("Compare the brightness of every optimized camera with its calibration"))
        self.btn_light.connect("clicked", lambda *_: self._check_lighting())
        left.append(self.btn_light)

        self.guide = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.guide.append(caption("BEFORE YOU START", "camopt-heading"))
        for t in ("Remove all darts from the board", "Turn the lights on the way you play",
                  "Keep people and hands away from the board for about 3 minutes"):
            self.guide.append(caption("☑  " + tr(t)))
        left.append(self.guide)

        left.append(Gtk.Box(vexpand=True))
        acts = Gtk.Box(spacing=8)
        self.btn_optimize = Gtk.Button(label=tr("Optimize cameras"), sensitive=False, hexpand=True)
        self.btn_optimize.add_css_class("pill")
        self.btn_optimize.connect("clicked", lambda *_: self._ask_prepare())
        acts.append(self.btn_optimize)
        self.btn_restore = Gtk.Button(label=tr("Restore…"), visible=False, css_classes=["compact-btn"],
                                      tooltip_text=tr("Back to the settings Autodarts uses"))
        self.btn_restore.connect("clicked", lambda *_: self._ask_restore())
        acts.append(self.btn_restore)
        left.append(acts)
        left.append(caption("Autodarts is stopped and restarted automatically. Nothing is flashed: settings live in "
                            "camera memory and are re-applied by the keeper.", wrap=True))
        cols.append(left)

        # right: progress of the run
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        right.set_size_request(380, -1)
        right.set_hexpand(False)  # set explicitly: the spacers inside would otherwise make the panel expand
        right.add_css_class("card")
        right.add_css_class("camopt-panel")
        right.append(caption("PROGRESS", "camopt-heading"))
        self.job_empty = Gtk.Label(label=tr("Steps and progress of an optimization appear here."), wrap=True, vexpand=True,
                                   css_classes=["camopt-empty"])
        right.append(self.job_empty)
        self.job_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=False, vexpand=True)
        self.job_heading = caption("", "camopt-heading")
        self.progress = Gtk.ProgressBar()
        self.lbl_time = caption("")
        pr = Gtk.Box(spacing=10)
        self.progress.set_hexpand(True)
        self.progress.set_valign(Gtk.Align.CENTER)
        pr.append(self.progress)
        pr.append(self.lbl_time)
        self.job_box.append(pr)
        self.steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        # a run with failed compression levels adds lines: they scroll (newest kept in view) instead of the page
        self.steps_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True,
                                               child=self.steps_box)
        self.job_box.append(self.steps_scroll)
        self.job_note = caption("", "camopt-note", wrap=True)
        self.job_note.set_visible(False)
        self.job_box.append(self.job_note)
        job_actions = Gtk.Box(spacing=6)
        self.btn_save_preset = Gtk.Button(label=tr("Save as Preset…"), visible=False, css_classes=["compact-btn"])
        self.btn_save_preset.set_tooltip_text(tr("Keep these calibrated settings under a name, to load them again later"))
        self.btn_save_preset.connect("clicked", lambda *_: ask_save_preset(
            self.window, tr("Optimized {when}").format(when=time.strftime('%Y-%m-%d %H:%M'))))
        job_actions.append(self.btn_save_preset)
        self.btn_after = Gtk.Button(label=tr("See before & after"), visible=False, css_classes=["compact-btn"])
        self.btn_after.connect("clicked", lambda *_: self.tab_toggle.set_active_name("compare"))
        job_actions.append(self.btn_after)
        job_actions.append(Gtk.Box(hexpand=True))
        self.log_view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_view.add_css_class("camopt-log")
        log_scroll = Gtk.ScrolledWindow(min_content_width=480, min_content_height=260)
        log_scroll.set_child(self.log_view)
        log_pop = Gtk.Popover(child=log_scroll)
        btn_log = Gtk.MenuButton(label=tr("Log"), popover=log_pop, tooltip_text=tr("Details of the last run"))
        btn_log.add_css_class("flat")
        btn_log.add_css_class("compact-btn")
        job_actions.append(btn_log)
        self.job_box.append(job_actions)
        right.append(self.job_box)
        cols.append(right)
        return cols

    def _build_compare_tab(self) -> Gtk.Widget:
        cols = Gtk.Box(spacing=12)

        # left: the pictures
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True)
        self.ba_stack = Gtk.Stack(vexpand=True)
        self.ba_empty = Gtk.Label(label=tr("Before and after pictures appear here after an optimization."), wrap=True)
        self.ba_empty.add_css_class("camopt-empty")
        self.ba_stack.add_named(self.ba_empty, "empty")
        self.ba_picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self.ba_picture.set_tooltip_text(tr("Double-tap or scroll to zoom into a spot; drag to move around when zoomed"))
        self.ba_picture.add_css_class("camopt-frame")
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", lambda g, x, y: self._ba_drag_begin(x))
        drag.connect("drag-update", lambda g, dx, dy: self._ba_drag_update(g.get_start_point()[1], dx, dy))
        drag.connect("drag-end", lambda *_: self._ba_cursor())
        self.ba_picture.add_controller(drag)
        click = Gtk.GestureClick()
        click.connect("pressed", lambda g, n, x, y: n == 2 and self._ba_zoom_at(x, y, max(2, self.ba_zoom)))
        self.ba_picture.add_controller(click)
        wheel = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        wheel.connect("scroll", self._ba_scroll)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda c, x, y: setattr(self, "_ba_pointer", (x, y)))
        self.ba_picture.add_controller(wheel)
        self.ba_picture.add_controller(motion)
        ov = Gtk.Overlay(child=self.ba_picture)
        self.tag_before = Gtk.Label(label=tr("BEFORE"), halign=Gtk.Align.START, valign=Gtk.Align.START, css_classes=["camopt-tag"])
        self.tag_after = Gtk.Label(label=tr("AFTER"), halign=Gtk.Align.END, valign=Gtk.Align.START, css_classes=["camopt-tag"])
        for t in (self.tag_before, self.tag_after):
            t.set_margin_top(10)
            t.set_margin_start(10)
            t.set_margin_end(10)
            ov.add_overlay(t)
        self.ba_zoom_toggle = Adw.ToggleGroup(valign=Gtk.Align.END, halign=Gtk.Align.END, tooltip_text=tr("Zoom"))
        self.ba_zoom_toggle.set_margin_bottom(10)
        self.ba_zoom_toggle.set_margin_end(10)
        self.ba_zoom_toggle.add_css_class("osd")
        for z in BA_ZOOMS:
            self.ba_zoom_toggle.add(Adw.Toggle(name=str(z), label=f"{z}×"))
        self.ba_zoom_toggle.set_active_name("1")
        self.ba_zoom_toggle.connect("notify::active-name", lambda *_: self._ba_set_zoom(
            int(self.ba_zoom_toggle.get_active_name() or 1)))
        ov.add_overlay(self.ba_zoom_toggle)
        self.ba_stack.add_named(ov, "picture")
        left.append(self.ba_stack)

        bar = Gtk.Box(spacing=10)
        self.ba_mode_toggle = segmented([("split", "Split"), ("side", "Side by side"), ("diff", "Difference")], "split",
                                        self._ba_set_mode)
        bar.append(self.ba_mode_toggle)
        self.ba_note = caption("")
        self.ba_note.set_hexpand(True)
        self.ba_note.set_halign(Gtk.Align.END)
        self.ba_note.set_xalign(1)
        bar.append(self.ba_note)
        left.append(bar)
        self.ba_adj = Gtk.Adjustment(value=50, lower=0, upper=100, step_increment=1)  # divider position, in %
        self.ba_adj.connect("value-changed", lambda *_: self._render_ba())
        cols.append(left)

        # right: what changed
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        right.set_size_request(340, -1)
        right.set_hexpand(False)
        right.add_css_class("card")
        right.add_css_class("camopt-panel")
        self.ba_title = caption("RESULT", "camopt-heading")
        right.append(self.ba_title)
        self.ba_table = Gtk.Grid(column_spacing=14, row_spacing=8)
        right.append(self.ba_table)
        self.ba_chip = chip("", "neutral")
        self.ba_chip.set_halign(Gtk.Align.START)
        self.ba_chip.set_visible(False)
        right.append(self.ba_chip)
        right.append(Gtk.Box(vexpand=True))
        self.ba_pending = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=False)
        self.ba_pending.append(caption("Not applied yet: the cameras still run their previous settings.", wrap=True))
        self.btn_apply = Gtk.Button(label=tr("Apply now & set as default"), css_classes=["pill", "suggested-action"])
        self.btn_apply.set_tooltip_text(tr("Keep this calibration, save it as a preset and load that preset at every start"))
        self.btn_apply.connect("clicked", lambda *_: self._start_job("apply", oneclick.apply))
        self.ba_pending.append(self.btn_apply)
        btn_discard = Gtk.Button(label=tr("Discard"), css_classes=["flat"])
        btn_discard.set_tooltip_text(tr("Forget this calibration and keep the previous settings"))
        btn_discard.connect("clicked", lambda *_: self._ask_discard())
        self.ba_pending.append(btn_discard)
        right.append(self.ba_pending)
        self.btn_ba_preset = Gtk.Button(label=tr("Save as Preset…"))
        self.btn_ba_preset.connect("clicked", lambda *_: ask_save_preset(
            self.window, tr("Optimized {when}").format(when=time.strftime('%Y-%m-%d %H:%M'))))
        right.append(self.btn_ba_preset)
        btn_fine = Gtk.Button(label=tr("Fine-tune in Camera Controls →"), css_classes=["flat"])
        btn_fine.connect("clicked", lambda *_: self.window.open_camera_controls_page())
        right.append(btn_fine)
        self.ba_side = right
        cols.append(right)
        return cols

    # --- dialogs ---

    def _ask_consent(self):
        cams = (self.status or {}).get("cameras", [])
        lines = []
        for c in cams:
            hbv = c.get("usb_id") == oneclick.HBV_USB_ID
            what = tr("HBV camera detected") if hbv else tr("other camera ({usb_id})").format(usb_id=c.get("usb_id") or tr("unknown"))
            lines.append(f"{'✓' if hbv else '✗'}  {cam_name(c['port'])}: {what}")
        body = tr("The optimizer uses hidden camera settings (JPEG quality, sensor gain) that were verified on "
                  "HBV OV2710 cameras only.")
        if lines:
            body += "\n\n" + "\n".join(lines)
        d = Adw.AlertDialog(heading=tr("Are these HBV OV2710 cameras?"), body=body)
        d.add_response("no", tr("No / Not Sure"))
        d.add_response("yes", tr("Yes, Continue"))
        d.set_response_appearance("yes", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("yes")

        def on_resp(_d, resp):
            run_async(profiles.set_consent, lambda _r: self.refresh_status(), None, resp == "yes")

        d.connect("response", on_resp)
        d.present(self.window)

    def _ask_prepare(self):
        d = Adw.AlertDialog(heading=tr("Start the Optimization?"),
                            body=tr("Autodarts stops for about 3 minutes and restarts by itself.\n\n"
                                 "Darts removed, lights on, hands away from the board?"))
        d.add_response("cancel", tr("Cancel"))
        d.add_response("start", tr("Start"))
        d.set_response_appearance("start", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("start")
        d.connect("response", lambda _d, r: r == "start" and self._start_job("optimize", oneclick.optimize, 30))
        d.present(self.window)

    def _ask_discard(self):
        d = Adw.AlertDialog(heading=tr("Discard This Calibration?"),
                            body=tr("The cameras keep the settings they run now. You can optimize again at any time."))
        d.add_response("cancel", tr("Cancel"))
        d.add_response("discard", tr("Discard"))
        d.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_resp(_d, r):
            if r == "discard":
                oneclick.discard()
                self.window.show_toast(tr("Calibration discarded"))
                self.refresh_status()

        d.connect("response", on_resp)
        d.present(self.window)

    def _ask_restore(self):
        d = Adw.AlertDialog(heading=tr("Restore Original Settings?"),
                            body=tr("Your cameras go back to the settings Autodarts uses. Autodarts will restart."))
        d.add_response("cancel", tr("Cancel"))
        d.add_response("restore", tr("Restore"))
        d.set_response_appearance("restore", Adw.ResponseAppearance.DESTRUCTIVE)
        d.connect("response", lambda _d, r: r == "restore" and self._start_job("restore", oneclick.restore))
        d.present(self.window)

    # --- lighting check ---

    def _check_lighting(self):
        targets = [c["device"] for c in self.status["cameras"] if c["supported"] and c["optimized"]]
        ports = {c["device"]: c["port"] for c in self.status["cameras"]}
        self.btn_light.set_sensitive(False)
        self.btn_light.set_label(tr("Checking…"))

        def work():
            out = {}
            for d in targets:
                r = live.recheck(d, False)
                if "deviation_pct" in r:
                    out[ports[d]] = r
            live.stop_all()
            return out

        def done(res):
            self.btn_light.set_sensitive(True)
            self.btn_light.set_label(tr("Check lighting"))
            self.lighting = res
            if not res:
                self.window.show_toast(tr("No picture to compare yet, try again"))
            self._render_cams()

        def fail(e):
            self.btn_light.set_sensitive(True)
            self.btn_light.set_label(tr("Check lighting"))
            self.window.show_toast(str(e))

        run_async(work, done, fail)

    # --- job ---

    def _start_job(self, name: str, fn: Callable, *args):
        live.stop_all()
        if not JOB.start(name, fn, live.pause_live, *args):
            self.window.show_toast(tr("A camera job is already running"))
            return
        self._job_t0 = time.time()
        self.lighting = {}
        self.tab_toggle.set_active_name("optimize")
        self._start_job_poll()

    def _start_job_poll(self):
        self.btn_optimize.set_sensitive(False)
        self.btn_restore.set_visible(False)
        self.btn_light.set_visible(False)
        self.banner.set_visible(False)
        if not self._job_t0:
            self._job_t0 = time.time()
        if not self._job_timer:
            self._job_timer = GLib.timeout_add(1000, self._poll_job)
        if not self._preview_timer:
            self._preview_seq = -1
            self._preview_timer = GLib.timeout_add(PREVIEW_MS, self._poll_preview)
        self._render_job()

    def _poll_preview(self):
        if not JOB.running:
            self._preview_timer = 0
            return False
        dev, phase, jpeg, seq = PREVIEW.state
        self.preview_phase.set_label(tr(phase or "Waiting for the cameras…"))
        if not jpeg:
            self.preview_pic.set_paintable(None)
            self.preview_tag.set_visible(False)
        elif seq != self._preview_seq:
            self._preview_seq = seq
            img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                self.preview_pic.set_paintable(to_texture(img, PREVIEW_VIEW))
            if dev != self._preview_dev:
                self._preview_dev = dev
                self.preview_tag.set_label(cam_name(profiles.port_key(dev)))
            self.preview_tag.set_visible(True)
        return True

    def _poll_job(self):
        self._render_job()
        if JOB.running:
            return True
        self._job_timer = 0
        self._job_t0 = 0.0
        j = JOB.to_json()
        if j["name"] == "optimize" and j["result"] and not j["error"]:
            self.tab_toggle.set_active_name("compare")  # fills once refresh_status reloads the pictures
        self.refresh_status()
        return False

    def _render_job(self):
        j = JOB.to_json()
        if not j["steps"]:
            return
        self.job_empty.set_visible(False)
        self.job_box.set_visible(True)
        restoring = j["name"] == "restore"
        applying = j["name"] == "apply"
        watching = j["running"] and j["name"] == "optimize"
        self.cam_list.set_visible(not watching)
        self.preview_box.set_visible(watching)
        if j["running"]:
            self.hero_title.set_label(tr("Restoring original settings…") if restoring else
                                      tr("Applying the calibration…") if applying else tr("Optimizing…"))
            self.hero_text.set_label(tr("Please don't touch the board or the cameras."))
            self.guide.set_visible(False)
            el = time.time() - (self._job_t0 or time.time())
            eta = ETA_S.get(j["name"], ETA_S["optimize"])
            self.progress.set_fraction(min(el / eta, 0.97))
            left = max(0, int(eta - el))
            self.lbl_time.set_label(f"{int(el) // 60}:{int(el) % 60:02d} · " +
                                    (tr("about {time} left").format(time=f"{left // 60}:{left % 60:02d}") if left
                                     else tr("almost done")))
        else:
            self.progress.set_fraction(0 if j["error"] else 1)
            self.lbl_time.set_label("")
        while (child := self.steps_box.get_first_child()) is not None:
            self.steps_box.remove(child)
        for st in j["steps"]:
            line = Gtk.Box(spacing=8)
            if st["state"] == "running":
                icon = Adw.Spinner()
            else:
                icon_name, css = {"done": ("object-select-symbolic", "success"),
                                  "skipped": ("dialog-warning-symbolic", "warning")}.get(
                    st["state"], ("window-close-symbolic", "error"))
                icon = Gtk.Image.new_from_icon_name(icon_name)
                icon.add_css_class(css)
            icon.set_size_request(16, 16)
            icon.set_valign(Gtk.Align.START)
            line.append(icon)
            lbl = caption(st["title"], wrap=True)
            lbl.set_max_width_chars(60)
            line.append(lbl)
            self.steps_box.append(line)
        adj = self.steps_scroll.get_vadjustment()
        GLib.idle_add(lambda: adj.set_value(adj.get_upper()) and False)
        self.log_view.get_buffer().set_text("\n".join(j["log"]))
        note, kind = "", ""
        if j["error"]:
            note, kind = tr("Something went wrong: {error}. Your previous settings are still in place.").format(
                error=tr(j["error"])), "bad"
        elif not j["running"] and applying and j["result"]:
            r = j["result"]
            note, kind = (tr("Applied. Saved as “{preset}”, loaded at every start.").format(preset=r["preset"])
                          + (" " + tr("Autodarts now runs at {resolution}.").format(resolution=r["resolution"])
                             if r.get("resolution") else "")
                          + {"started": " " + tr("Autodarts is calibrating the board at it."),
                             "needed": " " + tr("Calibrate the board in Autodarts at it.")}.get(r.get("board"), "")), "ok"
        elif not j["running"] and not restoring and j["result"]:
            qs = {r.get("mjpeg_q") for r in j["result"].values()}
            first = next(iter(j["result"].values()))
            change = (" " + tr("Applying sets Autodarts to {resolution} (needs a new board calibration if it"
                               " has none at it).").format(resolution=first["resolution"])
                      if first.get("resolution_change") else "")
            note, kind = ((tr("All cameras calibrated and matched at {resolution}").format(resolution=first["resolution"])
                           if first.get("resolution") else tr("All cameras calibrated and matched"))
                          + (" · " + tr("JPEG level {q}").format(q=qs.pop()) if len(qs) == 1 else "")
                          + "." + change + " " + tr("Review before & after, then apply.")), "review"
        elif not j["running"] and restoring:
            note, kind = tr("Original settings restored."), ""
        self.job_note.set_label(note)
        self.job_note.set_visible(bool(note))
        self.btn_save_preset.set_visible(False)  # saving happens on apply
        self.btn_after.set_visible(kind in ("ok", "review"))
        self.btn_after.set_label(tr("Review & apply") if kind == "review" else tr("See before & after"))
        for c in ("camopt-note-ok", "camopt-note-bad"):
            self.job_note.remove_css_class(c)
        if kind:
            self.job_note.add_css_class(f"camopt-note-{'ok' if kind == 'review' else kind}")

    # --- before / after ---

    def _load_before_after(self):
        def done(ba):
            self.ba = ba
            self._on_ba_pick()

        run_async(oneclick.before_after_info, on_done=done)

    def _on_ba_pick(self):
        pending = any(b.get("pending") for b in self.ba)
        self.ba_pending.set_visible(pending and not JOB.running)
        self.btn_ba_preset.set_visible(not pending)
        name = self.strip.active
        info = next((b for b in self.ba if b["port"] == name), None)
        cam = self._cam(name)
        if info is None:
            self.ba_stack.set_visible_child_name("empty")
            self.ba_empty.set_label(tr("Before and after pictures appear here after an optimization.") if not self.ba else
                                    tr("No before and after pictures for {camera} yet.").format(camera=cam_name(name))
                                    if name else "")
            self.ba_imgs, self.ba_data, self.ba_key = None, None, ""
            self.ba_note.set_label("")
            self._render_stats(None)
            return
        meta = info.get("meta") or {}
        after = meta.get("after", {})
        stamp = ""
        if after.get("time"):
            stamp = (tr("Calibrated {when}") if info.get("pending") else tr("Optimized {when}")).format(when=after["time"]) + " · "
        self.ba_note.set_label(stamp + tr("drag the handle · double-tap or scroll to zoom"))
        # the meta time is per minute: the file times tell a rerun within the same minute apart
        d = info["dir"]
        stamps = [(d / f"{w}.jpg").stat().st_mtime_ns if (d / f"{w}.jpg").exists() else 0 for w in ("before", "after")]
        key = name + repr(meta) + repr(stamps)
        if key == self.ba_key:
            return
        self.ba_key = key

        def load():
            imgs = [live.board_view(info["device"], d / f"{w}.jpg") for w in ("before", "after")]
            if any(i is None for i in imgs) or imgs[0].shape != imgs[1].shape:
                return None
            sizes = [(d / f"{w}.jpg").stat().st_size / 1024 for w in ("before", "after")]
            imgs = self._crop_black(imgs)
            return {"imgs": tuple(imgs), "kb": sizes, "stats": [self._stats(i) for i in imgs],
                    "q": info["q"], "port": info["port"]}

        def done(data):
            if key != self.ba_key:
                return  # the camera (or its pictures) changed while loading
            self.ba_data = data
            self.ba_imgs = data["imgs"] if data else None
            self.ba_stack.set_visible_child_name("picture" if data else "empty")
            if not data:
                self.ba_empty.set_label(tr("The before and after pictures cannot be compared."))
            self._render_stats(data)
            self._render_ba()

        run_async(load, on_done=done)

    @staticmethod
    def _crop_black(imgs: list, pad: int = 6) -> list:
        """Cut the black margin (outside the warped board view) so the board fills the view."""
        lum = np.maximum(cv2.cvtColor(imgs[0], cv2.COLOR_BGR2GRAY), cv2.cvtColor(imgs[1], cv2.COLOR_BGR2GRAY))
        ys, xs = np.where(lum > 14)
        if len(ys) < 100:
            return imgs
        y0, y1 = max(ys.min() - pad, 0), min(ys.max() + pad, lum.shape[0])
        x0, x1 = max(xs.min() - pad, 0), min(xs.max() + pad, lum.shape[1])
        return [i[y0:y1, x0:x1] for i in imgs]

    @staticmethod
    def _stats(img: np.ndarray) -> dict:
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        px = g[g > 14]  # only the picture itself, not the black margin
        if px.size == 0:
            px = g.ravel()
        return {"white": float(np.percentile(px, 97)), "clip": float(100 * (px >= 250).mean())}

    def _render_stats(self, data: Optional[dict]):
        t = self.ba_table
        while (child := t.get_first_child()) is not None:
            t.remove(child)
        has = data is not None
        self.ba_chip.set_visible(False)
        self.btn_ba_preset.set_sensitive(has)
        name = cam_name(self.strip.active) if self.strip.active else ""
        self.ba_title.set_label(f"{tr('RESULT')} · {name.upper()}" if has else tr("RESULT"))
        if not has:
            return
        b, a = data["stats"]
        rows = [("White level", f"{b['white']:.0f}", f"{a['white']:.0f}", a["white"] >= b["white"]),
                ("Clipped", f"{b['clip']:.1f} %", f"{a['clip']:.1f} %", a["clip"] <= max(b["clip"], 1)),
                ("JPEG level", str(data["q"][0] or "–"), str(data["q"][1] or "–"), True),
                ("Size / frame", f"{data['kb'][0]:.0f} KB", f"{data['kb'][1]:.0f} KB", None)]
        for c, h in enumerate(("", "BEFORE", "AFTER")):
            t.attach(Gtk.Label(label=tr(h), xalign=1 if c else 0, css_classes=["camopt-table-head"]), c, 0, 1, 1)
        for r, (label, before, after, good) in enumerate(rows, 1):
            t.attach(Gtk.Label(label=tr(label), xalign=0, css_classes=["camopt-cell"]), 0, r, 1, 1)
            t.attach(Gtk.Label(label=before, xalign=1, hexpand=True, css_classes=["camopt-cell"]), 1, r, 1, 1)
            la = Gtk.Label(label=after, xalign=1, css_classes=["camopt-cell"])
            if good:
                la.add_css_class("camopt-ok")
            t.attach(la, 2, r, 1, 1)
        others = [c for c in (self.status or {}).get("cameras", []) if c["optimized"] and c["port"] != self.strip.active]
        ref = (self._cam() or {}).get("info", {}).get("white")
        diffs = [abs(ref - (c.get("info") or {}).get("white", ref)) / ref for c in others
                 if ref and (c.get("info") or {}).get("white")]
        if diffs:
            worst = max(diffs) * 100
            set_chip(self.ba_chip, tr("✓ Matches the other cameras within {pct:.0f} %").format(pct=max(worst, 1)) if worst < 5
                     else tr("Differs from another camera by {pct:.0f} %").format(pct=worst), "ok" if worst < 5 else "warn")
            self.ba_chip.set_visible(True)

    def _ba_set_mode(self, mode: str):
        self.ba_mode = mode
        self._render_ba()

    def _ba_crop(self):
        """(x0, y0, w, h) of the zoomed view in full-picture pixels."""
        ih, iw = self.ba_imgs[0].shape[:2]
        cw, ch = iw / self.ba_zoom, ih / self.ba_zoom
        x0 = min(max(self.ba_center[0] * iw - cw / 2, 0), iw - cw)
        y0 = min(max(self.ba_center[1] * ih - ch / 2, 0), ih - ch)
        return x0, y0, cw, ch

    def _ba_shown(self):
        """(x0, y0, w, h) of the picture inside the widget (ContentFit.CONTAIN letterboxes it)."""
        w, h = self.ba_picture.get_width(), self.ba_picture.get_height()
        ih, iw = self._ba_comp
        s = min(w / iw, h / ih) if w > 0 and h > 0 else 0
        return (w - iw * s) / 2, (h - ih * s) / 2, iw * s, ih * s

    def _ba_pane(self, x: float, sx: float, sw: float) -> float:
        """Horizontal position within one picture (side by side shows two next to each other)."""
        f = min(max((x - sx) / sw, 0.0), 1.0) if sw > 0 else 0.0
        return (f * 2) % 1.0 if self.ba_mode == "side" and f < 1 else f

    def _ba_drag_begin(self, x: float):
        """Zoomed in: grab and move the picture. At 1× in split view the drag sets the divider."""
        if not self.ba_imgs:
            return
        if self.ba_zoom > 1:
            self._ba_drag_center = self.ba_center
            self.ba_picture.set_cursor_from_name("grabbing")
        elif self.ba_mode == "split":
            self._ba_divider_to(x)

    def _ba_drag_update(self, x0: float, dx: float, dy: float):
        if not self.ba_imgs:
            return
        if self.ba_zoom == 1:
            if self.ba_mode == "split":
                self._ba_divider_to(x0 + dx)
            return
        _sx, _sy, sw, sh = self._ba_shown()
        if sw <= 0 or sh <= 0:
            return
        if self.ba_mode == "side":
            sw /= 2
        cx, cy = self._ba_drag_center
        # the picture follows the finger: the view centre moves the other way, by the share of the full picture
        self.ba_center = (cx - dx / sw / self.ba_zoom, cy - dy / sh / self.ba_zoom)
        x, y, cw, ch = self._ba_crop()
        ih, iw = self.ba_imgs[0].shape[:2]
        self.ba_center = ((x + cw / 2) / iw, (y + ch / 2) / ih)  # clamped to the picture edges
        self._render_ba()

    def _ba_divider_to(self, x: float):
        sx, _sy, sw, _sh = self._ba_shown()
        if sw > 0:
            self.ba_adj.set_value(max(0.0, min(100.0, 100 * (x - sx) / sw)))

    def _ba_cursor(self):
        self.ba_picture.set_cursor_from_name("grab" if self.ba_zoom > 1 else None)

    def _ba_zoom_at(self, x: float, y: float, zoom: int):
        """Zoom with the picture point under (x, y) as the new centre."""
        if not self.ba_imgs:
            return
        sx, sy, sw, sh = self._ba_shown()
        if sw <= 0 or sh <= 0:
            return
        x0, y0, cw, ch = self._ba_crop()
        ih, iw = self.ba_imgs[0].shape[:2]
        fx = self._ba_pane(x, sx, sw)
        fy = min(max((y - sy) / sh, 0.0), 1.0)
        self.ba_center = ((x0 + fx * cw) / iw, (y0 + fy * ch) / ih)
        self._ba_set_zoom(zoom)

    def _ba_scroll(self, _c, _dx: float, dy: float) -> bool:
        if not self.ba_imgs or dy == 0:
            return False
        i = BA_ZOOMS.index(self.ba_zoom) + (1 if dy < 0 else -1)
        x, y = getattr(self, "_ba_pointer", (self.ba_picture.get_width() / 2, self.ba_picture.get_height() / 2))
        self._ba_zoom_at(x, y, BA_ZOOMS[min(max(i, 0), len(BA_ZOOMS) - 1)])
        return True

    def _ba_set_zoom(self, zoom: int):
        self.ba_zoom = zoom
        if zoom == 1:
            self.ba_center = (0.5, 0.5)
        if self.ba_zoom_toggle.get_active_name() != str(zoom):
            self.ba_zoom_toggle.set_active_name(str(zoom))  # re-enters with the same zoom: harmless
        self._ba_cursor()
        self._render_ba()

    def _render_ba(self):
        if not self.ba_imgs:
            self.ba_picture.set_paintable(None)
            return
        x0, y0, cw, ch = (int(round(v)) for v in self._ba_crop())
        before, after = (i[y0:y0 + ch, x0:x0 + cw] for i in self.ba_imgs)
        mode = self.ba_mode
        wide = cw * (2 if mode == "side" else 1)
        s = BA_VIEW / max(wide, ch)
        interp = cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC
        before = cv2.resize(before, None, fx=s, fy=s, interpolation=interp)
        after = cv2.resize(after, None, fx=s, fy=s, interpolation=interp)
        self.tag_before.set_visible(mode != "diff")
        self.tag_after.set_visible(mode != "diff")
        if mode == "side":
            gap = np.full((before.shape[0], 4, 3), 255, np.uint8)
            comp = np.hstack([before, gap, after])
        elif mode == "diff":
            comp = cv2.convertScaleAbs(cv2.absdiff(before, after), alpha=3.0)
        else:
            x = int(after.shape[1] * self.ba_adj.get_value() / 100)
            comp = after.copy()
            comp[:, :x] = before[:, :x]
            cv2.line(comp, (x, 0), (x, comp.shape[0] - 1), (255, 255, 255), 2)
            cy, r = comp.shape[0] // 2, max(14, comp.shape[0] // 28)
            cv2.circle(comp, (x, cy), r, (255, 255, 255), -1, cv2.LINE_AA)
            k = r // 2
            for sgn in (-1, 1):  # two small arrows pointing apart
                tip = (x + sgn * (k + k // 2), cy)
                pts = np.array([tip, (x + sgn * k // 4, cy - k // 2), (x + sgn * k // 4, cy + k // 2)], np.int32)
                cv2.fillPoly(comp, [pts], (40, 40, 40), cv2.LINE_AA)
        self._ba_comp = comp.shape[:2]
        self.ba_picture.set_paintable(to_texture(comp))

    def _consented(self) -> bool:
        c = (self.status or {}).get("consent")
        return bool(c and c.get("hbv_confirmed"))


def status_dot_box(text: str, kind: str) -> Gtk.Box:
    """● text with the dot coloured by kind (used where the text should be the camera's name)."""
    box = Gtk.Box(spacing=6)
    box.append(Gtk.Label(label="●", css_classes=["camopt-dot", f"camopt-{kind}"]))
    box.append(Gtk.Label(label=text, xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["camopt-cell", "heading"]))
    return box
