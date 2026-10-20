"""Camera Optimizer engine: sensor-referred exposure for one camera, with a not-worse check.

The settings are measured, not searched. On the rig (3 HBV OV2710 USB cameras, 1280x960 MJPEG; the
numbers per camera brand are in core.camera_profiles):
  * White sisal saturates IN THE SENSOR long before the 8-bit output clips: at gain 0 a saturated
    pixel reads ~127-140 DN, so a DN-based "no clipping" test never sees it. Past that point the dark
    groove of a wire on white (stand-in for a dark steel point on a white segment) collapses.
  * Steel points on black sisal (the weakest tips) gain CNR with every extra photon.
  * Gain and gamma are CNR-neutral but decide how much of the signal survives 8-bit quantisation and
    MJPEG; contrast > default crushes the black floor and with it the dark-on-black tips; sharpening
    above the default only adds noise, below it the thin specular points fade.
  * Writing exposure_time_absolute costs ~15 % more noise than the same brightness reached by the
    camera's own auto exposure, and no gain / gamma setting wins it back. Auto exposure frozen in place
    ("AE lock": switch to manual without writing an exposure) keeps the low-noise operating point, and
    backlight_compensation steers where its metering lands.
So: auto exposure metered to land just past the knee where white sisal saturates, then locked; gamma
lifted, gain to put white just under clipping, the rest at the camera defaults with guards. Measured
on 5 tips in 3 cameras against Autodarts' own auto exposure: mean tip CNR +11 %, weakest tip +33 %,
tip contrast +36 %, clipped board pixels 23 % -> 0 %.

Sequence per camera:
  1. Snapshot, initial burst; darts-moved check against step 2.
  2. Brightness: sensor ceiling (white median at a long exposure, gain 0), bisection for the knee
     exposure (white median = knee_frac of the ceiling). Each backlight_compensation level: auto
     exposure converges, is locked, and its exposure is read off the written-exposure response; the
     level landing nearest knee_overshoot x knee is used. None in range: written exposure at that point.
  3. Sensitivity: bisection for the gain that puts the white median at white_target.
  4. Detail: tone guards (black floor not crushed, blacks not washed out, no 8-bit clipping).
  5. Check: interleaved A/B (initial / new / initial / new) on tip CNR; the new settings are applied
     unless they are measurably worse (non-inferiority) or a guard fails.
  6. restore_snapshot() always (finally). Settings are only applied later, in Review.

White balance is left as it was (auto by default): with this firmware the manual temperature control
cannot produce a neutral image at any value, and auto white balance held its gains to 0.001 over 40 s.
"""

from __future__ import annotations
import dataclasses
import logging
import math
import threading
import time
from typing import Callable, List, Optional, Sequence, Tuple
import cv2
import numpy as np

from core.board_detector import BoardDetector, BoardROI, board_mask_from_homography, get_autodarts_resolution
from core.metrics import (
    VIS_MARGINAL_CNR,
    VIS_MIN_CNR,
    VIS_MIN_CONTRAST,
    VIS_MIN_VALID,
    QualityEvaluator,
    QualityMetrics,
    TipROI,
    TipScore,
    aggregate_tips,
    make_dart_exclusion,
    tip_metrics,
    _gray_stack,
)
from core.camera_profiles import DEFAULT_PROFILE, CameraProfile
from core.v4l2_device import CameraSettings, V4L2Device

logger = logging.getLogger(__name__)

ProgressCallback = Callable[..., None]

# The camera-specific numbers (exposure range, knee, AE lock, tone targets and guards) live in the
# selected CameraProfile (core.camera_profiles); what follows is the measurement method itself.

# Burst lengths (a candidate is only compared with a reference of the same N).
SEARCH_BURST = 4
VERIFY_BURST = 10

# Not-worse rule: the new settings are applied unless the tip score drops by more than this, or by more
# than twice the repeat-to-repeat spread of the A/B measurement, whichever is larger (capped).
NONINFERIORITY_PCT = 5.0
NONINFERIORITY_MAX_PCT = 15.0  # an auto-exposure baseline drifting by 25 % between its two bursts must not open the gate wider
GAIN_REPORT_PCT = 3.0  # below this the change in tip clarity is reported as "the same"

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

UNITS_PER_MM = 1.9  # Autodarts board space: 1000 units, bull at (500, 500)


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


