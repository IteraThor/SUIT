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

from core.board_detector import BoardDetector, BoardROI
from core.metrics import QualityEvaluator, QualityMetrics
from core.v4l2_device import CameraSettings, V4L2Device

logger = logging.getLogger(__name__)

ProgressCallback = Callable[..., None]


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

        cap = None
        if hasattr(self.device, "open_stream"):
            try:
                res_cap = self.device.open_stream(1280, 960)
                if isinstance(res_cap, cv2.VideoCapture) and res_cap.isOpened():
                    cap = res_cap
            except Exception:
                cap = None

        def get_frame(flush: int = 2) -> Optional[np.ndarray]:
            if cap is not None and hasattr(self.device, "read_stream_frame"):
                return self.device.read_stream_frame(cap, flush_frames=flush)
            return self.device.capture_frame(warmup_frames=max(3, flush))

        def get_burst(count: int, flush: int = 2) -> List[np.ndarray]:
            if cap is not None and hasattr(self.device, "read_stream_burst"):
                return self.device.read_stream_burst(cap, count=count, flush_frames=flush)
            return self.device.capture_burst(count=count, warmup_frames=max(3, flush))

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
            initial_metrics = self.evaluator.evaluate(initial_burst, roi)

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

            # Safe target luminance clamp
            target_white_lum = max(150.0, min(230.0, baseline_white_lum))
            target_mean_lum = baseline_mean_lum

            logger.info("Baseline AE Target: white=%.1f DN, mean=%.1f DN, clip=%.2f%%",
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

            # Optimal shutter speed for 30 FPS flicker-free capture (25ms - 33.3ms)
            if exp_max >= 300:
                nominal_shutter = 300
            elif exp_max >= 250:
                nominal_shutter = 250
            else:
                nominal_shutter = exp_max

            report("Stage 1 / 3", 0.18, f"Matching baseline brightness ({target_white_lum:.0f} DN) in Manual Mode...")

            # Set manual mode while preserving ALL factory ISP settings (brightness, saturation, gamma, wb)
            self.device.set_control("auto_exposure", 1)  # Manual Mode
            self.device.set_control("power_line_frequency", initial_settings.power_line_frequency)
            self.device.set_control("brightness", initial_settings.brightness)
            self.device.set_control("saturation", initial_settings.saturation)
            self.device.set_control("gamma", initial_settings.gamma)
            self.device.set_control("white_balance_automatic", initial_settings.wb_auto)
            if initial_settings.wb_auto == 0 and initial_settings.wb_temperature > 0:
                self.device.set_control("white_balance_temperature", initial_settings.wb_temperature)

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

            # If minimum gain at nominal shutter is already overexposed, reduce shutter time
            if best_g == min_g and best_g_lum > target_white_lum + 8.0:
                report("Stage 1 / 3 (Shutter Tuning)", 0.35, "Lighting is very bright — reducing shutter time...")
                shutter_candidates = sorted(list(set([
                    exp_min,
                    max(exp_min, 50),
                    max(exp_min, 100),
                    max(exp_min, 150),
                    max(exp_min, 200),
                    max(exp_min, nominal_shutter),
                ])), reverse=True)

                for exp_v in shutter_candidates:
                    if check_abort():
                        return None
                    self.device.set_control("exposure_time_absolute", exp_v)
                    time.sleep(0.04)
                    f = get_frame(flush=2)
                    if f is None:
                        continue
                    gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
                    u_pix = gray[roi.uniform_mask > 0]
                    lum = float(np.percentile(u_pix, 90)) if len(u_pix) > 50 else float(np.percentile(gray, 90))
                    if abs(lum - target_white_lum) < best_g_diff:
                        best_g_diff = abs(lum - target_white_lum)
                        optimal_exposure = exp_v
                    if lum <= target_white_lum:
                        optimal_exposure = exp_v
                        break

            self.device.set_control("exposure_time_absolute", optimal_exposure)
            self.device.set_control("gain", best_gain)
            time.sleep(0.06)
            logger.info("Locked Stage 1 physical anchor: exposure=%d, gain=%d (white=%.1f DN)",
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

            # STAGE 2: Bounded ISP Tuning around Factory Defaults
            report("Stage 2 / 3", 0.45, f"Scanning ISP sharpness & contrast around calibrated baseline ({burst_count}-frame burst)...")

            sharp_ctrl = initial_ctrls.get("sharpness")
            s_min = sharp_ctrl.min_val if sharp_ctrl else 0
            s_max = sharp_ctrl.max_val if sharp_ctrl else 100
            s_base = initial_settings.sharpness if initial_settings.sharpness is not None else (sharp_ctrl.default_val if sharp_ctrl else 3)

            contrast_ctrl = initial_ctrls.get("contrast")
            c_min = contrast_ctrl.min_val if contrast_ctrl else 0
            c_max = contrast_ctrl.max_val if contrast_ctrl else 100
            c_base = initial_settings.contrast if initial_settings.contrast is not None else (contrast_ctrl.default_val if contrast_ctrl else 32)

            # Bounded window around factory default
            s_span = max(6, int((s_max - s_min) * 0.15))
            c_span = max(4, int((c_max - c_min) * 0.12))

            coarse_s_levels = sorted(list(set([
                max(s_min, min(s_max, s_base + offset))
                for offset in [-s_span, -s_span // 2, 0, s_span // 2, s_span, int(s_span * 1.5)]
            ])))
            coarse_c_levels = sorted(list(set([
                max(c_min, min(c_max, c_base + offset))
                for offset in [-c_span, -c_span // 2, 0, c_span // 2, c_span]
            ])))

            total_pairs = len(coarse_s_levels) * len(coarse_c_levels)
            pair_idx = 0

            best_score = -1.0
            best_settings = dataclasses.replace(current_settings)
            best_metrics = initial_metrics
            best_burst: List[np.ndarray] = initial_burst
            best_s = s_base
            best_c = c_base

            for s in coarse_s_levels:
                if check_abort():
                    return None
                self.device.set_control("sharpness", s)

                for c in coarse_c_levels:
                    if check_abort():
                        return None

                    pair_idx += 1
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

                        # Veto 1: Shadows crushed below 8 DN
                        if p10 < 8.0:
                            pct = 0.45 + (pair_idx / total_pairs) * 0.35
                            report("Stage 2 / 3", pct, f"Skipped S={s}, C={c} (shadows crushed: {p10:.0f} DN)", frame=burst[0])
                            continue

                        # Veto 2: Overall board luminance dimmed by more than 7% from baseline
                        if curr_mean < baseline_mean_lum * 0.93:
                            pct = 0.45 + (pair_idx / total_pairs) * 0.35
                            report("Stage 2 / 3", pct, f"Skipped S={s}, C={c} (image darkened: {curr_mean:.0f}/{baseline_mean_lum:.0f} DN)", frame=burst[0])
                            continue

                    metrics = self.evaluator.evaluate(burst, roi)

                    if metrics.composite_score > best_score:
                        best_score = metrics.composite_score
                        best_metrics = metrics
                        best_s = s
                        best_c = c
                        best_settings = dataclasses.replace(current_settings, sharpness=s, contrast=c)
                        best_burst = burst

                    pct = 0.45 + (pair_idx / total_pairs) * 0.35
                    report("Stage 2 / 3", pct, f"Testing S={s}, C={c} -> Score: {metrics.composite_score}/100", frame=burst[0])

            if check_abort():
                return None

            # STAGE 3: Final Verification
            report("Stage 3 / 3", 0.88, "Final verification and burst capture...")
            self.device.set_control("sharpness", best_s)
            self.device.set_control("contrast", best_c)
            self.device.set_control("gain", best_gain)
            self.device.set_control("exposure_time_absolute", optimal_exposure)
            time.sleep(0.08)

            final_burst = get_burst(count=burst_count, flush=3)
            if final_burst:
                verif_metrics = self.evaluator.evaluate(final_burst, roi)
                if verif_metrics.composite_score >= best_score * 0.85:
                    best_burst = final_burst
                    best_metrics = verif_metrics
                    best_score = verif_metrics.composite_score

            best_settings = dataclasses.replace(
                current_settings,
                gain=best_gain,
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

            report("Complete", 1.0, f"Optimization finished! Quality score: {best_metrics.composite_score}/100 (+{improvement}%)", frame=optimized_frame)

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
