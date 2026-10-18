"""Step 4 (Review): before/after tip close-ups per camera, Apply (improved cameras only) / Discard."""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING, List, Optional, Tuple

import numpy as np

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from core.camera_manual_service import CameraManualService
from core.metrics import TipMetrics
from core.optimizer import OptimizationResult
from core.v4l2_device import V4L2Device
from modules_gtk.cam_calibration import widgets as W
from modules_gtk.cam_calibration.camera_source import cam_key
from modules_gtk.cam_calibration.step_tune import tip_crop

if TYPE_CHECKING:
    from modules_gtk.cam_calibration_view import CamCalibrationView

logger = logging.getLogger("cam_librator.review")

APPLIED_TOAST = "Settings applied. They load every time the computer starts."


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def review_row(res: Optional[OptimizationResult]) -> Tuple[str, str, str]:
    """(subtitle, chip kind, chip text) for a camera in the review list."""
    if res is None:
        return "Could not be tuned", "bad", "Failed"
    label, _cls = res.badge()
    if res.darts_moved:
        return "Not changed", "warn", label
    if res.fallback_board_only:
        return "Tuned on the whole board", "ok" if res.is_improvement else "neutral", label
    if res.is_improvement:
        pct = res.improvement_percentage
        sub = "Much clearer tips" if pct >= 25 else "Clearer tips" if pct >= 10 else "A little clearer"
        return sub, "ok", label
    return "Already well tuned", "neutral", label


def _pairs(res: OptimizationResult) -> List[Tuple[TipMetrics, TipMetrics]]:
    a = res.initial_tip_score.tips if res.initial_tip_score else []
    b = res.optimized_tip_score.tips if res.optimized_tip_score else []
    return [(x, y) for x, y in zip(a, b) if x is not None and y is not None]


def _what_changed(res: OptimizationResult) -> str:
    """One plain sentence about the metric that improved most."""
    gains = {}
    pairs = _pairs(res)
    if pairs:
        mean = lambda vals: float(np.mean(vals)) if vals else 0.0
        ew0, ew1 = mean([p.edge_width for p, _ in pairs]), mean([q.edge_width for _, q in pairs])
        c0, c1 = mean([p.contrast for p, _ in pairs]), mean([q.contrast for _, q in pairs])
        g0 = mean([p.spec_clip + p.bg_clip for p, _ in pairs])
        g1 = mean([q.spec_clip + q.bg_clip for _, q in pairs])
        if ew0 > 0:
            gains["sharp"] = (ew0 - ew1) / ew0
        if c0 > 0:
            gains["contrast"] = (c1 - c0) / c0
        if g0 > 0.005:
            gains["glare"] = (g0 - g1) / g0
    a, b = res.initial_tip_score, res.optimized_tip_score
    if a is not None and b is not None and a.board.noise > 0:
        gains["noise"] = (a.board.noise - b.board.noise) / a.board.noise
    elif res.initial_metrics is not None and res.optimized_metrics is not None:
        n0, n1 = res.initial_metrics.temporal_noise, res.optimized_metrics.temporal_noise
        if n0 > 0:
            gains["noise"] = (n0 - n1) / n0
        w0, w1 = res.initial_metrics.wire_fwhm, res.optimized_metrics.wire_fwhm
        if w0 > 0:
            gains["sharp"] = (w0 - w1) / w0
    best = max(gains.items(), key=lambda kv: kv[1], default=(None, 0.0))
    if best[0] is None or best[1] <= 0.02:
        return "The picture is a little cleaner overall."
    return {
        "sharp": "A crisper point against the sisal.",
        "contrast": "The steel points stand out more from the board.",
        "glare": "Less glare on the barrels.",
        "noise": "Less noise around the tips.",
    }[best[0]]


def outcome_text(title: str, res: Optional[OptimizationResult]) -> str:
    if res is None:
        return f"{title} could not be tuned. It keeps its current settings."
    if res.darts_moved:
        return f"{title}: the darts moved while it was tuned, so nothing was changed. Find the darts again to retry."
    if not res.is_improvement:
        return f"{title}: already well tuned. The new settings were not better, so it keeps its current ones."
    pct = res.improvement_percentage
    if res.fallback_board_only:
        head = "a clearer picture of the board" if pct >= 10 else "a slightly clearer picture"
        return f"{title}: {head}. {_what_changed(res)}"
    head = "tips are much clearer" if pct >= 25 else "tips are clearer" if pct >= 10 else "tips are a little clearer"
    return f"{title}: {head}. {_what_changed(res)}"


def board_spots(res: OptimizationResult, shape) -> List[Tuple[float, float]]:
    """Three board areas for cameras tuned without tips: centre, above and below it."""
    h, w = shape[:2]
    roi = res.roi
    if roi is not None and getattr(roi, "center", None) and getattr(roi, "radius", 0):
        cx, cy = roi.center
        r = float(roi.radius)
    else:
        cx, cy, r = w / 2.0, h / 2.0, min(w, h) * 0.35
    return [(cx, cy - 0.6 * r), (cx, cy), (cx, cy + 0.6 * r)]


