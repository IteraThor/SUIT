"""Image quality metrics for dartboard camera calibration.

Calculates edge steepness along segment wires, temporal sensor noise,
contrast-to-noise ratio (CNR), and dynamic range utilization.
"""

from __future__ import annotations
import dataclasses
import math
from typing import List, Optional, Tuple
import cv2
import numpy as np
from core.board_detector import BoardROI


@dataclasses.dataclass
class QualityMetrics:
    edge_steepness: float  # Mean gradient magnitude across wire edges
    temporal_noise: float  # Temporal stddev across static frame burst (lower is better)
    contrast_to_noise_ratio: float  # CNR (higher is better)
    mean_luminance: float  # Mean luminance in board ROI (0-255)
    highlight_clipping_ratio: float  # Fraction of clipped pixels (>253)
    shadow_clipping_ratio: float  # Fraction of crushed black pixels (<=8)
    composite_score: float  # Overall normalized score (0 - 100)
    wire_fwhm: float = 0.0  # Full-Width at Half-Maximum of wire gradient profile in px (lower is thinner/sharper)
    halo_overshoot: float = 0.0  # Digital unsharp ringing overshoot fraction (lower is cleaner)
    dart_separation: float = 0.0  # Dart tip signal delta in absdiff (higher is better)
    sisal_glow: float = 0.0  # Sisal background noise floor in absdiff (lower is better)


