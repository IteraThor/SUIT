"""Camera Optimizer page: one-click calibration of HBV OV2710 cameras (core.hbvcal).

Support check per camera, Optimize / Restore as a background job with a step checklist, and a before/after
comparison. Fine-tuning afterwards happens on the Camera Controls page.
"""

from __future__ import annotations
import time
from typing import Callable, List, Optional

import cv2

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from core.hbvcal import live, oneclick, profiles
from core.hbvcal.daemon import cameras
from core.logger import get_logger
from modules_gtk.async_utils import run_async
from modules_gtk.camera_controls_view import ask_save_preset
from modules_gtk.camera_widgets import PortToggles, caption, port_no, status_dot, to_texture, wrap_label

logger = get_logger("camera_optimizer_view")

JOB = oneclick.JOB
BA_VIEW = 960  # longest side of the rendered before/after view (the crop is cut from the full picture)
BA_ZOOMS = (1, 2, 3, 4)


class CameraOptimizerPage(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Camera Optimizer", tag="camera_optimizer")
        self.window = window
        self.status: Optional[dict] = None
        self._status_busy = False
        self._job_timer = 0
        self._consent_asked = False
        self.ba: List[dict] = []
        self.ba_imgs: Optional[tuple] = None
        self.ba_key = ""
        self.ba_zoom = 1
        self.ba_center = (0.5, 0.5)  # zoom centre, as a fraction of the full picture

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_margin_top(8)
        root.set_margin_bottom(10)
        root.set_margin_start(12)
        root.set_margin_end(12)
        self.set_child(root)

        top = Gtk.Box(spacing=10)
        btn_controls = Gtk.Button()
        btn_controls.set_child(Adw.ButtonContent(icon_name="preferences-system-symbolic", label="Camera Controls"))
        btn_controls.set_tooltip_text("Fine-tune every camera setting, keep settings, presets")
        btn_controls.connect("clicked", lambda *_: self.window.open_camera_controls_page())
        top.append(btn_controls)
        top.append(Gtk.Box(hexpand=True))
        self.autodarts_box = Gtk.Box(valign=Gtk.Align.CENTER)
        top.append(self.autodarts_box)
        root.append(top)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        root.append(self.stack)
        self.stack.add_named(self._build_declined(), "declined")
        self.stack.add_named(self._build_overview(), "overview")
        self.stack.set_visible_child_name("overview")

    # ------------------------------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------------------------------

    def on_page_opened(self):
        self.refresh_status()
        if JOB.running:
            self._start_job_poll()

    def on_page_closed(self):
        if self._job_timer:
            GLib.source_remove(self._job_timer)
            self._job_timer = 0

    @property
    def job_thread(self):
        """The running optimize/restore job: the window waits for it before quitting."""
        return JOB.thread if JOB.running else None

    # ------------------------------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------------------------------

    def refresh_status(self):
        if self._status_busy:
            return
        self._status_busy = True

        def work():
            return {"cameras": [oneclick.support(d) for d in cameras()], "consent": profiles.consent(),
                    "autodarts": oneclick.systemctl("is-active")}

        def done(st):
            self._status_busy = False
            self.status = st
            self._render_status()

        def fail(e):
            self._status_busy = False
            self.hero_title.set_label("Can't read the cameras")
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
        ad = st["autodarts"]
        if (child := self.autodarts_box.get_first_child()) is not None:
            self.autodarts_box.remove(child)
        self.autodarts_box.append(status_dot("Autodarts running" if ad == "active" else f"Autodarts {ad or 'not found'}",
                                             "ok" if ad == "active" else "neutral"))
        self._render_cams()
        self._render_job()
        self._load_before_after()

    def _render_cams(self):
        cams = self.status["cameras"]
        while (child := self.cam_list.get_first_child()) is not None:
            self.cam_list.remove(child)
        sup = [c for c in cams if c["supported"]]
        for c in cams:
            if not c["supported"]:
                kind, state, tip = "bad", "Not supported", c.get("reason", "")
            elif c["optimized"]:
                kind, state, tip = "ok", "Optimized", f"Firmware {c.get('firmware')}"
            else:
                kind, state, tip = "warn", "Not optimized", f"Firmware {c.get('firmware')}"
            line = Gtk.Box(spacing=8, tooltip_text=tip)
            line.append(status_dot(f"Port {port_no(c['port'])}", kind))
            line.append(Gtk.Box(hexpand=True))
            line.append(caption(state))
            self.cam_list.append(line)
        if not cams:
            self.cam_list.append(caption("No cameras connected"))

        done = sum(1 for c in sup if c["optimized"])
        if JOB.running:
            pass  # _render_job owns the headline
        elif not sup:
            self.hero_title.set_label("No supported camera found")
            self.hero_text.set_label("Connect your HBV OV2710 cameras, then open this page again.")
        elif done == len(sup):
            self.hero_title.set_label("Your camera is optimized" if len(sup) == 1 else f"All {len(sup)} cameras are optimized")
            self.hero_text.set_label("Settings stay applied, also after Autodarts restarts. Run it again after changing "
                                     "the lights or moving the board.")
        else:
            n = len(sup)
            self.hero_title.set_label("Ready to optimize")
            self.hero_text.set_label(f"Calibrates exposure, gain and JPEG quality for {n} camera{'s' if n > 1 else ''} "
                                     "and matches them. Takes about 3 minutes.")
        self.btn_optimize.set_label("Optimize again" if sup and done == len(sup) else "Optimize cameras")
        self.btn_optimize.set_sensitive(bool(sup) and not JOB.running)
        self.btn_restore.set_visible(any(c.get("optimized") for c in cams) and not JOB.running)

    # ------------------------------------------------------------------------------------------
    # Overview
    # ------------------------------------------------------------------------------------------

    def _build_declined(self) -> Gtk.Widget:
        page = Adw.StatusPage(icon_name="camera-disabled-symbolic", title="Optimization is off")
        page.set_description("This tool changes hidden settings that were only tested on HBV OV2710 cameras, "
                             "so it stays off for other cameras.")
        btn = Gtk.Button(label="I do use HBV OV2710 cameras", halign=Gtk.Align.CENTER)
        btn.add_css_class("pill")
        btn.connect("clicked", lambda *_: self._ask_consent())
        page.set_child(btn)
        return page

    def _build_overview(self) -> Gtk.Widget:
        cols = Gtk.Box(spacing=12)

        # left: status, actions, cameras, job
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        left.set_size_request(330, -1)
        left.set_hexpand(False)  # set explicitly: the spacers inside would otherwise make the panel expand
        left.add_css_class("card")
        left.add_css_class("camopt-panel")
        self.hero_title = wrap_label("Loading…", ["camopt-title"])
        self.hero_title.set_max_width_chars(30)
        self.hero_text = caption("", wrap=True)
        left.append(self.hero_title)
        left.append(self.hero_text)
        self.btn_optimize = Gtk.Button(label="Optimize cameras", sensitive=False)
        self.btn_optimize.add_css_class("suggested-action")
        self.btn_optimize.add_css_class("pill")
        self.btn_optimize.connect("clicked", lambda *_: self._ask_prepare())
        left.append(self.btn_optimize)
        self.btn_restore = Gtk.Button(label="Restore original settings", visible=False, halign=Gtk.Align.CENTER)
        self.btn_restore.add_css_class("flat")
        self.btn_restore.add_css_class("compact-btn")
        self.btn_restore.connect("clicked", lambda *_: self._ask_restore())
        left.append(self.btn_restore)

        left.append(caption("CAMERAS", "camopt-heading"))
        self.cam_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        left.append(self.cam_list)

        self.job_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=False)
        self.job_heading = caption("PROGRESS", "camopt-heading")
        self.job_box.append(self.job_heading)
        self.steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        # a run with failed compression levels adds lines: they scroll (newest kept in view) instead of the page
        self.steps_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                               max_content_height=360, child=self.steps_box)
        self.job_box.append(self.steps_scroll)
        self.job_note = caption("", "camopt-note", wrap=True)
        self.job_note.set_visible(False)
        self.job_box.append(self.job_note)
        job_actions = Gtk.Box(spacing=6)
        self.btn_save_preset = Gtk.Button(label="Save as Preset…", visible=False, css_classes=["compact-btn"])
        self.btn_save_preset.set_tooltip_text("Keep these calibrated settings under a name, to load them again later")
        self.btn_save_preset.connect("clicked", lambda *_: ask_save_preset(
            self.window, f"Optimized {time.strftime('%Y-%m-%d %H:%M')}"))
        job_actions.append(self.btn_save_preset)
        job_actions.append(Gtk.Box(hexpand=True))
        self.log_view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_view.add_css_class("camopt-log")
        log_scroll = Gtk.ScrolledWindow(min_content_width=480, min_content_height=260)
        log_scroll.set_child(self.log_view)
        log_pop = Gtk.Popover(child=log_scroll)
        btn_log = Gtk.MenuButton(label="Log", popover=log_pop, tooltip_text="Details of the last run")
        btn_log.add_css_class("flat")
        btn_log.add_css_class("compact-btn")
        job_actions.append(btn_log)
        self.job_box.append(job_actions)
        left.append(self.job_box)

        left.append(Gtk.Box(vexpand=True))
        left.append(caption("Nothing is flashed: settings live in camera memory and are re-applied by the keeper.",
                            wrap=True))
        cols.append(left)

        # right: before / after, filling the rest
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True)
        right.add_css_class("card")
        right.add_css_class("camopt-panel")
        head = Gtk.Box(spacing=8)
        head.append(Gtk.Label(label="BEFORE AND AFTER", css_classes=["camopt-heading"]))
        head.append(Gtk.Box(hexpand=True))
        self.ba_toggle = PortToggles(self._on_ba_pick)
        head.append(self.ba_toggle.widget)
        self.ba_zoom_toggle = Adw.ToggleGroup(valign=Gtk.Align.CENTER, tooltip_text="Zoom")
        for z in BA_ZOOMS:
            self.ba_zoom_toggle.add(Adw.Toggle(name=str(z), label=f"{z}×"))
        self.ba_zoom_toggle.set_active_name("1")
        self.ba_zoom_toggle.connect("notify::active-name", lambda *_: self._ba_set_zoom(
            int(self.ba_zoom_toggle.get_active_name() or 1)))
        head.append(self.ba_zoom_toggle)
        right.append(head)

        self.ba_stack = Gtk.Stack(vexpand=True)
        empty = Gtk.Label(label="Before and after pictures appear here after an optimization.", wrap=True)
        empty.add_css_class("camopt-empty")
        self.ba_stack.add_named(empty, "empty")
        self.ba_picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self.ba_picture.set_tooltip_text("Double-tap or scroll to zoom into a spot; drag to move around when zoomed")
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
        self.ba_stack.add_named(self.ba_picture, "picture")
        right.append(self.ba_stack)

        row = Gtk.Box(spacing=8)
        row.append(caption("Before"))
        self.ba_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.ba_scale.set_value(50)
        self.ba_scale.set_draw_value(False)
        self.ba_scale.set_hexpand(True)
        self.ba_scale.connect("value-changed", lambda *_: self._render_ba())
        row.append(self.ba_scale)
        row.append(caption("After"))
        right.append(row)
        self.ba_note = caption("")
        right.append(self.ba_note)
        self.ba_controls = [head.get_last_child(), self.ba_toggle.widget, row, self.ba_note]
        cols.append(right)
        return cols

    # --- dialogs ---

    def _ask_consent(self):
        cams = (self.status or {}).get("cameras", [])
        lines = []
        for c in cams:
            hbv = c.get("usb_id") == oneclick.HBV_USB_ID
            what = "HBV camera detected" if hbv else f"other camera ({c.get('usb_id') or 'unknown'})"
            lines.append(f"{'✓' if hbv else '✗'}  USB port {port_no(c['port'])}: {what}")
        body = ("The optimizer uses hidden camera settings (JPEG quality, sensor gain) that were verified on "
                "HBV OV2710 cameras only.")
        if lines:
            body += "\n\n" + "\n".join(lines)
        d = Adw.AlertDialog(heading="Are these HBV OV2710 cameras?", body=body)
        d.add_response("no", "No / Not Sure")
        d.add_response("yes", "Yes, Continue")
        d.set_response_appearance("yes", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("yes")

        def on_resp(_d, resp):
            run_async(profiles.set_consent, lambda _r: self.refresh_status(), None, resp == "yes")

        d.connect("response", on_resp)
        d.present(self.window)

    def _ask_prepare(self):
        d = Adw.AlertDialog(heading="Before You Start",
                            body="1. Remove all darts from the board.\n"
                                 "2. Turn the lights on the way you play.\n"
                                 "3. Keep people and hands away from the board for about 3 minutes.\n\n"
                                 "Autodarts is stopped and restarted automatically.")
        d.add_response("cancel", "Cancel")
        d.add_response("start", "Start")
        d.set_response_appearance("start", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("start")
        d.connect("response", lambda _d, r: r == "start" and self._start_job("optimize", oneclick.optimize, 30))
        d.present(self.window)

    def _ask_restore(self):
        d = Adw.AlertDialog(heading="Restore Original Settings?",
                            body="Your cameras go back to the settings Autodarts uses. Autodarts will restart.")
        d.add_response("cancel", "Cancel")
        d.add_response("restore", "Restore")
        d.set_response_appearance("restore", Adw.ResponseAppearance.DESTRUCTIVE)
        d.connect("response", lambda _d, r: r == "restore" and self._start_job("restore", oneclick.restore))
        d.present(self.window)

    # --- job ---

    def _start_job(self, name: str, fn: Callable, *args):
        live.stop_all()
        if not JOB.start(name, fn, live.pause_live, *args):
            self.window.show_toast("A camera job is already running")
            return
        self._start_job_poll()

    def _start_job_poll(self):
        self.btn_optimize.set_sensitive(False)
        self.btn_restore.set_visible(False)
        if not self._job_timer:
            self._job_timer = GLib.timeout_add(1000, self._poll_job)
        self._render_job()

    def _poll_job(self):
        self._render_job()
        if JOB.running:
            return True
        self._job_timer = 0
        self.refresh_status()
        return False

    def _render_job(self):
        j = JOB.to_json()
        if not j["steps"]:
            return
        self.job_box.set_visible(True)
        restoring = j["name"] == "restore"
        if j["running"]:
            self.hero_title.set_label("Restoring original settings…" if restoring else "Optimizing…")
            self.hero_text.set_label("Please don't touch the board or the cameras.")
        self.job_heading.set_label("RESTORE" if restoring else "PROGRESS")
        while (child := self.steps_box.get_first_child()) is not None:
            self.steps_box.remove(child)
        for st in j["steps"]:
            line = Gtk.Box(spacing=8)
            if st["state"] == "running":
                icon = Adw.Spinner()
            else:
                icon = Gtk.Image.new_from_icon_name("object-select-symbolic" if st["state"] == "done"
                                                    else "window-close-symbolic")
                icon.add_css_class("success" if st["state"] == "done" else "error")
            icon.set_size_request(14, 14)
            icon.set_valign(Gtk.Align.START)
            line.append(icon)
            line.append(caption(st["title"], wrap=True))
            self.steps_box.append(line)
        adj = self.steps_scroll.get_vadjustment()
        GLib.idle_add(lambda: adj.set_value(adj.get_upper()) and False)
        self.log_view.get_buffer().set_text("\n".join(j["log"]))
        note, kind = "", ""
        if j["error"]:
            note, kind = (f"Something went wrong: {j['error']}. Your previous settings are still in place."), "bad"
        elif not j["running"] and not restoring and j["result"]:
            qs = {r.get("mjpeg_q") for r in j["result"].values()}
            note, kind = "All cameras calibrated and matched" + (f" · JPEG level {qs.pop()}" if len(qs) == 1 else "") + ".", "ok"
        elif not j["running"] and restoring:
            note, kind = "Original settings restored.", ""
        self.job_note.set_label(note)
        self.job_note.set_visible(bool(note))
        self.btn_save_preset.set_visible(kind == "ok")
        for c in ("camopt-note-ok", "camopt-note-bad"):
            self.job_note.remove_css_class(c)
        if kind:
            self.job_note.add_css_class(f"camopt-note-{kind}")

    # --- before / after ---

    def _load_before_after(self):
        def done(ba):
            self.ba = ba
            self.ba_stack.set_visible_child_name("picture" if ba else "empty")
            for w in self.ba_controls:
                w.set_sensitive(bool(ba))
            if not ba:
                self.ba_imgs = None
                self.ba_note.set_label("")
                return
            self.ba_toggle.sync([b["port"] for b in ba])
            self._on_ba_pick(force=True)

        run_async(oneclick.before_after_info, on_done=done)

    def _on_ba_pick(self, force: bool = False):
        if self.ba_toggle.building or not self.ba:
            return
        name = self.ba_toggle.active
        info = next((b for b in self.ba if b["port"] == name), self.ba[0])
        meta = info.get("meta") or {}
        key = name + repr(meta)
        if key == self.ba_key and not force:
            return
        after = meta.get("after", {})
        self.ba_note.set_label((f"Optimized {after['time']} · " if after.get("time") else "") +
                               "double-tap or scroll to zoom, drag to move")
        if key == self.ba_key:
            return
        self.ba_key = key

        def load():
            d = oneclick.BEFORE_AFTER / info["port"]
            imgs = [live.board_view(info["device"], d / f"{w}.jpg") for w in ("before", "after")]
            if any(i is None for i in imgs) or imgs[0].shape != imgs[1].shape:
                return None
            return tuple(imgs)  # full resolution: zoomed views are cut from it

        def done(imgs):
            self.ba_imgs = imgs
            self._render_ba()

        run_async(load, on_done=done)

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
        ih, iw = self.ba_imgs[0].shape[:2]
        s = min(w / iw, h / ih) if w > 0 and h > 0 else 0
        return (w - iw * s) / 2, (h - ih * s) / 2, iw * s, ih * s

    def _ba_drag_begin(self, x: float):
        """Zoomed in: grab and move the picture. At 1× there is nothing to move: the drag sets the divider."""
        if not self.ba_imgs:
            return
        if self.ba_zoom > 1:
            self._ba_drag_center = self.ba_center
            self.ba_picture.set_cursor_from_name("grabbing")
        else:
            self._ba_divider_to(x)

    def _ba_drag_update(self, x0: float, dx: float, dy: float):
        if not self.ba_imgs:
            return
        if self.ba_zoom == 1:
            self._ba_divider_to(x0 + dx)
            return
        _sx, _sy, sw, sh = self._ba_shown()
        if sw <= 0 or sh <= 0:
            return
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
            self.ba_scale.set_value(max(0.0, min(100.0, 100 * (x - sx) / sw)))

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
        fx = min(max((x - sx) / sw, 0.0), 1.0)
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
        s = BA_VIEW / max(cw, ch)
        interp = cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC
        before = cv2.resize(before, None, fx=s, fy=s, interpolation=interp)
        after = cv2.resize(after, None, fx=s, fy=s, interpolation=interp)
        x = int(after.shape[1] * self.ba_scale.get_value() / 100)
        comp = after.copy()
        comp[:, :x] = before[:, :x]
        cv2.line(comp, (x, 0), (x, comp.shape[0] - 1), (255, 255, 255), 2)
        self.ba_picture.set_paintable(to_texture(comp))

    def _consented(self) -> bool:
        c = (self.status or {}).get("consent")
        return bool(c and c.get("hbv_confirmed"))