def sisal_masks(gray: np.ndarray, homography: Optional[np.ndarray], roi: Optional[BoardROI],
                exclude: Optional[np.ndarray] = None) -> Tuple[np.ndarray, np.ndarray]:
    """(white, black) boolean masks of the single-area sisal.

    With the image->board homography: the 40 single areas (20 sectors x inner/outer), 2 degrees clear
    of the radial wires, split into the brighter and the darker half by their median brightness, so the
    board's orientation does not matter. Without it: the detector's flat segment areas split at the
    30th / 70th brightness percentile.
    """
    h, w = gray.shape[:2]
    if homography is not None:
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
        p = np.stack([xx, yy, np.ones_like(xx)], -1) @ np.asarray(homography, np.float64).T
        bx, by = p[..., 0] / p[..., 2] - 500.0, p[..., 1] / p[..., 2] - 500.0
        r = np.hypot(bx, by) / UNITS_PER_MM
        ang = (np.degrees(np.arctan2(bx, -by)) + 369.0) % 360.0
        sector = (ang // 18).astype(int)
        clear = np.abs((ang % 18) - 9) < 7.0
        single = (((r > 19) & (r < 96)) | ((r > 110) & (r < 159))) & clear
        if exclude is not None:
            single &= exclude == 0
        if np.count_nonzero(single) > 2000:
            med = np.array([np.median(gray[single & (sector == s)]) if np.any(single & (sector == s)) else 0.0
                            for s in range(20)])
            bright = med > np.median(med)
            return single & bright[sector], single & ~bright[sector]
    flat = (roi.uniform_mask > 0) if roi is not None and roi.uniform_mask is not None else np.ones((h, w), bool)
    if exclude is not None:
        flat &= exclude == 0
    vals = gray[flat]
    if vals.size < 100:
        return flat, flat
    lo, hi = np.percentile(vals, [30, 70])
    return flat & (gray >= hi), flat & (gray <= lo)


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
    improvement_percentage: float  # change of the tip score (CNR) from the A/B means
    tips: List[TipROI] = dataclasses.field(default_factory=list)  # frozen ROIs used for scoring
    initial_tip_score: Optional[TipScore] = None  # A side (initial settings, 10-frame burst)
    optimized_tip_score: Optional[TipScore] = None  # B side (new settings, 10-frame burst)
    ab_spread_pct: float = 0.0  # largest repeat-to-repeat difference within one A/B side, in %
    darts_moved: bool = False
    fallback_board_only: bool = False  # no tips / no visible tip: only the guards decide
    tip_component_before: Optional[float] = None  # A/B means of the tip score (0.6 mean + 0.4 weakest tip CNR)
    tip_component_after: Optional[float] = None
    veto: Optional[str] = None  # guard that failed on the B side (diagnostic text)
    details: List[str] = dataclasses.field(default_factory=list)  # technical log for the Details dialog
    hidden_probe: List[HiddenProbe] = dataclasses.field(default_factory=list)  # faint tips, estimated position
    board_lum_before: Optional[float] = None  # mean board luminance (DN) with the initial settings
    board_lum_after: Optional[float] = None  # ... with the tuned settings
    lock_reference: Optional[float] = None  # mean frame level (DN) with the tuned settings: the lock watcher's check

    @property
    def board_lum_final(self) -> Optional[float]:
        """Board luminance of what the camera runs with after this result is applied (or kept)."""
        return self.board_lum_after if self.is_improvement else self.board_lum_before

    @property
    def required_improvement_pct(self) -> float:
        """Not-worse margin: the tip score may drop by at most this much (a negative number)."""
        return -min(NONINFERIORITY_MAX_PCT, max(NONINFERIORITY_PCT, 2.0 * (self.ab_spread_pct or 0.0)))

    @property
    def is_improvement(self) -> bool:
        """Apply the new settings: darts still, no guard failed, tips not measurably worse."""
        if self.darts_moved or self.veto:
            return False
        if self.improvement_percentage is None or self.improvement_percentage < self.required_improvement_pct:
            return False
        return True

    @property
    def clearer(self) -> bool:
        """Tips measurably clearer (not just not worse)."""
        return self.is_improvement and (self.improvement_percentage or 0.0) >= GAIN_REPORT_PCT

    def badge(self) -> Tuple[str, str]:
        """(label, css class) describing the outcome for review badges."""
        if self.darts_moved:
            return "Darts moved", "status-pill-warn"
        if self.clearer:
            return f"+{self.improvement_percentage:.0f} %", "status-pill-ok"
        if self.is_improvement:
            return "Fixed", "status-pill-ok"
        return "Kept", "status-pill-warn"


@dataclasses.dataclass
class _Eval:
    """One measured burst: tip score and the burst it came from."""
    settings: CameraSettings
    total: float
    tip_score: Optional[TipScore]
    burst: List[np.ndarray]


class _Cancelled(Exception):
    pass


class CameraOptimizer:
    """Measures the exposure, gain and tone settings of one camera and verifies them A/B."""

    def __init__(self, device: V4L2Device, profile: Optional[CameraProfile] = None):
        self.device = device
        self.profile = profile or DEFAULT_PROFILE
        self.detector = BoardDetector()
        self.evaluator = QualityEvaluator()
        self._sleep = time.sleep  # replaced by tests
        self.ab_settle_s = AB_SETTLE_S
        self._settle_fn: Callable[[], None] = lambda: None  # set per run: waits for auto exposure

    # ------------------------------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------------------------------

    def run_optimization(
        self,
        progress_cb: Optional[ProgressCallback] = None,
        cancel_event: Optional[threading.Event] = None,
        tips: Optional[List[TipROI]] = None,
        homography: Optional[np.ndarray] = None,
    ) -> Optional[OptimizationResult]:
        """Tune one camera. Returns None on cancel or capture failure.

        `tips` are the frozen TipROIs of this camera from step 2 (tip_rois_for_camera), in image
        px at the Autodarts capture resolution. `homography` (image -> board) locates the white and
        black sisal; without it the calibration file, then the detected board, is used.
        progress_cb(stage_key, fraction, user_text, frame=None): stage_key is one of
        "brightness", "sensitivity", "detail", "check"; user_text is plain language.
        """
        details: List[str] = []

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

            def get_frame(flush: Optional[int] = None) -> Optional[np.ndarray]:
                flush = self.profile.flush if flush is None else flush
                if cap is not None and cap.isOpened():
                    return self.device.read_stream_frame(cap, flush_frames=flush)
                return self.device.capture_frame(width=cap_w, height=cap_h, warmup_frames=max(3, flush))

            def get_burst(count: int, flush: Optional[int] = None) -> Optional[List[np.ndarray]]:
                flush = self.profile.flush if flush is None else flush
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

            return self._run(report, note, check_abort, get_frame, get_burst, tips, homography,
                             (cap_w, cap_h), details)
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

    def _run(self, report, note, check_abort, get_frame, get_burst, tips, homography, cap_size,
             details) -> Optional[OptimizationResult]:
        initial_burst = get_burst(VERIFY_BURST)
        if not initial_burst:
            logger.error("Failed to capture baseline frames from %s", self.device.dev_node)
            return None
        check_abort()
        initial_frame = initial_burst[0].copy()
        shape = initial_frame.shape[:2]

        roi = self.detector.detect_board(initial_frame, dev_node=self.device.dev_node)
        report(STAGE_BRIGHTNESS, 0.04, "Looking at the current picture", frame=initial_frame)

        initial_ctrls = self.device.query_controls() or {}
        note("camera profile: %s", self.profile.name)
        initial_settings = self._settings_from_controls(initial_ctrls)

        tips = list(tips or [])
        tips = [t for t in tips if self._tip_in_frame(t, shape)] if tips else []
        use_tips = any(t.visible for t in tips)
        H = self._homography(homography, cap_size, shape)
        board_mask = board_mask_from_homography(H, shape) if H is not None else roi.board_mask
        exclude = make_dart_exclusion(shape, tips) if tips else None
        gray0 = _gray_stack(initial_burst).mean(axis=0)
        white, black = sisal_masks(gray0, H, roi, exclude)
        note("scoring on %s; %d white / %d black sisal px", "%d visible tips" % sum(t.visible for t in tips)
             if use_tips else "the board only", int(white.sum()), int(black.sum()))

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

        def score(burst: List[np.ndarray], settings: CameraSettings) -> _Eval:
            """Tip score: 0.6 mean + 0.4 weakest CNR of the visible tips (CNR is what separates a point
            from the sisal; DN contrast alone rewards gain and sharpening noise as much as signal)."""
            ts = self.evaluator.evaluate_tips(burst, roi, tips, board_mask=board_mask, exclude_mask=exclude) \
                if use_tips else None
            total = 0.0
            if ts is not None:
                cnrs = [m.cnr for m in ts.tips if m is not None]
                marg = [t.marginal for t, m in zip(tips, ts.tips) if m is not None]
                total = aggregate_tips(cnrs, marg)
            return _Eval(settings, total, ts, burst)

        def result(**kw) -> OptimizationResult:
            base = dict(camera_node=self.device.dev_node, target_kelvin=0, initial_settings=initial_settings,
                        initial_frame=initial_frame, roi=roi, tips=tips, fallback_board_only=not use_tips,
                        details=details)
            base.update(kw)
            return OptimizationResult(**base)

        # 1. Did the darts move since step 2? (same settings as step 2, so contrast is comparable)
        moved = moved_tips(initial_burst)
        if moved:
            first = score(initial_burst, initial_settings)
            m = self._metrics(initial_burst, roi, first)
            report(STAGE_CHECK, 1.0, "The darts seem to have moved. This camera keeps its settings.", frame=initial_frame)
            return result(initial_metrics=m, optimized_settings=initial_settings, optimized_metrics=m,
                          optimized_frame=initial_frame, improvement_percentage=0.0, initial_tip_score=first.tip_score,
                          optimized_tip_score=first.tip_score, darts_moved=True)
        check_abort()

        self._settle_fn = lambda: self._settle(get_frame, roi.board_mask, note)

        # 2.-4. Measure the new settings.
        new = self._measure_settings(report, note, check_abort, get_burst, initial_ctrls, initial_settings,
                                     white, black)
        if new is None:
            return None
        settings, veto = new

        def settle_auto_exposure() -> None:
            self._settle(get_frame, roi.board_mask, note)

        # 5. A/B check, interleaved: initial -> new -> initial -> new.
        report(STAGE_CHECK, 0.80, "Comparing with the current settings")
        bursts = {}
        for i, side in enumerate(("a1", "b1", "a2", "b2")):
            check_abort()
            if side.startswith("a"):
                self.device.restore_snapshot()
                settle_auto_exposure()
            else:
                self._apply(settings)
            bursts[side] = get_burst(VERIFY_BURST)
            if not bursts[side]:
                return None
            report(STAGE_CHECK, 0.80 + 0.04 * (i + 1), "Comparing with the current settings", frame=bursts[side][0])
        a1, b1 = score(bursts["a1"], initial_settings), score(bursts["b1"], settings)
        a2, b2 = score(bursts["a2"], initial_settings), score(bursts["b2"], settings)
        if veto is None:
            veto = self._tone_veto(bursts["b2"], white, black)
        mean_a, mean_b = (a1.total + a2.total) / 2.0, (b1.total + b2.total) / 2.0
        if use_tips and mean_a > 0:
            improvement = round(max(-100.0, (mean_b - mean_a) / mean_a * 100.0), 1)
            spread = max(abs(a1.total - a2.total), abs(b1.total - b2.total))
            spread_pct = round(100.0 * spread / mean_a, 2)
        else:
            improvement, spread_pct = 0.0, 0.0  # no tips: the measured settings stand unless a guard fails
        note("A/B tip score: A %.2f/%.2f, B %.2f/%.2f -> %+.1f%% (spread %.1f%%)%s", a1.total, a2.total, b1.total,
             b2.total, improvement, spread_pct, f" VETO {veto}" if veto else "")

        # 6. Final darts-moved check on the restored-settings burst against the first burst (same
        # settings, so only movement changes the tip patches).
        report(STAGE_CHECK, 0.97, "Checking that the darts did not move")
        moved = moved_tips(bursts["a2"], reference=initial_burst)

        # Reported only: a failure here must never cost the finished measurement, and a moved dart
        # has no meaningful "estimated position" any more
        hidden_probe: List[HiddenProbe] = []
        if tips and not moved:
            try:
                hidden_probe = probe_faint_tips(tips, bursts["a2"], bursts["b2"])
            except Exception as e:
                logger.warning("Faint-tip probe failed on %s: %s", self.device.dev_node, e)
        for hp in hidden_probe:
            note("faint tip %d at its estimated position: C %.0f -> %.0f DN, CNR %.1f -> %.1f%s", hp.dart_id,
                 hp.before_contrast, hp.after_contrast, hp.before_cnr, hp.after_cnr,
                 " -> VISIBLE" if hp.became_visible else "")
        lum = lambda b: float(_gray_stack(b).mean(axis=0)[board_mask > 0].mean()) if np.any(board_mask) else None
        res = result(initial_metrics=self._metrics(bursts["a2"], roi, a2), optimized_settings=settings,
                     optimized_metrics=self._metrics(bursts["b2"], roi, b2), optimized_frame=bursts["b2"][0].copy(),
                     initial_frame=bursts["a2"][0].copy(), improvement_percentage=improvement,
                     initial_tip_score=a2.tip_score, optimized_tip_score=b2.tip_score, ab_spread_pct=spread_pct,
                     darts_moved=bool(moved), veto=veto, hidden_probe=hidden_probe,
                     tip_component_before=round(mean_a, 3) if use_tips else None,
                     tip_component_after=round(mean_b, 3) if use_tips else None,
                     board_lum_before=lum(bursts["a2"]), board_lum_after=lum(bursts["b2"]),
                     lock_reference=round(float(_gray_stack(bursts["b2"]).mean()), 2))
        note("decision: %s (allowed %+.1f%%)", "apply" if res.is_improvement else "keep", res.required_improvement_pct)
        if res.darts_moved:
            report(STAGE_CHECK, 1.0, "The darts seem to have moved. This camera keeps its settings.", frame=res.optimized_frame)
        elif res.clearer:
            report(STAGE_CHECK, 1.0, f"Done: tips {improvement:.0f} % clearer", frame=res.optimized_frame)
        elif res.is_improvement:
            report(STAGE_CHECK, 1.0, "Done: fixed exposure, tips as clear as before", frame=res.optimized_frame)
        else:
            report(STAGE_CHECK, 1.0, "Done: the current settings stay", frame=res.optimized_frame)
        return res

    # ------------------------------------------------------------------------------------------
    # Measured settings: knee exposure, gain, tone guards
    # ------------------------------------------------------------------------------------------

    def _measure_settings(self, report, note, check_abort, get_burst, ctrls, initial: CameraSettings,
                          white: np.ndarray, black: np.ndarray) -> Optional[Tuple[CameraSettings, Optional[str]]]:
        p = self.profile

        def rng(name: str, lo: int, hi: int, default: int) -> Tuple[int, int, int]:
            c = ctrls.get(name)
            return (c.min_val, c.max_val, c.default_val) if c is not None else p.fallback(name, lo, hi, default)

        exp_min, exp_max, _ = rng("exposure_time_absolute", 1, 1250, 166)
        gain_min, gain_max, _ = rng("gain", 0, 128, 64)
        g_min, g_max, g_def = rng("gamma", 100, 500, 300)
        con_min, con_max, con_def = rng("contrast", 0, 100, 34)
        sharp_min, sharp_max, sharp_def = rng("sharpness", 0, 100, 40)
        # A profile can fix tone controls its tip measurements found better than the camera default
        if p.contrast is not None:
            con_def = max(con_min, min(con_max, p.contrast))
        if p.sharpness is not None:
            sharp_def = max(sharp_min, min(sharp_max, p.sharpness))
        _, _, bright_def = rng("brightness", -64, 64, 0)
        e_long = max(exp_min, min(exp_max, p.max_exposure))

        gamma = int(round(g_def + p.gamma_lift * (g_max - g_def))) if g_max > g_def else g_def
        gamma = max(g_min, min(g_max, gamma))
        settings = dataclasses.replace(initial, exposure_auto=1, gain=gain_min, gamma=gamma, contrast=con_def,
                                       sharpness=sharp_def, brightness=bright_def)
        self.device.set_control("auto_exposure", 1)
        for name in ("contrast", "sharpness", "brightness"):
            if name in ctrls:
                self.device.set_control(name, getattr(settings, name))
        self.device.set_control("gamma", g_def)  # exposure is read off the response at the default curve
        self.device.set_control("gain", gain_min)

        steps = [0]

        def stats(exposure: Optional[int] = None, gain: Optional[int] = None, n: int = SEARCH_BURST):
            check_abort()
            if exposure is not None:
                self.device.set_control("exposure_time_absolute", int(exposure))
            if gain is not None:
                self.device.set_control("gain", int(gain))
            self._sleep(0.05)
            burst = get_burst(n)
            if not burst:
                return None
            steps[0] += 1
            st = _gray_stack(burst)
            m = st.mean(axis=0)
            return dict(white=float(np.median(m[white])) if white.any() else float(np.median(m)),
                        black=float(m[black].mean()) if black.any() else float(np.percentile(m, 10)),
                        black_p2=float(np.percentile(m[black], 2)) if black.any() else float(np.percentile(m, 2)),
                        white_p999=float(np.percentile(st[:, white], 99.9)) if white.any() else float(np.percentile(st, 99.9)),
                        frame=burst[0])

        exposure, lock_blc = e_long, None
        if p.exposure_strategy == "ae_target":
            lock_blc = self._pick_ae_target(report, note, stats, ctrls)
            exposure = 0  # the firmware does not report the exposure auto exposure picked
        if lock_blc is None:
            # Sensor ceiling: where a saturated white pixel lands at minimum gain.
            report(STAGE_BRIGHTNESS, 0.06, "Finding the brightest the sensor can see")
            s_long, s_short = stats(e_long), stats(int(e_long * 0.8))
            if s_long is None or s_short is None:
                return None
            ceiling = s_long["white"]
            saturated = abs(s_long["white"] - s_short["white"]) <= p.saturated_tol
            note("sensor ceiling %.1f DN at exposure %d (%.1f at %d)%s", ceiling, e_long, s_short["white"],
                 int(e_long * 0.8), "" if saturated else ": white never saturates, using the longest exposure")

            exposure = e_long
            if saturated:
                lo, hi = exp_min, e_long
                while hi - lo > max(2, lo // 50):
                    mid = (lo + hi) // 2
                    s = stats(mid)
                    if s is None:
                        return None
                    if s["white"] <= p.knee_frac * ceiling:
                        lo = mid
                    else:
                        hi = mid
                    report(STAGE_BRIGHTNESS, min(0.30, 0.08 + 0.025 * steps[0]), "Finding the right exposure", frame=s["frame"])
                knee = lo
                exposure = max(exp_min, min(e_long, int(round(knee * p.knee_overshoot))))
                note("knee exposure %d (white %.0f%% of the ceiling), aiming at %d", knee, p.knee_frac * 100, exposure)
                lock = self._pick_ae_lock(report, note, stats, ctrls, knee, exp_min, e_long)
                if lock is not None:
                    lock_blc, exposure = lock
        if lock_blc is None:
            self.device.set_control("auto_exposure", 1)
            self.device.set_control("exposure_time_absolute", exposure)
            settings = dataclasses.replace(settings, exposure_absolute=exposure)
        else:
            # exposure_auto stays as found: to_controls() / Autodarts' config store a lock as auto exposure
            settings = dataclasses.replace(settings, exposure_auto=3, exposure_lock=1, exposure_absolute=exposure,
                                           backlight_compensation=lock_blc)
            self._lock_ae(lock_blc, ctrls)  # lock: no exposure written
        self.device.set_control("gamma", settings.gamma)

        # Gain: white median at p.white_target (monotonic in gain -> bisection).
        lo, hi = gain_min, gain_max
        k = 0
        while hi - lo > 1:
            mid = (lo + hi) // 2
            s = stats(gain=mid, n=2)
            if s is None:
                return None
            if s["white"] <= p.white_target:
                lo = mid
            else:
                hi = mid
            k += 1
            report(STAGE_SENSITIVITY, min(0.60, 0.42 + 0.025 * k), "Adjusting sensitivity", frame=s["frame"])
        settings = dataclasses.replace(settings, gain=lo)
        note("gain %d puts white at %.0f DN", lo, p.white_target)

        # Tone guards.
        report(STAGE_DETAIL, 0.62, "Checking blacks and whites")
        s = stats(gain=settings.gain, n=VERIFY_BURST)
        if s is None:
            return None
        while s["white_p999"] >= p.clip_p999 and settings.gain > gain_min:
            settings = dataclasses.replace(settings, gain=settings.gain - 1)
            s = stats(gain=settings.gain, n=VERIFY_BURST)
            if s is None:
                return None
        # Gain at its minimum and white still clipping: an auto-exposure target can step down instead
        # (it moves in steps and can land one step higher than where it was measured)
        blc_ctrl = ctrls.get("backlight_compensation")
        for _ in range(10):
            if not (s["white_p999"] >= p.clip_p999 and p.exposure_strategy == "ae_target" and settings.exposure_lock
                    and blc_ctrl is not None and settings.backlight_compensation - 2 >= blc_ctrl.min_val):
                break
            settings = dataclasses.replace(settings, backlight_compensation=settings.backlight_compensation - 2)
            self._lock_ae(settings.backlight_compensation, ctrls)
            s = stats(n=VERIFY_BURST)
            if s is None:
                return None
            note("white clips: auto exposure locked again with backlight compensation %d", settings.backlight_compensation)
        while s["black"] > p.black_max_mean and settings.gamma - p.gamma_step >= max(g_min, g_def):
            settings = dataclasses.replace(settings, gamma=settings.gamma - p.gamma_step)
            self.device.set_control("gamma", settings.gamma)
            s = stats(n=VERIFY_BURST)
            if s is None:
                return None
        while s["black_p2"] < p.black_floor_p2 and settings.contrast - p.contrast_step >= 0 and "contrast" in ctrls:
            settings = dataclasses.replace(settings, contrast=settings.contrast - p.contrast_step)
            self.device.set_control("contrast", settings.contrast)
            s = stats(n=VERIFY_BURST)
            if s is None:
                return None
        report(STAGE_DETAIL, 0.78, "Blacks and whites checked", frame=s["frame"])
        note("%s: white %.0f (p99.9 %.0f), black %.0f (p2 %.1f) DN", self._fmt(settings), s["white"], s["white_p999"],
             s["black"], s["black_p2"])
        veto = None
        if s["white_p999"] >= p.clip_p999:
            veto = f"white sisal clips ({s['white_p999']:.0f} DN)"
        elif s["black_p2"] < p.black_floor_p2 / 2:
            veto = f"black sisal crushed ({s['black_p2']:.1f} DN)"
        return settings, veto

    def _pick_ae_target(self, report, note, stats, ctrls) -> Optional[int]:
        """backlight_compensation at which auto exposure puts the white sisal at white_target.

        For cameras whose auto exposure uses backlight_compensation as a fine, repeatable brightness
        target (and whose written exposure is not repeatable): white rises with it, so bisect for the
        brightest target whose white sisal stays at white_target and below the clip guard.
        """
        p = self.profile
        c = ctrls.get("backlight_compensation")
        if c is None or "auto_exposure" not in ctrls:
            return None
        self.device.set_control("auto_exposure", 3)

        def fits(blc: int) -> Optional[bool]:
            self.device.set_control("backlight_compensation", blc)
            self._settle_fn()
            st = stats()
            if st is None:
                return None
            note("auto exposure with backlight compensation %d: white %.0f (p99.9 %.0f) DN", blc, st["white"],
                 st["white_p999"])
            report(STAGE_BRIGHTNESS, min(0.40, 0.08 + 0.03 * steps), "Finding the right exposure", frame=st["frame"])
            return st["white"] <= p.white_target and st["white_p999"] < p.clip_p999

        steps = 0
        lo, hi = c.min_val, c.max_val
        ok = fits(hi)
        if ok is None:
            return None
        if ok:
            lo = hi  # even the brightest target stays below: use it, gain makes up the rest
        while hi - lo > max(1, c.step):
            steps += 1
            mid = lo + (hi - lo) // 2
            ok = fits(mid)
            if ok is None:
                return None
            if ok:
                lo = mid
            else:
                hi = mid
        note("locking auto exposure with backlight compensation %d", lo)
        return lo

    def _pick_ae_lock(self, report, note, stats, ctrls, knee: int, exp_min: int, e_long: int) -> Optional[Tuple[int, int]]:
        """(backlight_compensation, equivalent exposure) of the locked auto exposure nearest the target.

        The written-exposure response (black-sisal level at gain 0) is the ruler: each metering level
        converges, is locked and measured, and its exposure is read off that response. Brightness is
        what matters for the comparison; the lock's lower noise comes on top.
        """
        p = self.profile
        c = ctrls.get("backlight_compensation")
        if not p.ae_lock or c is None or "auto_exposure" not in ctrls:
            return None
        table = []
        for f in p.response_steps:
            e = max(exp_min, min(e_long, int(round(knee * f))))
            st = stats(e)
            if st is None:
                return None
            table.append((e, st["black"]))
        table.sort()
        es, bl = np.array([t[0] for t in table], float), np.array([t[1] for t in table], float)
        bl = np.maximum.accumulate(bl)  # noise must not make the ruler run backwards
        target = knee * p.knee_overshoot
        best = None
        for i, blc in enumerate(range(c.min_val, c.max_val + 1, max(1, c.step))):
            self.device.set_control("backlight_compensation", blc)
            self.device.set_control("auto_exposure", 3)
            self._settle_fn()
            self.device.set_control("auto_exposure", 1)
            st = stats()
            if st is None:
                return None
            if st["black"] > bl[-1] or st["black"] < bl[0]:
                note("auto exposure with backlight compensation %d lands outside %d..%d", blc, es[0], es[-1])
                continue
            e_eq = float(np.interp(st["black"], bl, es))
            kf = e_eq / knee
            note("auto exposure with backlight compensation %d: like exposure %.0f (%.2f x knee)", blc, e_eq, kf)
            report(STAGE_BRIGHTNESS, min(0.40, 0.30 + 0.03 * (i + 1)), "Trying the camera's own exposure", frame=st["frame"])
            if p.lock_range[0] <= kf <= p.lock_range[1] and (best is None or abs(math.log(e_eq / target)) < abs(math.log(best[1] / target))):
                best = (blc, int(round(e_eq)))
        self.device.set_control("backlight_compensation", c.default_val)
        if best is None:
            note("no auto-exposure lock lands near the knee: writing the exposure")
        else:
            note("locking auto exposure with backlight compensation %d", best[0])
        return best

    def _tone_veto(self, burst: List[np.ndarray], white: np.ndarray, black: np.ndarray) -> Optional[str]:
        """Guards on the final B burst: no clipped white sisal, no crushed black sisal."""
        p = self.profile
        st = _gray_stack(burst)
        if white.any() and float(np.percentile(st[:, white], 99.9)) >= p.clip_p999 + 3:
            return "white sisal clips"
        if black.any() and float(np.percentile(st.mean(axis=0)[black], 2)) < p.black_floor_p2 / 2:
            return "black sisal crushed"
        return None

    # ------------------------------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------------------------------

    def _apply(self, s: CameraSettings):
        """Put the camera on the measured settings and let it settle.

        A lock is re-created the way the watcher does it: auto exposure with the metering bias, wait
        until it settled, then manual mode without writing an exposure.
        """
        for name in ("gain", "gamma", "contrast", "sharpness", "brightness"):
            self.device.set_control(name, getattr(s, name))
        if s.exposure_lock:
            self._lock_ae(s.backlight_compensation)
        else:
            self.device.set_control("auto_exposure", 1)
            self.device.set_control("exposure_time_absolute", s.exposure_absolute)
        self._sleep(0.08)

    def _lock_ae(self, blc: int, ctrls=None) -> None:
        """Auto exposure metered with `blc`, settled, then locked (manual mode, no exposure written).

        With a profile's ae_approach_below it first settles that much lower, so auto exposure always
        reaches its target from below and lands on the same step as the lock watcher's re-lock.
        """
        below = self.profile.ae_approach_below
        if below:
            c = (ctrls or {}).get("backlight_compensation")
            lo = c.min_val if c is not None else 0
            self.device.set_control("backlight_compensation", max(lo, blc - below))
            self.device.set_control("auto_exposure", 3)
            self._settle_fn()
        self.device.set_control("backlight_compensation", blc)
        self.device.set_control("auto_exposure", 3)
        self._settle_fn()
        self.device.set_control("auto_exposure", 1)

    def _settle(self, get_frame, mask: Optional[np.ndarray], note) -> None:
        """Wait until auto exposure has converged (after a restore, or before a lock).

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
            lum = float(gray[mask > 0].mean()) if mask is not None and mask.shape == gray.shape else float(gray.mean())
            if prev is not None and abs(lum - prev) <= AB_SETTLE_TOL * max(prev, 1.0):
                steady += 1
                if steady >= 2:
                    return
            else:
                steady = 0
            prev = lum
            self._sleep(0.25)
        note("auto exposure still drifting after the settle wait")

    @staticmethod
    def _fmt(s: CameraSettings) -> str:
        if s.exposure_lock:
            e = f"AE-lock(blc {s.backlight_compensation})" + (f" ~{s.exposure_absolute}" if s.exposure_absolute > 0 else "")
        else:
            e = str(s.exposure_absolute)
        return f"E={e} G={s.gain} S={s.sharpness} C={s.contrast} gamma={s.gamma}"

    @staticmethod
    def _tip_in_frame(t: TipROI, shape) -> bool:
        return 0 <= t.tip[0] < shape[1] and 0 <= t.tip[1] < shape[0]

    def _homography(self, homography, cap_size, shape) -> Optional[np.ndarray]:
        H = homography
        if H is None:
            try:
                H = self.detector.load_calibration_homography(cap_size[0], cap_size[1], dev_node=self.device.dev_node)
            except Exception:
                H = None
        if H is None or (shape[1], shape[0]) != tuple(cap_size):
            return None
        H = np.asarray(H, np.float64)
        return H if np.count_nonzero(board_mask_from_homography(H, shape)) > 1000 else None

    def _metrics(self, burst: List[np.ndarray], roi: BoardROI, e: _Eval) -> QualityMetrics:
        """Board-wide QualityMetrics (Details / session export), with the tip score attached."""
        m = self.evaluator.evaluate(burst, roi)
        if e.tip_score is not None and not e.tip_score.board_only:
            m.composite_score = e.tip_score.total
            m.tip_component = e.tip_score.tip_component
            m.board_component = e.tip_score.board_component
            m.tips = e.tip_score.tips
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
            backlight_compensation=val("backlight_compensation", 0),
        )
