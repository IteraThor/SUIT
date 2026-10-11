"""Vision Critic: Empirical image analysis based on decompiled Autodarts pipeline."""

from __future__ import annotations
from dataclasses import dataclass
from typing import List, Tuple, Optional
import cv2
import numpy as np

from core.board_detector import BoardROI


@dataclass
class CriticMetrics:
    hough_line_count: int
    longest_shaft_len: float
    specular_clip_ratio: float
    halo_overshoot: float
    sisal_difference_floor: float
    needle_contrast_peak: float


class VisionCritic:
    """Evaluates image bursts using Hough line extraction and specular analysis."""

    def extract_hough_shaft_lines(
        self,
        frame: np.ndarray,
        roi: BoardROI,
        min_line_len: int = 40,
        max_line_gap: int = 15,
    ) -> List[Tuple[int, int, int, int]]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        if roi and roi.board_mask is not None and roi.board_mask.shape == gray.shape:
            masked = cv2.bitwise_and(gray, gray, mask=roi.board_mask)
        else:
            masked = gray

        edges = cv2.Canny(masked, 50, 150, apertureSize=3)
        raw_lines = cv2.HoughLinesP(
            edges,
            rho=1,
            theta=np.pi / 180,
            threshold=40,
            minLineLength=min_line_len,
            maxLineGap=max_line_gap,
        )
        if raw_lines is None:
            return []
        return [tuple(line[0]) for line in raw_lines]

    def calculate_specular_blooming(self, frame: np.ndarray, roi: BoardROI) -> float:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        if roi and roi.board_mask is not None and roi.board_mask.shape == gray.shape:
            board_px = gray[roi.board_mask > 0]
        else:
            board_px = gray.ravel()

        if len(board_px) == 0:
            return 0.0
        return float(np.sum(board_px >= 253)) / float(len(board_px))

    def calculate_quantile_tone_match_diff(
        self,
        cand_frame: np.ndarray,
        base_frame: np.ndarray,
        roi: BoardROI,
        apply_matching: bool = True,
    ) -> Tuple[np.ndarray, float]:
        gray_c = cv2.cvtColor(cand_frame, cv2.COLOR_BGR2GRAY) if len(cand_frame.shape) == 3 else cand_frame
        gray_b = cv2.cvtColor(base_frame, cv2.COLOR_BGR2GRAY) if len(base_frame.shape) == 3 else base_frame

        if roi and roi.board_mask is not None and roi.board_mask.shape == gray_c.shape:
            mask = roi.board_mask > 0
        else:
            mask = np.ones_like(gray_c, dtype=bool)

        if apply_matching:
            b_vals = gray_b[mask]
            c_vals = gray_c[mask]
            if len(b_vals) > 100 and len(c_vals) > 100:
                b_quantiles = np.percentile(b_vals, np.linspace(0, 100, 256))
                c_quantiles = np.percentile(c_vals, np.linspace(0, 100, 256))
                lut = np.interp(np.arange(256), b_quantiles, c_quantiles).astype(np.uint8)
                matched_b = cv2.LUT(gray_b, lut)
            else:
                matched_b = gray_b
        else:
            matched_b = gray_b

        diff = cv2.absdiff(gray_c, matched_b)
        board_diff = diff[mask]
        mean_diff = float(np.mean(board_diff)) if len(board_diff) > 0 else 0.0
        return diff, round(mean_diff, 2)

    def evaluate_frame_quality(
        self,
        cand_frame: np.ndarray,
        base_frame: Optional[np.ndarray],
        roi: BoardROI,
    ) -> CriticMetrics:
        lines = self.extract_hough_shaft_lines(cand_frame, roi)
        longest = 0.0
        for x1, y1, x2, y2 in lines:
            length = float(np.hypot(x2 - x1, y2 - y1))
            if length > longest:
                longest = length

        bloom = self.calculate_specular_blooming(cand_frame, roi)

        if base_frame is not None:
            diff, floor = self.calculate_quantile_tone_match_diff(cand_frame, base_frame, roi, apply_matching=True)
            peak = float(np.percentile(diff[roi.board_mask > 0], 98)) if roi.board_mask is not None else float(np.percentile(diff, 98))
        else:
            floor = 0.0
            peak = 0.0

        return CriticMetrics(
            hough_line_count=len(lines),
            longest_shaft_len=round(longest, 1),
            specular_clip_ratio=round(bloom, 4),
            halo_overshoot=0.0,
            sisal_difference_floor=floor,
            needle_contrast_peak=round(peak, 1),
        )
