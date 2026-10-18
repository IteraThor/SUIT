"""Camera Optimizer engine: tip-focused tuning of one camera with a never-worse guarantee.

Sequence per camera (no empty-board reference anywhere):
  1. Snapshot, initial 6-frame burst. With dart tips: re-check that the darts did not move since
     step 2, then score the burst on the frozen tip ROIs (evaluate_tips).
  2. Stage 1 (brightness / sensitivity): flicker-safe shutter, then the gain that matches the
     auto-exposure white level. The result is candidate 0; a noise veto steps the gain down.
  3. Stage 2 (detail): coordinate descent over sharpness, contrast and gamma (6-frame bursts).
  4. Top-3 re-measure with fresh 10-frame bursts against a 10-frame initial reference.
  5. A/B check (best / initial / best), score spread, final darts-moved check.
  6. restore_snapshot() always (finally). Settings are only applied later, in Review.

Without tips (or with no visible tip in this camera) the board-wide evaluate() score drives the
same search ("tuned on the whole board").
"""

from __future__ import annotations
import dataclasses
import logging
import math
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple
import cv2
import numpy as np

from core.board_detector import BoardDetector, BoardROI, board_mask_from_homography, get_autodarts_resolution
from core.metrics import (
    HALO_VETO,
    VIS_MARGINAL_CNR,
    VIS_MIN_CNR,
    VIS_MIN_CONTRAST,
    VIS_MIN_VALID,
    QualityEvaluator,
    QualityMetrics,
    TipROI,
    TipScore,
    board_guard,
    board_veto,
    make_dart_exclusion,
    tip_metrics,
    _gray_stack,
)
from core.v4l2_device import CameraSettings, V4L2Device

logger = logging.getLogger(__name__)

ProgressCallback = Callable[..., None]

# V4L2 exposure_time_absolute is in 100 µs units.
FLICKER_PERIOD_UNITS = {1: 10000.0 / 100.0, 2: 10000.0 / 120.0}  # 50 Hz -> 10 ms, 60 Hz -> 8.33 ms

# Burst lengths (burst-length rule: a candidate is only compared with a reference of the same N).
SEARCH_BURST = 6
VERIFY_BURST = 10

# ISP levels for the coordinate descent (clamped to the driver's range, unsupported controls skipped).
ISP_LEVELS: Dict[str, Tuple[int, ...]] = {
    "sharpness": (60, 65, 70, 75, 80),
    "contrast": (30, 32, 34, 36, 40),
    "gamma": (300, 340, 380, 420),
}
ISP_ORDER = ("sharpness", "contrast", "gamma")
LUM_MATCH_BAND = 0.07  # candidates stay within this part of the other cameras' board luminance

MOVED_CONTRAST_RATIO = 0.5  # a visible tip whose contrast ON ITS FROZEN AXIS fell below half of the step-2 value moved
MOVED_MIN_NCC = 0.90  # tip patches of two bursts at the same settings correlating less than this moved
# Re-running the sideways axis search is NOT used to detect movement: on a tip next to wires or a
# segment edge the search can flip between two similar ridges several px apart from burst to burst.
AB_SETTLE_S = 1.5  # minimum wait after restoring the snapshot so auto exposure settles again
AB_SETTLE_POLLS = 20  # then poll (every ~0.3 s, so at most ~6 s more) until the brightness holds still
AB_SETTLE_TOL = 0.01  # two consecutive readings within 1 % count as settled

# Stage keys for the progress callback (the UI maps them to its stage chips).
STAGE_BRIGHTNESS = "brightness"
STAGE_SENSITIVITY = "sensitivity"
STAGE_DETAIL = "detail"
STAGE_CHECK = "check"