class ReviewStep:
    """Headline, per-camera outcome rows, the tip grid and Apply / Discard."""

    def __init__(self, view: CamCalibrationView):
        self.view = view
        self.moved_banner = W.ResultBanner("warn", "", "", compact=True)
        self.bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.outcome_label = W.wrap_label("", ("review-outcome",), max_chars=30)
        self.again_btn = Gtk.Button(label="Find darts again", halign=Gtk.Align.START)
        self.again_btn.add_css_class("optimizer-btn")
        self.again_btn.connect("clicked", lambda _b: self._find_again())
        self.bottom.append(self.outcome_label)
        self.bottom.append(self.again_btn)
        self._finishing = False

    # -- step protocol ------------------------------------------------------------------

    def enter(self):
        v = self.view
        self._finishing = False
        v.set_main(v.tip_grid)
        keys = [cam_key(c) for c in v.cameras]
        improved = [k for k in keys if k in v.results and v.results[k].is_improvement]
        others = len(keys) - len(improved)
        if improved:
            title = f"{_plural(len(improved), 'camera')} improved"
            if others:
                title += f", {others} kept"
            sub = "Tap a camera to compare its tips."
        else:
            title = "No camera improved"
            sub = "Every camera keeps its current settings."
        v.heading.set_text(title, sub)

        for k in keys:
            v.camera_list.set_row(k, *review_row(v.results.get(k)))

        top = None
        if v.moved_after_run:
            word = "Tip" if len(v.moved_after_run) == 1 else "Tips"
            ids = ", ".join(str(i) for i in v.moved_after_run)
            self.moved_banner.set_result("warn", "Darts moved during tuning",
                                         f"{word} {ids} moved. Results may be less precise.")
            top = self.moved_banner
        v.set_panels(top, self.bottom)

        v.action_bar.set_secondary("Discard", callback=self.discard)
        if improved:
            v.action_bar.set_primary("Apply settings", callback=self.apply_settings)
        else:
            v.action_bar.set_primary("Done", callback=self.discard)

        # Start on an improved camera: that is where the close-ups show a difference
        if v.selected in improved:
            sel = v.selected
        else:
            sel = next(iter(improved), next((k for k in keys if k in v.results), v.selected))
        v.select_camera(sel)
        self._show_camera(sel)

    def leave(self):
        pass

    def on_camera_selected(self, key: str):
        self._show_camera(key)

    # -- display ------------------------------------------------------------------------

    def _show_camera(self, key: Optional[str]):
        v = self.view
        if key is None:
            return
        res = v.results.get(key)
        title = v.cam_title(key)
        self.outcome_label.set_label(outcome_text(title, res))
        self.again_btn.set_visible(bool(res is not None and res.darts_moved and not v.board_only))
        grid = v.tip_grid
        if res is None:
            grid.set_tips([], [], kept=True, empty_text="No picture")
            return
        kept = not res.is_improvement
        before_f = res.initial_frame
        after_f = res.initial_frame if kept else res.optimized_frame
        tips = list(res.tips or [])
        if tips:
            before: List[Optional[np.ndarray]] = []
            after: List[Optional[np.ndarray]] = []
            ids = []
            for r in tips[:3]:
                ids.append(r.dart_id)
                before.append(tip_crop(before_f, r) if r.visible else None)
                after.append(tip_crop(after_f, r) if r.visible else None)
            grid.set_tips(before, after, kept=kept, tip_ids=ids)
        else:
            spots = board_spots(res, before_f.shape) if before_f is not None else []
            before = [W.crop_square(before_f, p, 180) for p in spots]
            after = [W.crop_square(after_f, p, 180) for p in spots]
            grid.set_tips(before, after, kept=kept, headers=("Upper board", "Centre", "Lower board"))

    # -- actions ------------------------------------------------------------------------

    def apply_settings(self):
        """Write the improved cameras' settings to hardware and their boot presets."""
        v = self.view
        if self._finishing:
            return              # a second tap while the first one is still writing
        self._finishing = True
        v.action_bar.set_primary_sensitive(False)
        v.action_bar.set_secondary_sensitive(False)
        applied: List[str] = []
        try:
            for key, res in v.results.items():
                cam = v.camera_for(key)
                if cam is None:
                    continue
                if not res.is_improvement:
                    logger.info("Keeping current settings on %s: %s", cam.dev_node, res.badge()[0])
                    continue
                try:
                    V4L2Device(cam.dev_node).apply_settings(res.optimized_settings)
                    # Also persist to manual service presets
                    CameraManualService.save_preset(cam.dev_node, res.optimized_settings.to_dict())
                    applied.append(cam.dev_node)
                except Exception as e:
                    logger.error("Failed to apply settings to %s: %s", cam.dev_node, e)

            # Persist across reboots via the single unified SUIT camera boot service
            if applied:
                all_nodes = [c.dev_node for c in v.cameras] or applied
                CameraManualService.enable_boot_service(all_nodes)
        except Exception:
            logger.exception("Applying the optimizer settings failed")
        finally:
            # Autodarts resumes and the wizard closes whatever happened above
            v.resume_autodarts()
            if applied:
                v.toast(APPLIED_TOAST)
            else:
                v.toast("Nothing changed. The cameras keep their current settings.")
            v.leave_wizard()

    # Backwards-compatible name
    _apply_and_finish = apply_settings

    def discard(self):
        """Keep the old settings (the optimizer already restored every camera)."""
        v = self.view
        if self._finishing:
            return
        self._finishing = True
        v.action_bar.set_primary_sensitive(False)
        v.action_bar.set_secondary_sensitive(False)
        v.resume_autodarts()
        v.toast("Nothing changed. The cameras keep their current settings.")
        v.leave_wizard()

    def _find_again(self):
        v = self.view
        v.results.clear()
        v.show_step("find")
