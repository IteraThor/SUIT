"""Hierarchical Camera Calibration and Optimization Engine.

Implements the two-stage coarse-to-fine precision scan:
Stage 1: Sensor Physics Anchor (Kelvin, Max Exposure at 30 FPS, Minimum Gain)
Stage 2: ISP 2D Grid Scan (Sharpness x Contrast with temporal noise rejection)
"""

from __future__ import annotations
import dataclasses
import logging
import threading
import time
from typing import Callable, List, Optional, Tuple
import cv2
import numpy as np

from core.board_detector import BoardDetector, BoardROI, get_autodarts_resolution
from core.metrics import QualityEvaluator, QualityMetrics
from core.v4l2_device import CameraSettings, V4L2Device

logger = logging.getLogger(__name__)

ProgressCallback = Callable[..., None]

# V4L2 exposure_time_absolute is in 100 µs units.
FLICKER_PERIOD_UNITS = {1: 10000.0 / 100.0, 2: 10000.0 / 120.0}  # 50 Hz -> 10 ms, 60 Hz -> 8.33 ms


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


@dataclasses.dataclass
class OptimizationResult:
    camera_node: str
    target_kelvin: int
    initial_settings: CameraSettings
    initial_metrics: QualityMetrics
    initial_frame: np.ndarray
    optimized_settings: CameraSettings
    optimized_metrics: QualityMetrics
    optimized_frame: np.ndarray
    roi: BoardROI
    improvement_percentage: float

    # Below this the gain is within measurement noise, so applying the new settings is not worth it.
    MIN_IMPROVEMENT_PCT = 2.0

    @property
    def is_improvement(self) -> bool:
        return self.improvement_percentage >= self.MIN_IMPROVEMENT_PCT

    def badge(self) -> Tuple[str, str]:
        """(label, css class) describing the outcome for review badges."""
        if self.is_improvement:
            return f"+{self.improvement_percentage:.0f}% Quality", "status-pill-ok"
        return "No gain – keeping current", "status-pill-warn"