def flicker_safe_exposures(power_line_frequency: int, exp_min: int, exp_max: int, fps: int = 30) -> List[int]:
    """Exposure times (longest first) that integrate whole mains flicker cycles.

    Under mains-powered lighting these see constant brightness frame to frame. None exceeds the
    frame period, so the stream keeps its frame rate. Without a known mains frequency the frame
    period itself is the only candidate.
    """
    frame_limit = max(exp_min, min(exp_max, int(10000 / max(1, fps))))
    period = FLICKER_PERIOD_UNITS.get(power_line_frequency)
    if not period:
        return [frame_limit]
    cycles = int((frame_limit + 0.5) // period)  # tolerate 100 µs rounding of the frame period
    candidates = [int(round(n * period)) for n in range(cycles, 0, -1)]
    return [c for c in candidates if exp_min <= c <= frame_limit] or [frame_limit]


def _patch_ncc(a: np.ndarray, b: np.ndarray, box: Tuple[int, int, int, int]) -> float:
    """Normalised cross-correlation of two gray images inside box (x, y, w, h); 1.0 = identical."""
    x, y, w, h = box
    pa = a[y:y + h, x:x + w].astype(np.float64).ravel()
    pb = b[y:y + h, x:x + w].astype(np.float64).ravel()
    if pa.size < 16:
        return 1.0
    pa -= pa.mean()
    pb -= pb.mean()
    den = math.sqrt(float(pa @ pa) * float(pb @ pb))
    return float(pa @ pb) / den if den > 1e-9 else 1.0


@dataclasses.dataclass
class HiddenProbe:
    """A tip that is too faint to see, measured at its ESTIMATED position before and after tuning.

    Reported only: faint tips are not part of the score, because a position without a visible tip
    would just reward whatever noise lands there. `became_visible` needs a clear margin over the
    visibility threshold on the tuned picture.
    """
    dart_id: int
    before_contrast: float  # DN
    before_cnr: float
    after_contrast: float
    after_cnr: float
    became_visible: bool = False


def probe_faint_tips(tips: Sequence[TipROI], before_burst: Sequence[np.ndarray],
                     after_burst: Sequence[np.ndarray]) -> List[HiddenProbe]:
    """Measure every faint tip at its estimated position on two bursts of the same length."""
    faint = [t for t in (tips or []) if not t.visible and t.faint]
    if not faint or not before_burst or not after_burst:
        return []
    # CNR depends on how many frames are averaged (a single frame has no temporal noise estimate and
    # reads ~5 on pure noise), so two bursts of different length cannot be compared
    if len(before_burst) != len(after_burst) or len(before_burst) < 2:
        return []
    sb, sa = _gray_stack(before_burst), _gray_stack(after_burst)
    mb_mean, ma_mean = sb.mean(axis=0), sa.mean(axis=0)
    out = []
    for t in faint:
        mb, ma = tip_metrics(sb, t, mb_mean), tip_metrics(sa, t, ma_mean)
        # It must pass with a clear margin after tuning and must not have passed before: a tip that
        # already measures as visible was misjudged earlier, it did not become visible through tuning
        passes_before = (mb.contrast >= VIS_MIN_CONTRAST and mb.cnr >= VIS_MIN_CNR
                         and mb.valid_profiles >= VIS_MIN_VALID)
        became = (ma.contrast >= VIS_MIN_CONTRAST and ma.cnr >= VIS_MARGINAL_CNR
                  and ma.valid_profiles >= VIS_MIN_VALID and not passes_before)
        out.append(HiddenProbe(t.dart_id, round(mb.contrast, 1), round(mb.cnr, 1),
                               round(ma.contrast, 1), round(ma.cnr, 1), bool(became)))
    return out


def lum_match_veto(lum: float, ref_lum: float, target: float) -> Optional[str]:
    """Veto text when a candidate's board luminance is further from the other cameras' than allowed.

    Allowed: within LUM_MATCH_BAND of `target`, or at least as close as the initial settings
    (`ref_lum`) were, so a camera that starts off-target may still move toward it.
    """
    if abs(lum - target) > max(LUM_MATCH_BAND * target, abs(ref_lum - target)):
        return f"brightness differs from the other cameras ({lum:.0f} vs {target:.0f} DN)"
    return None


@dataclasses.dataclass
class OptimizationResult:
    camera_node: str
    target_kelvin: int
    initial_settings: CameraSettings
    initial_metrics: QualityMetrics
    initial_frame: np.ndarray  # full capture resolution (tip crops)
    optimized_settings: CameraSettings
    optimized_metrics: QualityMetrics
    optimized_frame: np.ndarray  # full capture resolution (tip crops)
    roi: BoardROI
    improvement_percentage: float  # from the A/B means
    tips: List[TipROI] = dataclasses.field(default_factory=list)  # frozen ROIs used for scoring
    initial_tip_score: Optional[TipScore] = None  # A side (initial settings, 10-frame burst)
    optimized_tip_score: Optional[TipScore] = None  # B side (best settings, 10-frame burst)
    ab_spread_pct: float = 0.0  # largest repeat-to-repeat difference within one A/B side, in %
    darts_moved: bool = False
    fallback_board_only: bool = False  # scored on the whole board (no tips / no visible tip)
    tip_component_before: Optional[float] = None  # A/B means of the tip component
    tip_component_after: Optional[float] = None
    veto: Optional[str] = None  # hard veto seen on the B side (diagnostic text)
    details: List[str] = dataclasses.field(default_factory=list)  # technical log for the Details dialog
    hidden_probe: List[HiddenProbe] = dataclasses.field(default_factory=list)  # faint tips, estimated position
    board_lum_before: Optional[float] = None  # mean board luminance (DN) with the initial settings
    board_lum_after: Optional[float] = None  # ... with the tuned settings

    @property
    def board_lum_final(self) -> Optional[float]:
        """Board luminance of what the camera runs with after this result is applied (or kept)."""
        return self.board_lum_after if self.is_improvement else self.board_lum_before

    # Below this the gain is within measurement noise, so applying the new settings is not worth it.
    MIN_IMPROVEMENT_PCT = 3.0

    @property
    def required_improvement_pct(self) -> float:
        return max(self.MIN_IMPROVEMENT_PCT, 2.0 * (self.ab_spread_pct or 0.0))

    @property
    def is_improvement(self) -> bool:
        """Never-worse rule: only clearly better, un-vetoed, tip-preserving results are applied."""
        if self.darts_moved or self.veto:
            return False
        if self.improvement_percentage is None or self.improvement_percentage < self.required_improvement_pct:
            return False
        if self.tip_component_before is not None and self.tip_component_after is not None:
            if self.tip_component_after < self.tip_component_before - 0.01:
                return False
        return True

    def badge(self) -> Tuple[str, str]:
        """(label, css class) describing the outcome for review badges."""
        if self.darts_moved:
            return "Darts moved", "status-pill-warn"
        if self.is_improvement:
            return f"+{self.improvement_percentage:.0f} %", "status-pill-ok"
        return "Kept", "status-pill-warn"


@dataclasses.dataclass
class _Eval:
    """One measured setting: score, veto and the burst it came from."""
    settings: CameraSettings
    total: float
    veto: Optional[str]
    tip_score: Optional[TipScore]
    burst: List[np.ndarray]
    n: int

    @property
    def tip_component(self) -> Optional[float]:
        return self.tip_score.tip_component if self.tip_score is not None else None


class _Cancelled(Exception):
    pass


class CameraOptimizer:
    """Coordinates camera parameter tuning and A/B verification."""

    def __init__(self, device: V4L2Device):
        self.device = device
        self.detector = BoardDetector()
        self.evaluator = QualityEvaluator()
        self._sleep = time.sleep  # replaced by tests
        self.ab_settle_s = AB_SETTLE_S
        # Every setting scored by the ISP search as (settings, total, veto); diagnostics and tests.
        self.evaluations: List[Tuple[CameraSettings, float, Optional[str]]] = []

    # ------------------------------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------------------------------

    def run_optimization(
        self,
        progress_cb: Optional[ProgressCallback] = None,
        cancel_event: Optional[threading.Event] = None,
        precision_mode: str = "ultra",
        tips: Optional[List[TipROI]] = None,
        homography: Optional[np.ndarray] = None,
        target_lum: Optional[float] = None,
    ) -> Optional[OptimizationResult]:
        """Tune one camera. Returns None on cancel or capture failure.

        `tips` are the frozen TipROIs of this camera from step 2 (tip_rois_for_camera), in image
        px at the Autodarts capture resolution. `homography` (image -> board) gives the r=330
        board mask; without it the calibration file, then the detected board, is used.
        `target_lum` is the mean board luminance (DN) of the cameras tuned before this one: the
        three pictures should look alike, so a candidate must stay within LUM_MATCH_BAND of it
        (or at least not be further from it than the initial settings).
        progress_cb(stage_key, fraction, user_text, frame=None): stage_key is one of
        "brightness", "sensitivity", "detail", "check"; user_text is plain language.
        """
        details: List[str] = []
        self.evaluations = []

        def report(stage: str, progress: float, text: str, frame: Optional[np.ndarray] = None):
            if progress_cb:
                try:
                    progress_cb(stage, progress, text, frame)
                except TypeError:
                    progress_cb(stage, progress, text)

        def note(msg: str, *args):
            line = msg % args if args else msg
            details.append(line)
            logger.info("[%s] %s", self.device.dev_node, line)

        def check_abort():
            if cancel_event is not None and cancel_event.is_set():
                raise _Cancelled()

        report(STAGE_BRIGHTNESS, 0.01, "Looking at the current picture")
        snapshot = self.device.take_snapshot()
        if isinstance(snapshot, dict) and not snapshot:
            # Without a snapshot the camera could not be put back afterwards: change nothing
            logger.error("Could not read the controls of %s; not tuning it", self.device.dev_node)
            return None

        cap_w, cap_h = get_autodarts_resolution()
        cap = None
        try:
            try:
                res_cap = self.device.open_stream(cap_w, cap_h)
                if isinstance(res_cap, cv2.VideoCapture) and res_cap.isOpened():
                    cap = res_cap
            except Exception:
                cap = None

            def get_frame(flush: int = 2) -> Optional[np.ndarray]:
                if cap is not None and cap.isOpened():
                    return self.device.read_stream_frame(cap, flush_frames=flush)
                return self.device.capture_frame(width=cap_w, height=cap_h, warmup_frames=max(3, flush))

            def get_burst(count: int, flush: int = 3) -> Optional[List[np.ndarray]]:
                # A short burst would break the burst-length rule, so retry once, then give up.
                for _ in range(2):
                    if cap is not None and cap.isOpened():
                        burst = self.device.read_stream_burst(cap, count=count, flush_frames=flush)
                    else:
                        burst = self.device.capture_burst(count=count, width=cap_w, height=cap_h,
                                                          warmup_frames=max(3, flush))
                    burst = [f for f in (burst or []) if f is not None]
                    if len(burst) >= count:
                        return burst[:count]
                return None

            return self._run(report, note, check_abort, get_frame, get_burst, precision_mode, tips,
                             homography, (cap_w, cap_h), details, target_lum)
        except _Cancelled:
            logger.info("Optimization cancelled on %s. Restoring camera snapshot.", self.device.dev_node)
            return None
        finally:
            # Safety-critical: the camera always leaves the optimizer on its original settings.
            try:
                self.device.restore_snapshot()
            except Exception as e:
                logger.error("Failed to restore camera snapshot on %s: %s", self.device.dev_node, e)
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass

    # ------------------------------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------------------------------

    def _run(self, report, note, check_abort, get_frame, get_burst, precision_mode, tips, homography,
             cap_size, details, target_lum: Optional[float] = None) -> Optional[OptimizationResult]:
        initial_burst = get_burst(SEARCH_BURST, flush=4)
        if not initial_burst:
            logger.error("Failed to capture baseline frames from %s", self.device.dev_node)
            return None
        check_abort()
        initial_frame = initial_burst[0].copy()
        shape = initial_frame.shape[:2]

        roi = self.detector.detect_board(initial_frame, dev_node=self.device.dev_node)
        report(STAGE_BRIGHTNESS, 0.04, "Looking at the current picture", frame=initial_frame)

        initial_ctrls = self.device.query_controls() or {}
        initial_settings = self._settings_from_controls(initial_ctrls)

        # Scoring mode: tips when at least one is visible in this camera, else the whole board.
        tips = list(tips or [])
        tips = [t for t in tips if self._tip_in_frame(t, shape)] if tips else []
        use_tips = any(t.visible for t in tips)
        board_mask = self._board_mask(homography, cap_size, shape, roi)
        exclude = make_dart_exclusion(shape, tips) if tips else None
        note("scoring on %s", "%d visible tips" % sum(t.visible for t in tips) if use_tips else "the whole board")

        def moved_tips(burst: Sequence[np.ndarray], reference: Optional[Sequence[np.ndarray]] = None) -> List[int]:
            """Visible tips that moved, judged at the step-2 settings.

            Contrast is measured on the frozen axis (a point only ~4 px wide loses it after a few px of
            movement). With `reference` (an earlier burst at the same settings) the tip patches must
            also still correlate.
            """
            if not use_tips:
                return []
            stack = _gray_stack(burst)
            mean = stack.mean(axis=0)
            ref_mean = _gray_stack(reference).mean(axis=0) if reference else None
            out = []
            for t in tips:
                if not t.visible:
                    continue
                reasons = []
                if t.ref_contrast > 0:
                    c = tip_metrics(stack, t, mean).contrast
                    if c < MOVED_CONTRAST_RATIO * t.ref_contrast:
                        reasons.append("contrast %.0f of %.0f" % (c, t.ref_contrast))
                if ref_mean is not None:
                    ncc = _patch_ncc(mean, ref_mean, t.box)
                    if ncc < MOVED_MIN_NCC:
                        reasons.append("patch changed (ncc %.2f)" % ncc)
                if reasons:
                    note("tip %d moved: %s", t.dart_id, ", ".join(reasons))
                    out.append(t.dart_id)
            return out

        # Scorer shared by every measurement. The reference must have the same burst length.
        def score(burst: List[np.ndarray], settings: CameraSettings, reference: Optional[_Eval]) -> _Eval:
            e = score_one(burst, settings, reference)
            if target_lum and reference is not None and e.veto is None and e.tip_score is not None \
                    and reference.tip_score is not None:
                e.veto = lum_match_veto(e.tip_score.board.mean_lum, reference.tip_score.board.mean_lum, target_lum)
                e.tip_score.veto = e.veto
            return e

        def score_one(burst: List[np.ndarray], settings: CameraSettings, reference: Optional[_Eval]) -> _Eval:
            ref_ts = reference.tip_score if reference is not None else None
            if use_tips:
                ts = self.evaluator.evaluate_tips(burst, roi, tips, reference=ref_ts,
                                                  board_mask=board_mask, exclude_mask=exclude)
                return _Eval(settings, ts.total, ts.veto, ts, burst, len(burst))
            # Board-only: legacy board-wide score, guard terms only for the vetoes.
            m = self.evaluator.evaluate(burst, roi)
            stack = _gray_stack(burst)
            guard = board_guard(stack, board_mask, exclude, ref_ts.board if ref_ts is not None else None)
            gts = TipScore(total=m.composite_score, tip_component=0.0, board_component=guard.score, tips=[],
                           board=guard, n_frames=len(burst), board_only=True)
            veto = None
            if ref_ts is not None:
                if ref_ts.n_frames != len(burst):
                    raise ValueError("burst length mismatch against the reference")
                veto = board_veto(guard, ref_ts.board)
                if veto is None:
                    halo = self.evaluator.calculate_halo_overshoot(burst[0], roi.wire_mask, roi.uniform_mask)
                    if halo > HALO_VETO:
                        veto = f"wire halo ({halo * 100:.0f}%)"
            gts.veto = veto
            return _Eval(settings, m.composite_score, veto, gts, burst, len(burst))

        def result(**kw) -> OptimizationResult:
            base = dict(camera_node=self.device.dev_node, target_kelvin=0, initial_settings=initial_settings,
                        initial_frame=initial_frame, roi=roi, tips=tips, fallback_board_only=not use_tips,
                        details=details)
            base.update(kw)
            return OptimizationResult(**base)

        # 1. Did the darts move since step 2? (same settings as step 2, so contrast is comparable)
        moved = moved_tips(initial_burst)
        initial6 = score(initial_burst, initial_settings, None)
        initial_metrics = self._metrics(initial_burst, roi, initial6)
        if moved:
            report(STAGE_CHECK, 1.0, "The darts seem to have moved. This camera keeps its settings.", frame=initial_frame)
            return result(initial_metrics=initial_metrics, optimized_settings=initial_settings,
                          optimized_metrics=initial_metrics, optimized_frame=initial_frame,
                          improvement_percentage=0.0, initial_tip_score=initial6.tip_score,
                          optimized_tip_score=initial6.tip_score, darts_moved=True)
        note("initial (N=%d): total %.1f", SEARCH_BURST, initial6.total)
        check_abort()

        # 2. Stage 1: shutter and gain (candidate 0).
        stage1, gain_info = self._stage1(report, note, check_abort, get_frame, initial_frame, roi,
                                         initial_ctrls, initial_settings)
        self._apply(stage1)
        burst = get_burst(SEARCH_BURST)
        if not burst:
            return None
        cand0 = score(burst, stage1, initial6)
        min_g, gain_step = gain_info
        tries = 0
        while cand0.veto and "noise" in cand0.veto and stage1.gain > min_g and tries < 6:
            check_abort()
            tries += 1
            stage1 = dataclasses.replace(stage1, gain=max(min_g, stage1.gain - gain_step))
            note("candidate 0 vetoed (%s), gain down to %d", cand0.veto, stage1.gain)
            report(STAGE_SENSITIVITY, 0.28, "Lowering sensitivity to keep the picture clean")
            self.device.set_control("gain", stage1.gain)
            self._sleep(0.05)
            burst = get_burst(SEARCH_BURST)
            if not burst:
                return None
            cand0 = score(burst, stage1, initial6)
        note("candidate 0 %s: total %.1f%s", self._fmt(stage1), cand0.total, f" VETO {cand0.veto}" if cand0.veto else "")
        report(STAGE_SENSITIVITY, 0.30, "Sensitivity set", frame=burst[0])
        check_abort()

        # 3. Stage 2: coordinate descent over the ISP controls.
        evals = self._search_isp(report, note, check_abort, get_burst, score, initial6, cand0, initial_ctrls,
                                 two_passes=precision_mode not in ("fast", "standard"))

        def settle_auto_exposure() -> None:
            """Wait until auto exposure has converged after a restore.

            A fixed delay was not enough on the rig: the two initial-settings measurements of the
            A/B check differed by up to 34 %, which made the never-worse rule reject real gains.
            """
            self._sleep(self.ab_settle_s)
            prev, steady = None, 0
            for _ in range(AB_SETTLE_POLLS):
                f = get_frame(flush=2)
                if f is None:
                    return
                gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
                lum = float(gray[roi.board_mask > 0].mean()) if roi.board_mask is not None and roi.board_mask.shape == gray.shape else float(gray.mean())
                if prev is not None and abs(lum - prev) <= AB_SETTLE_TOL * max(prev, 1.0):
                    steady += 1
                    if steady >= 2:
                        return
                else:
                    steady = 0
                prev = lum
                self._sleep(0.25)
            note("auto exposure still drifting after the settle wait")

        # 4. Top-3 re-measure at N=10 against a fresh 10-frame initial reference.
        ok = sorted([e for e in evals if not e.veto], key=lambda e: -e.total)[:3]
        report(STAGE_DETAIL, 0.76, "Comparing the best settings")
        self.device.restore_snapshot()
        settle_auto_exposure()
        a1_burst = get_burst(VERIFY_BURST, flush=4)
        if not a1_burst:
            return None
        a1 = score(a1_burst, initial_settings, None)
        check_abort()
        remeasured: List[_Eval] = []
        for i, e in enumerate(ok):
            check_abort()
            self._apply(e.settings)
            b = get_burst(VERIFY_BURST)
            if not b:
                continue
            r = score(b, e.settings, a1)
            note("re-measure %s: %.1f (search %.1f)%s", self._fmt(e.settings), r.total, e.total,
                 f" VETO {r.veto}" if r.veto else "")
            remeasured.append(r)
            report(STAGE_DETAIL, 0.76 + 0.04 * (i + 1) / max(1, len(ok)), "Comparing the best settings", frame=b[0])
        best = max((r for r in remeasured if not r.veto), key=lambda r: r.total, default=None)
        if best is None:
            note("no un-vetoed candidate left: keeping the current settings")
            report(STAGE_CHECK, 1.0, "No better settings found. This camera keeps its current ones.")
            final_metrics = self._metrics(a1_burst, roi, a1)
            return result(initial_metrics=final_metrics, optimized_settings=initial_settings,
                          optimized_metrics=final_metrics, optimized_frame=a1_burst[0].copy(),
                          improvement_percentage=0.0, initial_tip_score=a1.tip_score,
                          optimized_tip_score=a1.tip_score, veto=(remeasured[0].veto if remeasured else None),
                          tip_component_before=a1.tip_component, tip_component_after=a1.tip_component)
        check_abort()

        # 5. A/B check: best -> restore, settle -> initial -> best. Two measurements per side.
        report(STAGE_CHECK, 0.82, "Double-checking the result")
        self._apply(best.settings)
        b1_burst = get_burst(VERIFY_BURST)
        check_abort()
        self.device.restore_snapshot()
        settle_auto_exposure()
        a2_burst = get_burst(VERIFY_BURST, flush=4)
        check_abort()
        report(STAGE_CHECK, 0.90, "Double-checking the result")
        self._apply(best.settings)
        b2_burst = get_burst(VERIFY_BURST)
        if not (b1_burst and a2_burst and b2_burst):
            return None
        b1 = score(b1_burst, best.settings, a1)
        a2 = score(a2_burst, initial_settings, a1)
        b2 = score(b2_burst, best.settings, a1)
        mean_a = (a1.total + a2.total) / 2.0
        mean_b = (b1.total + b2.total) / 2.0
        spread = max(abs(a1.total - a2.total), abs(b1.total - b2.total))
        spread_pct = round(100.0 * spread / mean_a, 2) if mean_a > 0 else 0.0
        if mean_a > 0:
            improvement = round(max(-100.0, (mean_b - mean_a) / mean_a * 100.0), 1)
        else:
            improvement = 100.0 if mean_b > 0 else 0.0
        veto = b1.veto or b2.veto
        tip_before = tip_after = None
        if use_tips:
            tip_before = round((a1.tip_component + a2.tip_component) / 2.0, 4)
            tip_after = round((b1.tip_component + b2.tip_component) / 2.0, 4)
        note("A/B: A %.1f/%.1f, B %.1f/%.1f -> %+.1f%% (spread %.1f%%)%s", a1.total, a2.total, b1.total,
             b2.total, improvement, spread_pct, f" VETO {veto}" if veto else "")

        # 6. Final darts-moved check on the restored-settings burst against the first burst (same
        # settings, so only movement changes the tip patches).
        report(STAGE_CHECK, 0.96, "Checking that the darts did not move")
        moved = moved_tips(a2_burst, reference=initial_burst)

        # Reported only: a failure here must never cost the finished measurement, and a moved dart
        # has no meaningful "estimated position" any more
        hidden_probe: List[HiddenProbe] = []
        if tips and not moved:
            try:
                hidden_probe = probe_faint_tips(tips, a2_burst, b2_burst)
            except Exception as e:
                logger.warning("Faint-tip probe failed on %s: %s", self.device.dev_node, e)
        for hp in hidden_probe:
            note("faint tip %d at its estimated position: C %.0f -> %.0f DN, CNR %.1f -> %.1f%s", hp.dart_id,
                 hp.before_contrast, hp.after_contrast, hp.before_cnr, hp.after_cnr,
                 " -> VISIBLE" if hp.became_visible else "")
        res = result(initial_metrics=self._metrics(a2_burst, roi, a2), optimized_settings=best.settings,
                     optimized_metrics=self._metrics(b2_burst, roi, b2), optimized_frame=b2_burst[0].copy(),
                     initial_frame=a2_burst[0].copy(), improvement_percentage=improvement,
                     initial_tip_score=a1.tip_score, optimized_tip_score=b2.tip_score, ab_spread_pct=spread_pct,
                     darts_moved=bool(moved), tip_component_before=tip_before, tip_component_after=tip_after,
                     veto=veto, hidden_probe=hidden_probe,
                     board_lum_before=a2.tip_score.board.mean_lum if a2.tip_score else None,
                     board_lum_after=b2.tip_score.board.mean_lum if b2.tip_score else None)
        note("decision: %s (needs %+.1f%%)", "apply" if res.is_improvement else "keep", res.required_improvement_pct)
        if res.darts_moved:
            report(STAGE_CHECK, 1.0, "The darts seem to have moved. This camera keeps its settings.", frame=res.optimized_frame)
        elif res.is_improvement:
            report(STAGE_CHECK, 1.0, f"Done: {improvement:.0f} % better", frame=res.optimized_frame)
        else:
            report(STAGE_CHECK, 1.0, "Done: the current settings are already as good", frame=res.optimized_frame)
        return res

    # ------------------------------------------------------------------------------------------
    # Stage 1: shutter and gain (flicker-safe, matches the auto-exposure white level)
    # ------------------------------------------------------------------------------------------

    def _stage1(self, report, note, check_abort, get_frame, initial_frame, roi, initial_ctrls,
                initial_settings) -> Tuple[CameraSettings, Tuple[int, int]]:
        gray_baseline = cv2.cvtColor(initial_frame, cv2.COLOR_BGR2GRAY)
        baseline_board = gray_baseline[roi.board_mask > 0]
        baseline_uniform = gray_baseline[roi.uniform_mask > 0]
        if len(baseline_uniform) > 50:
            baseline_white_lum = float(np.percentile(baseline_uniform, 90))
        elif len(baseline_board) > 50:
            baseline_white_lum = float(np.percentile(baseline_board, 90))
        else:
            baseline_white_lum = float(np.percentile(gray_baseline, 90))

        # Safe target luminance clamp: anti-blooming white ceiling (175 - 215 DN) keeps >= 40 DN of
        # specular headroom so metallic dart barrels do not clip and bloom.
        target_white_lum = max(175.0, min(215.0, baseline_white_lum))
        note("auto-exposure white %.1f DN, target %.1f DN", baseline_white_lum, target_white_lum)

        exp_ctrl = initial_ctrls.get("exposure_time_absolute")
        exp_min = exp_ctrl.min_val if exp_ctrl else 1
        exp_max = exp_ctrl.max_val if exp_ctrl else 1000

        gain_ctrl = initial_ctrls.get("gain")
        min_g = gain_ctrl.min_val if gain_ctrl else 0
        max_g = gain_ctrl.max_val if gain_ctrl else 128
        gain_step = max(1, getattr(gain_ctrl, "step", 1) or 1)

        # The reported exposure_time_absolute is meaningless while auto exposure is active (the
        # driver marks it inactive and keeps a stale value), so pick the shutter from scratch:
        # the longest flicker-free exposure that does not overshoot the target at minimum gain.
        # More light per frame means less gain and therefore less sensor noise.
        shutter_candidates = flicker_safe_exposures(initial_settings.power_line_frequency, exp_min, exp_max)
        nominal_shutter = shutter_candidates[-1]

        # Manual mode while preserving ALL factory ISP settings (brightness, saturation, gamma, wb).
        self.device.set_control("auto_exposure", 1)
        self.device.set_control("power_line_frequency", initial_settings.power_line_frequency)
        self.device.set_control("brightness", initial_settings.brightness)
        self.device.set_control("saturation", initial_settings.saturation)
        self.device.set_control("gamma", initial_settings.gamma)
        self.device.set_control("white_balance_automatic", initial_settings.wb_auto)
        if initial_settings.wb_auto == 0 and initial_settings.wb_temperature > 0:
            self.device.set_control("white_balance_temperature", initial_settings.wb_temperature)

        def measure_white(f: np.ndarray) -> float:
            gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
            u_pix = gray[roi.uniform_mask > 0]
            return float(np.percentile(u_pix, 90)) if len(u_pix) > 50 else float(np.percentile(gray, 90))

        self.device.set_control("gain", min_g)
        for i, shutter in enumerate(shutter_candidates):
            check_abort()
            self.device.set_control("exposure_time_absolute", shutter)
            self._sleep(0.08)
            f = get_frame(flush=3)
            if f is None:
                continue
            lum = measure_white(f)
            note("shutter %.1f ms at minimum gain -> white %.1f/%.0f DN", shutter * 0.1, lum, target_white_lum)
            report(STAGE_BRIGHTNESS, 0.05 + 0.10 * (i + 1) / len(shutter_candidates), "Finding the right brightness", frame=f)
            if lum <= target_white_lum + 5.0:
                nominal_shutter = shutter
                break

        self.device.set_control("exposure_time_absolute", nominal_shutter)
        self._sleep(0.08)
        check_abort()

        # Gain sweep: the gain that reproduces the target white level at the locked shutter.
        coarse_gains = sorted(set([min_g] + [min_g + int((max_g - min_g) * p) for p in
                                             (0.05, 0.12, 0.20, 0.30, 0.42, 0.55, 0.70)] + [max_g]))
        if min_g <= initial_settings.gain <= max_g:
            coarse_gains = sorted(set(coarse_gains + [initial_settings.gain]))

        best_g, best_diff = coarse_gains[0], float("inf")
        for idx, g in enumerate(coarse_gains):
            check_abort()
            self.device.set_control("gain", g)
            self._sleep(0.05)
            f = get_frame(flush=2)
            if f is None:
                continue
            lum = measure_white(f)
            if abs(lum - target_white_lum) < best_diff:
                best_diff, best_g = abs(lum - target_white_lum), g
            note("gain %d -> white %.1f/%.0f DN", g, lum, target_white_lum)
            report(STAGE_SENSITIVITY, 0.15 + 0.07 * (idx + 1) / len(coarse_gains), "Adjusting sensitivity", frame=f)
            if lum >= target_white_lum + 5.0:
                break

        # Fine gain polish around the coarse winner.
        fine_step = max(1, gain_step * 2, int((max_g - min_g) * 0.02))
        fine_gains = sorted(set(max(min_g, min(max_g, best_g + o * fine_step)) for o in (-3, -2, -1, 0, 1, 2, 3)))
        for idx, g in enumerate(fine_gains):
            check_abort()
            if g in coarse_gains and g != best_g:
                continue
            self.device.set_control("gain", g)
            self._sleep(0.04)
            f = get_frame(flush=2)
            if f is None:
                continue
            lum = measure_white(f)
            if abs(lum - target_white_lum) < best_diff:
                best_diff, best_g = abs(lum - target_white_lum), g
            report(STAGE_SENSITIVITY, 0.22 + 0.05 * (idx + 1) / len(fine_gains), "Adjusting sensitivity", frame=f)

        note("stage 1: exposure %d, gain %d", nominal_shutter, best_g)
        settings = dataclasses.replace(initial_settings, exposure_auto=1, exposure_absolute=nominal_shutter, gain=best_g)
        return settings, (min_g, fine_step)

    # ------------------------------------------------------------------------------------------
    # Stage 2: coordinate descent over sharpness, contrast and gamma
    # ------------------------------------------------------------------------------------------

    def _search_isp(self, report, note, check_abort, get_burst, score, reference, cand0: "_Eval", ctrls,
                    order: Sequence[str] = ISP_ORDER, two_passes: bool = True) -> List["_Eval"]:
        """Pass 1 varies one control at a time around the current best; pass 2 tries +-1 level.

        Every evaluation: set the controls, flush 3 frames, take a 6-frame burst, score against the
        6-frame initial reference, apply the vetoes. Returns all evaluations including candidate 0.
        """
        levels: Dict[str, List[int]] = {}
        for name in order:
            c = ctrls.get(name)
            if c is None:
                continue  # unsupported control
            vals = sorted({max(c.min_val, min(c.max_val, v)) for v in ISP_LEVELS[name]})
            levels[name] = vals

        def key(s: CameraSettings) -> Tuple[int, ...]:
            return tuple(getattr(s, n) for n in order)

        evals: Dict[Tuple[int, ...], _Eval] = {key(cand0.settings): cand0}
        total_est = sum(len(v) for v in levels.values()) + (2 * len(levels) if two_passes else 0)
        done = [0]

        def better(a: _Eval, b: _Eval) -> bool:
            if a.veto:
                return False
            return bool(b.veto) or a.total > b.total

        def measure(s: CameraSettings) -> Optional[_Eval]:
            k = key(s)
            if k in evals:
                return evals[k]
            check_abort()
            for name in levels:
                self.device.set_control(name, getattr(s, name))
            self._sleep(0.04)
            burst = get_burst(SEARCH_BURST, flush=3)
            if not burst:
                return None
            e = score(burst, s, reference)
            evals[k] = e
            self.evaluations.append((s, e.total, e.veto))
            done[0] += 1
            note("%s -> %.1f%s", self._fmt(s), e.total, f" VETO {e.veto}" if e.veto else "")
            report(STAGE_DETAIL, 0.30 + 0.45 * min(1.0, done[0] / max(1, total_est)),
                   f"Adjusting detail and contrast ({done[0]} of about {total_est})", frame=burst[0])
            return e

        best = cand0
        for name in levels:  # pass 1
            for v in levels[name]:
                e = measure(dataclasses.replace(best.settings, **{name: v}))
                if e is not None and better(e, best):
                    best = e
        if two_passes:  # pass 2: +-1 level around the best
            for name in levels:
                vals = levels[name]
                cur = getattr(best.settings, name)
                idx = min(range(len(vals)), key=lambda i: abs(vals[i] - cur))
                for j in (idx - 1, idx + 1):
                    if 0 <= j < len(vals):
                        e = measure(dataclasses.replace(best.settings, **{name: vals[j]}))
                        if e is not None and better(e, best):
                            best = e
        return list(evals.values())

    # ------------------------------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------------------------------

    def _apply(self, s: CameraSettings):
        """Put the camera on a candidate (manual exposure) and let it settle."""
        self.device.set_control("auto_exposure", 1)
        self.device.set_control("exposure_time_absolute", s.exposure_absolute)
        self.device.set_control("gain", s.gain)
        for name in ISP_ORDER:
            self.device.set_control(name, getattr(s, name))
        self._sleep(0.08)

    @staticmethod
    def _fmt(s: CameraSettings) -> str:
        return f"E={s.exposure_absolute} G={s.gain} S={s.sharpness} C={s.contrast} gamma={s.gamma}"

    @staticmethod
    def _tip_in_frame(t: TipROI, shape) -> bool:
        return 0 <= t.tip[0] < shape[1] and 0 <= t.tip[1] < shape[0]

    def _board_mask(self, homography, cap_size, shape, roi: BoardROI) -> np.ndarray:
        H = homography
        if H is None:
            try:
                H = self.detector.load_calibration_homography(cap_size[0], cap_size[1], dev_node=self.device.dev_node)
            except Exception:
                H = None
        if H is not None and (shape[1], shape[0]) == tuple(cap_size):
            mask = board_mask_from_homography(np.asarray(H, np.float64), shape)
            if np.count_nonzero(mask) > 1000:
                return mask
        return roi.board_mask

    def _metrics(self, burst: List[np.ndarray], roi: BoardROI, e: _Eval) -> QualityMetrics:
        """Board-wide QualityMetrics (Details / session export), with the tip score attached."""
        m = self.evaluator.evaluate(burst, roi)
        if e.tip_score is not None and not e.tip_score.board_only:
            m.composite_score = e.tip_score.total
            m.tip_component = e.tip_score.tip_component
            m.board_component = e.tip_score.board_component
            m.tips = e.tip_score.tips
        m.veto = e.veto
        return m

    @staticmethod
    def _settings_from_controls(ctrls) -> CameraSettings:
        def val(name, default):
            c = ctrls.get(name)
            return c.current_val if c is not None else default

        return CameraSettings(
            exposure_auto=val("auto_exposure", 1),
            exposure_absolute=val("exposure_time_absolute", 166),
            gain=val("gain", 0),
            wb_auto=val("white_balance_automatic", 1),
            wb_temperature=val("white_balance_temperature", 4600),
            sharpness=val("sharpness", 3),
            contrast=val("contrast", 32),
            brightness=val("brightness", 0),
            gamma=val("gamma", 300),
            saturation=val("saturation", 64),
            power_line_frequency=val("power_line_frequency", 1),
        )