class QualityEvaluator:
    """Computes objective optical metrics tailored to Autodarts segmentation."""

    @staticmethod
    def calculate_focus_score(
        patch: np.ndarray,
        target_roi: Optional[Tuple[int, int, int, int]] = None,
    ) -> float:
        """Compute normalized sharpness score (0–100) tuned for 2170 CMOS sensors on dartboard wires."""
        if patch is None or patch.size == 0:
            return 0.0

        h, w = patch.shape[:2]
        if target_roi:
            tx, ty, tw, th = target_roi
            crop = patch[max(0, ty):min(h, ty + th), max(0, tx):min(w, tx + tw)]
            if crop.size > 0:
                patch = crop

        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY) if len(patch.shape) == 3 else patch
        lap = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        ten = float(np.mean(gx**2 + gy**2))

        log_lap = math.log10(lap + 1e-6)
        log_ten = math.log10(ten + 1e-6)
        norm_lap = np.clip((log_lap - 1.2) / (3.35 - 1.2), 0.0, 1.0)
        norm_ten = np.clip((log_ten - 2.2) / (4.70 - 2.2), 0.0, 1.0)

        blended = 0.5 * norm_lap + 0.5 * norm_ten
        return float(np.clip(blended * 100.0, 0.0, 100.0))

    @staticmethod
    def calculate_edge_steepness(frame: np.ndarray, wire_mask: np.ndarray, roi: Optional[BoardROI] = None) -> float:
        """Calculate weighted gradient magnitude along dartboard segment wires, prioritizing Treble & Bullseye."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        grad_x = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        grad_y = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        magnitude = np.sqrt(grad_x ** 2 + grad_y ** 2)

        if roi and roi.center and roi.radius > 20:
            h, w = gray.shape[:2]
            cx, cy = roi.center
            r = roi.radius
            y_indices, x_indices = np.indices((h, w))
            dist_from_center = np.sqrt((x_indices - cx) ** 2 + (y_indices - cy) ** 2)

            # Sweet spot mask: Bullseye (0-18% radius) and Treble region (45-72% radius)
            sweet_mask = (wire_mask > 0) & (((dist_from_center <= r * 0.18)) | ((dist_from_center >= r * 0.45) & (dist_from_center <= r * 0.72)))
            outer_mask = (wire_mask > 0) & (~sweet_mask)

            sweet_vals = magnitude[sweet_mask]
            outer_vals = magnitude[outer_mask]

            if len(sweet_vals) > 0 and len(outer_vals) > 0:
                return float(0.65 * np.mean(sweet_vals) + 0.35 * np.mean(outer_vals))
            elif len(sweet_vals) > 0:
                return float(np.mean(sweet_vals))

        wire_pixels = magnitude[wire_mask > 0]
        if len(wire_pixels) == 0:
            return float(np.mean(magnitude))
        return float(np.mean(wire_pixels))

    @staticmethod
    def calculate_temporal_noise(frames: List[np.ndarray], uniform_mask: np.ndarray) -> float:
        """Calculate true temporal sensor noise across N consecutive frames.

        Isolates random thermal/shot noise from static MJPEG 8x8 DCT compression blocks.
        """
        if len(frames) < 2:
            return 1.0  # Baseline fallback

        # Convert all frames to grayscale float32
        gray_stack = []
        for f in frames:
            g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) if len(f.shape) == 3 else f
            gray_stack.append(g.astype(np.float32))

        stack = np.stack(gray_stack, axis=0)  # Shape: (N, H, W)
        temporal_std = np.std(stack, axis=0)  # Stddev per pixel over time

        uniform_pixels = temporal_std[uniform_mask > 0]
        if len(uniform_pixels) == 0:
            return float(np.mean(temporal_std))
        return float(np.mean(uniform_pixels))

    @staticmethod
    def calculate_cnr(frame: np.ndarray, roi: BoardROI, temporal_noise: float) -> float:
        """Calculate Contrast-to-Noise Ratio (CNR) between light and dark segments."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        board_pixels = gray[roi.uniform_mask > 0]
        if len(board_pixels) < 100:
            return 10.0

        # High percentile (light segments) vs Low percentile (dark segments)
        p_high = np.percentile(board_pixels, 85)
        p_low = np.percentile(board_pixels, 15)
        contrast = max(0.0, float(p_high - p_low))

        noise = max(0.1, temporal_noise)
        return float(contrast / noise)

    @staticmethod
    def calculate_halo_overshoot(frame: np.ndarray, wire_mask: np.ndarray, uniform_mask: np.ndarray) -> float:
        """Calculate digital unsharp-mask ringing overshoot along wire borders."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        dilated_wire = cv2.dilate(wire_mask, kernel, iterations=2)
        halo_zone = cv2.bitwise_and(dilated_wire, cv2.bitwise_not(wire_mask))
        halo_zone = cv2.bitwise_and(halo_zone, uniform_mask)  # Only inspect in uniform sisal
        
        halo_pixels = gray[halo_zone > 0]
        sisal_pixels = gray[uniform_mask > 0]
        if len(halo_pixels) < 50 or len(sisal_pixels) < 50:
            return 0.0
            
        p95_halo = float(np.percentile(halo_pixels, 95))
        p90_sisal = float(np.percentile(sisal_pixels, 90))
        if p90_sisal <= 0:
            return 0.0
            
        overshoot = max(0.0, (p95_halo - p90_sisal) / p90_sisal)
        return float(overshoot)

    @staticmethod
    def calculate_wire_fwhm(frame: np.ndarray, roi: BoardROI) -> float:
        """Calculate mean Full-Width at Half-Maximum (FWHM) of wire edge profiles in pixels.

        Extracts 1D normal profiles across the Bullseye and Treble scoring rings to measure
        true physical edge thinness. Lower FWHM indicates a thinner, sharper wire edge.
        """
        if frame is None or frame.size == 0 or roi is None:
            return 3.5  # Baseline default

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        h, w = gray.shape[:2]

        cx, cy = roi.center if roi.center else (w // 2, h // 2)
        radius = roi.radius if (roi.radius and roi.radius > 30) else int(min(w, h) * 0.35)

        # Concentric radii representing critical wire boundaries:
        # Bullseye circle (~9% of radius), Treble inner/outer (~55-62%), Double (~96%)
        test_radii = [
            int(radius * 0.09),
            int(radius * 0.55),
            int(radius * 0.62),
            int(radius * 0.96),
        ]

        fwhm_measurements: List[float] = []
        half_win = 8  # profile length = 17 pixels

        for r in test_radii:
            if r <= 5:
                continue
            # Sample 24 angles around the ring
            for deg in range(0, 360, 15):
                theta = math.radians(deg)
                cos_t = math.cos(theta)
                sin_t = math.sin(theta)

                # Center of profile along the wire ring
                px = cx + r * cos_t
                py = cy + r * sin_t

                if px - half_win < 0 or px + half_win >= w or py - half_win < 0 or py + half_win >= h:
                    continue

                profile = []
                for s in range(-half_win, half_win + 1):
                    sx = int(round(px + s * cos_t))
                    sy = int(round(py + s * sin_t))
                    if 0 <= sx < w and 0 <= sy < h:
                        profile.append(float(gray[sy, sx]))
                    else:
                        break

                if len(profile) != (2 * half_win + 1):
                    continue

                arr = np.array(profile, dtype=np.float32)
                grad = np.abs(np.diff(arr))
                max_g = float(np.max(grad))

                if max_g >= 12.0:
                    half_max = max_g * 0.5
                    peak_idx = int(np.argmax(grad))

                    left_idx = peak_idx
                    while left_idx > 0 and grad[left_idx] >= half_max:
                        left_idx -= 1

                    right_idx = peak_idx
                    while right_idx < len(grad) - 1 and grad[right_idx] >= half_max:
                        right_idx += 1

                    left_pos = float(left_idx)
                    if left_idx < peak_idx and (grad[left_idx + 1] - grad[left_idx]) > 1e-4:
                        left_pos = left_idx + (half_max - grad[left_idx]) / (grad[left_idx + 1] - grad[left_idx])

                    right_pos = float(right_idx)
                    if right_idx > peak_idx and (grad[right_idx - 1] - grad[right_idx]) > 1e-4:
                        right_pos = right_idx - (half_max - grad[right_idx]) / (grad[right_idx - 1] - grad[right_idx])

                    fwhm_val = max(1.0, right_pos - left_pos)
                    if fwhm_val <= 10.0:
                        fwhm_measurements.append(fwhm_val)

        if not fwhm_measurements:
            return 3.5

        return float(np.percentile(fwhm_measurements, 35))

    @staticmethod
    def calculate_dart_difference_metrics(
        dart_frame: np.ndarray,
        empty_frame: np.ndarray,
        roi: Optional[BoardROI] = None,
    ) -> Tuple[float, float, float]:
        """Calculates dart difference metrics between dart frame and empty frame.

        Returns:
            (dart_separation, sisal_glow, separation_ratio)
            - dart_separation: peak contrast delta at dart silhouettes in absdiff (0-255)
            - sisal_glow: background difference noise floor in empty sisal (0-255)
            - separation_ratio: dart_separation / max(1.0, sisal_glow)
        """
        if empty_frame is None or empty_frame.size == 0 or dart_frame is None or dart_frame.size == 0:
            return 0.0, 0.0, 0.0

        if empty_frame.shape[:2] != dart_frame.shape[:2]:
            empty_frame = cv2.resize(
                empty_frame,
                (dart_frame.shape[1], dart_frame.shape[0]),
                interpolation=cv2.INTER_LINEAR,
            )

        gray_dart = cv2.cvtColor(dart_frame, cv2.COLOR_BGR2GRAY) if len(dart_frame.shape) == 3 else dart_frame
        gray_empty = cv2.cvtColor(empty_frame, cv2.COLOR_BGR2GRAY) if len(empty_frame.shape) == 3 else empty_frame

        if roi and roi.board_mask is not None and roi.board_mask.shape == gray_dart.shape:
            board_mask = roi.board_mask > 0
        else:
            board_mask = np.ones_like(gray_dart, dtype=bool)

        # Non-linear tone-curve matching between empty baseline and candidate frame
        # (Uses 256-element quantile transfer LUT to eliminate cross-gamma curve mismatch)
        dart_board = gray_dart[board_mask]
        empty_board = gray_empty[board_mask]
        if len(dart_board) > 100 and len(empty_board) > 100:
            b_quantiles = np.percentile(empty_board, np.linspace(0, 100, 256))
            c_quantiles = np.percentile(dart_board, np.linspace(0, 100, 256))
            lut = np.interp(np.arange(256), b_quantiles, c_quantiles).astype(np.uint8)
            scaled_empty = cv2.LUT(gray_empty, lut)
        else:
            scaled_empty = gray_empty

        gray_diff = cv2.absdiff(gray_dart, scaled_empty)
        if roi and roi.board_mask is not None and roi.board_mask.shape == gray_diff.shape:
            masked_diff = cv2.bitwise_and(gray_diff, gray_diff, mask=roi.board_mask)
        else:
            masked_diff = gray_diff

        _, thresh = cv2.threshold(masked_diff, 25, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        dart_contours = [c for c in contours if cv2.contourArea(c) > 60]
        dart_contours = sorted(dart_contours, key=cv2.contourArea, reverse=True)[:3]

        if dart_contours:
            dart_mask = np.zeros_like(gray_diff)
            cv2.drawContours(dart_mask, dart_contours, -1, 255, -1)
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
            dart_dilated = cv2.dilate(dart_mask, kernel, iterations=1)

            dart_pixels = masked_diff[dart_mask > 0]
            sisal_mask = board_mask & (dart_dilated == 0)
            sisal_pixels = masked_diff[sisal_mask]

            tip_signal = float(np.percentile(dart_pixels, 95)) if len(dart_pixels) > 0 else 0.0
            glow_noise = float(np.mean(sisal_pixels)) if len(sisal_pixels) > 0 else 0.0
        else:
            board_pixels = masked_diff[board_mask]
            tip_signal = float(np.percentile(board_pixels, 98)) if len(board_pixels) > 0 else 0.0
            glow_noise = float(np.mean(board_pixels)) if len(board_pixels) > 0 else 0.0

        ratio = tip_signal / max(1.0, glow_noise)
        return (round(tip_signal, 1), round(glow_noise, 2), round(ratio, 2))

    def evaluate(
        self,
        frames: List[np.ndarray],
        roi: BoardROI,
        target_white_lum: Optional[float] = None,
        target_mean_lum: Optional[float] = None,
        empty_frame: Optional[np.ndarray] = None,
    ) -> QualityMetrics:
        """Perform comprehensive evaluation of single or multi-frame capture."""
        if not frames:
            return QualityMetrics(0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 3.5, 0.0, 0.0, 0.0)

        if len(frames) > 1:
            # Multi-frame temporal stacking: compute noise-free pixel average
            main_frame = np.mean(np.stack(frames, axis=0), axis=0).astype(np.uint8)
        else:
            main_frame = frames[0]

        gray = cv2.cvtColor(main_frame, cv2.COLOR_BGR2GRAY) if len(main_frame.shape) == 3 else main_frame

        # 1. Edge Steepness & Wire Thinness (FWHM) with Treble/Bull Sweet Spot Weighting
        steepness = self.calculate_edge_steepness(main_frame, roi.wire_mask, roi=roi)
        wire_fwhm = self.calculate_wire_fwhm(main_frame, roi)

        # 2. Temporal Noise
        t_noise = self.calculate_temporal_noise(frames, roi.uniform_mask)

        # 3. CNR & Halo Ringing
        cnr = self.calculate_cnr(main_frame, roi, t_noise)
        halo_overshoot = self.calculate_halo_overshoot(main_frame, roi.wire_mask, roi.uniform_mask)

        # 4. Luminance, Highlights, Dynamic Range, and Shadow Clipping
        board_pixels = gray[roi.board_mask > 0]
        if len(board_pixels) == 0:
            board_pixels = gray.flatten()

        uniform_pixels_lum = gray[roi.uniform_mask > 0]
        if len(uniform_pixels_lum) > 50:
            white_bed_lum = float(np.percentile(uniform_pixels_lum, 90))
        else:
            white_bed_lum = float(np.percentile(board_pixels, 90))

        mean_lum = float(np.mean(board_pixels))
        hl_clipped_pixels = np.sum(board_pixels >= 253)
        hl_clipping_ratio = float(hl_clipped_pixels / len(board_pixels))

        sh_clipped_pixels = np.sum(board_pixels <= 8)
        sh_clipping_ratio = float(sh_clipped_pixels / len(board_pixels))

        p_black_floor = float(np.percentile(board_pixels, 10))
        p_white_peak = float(np.percentile(board_pixels, 90))
        dynamic_range = max(0.0, p_white_peak - p_black_floor)

        # 5. Penalties and Composite Score Calculation (0 - 100)
        if target_white_lum is not None:
            lum_penalty = max(0.0, 1.0 - abs(white_bed_lum - target_white_lum) / 35.0)
        else:
            lum_penalty = max(0.0, 1.0 - abs(white_bed_lum - 205.0) / 60.0)

        # Highlight clipping penalty (specular wire burnout)
        hl_penalty = max(0.0, 1.0 - (hl_clipping_ratio * 18.0))

        # Shadow clipping penalty (crushed dart shadow triangulation)
        sh_penalty = max(0.0, 1.0 - (sh_clipping_ratio * 14.0))

        # Halo ringing penalty (strictly penalizes unsharp-mask edge doubling)
        halo_penalty = max(0.25, 1.0 - (halo_overshoot / 0.18) ** 2)

        # Black Floor Factor: healthy blacks down to 4.0 DN receive full score
        if 4.0 <= p_black_floor <= 45.0:
            black_floor_factor = 1.0
        elif p_black_floor < 4.0:
            black_floor_factor = max(0.40, (p_black_floor / 4.0) ** 2)
        else:
            black_floor_factor = max(0.40, 1.0 - (p_black_floor - 45.0) / 40.0)

        # White bed peak target: optimal 185-245 DN (accommodates high-gamma sisal boost)
        if 185.0 <= p_white_peak <= 245.0:
            white_peak_factor = 1.0
        elif p_white_peak > 245.0:
            white_peak_factor = max(0.20, 1.0 - (p_white_peak - 245.0) / 8.0)
        else:
            white_peak_factor = max(0.30, (p_white_peak / 185.0) ** 2)

        penalties = [
            max(0.30, hl_penalty),
            max(0.30, sh_penalty),
            halo_penalty,
            black_floor_factor,
            white_peak_factor,
        ]
        clip_penalty = float(np.prod(penalties) ** (1.0 / len(penalties)))

        # Normalized Components (scaled to real-world CMOS dartboard optics):
        steep_norm = float(np.clip((steepness - 180.0) / (420.0 - 180.0), 0.0, 1.0))
        fwhm_norm = float(np.clip((4.2 - wire_fwhm) / (4.2 - 1.6), 0.0, 1.0))
        dr_norm = float(np.clip((dynamic_range - 130.0) / (235.0 - 130.0), 0.0, 1.0))
        noise_norm = max(0.0, 1.0 - min(1.0, (t_noise - 0.5) / 3.0))

        # 6. Dart Silhouette Separation (if empty baseline provided)
        dart_sep = 0.0
        sisal_glow = 0.0
        if empty_frame is not None:
            dart_sep, sisal_glow, sep_ratio = self.calculate_dart_difference_metrics(main_frame, empty_frame, roi)
            dart_sep_norm = float(np.clip((dart_sep - 80.0) / (220.0 - 80.0), 0.0, 1.0))
            glow_norm = max(0.0, 1.0 - min(1.0, max(0.0, sisal_glow - 5.0) / 15.0))
            ratio_norm = float(np.clip((sep_ratio - 8.0) / (32.0 - 8.0), 0.0, 1.0))
            # 30% tip separation, 15% tip-to-sisal contrast ratio, 20% sisal glow suppression, 18% wire steepness, 17% wire thinness
            raw_score = (dart_sep_norm * 30.0 + ratio_norm * 15.0 + glow_norm * 20.0 + steep_norm * 18.0 + fwhm_norm * 17.0) * clip_penalty
        else:
            # Composite score: 30% steepness, 25% wire thinness (FWHM), 25% dynamic range, 20% noise
            raw_score = (steep_norm * 30.0 + fwhm_norm * 25.0 + dr_norm * 25.0 + noise_norm * 20.0) * clip_penalty

        composite_score = round(max(0.0, min(100.0, raw_score)), 1)

        return QualityMetrics(
            edge_steepness=round(steepness, 2),
            temporal_noise=round(t_noise, 3),
            contrast_to_noise_ratio=round(cnr, 2),
            mean_luminance=round(mean_lum, 1),
            highlight_clipping_ratio=round(hl_clipping_ratio, 4),
            shadow_clipping_ratio=round(sh_clipping_ratio, 4),
            composite_score=composite_score,
            wire_fwhm=round(wire_fwhm, 2),
            halo_overshoot=round(halo_overshoot, 3),
            dart_separation=round(dart_sep, 1),
            sisal_glow=round(sisal_glow, 2),
        )
