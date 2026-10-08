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
                wb_auto=initial_ctrls.get("white_balance_automatic", type("", (), {"current_val": 0})).current_val,
                wb_temperature=initial_ctrls.get("white_balance_temperature", type("", (), {"current_val": 4600})).current_val,
                sharpness=initial_ctrls.get("sharpness", type("", (), {"current_val": 3})).current_val,
                contrast=initial_ctrls.get("contrast", type("", (), {"current_val": 32})).current_val,
                brightness=initial_ctrls.get("brightness", type("", (), {"current_val": 0})).current_val,
                gamma=initial_ctrls.get("gamma", type("", (), {"current_val": 300})).current_val,
                saturation=initial_ctrls.get("saturation", type("", (), {"current_val": 68})).current_val,
                power_line_frequency=initial_ctrls.get("power_line_frequency", type("", (), {"current_val": 1})).current_val,
            )

            # STAGE 1: Sensor Physics Anchor
            # Strategy: Measure auto-exposure baseline quality first.
            # If it's already good, preserve the camera's exposure and only tune ISP.
            # If it's broken, auto-detect the right exposure for this sensor.
            report("Stage 1 / 3", 0.16, "Analyzing sensor exposure baseline...")

            # Measure baseline auto-exposure quality from initial capture
            gray_baseline = cv2.cvtColor(initial_frame, cv2.COLOR_BGR2GRAY)
            baseline_board = gray_baseline[roi.board_mask > 0]
            baseline_uniform = gray_baseline[roi.uniform_mask > 0]
            if len(baseline_uniform) > 50:
                baseline_white = float(np.percentile(baseline_uniform, 90))
            else:
                baseline_white = float(np.percentile(gray_baseline, 90))
            baseline_clip = float(np.sum(baseline_board >= 253)) / max(1, len(baseline_board))

            # Decide exposure strategy based on auto-exposure quality
            auto_exposure_usable = 180.0 <= baseline_white <= 250.0 and baseline_clip < 0.08
            logger.info(
                "Baseline analysis: white=%.1f DN, clip=%.2f%%, auto_usable=%s",
                baseline_white, baseline_clip * 100, auto_exposure_usable,
            )

            if auto_exposure_usable:
                # AUTO-EXPOSURE IS WORKING — preserve it, only optimize ISP params
                # Lock to manual mode at the camera's CURRENT exposure to prevent drift
                report("Stage 1 / 3", 0.18, f"Auto-exposure is good (whites={baseline_white:.0f} DN) — preserving...")
                optimal_exposure = initial_settings.exposure_absolute
                start_gain = initial_settings.gain
                target_white_lum = min(235.0, baseline_white + 5.0)  # Small nudge, don't chase
            else:
                # AUTO-EXPOSURE IS BROKEN — auto-detect correct exposure
                # Try 50Hz-synchronized harmonics (20ms, 40ms, 60ms) until whites are usable
                report("Stage 1 / 3", 0.18, "Auto-exposure needs correction — finding optimal exposure...")
                optimal_exposure = 200  # Default: 20ms = 1/50s
                start_gain = 0

                # Set manual mode with minimum gain to measure pure sensor sensitivity
                self.device.set_control("auto_exposure", 1)
                self.device.set_control("gain", 0)
                self.device.set_control("power_line_frequency", 1)
                time.sleep(0.08)

                for try_exp in [200, 400, 600]:
                    if check_abort():
                        return None
                    self.device.set_control("exposure_time_absolute", try_exp)
                    time.sleep(0.10)
                    f = get_frame(flush=3)
                    if f is not None:
                        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
                        u_pix = gray[roi.uniform_mask > 0]
                        wp = float(np.percentile(u_pix, 90)) if len(u_pix) > 50 else float(np.percentile(gray, 90))
                        report("Stage 1 / 3", 0.20, f"Exposure {try_exp * 0.1:.0f}ms → whites={wp:.0f} DN", frame=f)
                        if wp >= 120.0:
                            optimal_exposure = try_exp
                            break
                    optimal_exposure = try_exp  # Use longest tried if none reached 120

                target_white_lum = 215.0  # Sensor needs help, aim high

            logger.info("Exposure strategy: exposure=%d (%.1fms), target_white=%.1f DN",
                        optimal_exposure, optimal_exposure * 0.1, target_white_lum)

            current_settings = CameraSettings(
                exposure_auto=1,  # Manual Mode (locked)
                exposure_absolute=optimal_exposure,
                gain=start_gain,
                wb_auto=1,  # Auto White Balance during initial convergence
                wb_temperature=4600,
                sharpness=initial_ctrls.get("sharpness", type("", (), {"default_val": 40})).default_val,
                contrast=initial_ctrls.get("contrast", type("", (), {"default_val": 34})).default_val,
                brightness=0,
                gamma=initial_settings.gamma if auto_exposure_usable else 290,
                saturation=64,
                power_line_frequency=1,  # 50Hz anti-flicker
            )
            self.device.apply_settings(current_settings)
            time.sleep(0.12)

            if check_abort():
                return None

            # Gain search: scope depends on exposure strategy
            gain_ctrl = initial_ctrls.get("gain")
            max_g = gain_ctrl.max_val if gain_ctrl else 128
            min_g = gain_ctrl.min_val if gain_ctrl else 0

            if auto_exposure_usable:
                # Minimal gain search: only ±12 around current gain
                report("Stage 1 / 3", 0.22, f"Fine-tuning gain around {start_gain}...")
                gain_candidates = sorted(list(set([
                    max(min_g, min(max_g, start_gain + offset))
                    for offset in [-12, -8, -4, 0, 4, 8, 12]
                ])))
            else:
                # Full gain search from minimum
                report("Stage 1 / 3", 0.22, f"Searching gain for target {target_white_lum:.0f} DN...")
                gain_candidates = sorted(list(set([
                    min_g,
                    int(min_g + (max_g - min_g) * 0.10),
                    int(min_g + (max_g - min_g) * 0.22),
                    int(min_g + (max_g - min_g) * 0.35),
                    int(min_g + (max_g - min_g) * 0.45),
                    int(min_g + (max_g - min_g) * 0.55),
                    int(min_g + (max_g - min_g) * 0.65),
                ])))

            best_gain = start_gain
            best_gain_diff = float("inf")

            for idx, g in enumerate(gain_candidates):
                if check_abort():
                    return None

                current_settings.gain = g
                self.device.set_control("gain", g)
                time.sleep(0.04)

                f = get_frame(flush=2)
                if f is None:
                    continue

                gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
                uniform_pixels = gray[roi.uniform_mask > 0]
                if len(uniform_pixels) > 50:
                    white_lum = float(np.percentile(uniform_pixels, 90))
                else:
                    white_lum = float(np.percentile(gray, 90))

                diff = abs(white_lum - target_white_lum)
                if diff < best_gain_diff:
                    best_gain_diff = diff
                    best_gain = g

                if white_lum >= target_white_lum:
                    break

                pct = 0.22 + (idx / len(gain_candidates)) * 0.08
                report("Stage 1 / 3 (Gain)", pct, f"Gain {g} → White: {white_lum:.1f}/{target_white_lum:.0f} DN", frame=f)

            if check_abort():
                return None

            # Fine polish around coarse winner
            if is_deep:
                fine_gain_candidates = sorted(list(set([
                    max(min_g, min(max_g, best_gain + offset)) for offset in range(-6, 7)
                ])))
            else:
                fine_gain_candidates = sorted(list(set([
                    max(min_g, min(max_g, best_gain + offset)) for offset in [-4, -2, 0, 2, 4]
                ])))

            for idx, g in enumerate(fine_gain_candidates):
                if check_abort():
                    return None

                if g in gain_candidates:
                    continue
                current_settings.gain = g
                self.device.set_control("gain", g)
                time.sleep(0.04)

                f = get_frame(flush=2)
                if f is None:
                    continue

                gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
                uniform_pixels = gray[roi.uniform_mask > 0]
                white_lum = float(np.percentile(uniform_pixels, 90)) if len(uniform_pixels) > 50 else float(np.percentile(gray, 90))

                diff = abs(white_lum - target_white_lum)
                if diff < best_gain_diff:
                    best_gain_diff = diff
                    best_gain = g

                pct = 0.30 + (idx / len(fine_gain_candidates)) * 0.08
                report("Stage 1 / 3 (Fine)", pct, f"Fine gain {g} → White: {white_lum:.1f}/{target_white_lum:.0f} DN", frame=f)

            if check_abort():
                return None

            current_settings.gain = best_gain
            self.device.set_control("gain", best_gain)
            time.sleep(0.06)

            # Stage 1.5: White Balance Handling
            report("Stage 1.5 / 3", 0.40, "Ensuring Auto White Balance for neutral color balance...")
            current_settings.wb_auto = 1
            self.device.set_control("white_balance_automatic", 1)
            time.sleep(0.06)

            # STAGE 2: ISP 2D Grid Scan (Sweet-Spot Sharpness x Contrast)
            report("Stage 2 / 3 (Coarse)", 0.45, f"Running 2D Grid Scan ({burst_count}-frame burst averaging)...")

            sharp_ctrl = initial_ctrls.get("sharpness")
            s_min = sharp_ctrl.min_val if sharp_ctrl else 0
            s_max = sharp_ctrl.max_val if sharp_ctrl else 100
            contrast_ctrl = initial_ctrls.get("contrast")
            c_min = contrast_ctrl.min_val if contrast_ctrl else 0
            c_max = contrast_ctrl.max_val if contrast_ctrl else 100

            def _grid_levels(lo: int, hi: int, n_coarse: int, n_sparse: int, dense: bool) -> List[int]:
                """Derive grid sample points from actual control bounds."""
                span = hi - lo
                if span <= 0:
                    return [lo]
                if span <= 10:
                    return list(range(lo, hi + 1))
                n = n_coarse if dense else n_sparse
                # Sample the middle 60% of the range (avoid extreme ends)
                start_pct, end_pct = 0.20, 0.80
                return sorted(list(set(
                    int(lo + span * (start_pct + (end_pct - start_pct) * i / max(1, n - 1)))
                    for i in range(n)
                )))

            coarse_s_levels = _grid_levels(s_min, s_max, 6, 4, is_deep or is_ultra)
            coarse_c_levels = _grid_levels(c_min, c_max, 6, 5, is_deep or is_ultra)

            total_coarse = len(coarse_s_levels) * len(coarse_c_levels)
            coarse_idx = 0

            best_score = -1.0
            best_settings = dataclasses.replace(current_settings)
            best_metrics = initial_metrics
            best_burst: List[np.ndarray] = initial_burst
            best_s = coarse_s_levels[len(coarse_s_levels) // 2]
            best_c = coarse_c_levels[len(coarse_c_levels) // 2]
            tested_pairs = set()

            for s in coarse_s_levels:
                if check_abort():
                    return None

                self.device.set_control("sharpness", s)
                for c in coarse_c_levels:
                    if check_abort():
                        return None

                    tested_pairs.add((s, c))
                    self.device.set_control("contrast", c)
                    time.sleep(0.04)

                    burst = get_burst(count=burst_count, flush=2)
                    if not burst:
                        continue

                    metrics = self.evaluator.evaluate(burst, roi)

                    # Black-floor veto: reject any (S,C) combo that crushes blacks below 8 DN.
                    # High contrast pushes blacks down, making the image look very dark even
                    # if white segments are at target. This prevents the optimizer from
                    # selecting settings that look dark to the human eye.
                    gray_check = cv2.cvtColor(burst[0], cv2.COLOR_BGR2GRAY)
                    board_px = gray_check[roi.board_mask > 0]
                    if len(board_px) > 100:
                        p10 = float(np.percentile(board_px, 10))
                        if p10 < 8.0:
                            coarse_idx += 1
                            pct = 0.45 + (coarse_idx / total_coarse) * 0.22
                            report("Stage 2 / 3 (Coarse)", pct, f"Skipped S={s}, C={c} (blacks crushed: {p10:.0f} DN)", frame=burst[0])
                            continue

                    if metrics.composite_score > best_score:
                        best_score = metrics.composite_score
                        best_metrics = metrics
                        best_s = s
                        best_c = c
                        best_settings = dataclasses.replace(current_settings, sharpness=s, contrast=c)
                        best_burst = burst

                    coarse_idx += 1
                    pct = 0.45 + (coarse_idx / total_coarse) * 0.22
                    report("Stage 2 / 3 (Coarse)", pct, f"Testing S={s}, C={c} -> Score: {metrics.composite_score}/100", frame=burst[0])

            if check_abort():
                return None

            # Stage 2 Pass 2: Fine Precision Neighborhood Polish around coarse winner
            report("Stage 2 / 3 (Fine Precision)", 0.68, f"Micro-tuning around peak Sharpness={best_s}, Contrast={best_c}...")
            
            fine_s_levels = sorted(list(set([
                max(s_min, min(s_max, best_s + offset)) for offset in ([-3, -1, 0, 1, 3] if is_deep else [-2, 0, 2])
            ])))
            fine_c_levels = sorted(list(set([
                max(c_min, min(c_max, best_c + offset)) for offset in ([-3, -1, 0, 1, 3] if is_deep else [-2, 0, 2])
            ])))

            fine_pairs = [(s, c) for s in fine_s_levels for c in fine_c_levels if (s, c) not in tested_pairs]
            total_fine = max(1, len(fine_pairs))

            for idx, (s, c) in enumerate(fine_pairs):
                if check_abort():
                    return None

                self.device.set_control("sharpness", s)
                self.device.set_control("contrast", c)
                time.sleep(0.04)

                burst = get_burst(count=burst_count, flush=2)
                if not burst:
                    continue

                metrics = self.evaluator.evaluate(burst, roi)

                # Same black-floor veto as coarse pass
                gray_check = cv2.cvtColor(burst[0], cv2.COLOR_BGR2GRAY)
                board_px = gray_check[roi.board_mask > 0]
                if len(board_px) > 100:
                    p10 = float(np.percentile(board_px, 10))
                    if p10 < 8.0:
                        pct = 0.68 + (idx / total_fine) * 0.18
                        report("Stage 2 / 3 (Fine Precision)", pct, f"Skipped S={s}, C={c} (blacks crushed)", frame=burst[0])
                        continue

                if metrics.composite_score > best_score:
                    best_score = metrics.composite_score
                    best_metrics = metrics
                    best_s = s
                    best_c = c
                    best_settings = dataclasses.replace(current_settings, sharpness=s, contrast=c)
                    best_burst = burst

                pct = 0.68 + (idx / total_fine) * 0.18
                report("Stage 2 / 3 (Fine Precision)", pct, f"Precision S={s}, C={c} -> Score: {metrics.composite_score}/100", frame=burst[0])

            if check_abort():
                return None

            # STAGE 3: 2-Pass Luminance Re-Anchor
            report("Stage 3 / 3 (Re-Anchor)", 0.88, "Precision re-anchoring white luminance under final contrast curve...")

            self.device.set_control("sharpness", best_s)
            self.device.set_control("contrast", best_c)
            time.sleep(0.06)

            current_f = get_frame(flush=2)
            if current_f is not None:
                gray = cv2.cvtColor(current_f, cv2.COLOR_BGR2GRAY)
                uniform_pixels = gray[roi.uniform_mask > 0]
                current_white = float(np.percentile(uniform_pixels, 90)) if len(uniform_pixels) > 50 else float(np.percentile(gray, 90))
            else:
                current_white = target_white_lum

            reanchor_gains = sorted(list(set([
                max(min_g, min(max_g, best_gain + offset))
                for offset in [-8, -5, -3, 0, 3, 5, 8]
            ])))
            reanchor_best_gain = best_gain
            reanchor_best_diff = abs(current_white - target_white_lum)

            for idx, g in enumerate(reanchor_gains):
                if check_abort():
                    return None
                if g == best_gain:
                    continue
                self.device.set_control("gain", g)
                time.sleep(0.04)
                f = get_frame(flush=2)
                if f is None:
                    continue
                gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
                uniform_pixels = gray[roi.uniform_mask > 0]
                wl = float(np.percentile(uniform_pixels, 90)) if len(uniform_pixels) > 50 else float(np.percentile(gray, 90))
                d = abs(wl - target_white_lum)
                if d < reanchor_best_diff:
                    reanchor_best_diff = d
                    reanchor_best_gain = g

                pct = 0.88 + (idx / len(reanchor_gains)) * 0.08
                report("Stage 3 / 3 (Re-Anchor)", pct, f"Tuning Gain {g} -> White: {wl:.1f}/255", frame=f)

            best_gain = reanchor_best_gain
            self.device.set_control("gain", best_gain)
            time.sleep(0.06)

            # Final multi-frame burst verification
            report("Stage 3 / 3 (Verification)", 0.96, "Capturing final verified multi-frame burst...")
            final_burst = get_burst(count=burst_count, flush=3)
            if final_burst:
                verif_metrics = self.evaluator.evaluate(final_burst, roi)
                # Keep verified burst if it meets or approaches peak score
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

            # Always restore physical camera to baseline snapshot so subsequent runs start clean
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
