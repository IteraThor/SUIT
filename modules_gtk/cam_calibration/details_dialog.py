"""Camera Optimizer Details dialog: technical values for support and testing.

Everything the main wizard keeps out of sight lives here: settings before/after, scores, the
A/B spread and vetoes, per-tip metrics, the detector result, the engine log, the detector
overlay and Edges pictures, and the steel-point cross-section before against after.
"""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING, List, Optional, Sequence

import numpy as np

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Pango

from core.cv_utils import apply_edge_contrast_filter
from core.dart_tip_detector import draw_tip_overlay
from core.metrics import tip_cross_section
from core.optimizer import OptimizationResult
from core.v4l2_device import CameraSettings
from modules_gtk.cam_calibration import widgets as W
from modules_gtk.cam_calibration.camera_source import cam_key

if TYPE_CHECKING:
    from modules_gtk.cam_calibration_view import CamCalibrationView

logger = logging.getLogger("cam_librator.details")

PICTURE_MODES = (("overlay", "Detector"), ("normal", "After"), ("edge", "Edges"))


def _arrow(a, b, fmt="{}") -> str:
    if a is None and b is None:
        return "–"
    sa = fmt.format(a) if a is not None else "–"
    sb = fmt.format(b) if b is not None else "–"
    return sa if sa == sb else f"{sa} → {sb}"


def _exposure_text(s: CameraSettings) -> str:
    if s.exposure_lock:
        ms = f" ~{s.exposure_absolute / 10.0:.1f} ms" if s.exposure_absolute > 0 else ""
        return f"locked auto (blc {s.backlight_compensation}){ms}"
    if s.exposure_auto != 1:
        return "auto"
    return f"{s.exposure_absolute / 10.0:.1f} ms"


def settings_rows(res: Optional[OptimizationResult]) -> List[str]:
    if res is None:
        return ["–"] * 5
    a: CameraSettings = res.initial_settings
    b: CameraSettings = res.optimized_settings
    return [
        _arrow(_exposure_text(a), _exposure_text(b)),
        _arrow(a.gain, b.gain),
        _arrow(a.sharpness, b.sharpness),
        _arrow(a.contrast, b.contrast),
        _arrow(a.gamma, b.gamma),
    ]


def score_rows(res: Optional[OptimizationResult]) -> List[str]:
    if res is None:
        return ["–"] * 6
    a = res.initial_tip_score.total if res.initial_tip_score else res.initial_metrics.composite_score
    b = res.optimized_tip_score.total if res.optimized_tip_score else res.optimized_metrics.composite_score
    if res.darts_moved:
        decision = "keep (darts moved)"
    elif res.is_improvement:
        decision = "apply"
    else:
        decision = "keep"
    return [
        _arrow(a, b, "{:.1f}"),
        _arrow(res.tip_component_before, res.tip_component_after, "{:.3f}"),
        f"{res.improvement_percentage:+.1f} % (allowed down to {res.required_improvement_pct:+.1f} %)",
        f"{res.ab_spread_pct:.1f} %",
        res.veto or "none",
        ("board only · " if res.fallback_board_only else "") + decision,
    ]