class CameraOptimizer:
    """Coordinates camera parameter tuning and A/B verification."""

    def __init__(self, device: V4L2Device):
        self.device = device
        self.detector = BoardDetector()
        self.evaluator = QualityEvaluator()

    def run_optimization(
        self,
        precision_mode: str = "ultra",
        progress_cb: Optional[ProgressCallback] = None,
        cancel_event: Optional[threading.Event] = None,
        empty_baseline: Optional[np.ndarray] = None,
    ) -> Optional[OptimizationResult]:
        """Execute full calibration sequence with cancellation support."""
        is_ultra = precision_mode == "ultra"
        is_deep = precision_mode in ("deep", "ultra")
        burst_count = 10 if is_ultra else (5 if is_deep else 3)

        def report(stage: str, progress: float, details: str, frame: Optional[np.ndarray] = None):
            if progress_cb:
                try:
                    progress_cb(stage, progress, details, frame)
                except TypeError:
                    progress_cb(stage, progress, details)

        def is_cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        def check_abort() -> bool:
            if is_cancelled():
                logger.info("Optimization aborted by user on %s. Restoring camera snapshot.", self.device.dev_node)
                self.device.restore_snapshot()
                report("Aborted", 0.0, "Calibration cancelled by user")
                return True
            return False

        report("Initializing", 0.03, f"Starting {precision_mode.upper()} calibration ({burst_count}-frame burst averaging)...")
        self.device.take_snapshot()

        if check_abort():
            return None

        cap_w, cap_h = get_autodarts_resolution()
        cap = None
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

        def get_burst(count: int, flush: int = 2) -> List[np.ndarray]:
            if cap is not None and cap.isOpened():
                return self.device.read_stream_burst(cap, count=count, flush_frames=flush)
            return self.device.capture_burst(count=count, width=cap_w, height=cap_h, warmup_frames=max(3, flush))


        try:
            # Capture initial baseline frames with multi-frame burst
            initial_burst = get_burst(count=burst_count, flush=4)
            if not initial_burst:
                logger.error("Failed to capture baseline frames from %s", self.device.dev_node)
                return None

            if check_abort():
                return None

            initial_frame = initial_burst[0].copy()

            report("Detecting Board", 0.08, "Analyzing dartboard geometry and wire masks...", frame=initial_frame)
            roi = self.detector.detect_board(initial_frame, dev_node=self.device.dev_node)
            roi_overlay = self.detector.create_roi_overlay(initial_frame, roi)
            report("Detecting Board", 0.12, "Identified wire edges and segment sampling zones", frame=roi_overlay)
            initial_metrics = self.evaluator.evaluate(initial_burst, roi, empty_frame=empty_baseline)

            if check_abort():
                return None

            initial_ctrls = self.device.query_controls()
            initial_settings = CameraSettings(
                exposure_auto=initial_ctrls.get("auto_exposure", type("", (), {"current_val": 1})).current_val,
                exposure_absolute=initial_ctrls.get("exposure_time_absolute", type("", (), {"current_val": 166})).current_val,
                gain=initial_ctrls.get("gain", type("", (), {"current_val": 0})).current_val,
                wb_auto=initial_ctrls.get("white_balance_automatic", type("", (), {"current_val": 1})).current_val,
                wb_temperature=initial_ctrls.get("white_balance_temperature", type("", (), {"current_val": 4600})).current_val,
                sharpness=initial_ctrls.get("sharpness", type("", (), {"current_val": 3})).current_val,
                contrast=initial_ctrls.get("contrast", type("", (), {"current_val": 32})).current_val,
                brightness=initial_ctrls.get("brightness", type("", (), {"current_val": 0})).current_val,
                gamma=initial_ctrls.get("gamma", type("", (), {"current_val": 300})).current_val,
                saturation=initial_ctrls.get("saturation", type("", (), {"current_val": 64})).current_val,
                power_line_frequency=initial_ctrls.get("power_line_frequency", type("", (), {"current_val": 1})).current_val,
            )

            # STAGE 1: Auto-Exposure Ground-Truth Matching
            # We measure the natural auto-exposure baseline brightness and find the exact
            # manual gain at 30 FPS flicker-free shutter that replicates the exact same brightness.
            report("Stage 1 / 3", 0.16, "Measuring auto-exposure baseline brightness...")

            gray_baseline = cv2.cvtColor(initial_frame, cv2.COLOR_BGR2GRAY)
            baseline_board = gray_baseline[roi.board_mask > 0]
            baseline_uniform = gray_baseline[roi.uniform_mask > 0]
            if len(baseline_uniform) > 50:
                baseline_white_lum = float(np.percentile(baseline_uniform, 90))
            elif len(baseline_board) > 50:
                baseline_white_lum = float(np.percentile(baseline_board, 90))
            else:
                baseline_white_lum = float(np.percentile(gray_baseline, 90))

            baseline_mean_lum = float(np.mean(baseline_board)) if len(baseline_board) > 0 else float(np.mean(gray_baseline))
            baseline_clip = float(np.sum(baseline_board >= 253)) / max(1, len(baseline_board)) if len(baseline_board) > 0 else 0.0

            # Safe target luminance clamp: Anti-blooming white ceiling protection (175 - 215 DN)
            # Guarantees >=40 DN specular headroom so metallic dart barrels do not clip and bloom into Hough lines.
            target_white_lum = max(175.0, min(215.0, baseline_white_lum))
            target_mean_lum = baseline_mean_lum

            logger.info("Baseline AE Target: white=%.1f DN (anti-blooming capped), mean=%.1f DN, clip=%.2f%%",
                        target_white_lum, target_mean_lum, baseline_clip * 100)

            # Hardware control limits
            exp_ctrl = initial_ctrls.get("exposure_time_absolute")
            exp_min = exp_ctrl.min_val if exp_ctrl else 1
            exp_max = exp_ctrl.max_val if exp_ctrl else 1000
            exp_step = getattr(exp_ctrl, "step", 1) or 1

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

            # Set manual mode while preserving ALL factory ISP settings (brightness, saturation, gamma, wb)
            self.device.set_control("auto_exposure", 1)  # Manual Mode
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
            for shutter in shutter_candidates:
                if check_abort():
                    return None
                self.device.set_control("exposure_time_absolute", shutter)
                time.sleep(0.08)
                f = get_frame(flush=3)
                if f is None:
                    continue
                lum = measure_white(f)
                report("Stage 1 / 3 (Shutter)", 0.17, f"Shutter {shutter * 0.1:.1f}ms at minimum gain -> White: {lum:.1f}/{target_white_lum:.0f} DN", frame=f)
                if lum <= target_white_lum + 5.0:
                    nominal_shutter = shutter
                    break

            logger.info("Selected flicker-free shutter %d (%.1f ms)", nominal_shutter, nominal_shutter * 0.1)
            report("Stage 1 / 3", 0.18, f"Matching baseline brightness ({target_white_lum:.0f} DN) in Manual Mode (Exposure {nominal_shutter * 0.1:.1f}ms locked)...")
            self.device.set_control("exposure_time_absolute", nominal_shutter)
            time.sleep(0.08)

            if check_abort():
                return None

            # Gain Sweep: Search for the exact gain that produces target_white_lum at nominal_shutter
            coarse_gains = sorted(list(set([
                min_g,
                min_g + int((max_g - min_g) * 0.05),
                min_g + int((max_g - min_g) * 0.12),
                min_g + int((max_g - min_g) * 0.20),
                min_g + int((max_g - min_g) * 0.30),
                min_g + int((max_g - min_g) * 0.42),
                min_g + int((max_g - min_g) * 0.55),
                min_g + int((max_g - min_g) * 0.70),
                max_g,
            ])))
            if initial_settings.gain in range(min_g, max_g + 1):
                coarse_gains.append(initial_settings.gain)
                coarse_gains = sorted(list(set(coarse_gains)))

            best_g = coarse_gains[0]
            best_g_diff = float("inf")
            best_g_lum = 0.0

            for idx, g in enumerate(coarse_gains):
                if check_abort():
                    return None

                self.device.set_control("gain", g)
                time.sleep(0.05)

                f = get_frame(flush=2)
                if f is None:
                    continue

                gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
                u_pix = gray[roi.uniform_mask > 0]
                lum = float(np.percentile(u_pix, 90)) if len(u_pix) > 50 else float(np.percentile(gray, 90))

                diff = abs(lum - target_white_lum)
                if diff < best_g_diff:
                    best_g_diff = diff
                    best_g = g
                    best_g_lum = lum

                pct = 0.18 + (idx / len(coarse_gains)) * 0.10
                report("Stage 1 / 3 (Gain Convergence)", pct, f"Gain {g} (Shutter {nominal_shutter*0.1:.0f}ms) -> White: {lum:.1f}/{target_white_lum:.0f} DN", frame=f)

                if lum >= target_white_lum + 5.0:
                    break

            # Fine gain polish around coarse winner
            fine_gain_step = max(1, gain_step * 2, int((max_g - min_g) * 0.02))
            fine_gains = sorted(list(set([
                max(min_g, min(max_g, best_g + offset * fine_gain_step))
                for offset in [-3, -2, -1, 0, 1, 2, 3]
            ])))

            for idx, g in enumerate(fine_gains):
                if check_abort():
                    return None
                if g in coarse_gains and g != best_g:
                    continue

                self.device.set_control("gain", g)
                time.sleep(0.04)

                f = get_frame(flush=2)
                if f is None:
                    continue

                gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
                u_pix = gray[roi.uniform_mask > 0]
                lum = float(np.percentile(u_pix, 90)) if len(u_pix) > 50 else float(np.percentile(gray, 90))

                diff = abs(lum - target_white_lum)
                if diff < best_g_diff:
                    best_g_diff = diff
                    best_g = g
                    best_g_lum = lum

                pct = 0.28 + (idx / len(fine_gains)) * 0.06
                report("Stage 1 / 3 (Fine Gain)", pct, f"Fine Gain {g} -> White: {lum:.1f}/{target_white_lum:.0f} DN", frame=f)

            optimal_exposure = nominal_shutter
            best_gain = best_g

            self.device.set_control("exposure_time_absolute", optimal_exposure)
            self.device.set_control("gain", best_gain)
            time.sleep(0.06)
            logger.info("Locked Stage 1 physical anchor: exposure=%d (locked), gain=%d (white=%.1f DN)",
                        optimal_exposure, best_gain, target_white_lum)

            current_settings = CameraSettings(
                exposure_auto=1,
                exposure_absolute=optimal_exposure,
                gain=best_gain,
                wb_auto=initial_settings.wb_auto,
                wb_temperature=initial_settings.wb_temperature,
                sharpness=initial_settings.sharpness,
                contrast=initial_settings.contrast,
                brightness=initial_settings.brightness,
                gamma=initial_settings.gamma,
                saturation=initial_settings.saturation,
                power_line_frequency=initial_settings.power_line_frequency,
            )

            # STAGE 2: Bounded ISP Tuning (Gamma, Contrast, Sharpness)
            report("Stage 2 / 3", 0.45, f"Scanning ISP gamma, contrast & sharpness ({burst_count}-frame burst)...")

            sharp_ctrl = initial_ctrls.get("sharpness")
            s_min = sharp_ctrl.min_val if sharp_ctrl else 0
            s_max = sharp_ctrl.max_val if sharp_ctrl else 100
            s_base = initial_settings.sharpness if initial_settings.sharpness is not None else (sharp_ctrl.default_val if sharp_ctrl else 40)

            contrast_ctrl = initial_ctrls.get("contrast")
            c_min = contrast_ctrl.min_val if contrast_ctrl else 0
            c_max = contrast_ctrl.max_val if contrast_ctrl else 100
            c_base = initial_settings.contrast if initial_settings.contrast is not None else (contrast_ctrl.default_val if contrast_ctrl else 34)

            gamma_ctrl = initial_ctrls.get("gamma")
            g_min = gamma_ctrl.min_val if gamma_ctrl else 100
            g_max = gamma_ctrl.max_val if gamma_ctrl else 500
            g_base = initial_settings.gamma if initial_settings.gamma is not None else 300

            # ISP Candidate Levels: focused search around empirical high-performance zone
            if precision_mode == "standard":
                gamma_levels = sorted(list(set([
                    max(g_min, min(g_max, val))
                    for val in [300, 380, 420]
                ])))
                coarse_s_levels = sorted(list(set([
                    max(s_min, min(s_max, val))
                    for val in [60, 70, 80]
                ])))
                coarse_c_levels = sorted(list(set([
                    max(c_min, min(c_max, val))
                    for val in [30, 34, 36]
                ])))
            else:
                gamma_levels = sorted(list(set([
                    max(g_min, min(g_max, val))
                    for val in [300, 340, 380, 420]
                ])))
                coarse_s_levels = sorted(list(set([
                    max(s_min, min(s_max, val))
                    for val in [60, 70, 75, 80]
                ])))
                coarse_c_levels = sorted(list(set([
                    max(c_min, min(c_max, val))
                    for val in [30, 32, 34, 36, 40]
                ])))

            total_triplets = len(gamma_levels) * len(coarse_s_levels) * len(coarse_c_levels)
            step_idx = 0

            best_score = -1.0
            best_settings = dataclasses.replace(current_settings)
            best_metrics = initial_metrics
            best_burst: List[np.ndarray] = initial_burst
            best_s = s_base
            best_c = c_base
            best_g = g_base

            for gam in gamma_levels:
                if check_abort():
                    return None
                self.device.set_control("gamma", gam)

                for s in coarse_s_levels:
                    if check_abort():
                        return None
                    self.device.set_control("sharpness", s)

                    for c in coarse_c_levels:
                        if check_abort():
                            return None

                        step_idx += 1
                        self.device.set_control("contrast", c)
                        time.sleep(0.04)

                        burst = get_burst(count=burst_count, flush=2)
                        if not burst:
                            continue

                        gray_check = cv2.cvtColor(burst[0], cv2.COLOR_BGR2GRAY)
                        board_px = gray_check[roi.board_mask > 0]
                        if len(board_px) > 100:
                            p10 = float(np.percentile(board_px, 10))
                            curr_mean = float(np.mean(board_px))

                            # Veto 1: True sensor zero clipping
                            if p10 < 3.0:
                                pct = 0.45 + (step_idx / total_triplets) * 0.40
                                report("Stage 2 / 3", pct, f"Skipped G={gam}, S={s}, C={c} (shadows clipped: {p10:.0f} DN)", frame=burst[0])
                                continue

                            # Veto 2: Excessive image darkening
                            if curr_mean < baseline_mean_lum * 0.65:
                                pct = 0.45 + (step_idx / total_triplets) * 0.40
                                report("Stage 2 / 3", pct, f"Skipped G={gam}, S={s}, C={c} (image darkened: {curr_mean:.0f}/{baseline_mean_lum:.0f} DN)", frame=burst[0])
                                continue

                        halo_val = self.evaluator.calculate_halo_overshoot(burst[0], roi.wire_mask, roi.uniform_mask)
                        if halo_val > 0.22:
                            pct = 0.45 + (step_idx / total_triplets) * 0.40
                            report("Stage 2 / 3", pct, f"Skipped G={gam}, S={s}, C={c} (halo ringing: {halo_val*100:.1f}%)", frame=burst[0])
                            continue

                        metrics = self.evaluator.evaluate(
                            burst,
                            roi,
                            target_white_lum=target_white_lum,
                            target_mean_lum=target_mean_lum,
                            empty_frame=empty_baseline,
                        )

                        if metrics.composite_score > best_score:
                            best_score = metrics.composite_score
                            best_metrics = metrics
                            best_s = s
                            best_c = c
                            best_g = gam
                            best_settings = dataclasses.replace(current_settings, sharpness=s, contrast=c, gamma=gam)
                            best_burst = burst

                        pct = 0.45 + (step_idx / total_triplets) * 0.40
                        details_text = f"Testing G={gam}, S={s}, C={c} -> Score: {metrics.composite_score}/100"
                        if metrics.dart_separation > 0:
                            details_text += f" (Tip: {metrics.dart_separation:.0f} DN, Glow: {metrics.sisal_glow:.1f} DN)"
                        report("Stage 2 / 3", pct, details_text, frame=burst[0])

            if check_abort():
                return None

            # STAGE 3: Final Verification
            report("Stage 3 / 3", 0.88, "Final verification and burst capture...")
            self.device.set_control("gamma", best_g)
            self.device.set_control("sharpness", best_s)
            self.device.set_control("contrast", best_c)
            self.device.set_control("gain", best_gain)
            self.device.set_control("exposure_time_absolute", optimal_exposure)
            time.sleep(0.08)

            final_burst = get_burst(count=burst_count, flush=3)
            if final_burst:
                verif_metrics = self.evaluator.evaluate(
                    final_burst,
                    roi,
                    target_white_lum=target_white_lum,
                    target_mean_lum=target_mean_lum,
                    empty_frame=empty_baseline,
                )
                if verif_metrics.composite_score >= best_score * 0.85:
                    best_burst = final_burst
                    best_metrics = verif_metrics
                    best_score = verif_metrics.composite_score

            best_settings = dataclasses.replace(
                current_settings,
                gain=best_gain,
                gamma=best_g,
                sharpness=best_s,
                contrast=best_c,
                exposure_absolute=optimal_exposure,
            )

            report("Finalizing", 0.98, f"Calibration complete! Best Score: {best_score}/100")

            optimized_frame = best_burst[0].copy() if best_burst else initial_frame.copy()
            self.device.restore_snapshot()

            improvement = 0.0
            if initial_metrics.composite_score > 0:
                raw_imp = ((best_metrics.composite_score - initial_metrics.composite_score) / initial_metrics.composite_score) * 100.0
                improvement = round(max(-100.0, raw_imp), 1)
            elif best_metrics.composite_score > 0:
                improvement = 100.0

            report("Complete", 1.0, f"Optimization finished! Quality score: {best_metrics.composite_score}/100 ({improvement:+.1f}%)", frame=optimized_frame)

            return OptimizationResult(
                camera_node=self.device.dev_node,
                target_kelvin=0,
                initial_settings=initial_settings,
                initial_metrics=initial_metrics,
                initial_frame=initial_frame,
                optimized_settings=best_settings,
                optimized_metrics=best_metrics,
                optimized_frame=optimized_frame,
                roi=roi,
                improvement_percentage=improvement,
            )

        finally:
            if cap is not None and cap.isOpened():
                try:
                    cap.release()
                except Exception:
                    pass
