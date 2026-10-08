"""Image quality metrics for dartboard camera calibration.

Calculates edge steepness along segment wires, temporal sensor noise,
contrast-to-noise ratio (CNR), and dynamic range utilization.
"""

from __future__ import annotations
import dataclasses
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


class QualityEvaluator:
    """Computes objective optical metrics tailored to Autodarts segmentation."""

    @staticmethod
    def calculate_edge_steepness(frame: np.ndarray, wire_mask: np.ndarray) -> float:
        """Calculate mean gradient magnitude along dartboard segment wires."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        # Compute Scharr or Sobel gradients
        grad_x = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        grad_y = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        magnitude = np.sqrt(grad_x ** 2 + grad_y ** 2)

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

    def evaluate(self, frames: List[np.ndarray], roi: BoardROI) -> QualityMetrics:
        """Perform comprehensive evaluation of single or multi-frame capture."""
        if not frames:
            return QualityMetrics(0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0)

        if len(frames) > 1:
            # Multi-frame temporal stacking: compute noise-free pixel average
            main_frame = np.mean(np.stack(frames, axis=0), axis=0).astype(np.uint8)
        else:
            main_frame = frames[0]

        gray = cv2.cvtColor(main_frame, cv2.COLOR_BGR2GRAY) if len(main_frame.shape) == 3 else main_frame

        # 1. Edge Steepness
        steepness = self.calculate_edge_steepness(main_frame, roi.wire_mask)

        # 2. Temporal Noise
        t_noise = self.calculate_temporal_noise(frames, roi.uniform_mask)

        # 3. CNR
        cnr = self.calculate_cnr(main_frame, roi, t_noise)

        # 4. Luminance, Highlights, Dynamic Range, and Shadow Clipping
        board_pixels = gray[roi.board_mask > 0]
        if len(board_pixels) == 0:
            board_pixels = gray.flatten()

        # Use uniform mask (sisal segments) for luminance scoring — the white-bed
        # brightness is what matters for dart detection, not the mean of the whole
        # board which is dragged down by dark segments and misleads the optimizer.
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
        # Optimal white-bed luminance between 170 and 220 DN (target 195)
        # Using white-bed percentile instead of whole-board mean prevents the
        # optimizer from chasing extreme brightness to compensate for dark blacks.
        lum_penalty = max(0.0, 1.0 - abs(white_bed_lum - 195.0) / 60.0)
        
        # Highlight clipping penalty (overexposed wires and specular reflections > 250 DN)
        # Strict penalty: even 1% clipping on board wires significantly degrades CV tip detection
        hl_penalty = max(0.0, 1.0 - (hl_clipping_ratio * 18.0))

        # Shadow clipping penalty (crushed blacks destroy dart shadow triangulation)
        sh_penalty = max(0.0, 1.0 - (sh_clipping_ratio * 12.0))

        # Two-sided Black Floor Factor:
        # - Target: 10 to 45 DN (relaxed: accommodates weaker sensors with higher noise floors)
        # - < 10 DN: Penalizes crushing / clipping
        # - > 45 DN: Penalizes washed-out / bleached / milky blacks
        if 10.0 <= p_black_floor <= 45.0:
            black_floor_factor = 1.0
        elif p_black_floor < 10.0:
            black_floor_factor = max(0.40, (p_black_floor / 10.0) ** 2)
        else:
            black_floor_factor = max(0.40, 1.0 - (p_black_floor - 45.0) / 40.0)

        # White bed peak target: optimal 170-235 DN (widened lower bound for weaker sensors)
        if 170.0 <= p_white_peak <= 235.0:
            white_peak_factor = 1.0
        elif p_white_peak > 235.0:
            white_peak_factor = max(0.40, 1.0 - (p_white_peak - 235.0) / 20.0)
        else:
            white_peak_factor = max(0.40, (p_white_peak / 170.0) ** 2)

        # Soft combination: geometric mean with individual floors prevents
        # a single moderate miss from collapsing the entire composite score.
        # Floor of 0.4 per factor ensures even a camera with one bad metric
        # still gets fairly evaluated on sharpness / noise / DR.
        penalties = [
            max(0.40, hl_penalty),
            max(0.40, sh_penalty),
            black_floor_factor,
            white_peak_factor,
        ]
        clip_penalty = float(np.prod(penalties) ** (1.0 / len(penalties)))

        # Normalized Components:
        # - Wire edge steepness (typical good values ~35-85)
        steep_norm = min(1.0, steepness / 75.0)
        # - Dynamic range separation between white and black sisal (target >= 130 DN)
        dr_norm = min(1.0, dynamic_range / 145.0)
        # - Temporal noise (typical good values ~0.4 - 2.0, lower is better)
        noise_norm = max(0.0, 1.0 - min(1.0, (t_noise - 0.3) / 2.2))

        raw_score = (steep_norm * 40.0 + dr_norm * 25.0 + noise_norm * 25.0 + lum_penalty * 10.0) * clip_penalty
        composite_score = round(max(0.0, min(100.0, raw_score)), 1)

        return QualityMetrics(
            edge_steepness=round(steepness, 2),
            temporal_noise=round(t_noise, 3),
            contrast_to_noise_ratio=round(cnr, 2),
            mean_luminance=round(mean_lum, 1),
            highlight_clipping_ratio=round(hl_clipping_ratio, 4),
            shadow_clipping_ratio=round(sh_clipping_ratio, 4),
            composite_score=composite_score,
        )