class DetailsDialog(Adw.Dialog):
    """Adw.Dialog with the technical table, per-tip metrics and diagnostic pictures."""

    def __init__(self, view: CamCalibrationView):
        super().__init__(title="Details", content_width=820, content_height=560)
        self.view = view
        self.keys = [cam_key(c) for c in view.cameras]

        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroll.set_propagate_natural_width(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for m in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{m}")(18)
        scroll.set_child(box)
        self.scroll = scroll
        tv.set_content(scroll)
        self.set_child(tv)

        intro = W.wrap_label("Technical values for support and testing. Values fill in as each camera "
                             "finishes.", ("dim-label",), max_chars=80)
        box.append(intro)

        titles = [view.cam_title(k) for k in self.keys]
        box.append(self._section("Settings and scores"))
        rows = [("Shutter",), ("Gain",), ("Sharpness",), ("Contrast",), ("Gamma",),
                ("Score",), ("Tip component",), ("Change",), ("A/B spread",), ("Veto",), ("Decision",)]
        cols = []
        for k in self.keys:
            res = view.results.get(k)
            col = settings_rows(res) + score_rows(res)
            if res is None and k in view.failed:
                col[-1] = "failed: " + view.failed[k]
            cols.append(col)
        box.append(self._table([""] + titles, [[r[0]] + [c[i] for c in cols] for i, r in enumerate(rows)]))

        box.append(self._section("Dart finding"))
        box.append(self._mono(self._detection_text()))

        box.append(self._section("Tips per camera"))
        box.append(self._table(["", "Tip", "State", "C (DN)", "CNR", "Edge width", "Halo", "Glare"],
                               self._tip_rows()))

        box.append(self._section("Pictures"))
        box.append(self._pictures(titles))

        box.append(self._section("Steel point cross-section"))
        box.append(W.wrap_label("Brightness across the steel point, before against after, with the "
                                "surrounding board at 0. Height is the tip contrast (the score counts it "
                                "only up to 60 DN, so a drop from a much higher value is not a loss), "
                                "width is the edge width, and a dip beside the peak is ringing.",
                                ("dim-label", "caption"), max_chars=90))
        box.append(self._profiles())

        box.append(self._section("Engine log"))
        log: List[str] = []
        for k, t in zip(self.keys, titles):
            res = view.results.get(k)
            if res is not None and res.details:
                log.append(f"{t} ({k})")
                log.extend("  " + line for line in res.details)
            elif k in view.failed:
                log.append(f"{t} ({k}): {view.failed[k]}")
        box.append(self._mono("\n".join(log) or "No camera tuned yet."))

    # -- building blocks ----------------------------------------------------------------

    @staticmethod
    def _section(text: str) -> Gtk.Label:
        lbl = Gtk.Label(label=text, xalign=0.0)
        lbl.add_css_class("heading")
        return lbl

    @staticmethod
    def _mono(text: str) -> Gtk.Label:
        lbl = Gtk.Label(label=text, xalign=0.0, selectable=True, wrap=True,
                        wrap_mode=Pango.WrapMode.WORD_CHAR, max_width_chars=90)
        lbl.add_css_class("monospace")
        lbl.add_css_class("caption")
        return lbl

    @staticmethod
    def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> Gtk.Widget:
        grid = Gtk.Grid(column_spacing=18, row_spacing=6)
        for c, h in enumerate(header):
            lbl = Gtk.Label(label=h, xalign=0.0)
            lbl.add_css_class("dim-label")
            lbl.add_css_class("caption-heading")
            grid.attach(lbl, c, 0, 1, 1)
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                lbl = Gtk.Label(label=str(val), xalign=0.0)
                lbl.add_css_class("numeric")
                if c == 0:
                    lbl.add_css_class("dim-label")
                grid.attach(lbl, c, r + 1, 1, 1)
        sw = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        sw.set_propagate_natural_width(False)
        sw.set_child(grid)
        return sw

    def _detection_text(self) -> str:
        v = self.view
        det = v.detection
        if v.board_only:
            return "No dart finding: tuned on the whole board" + (
                f" ({v.board_only_reason})." if v.board_only_reason else ".")
        if det is None:
            return "Not run yet."
        lines = [f"{len(det.tips)} tips in {det.runtime_s:.2f} s"
                 + (f" · warnings: {', '.join(det.warnings)}" if det.warnings else "")]
        for t in det.tips:
            lines.append(f"Tip {t.id}: {t.ring_hint} at ({t.board_mm[0]:.0f}, {t.board_mm[1]:.0f}) mm · "
                         f"{t.n_cams} cameras · confidence {t.confidence:.2f} · "
                         f"line residual {t.line_residual_mm:.2f} mm")
        for k, rej in det.rejected.items():
            if rej:
                lines.append(f"{v.cam_title(k)}: {len(rej)} rejected axes")
        if v.moved_after_run:
            lines.append(f"End-of-run check: tips {v.moved_after_run} moved")
        return "\n".join(lines)

    def _tip_rows(self) -> List[List[str]]:
        v = self.view
        rows: List[List[str]] = []
        for k in self.keys:
            res = v.results.get(k)
            rois = list(res.tips) if res is not None and res.tips else v.tip_rois.get(k, [])
            before = res.initial_tip_score.tips if res is not None and res.initial_tip_score else []
            after = res.optimized_tip_score.tips if res is not None and res.optimized_tip_score else []
            probes = {hp.dart_id: hp for hp in res.hidden_probe} if res is not None else {}
            for i, r in enumerate(rois):
                a = before[i] if i < len(before) else None
                b = after[i] if i < len(after) else None
                reason = (r.hidden_reason or "").split(" (")[0]
                if r.faint:
                    reason = reason.replace(" at the estimated position", "")   # said once, below
                state = "visible" + (" (marginal)" if r.marginal else "") if r.visible else f"hidden: {reason}"
                hp = probes.get(r.dart_id)
                if hp is not None:      # faint tip: numbers at the ESTIMATED position, before -> after
                    state += " · now visible" if hp.became_visible else " · estimated position"
                    c_cell = _arrow(hp.before_contrast, hp.after_contrast, "{:.0f}")
                    cnr_cell = _arrow(hp.before_cnr, hp.after_cnr, "{:.1f}")
                elif not r.visible and r.faint:
                    c_cell = _arrow(r.est_contrast or None, None, "{:.0f}")
                    cnr_cell = _arrow(r.est_cnr or None, None, "{:.1f}")
                    state += " · estimated position"
                else:
                    c_cell = _arrow(a.contrast if a else (r.ref_contrast or None), b.contrast if b else None, "{:.0f}")
                    cnr_cell = _arrow(a.cnr if a else None, b.cnr if b else None, "{:.1f}")
                rows.append([
                    v.cam_title(k), str(r.dart_id), state,
                    c_cell,
                    cnr_cell,
                    _arrow(a.edge_width if a else None, b.edge_width if b else None, "{:.2f}"),
                    _arrow(a.halo if a else None, b.halo if b else None, "{:.2f}"),
                    _arrow(a.spec_clip * 100 if a else None, b.spec_clip * 100 if b else None, "{:.1f} %"),
                ])
        return rows or [["–"] * 8]

    def _pictures(self, titles: Sequence[str]) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        controls = Gtk.Box(spacing=8)
        self.cam_dd = Gtk.DropDown.new_from_strings(list(titles) or ["–"])
        if self.view.selected in self.keys:
            self.cam_dd.set_selected(self.keys.index(self.view.selected))
        self.cam_dd.connect("notify::selected", lambda *_: self._on_camera_changed())
        controls.append(self.cam_dd)
        self.mode = "overlay"
        first = None
        modes = Gtk.Box(css_classes=["linked"])
        for key, label in PICTURE_MODES:
            b = Gtk.ToggleButton(label=label)
            if first is None:
                first = b
                b.set_active(True)
            else:
                b.set_group(first)
            b.connect("toggled", lambda btn, k=key: btn.get_active() and self._set_mode(k))
            modes.append(b)
        controls.append(modes)
        box.append(controls)
        self.picture = W.FlexPicture(640, 480, Gtk.ContentFit.CONTAIN)
        self.picture.set_size_request(-1, 360)
        self.empty = Gtk.Label(label="No picture yet.", xalign=0.0)
        self.empty.add_css_class("dim-label")
        box.append(self.picture)
        box.append(self.empty)
        self._update_picture()
        return box

    def _set_mode(self, mode: str):
        self.mode = mode
        self._update_picture()

    def _picture_frame(self) -> Optional[np.ndarray]:
        v = self.view
        idx = self.cam_dd.get_selected()
        if not self.keys or idx >= len(self.keys):
            return None
        key = self.keys[idx]
        res = v.results.get(key)
        burst = v.find_frames.get(key)
        if self.mode == "overlay":
            if burst and v.detection is not None:
                return draw_tip_overlay(burst[0], v.detection, key)
            return burst[0] if burst else None
        after = res.optimized_frame if res is not None else (burst[0] if burst else None)
        if after is None:
            return None
        if self.mode == "edge":
            return apply_edge_contrast_filter(after)
        return after

    def _profiles(self) -> Gtk.Widget:
        self.profile_row = Gtk.Box(spacing=12, homogeneous=True)
        self.profile_plots = [W.ProfilePlot() for _ in range(3)]
        for plot in self.profile_plots:
            self.profile_row.append(plot)
        self.profile_empty = Gtk.Label(label="No tips to plot (this camera was tuned on the whole board).",
                                      xalign=0.0)
        self.profile_empty.add_css_class("dim-label")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(self.profile_row)
        box.append(self.profile_empty)
        self._update_profiles()
        return box

    def _on_camera_changed(self) -> None:
        self._update_picture()
        self._update_profiles()

    def _update_profiles(self) -> None:
        idx = self.cam_dd.get_selected()
        key = self.keys[idx] if self.keys and idx < len(self.keys) else None
        res = self.view.results.get(key) if key else None
        tips = [t for t in (res.tips if res is not None and res.tips else []) if t.visible][:3]
        for i, plot in enumerate(self.profile_plots):
            if i >= len(tips):
                plot.set_visible(False)
                continue
            roi = tips[i]
            try:
                before = tip_cross_section(res.initial_frame, roi)
                after = before if not res.is_improvement else tip_cross_section(res.optimized_frame, roi)
            except Exception as e:
                logger.error("Profile plot failed: %s", e)
                before = after = None
            plot.set_visible(True)
            plot.set_data(f"{self.view.cam_title(key)} · Tip {roi.dart_id}", before, after)
        self.profile_empty.set_visible(not tips)
        self.profile_row.set_visible(bool(tips))

    def _update_picture(self):
        try:
            frame = self._picture_frame()
        except Exception as e:
            logger.error("Details picture failed: %s", e)
            frame = None
        self.picture.set_frame(frame, 1280, 960)
        self.picture.set_visible(frame is not None)
        self.empty.set_visible(frame is None)
