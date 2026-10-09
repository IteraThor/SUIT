"""Automated Pre-Flight Hardware Diagnostics Engine.

Evaluates camera hardware setup before optimization:
- Optical Focus Quality (Tenengrad Sobel wire edge gradient & Laplacian high-frequency variance)
- Lighting Quality (Segment luminance, underexposure, highlight glare / clipping)
- Dartboard Framing & Geometry (Center alignment, wire mask coverage)
"""

from __future__ import annotations
import dataclasses
import logging
from typing import List, Optional, Tuple
import cv2
import numpy as np

from core.board_detector import BoardDetector, BoardROI
from core.metrics import QualityEvaluator

logger = logging.getLogger("cam_librator.diagnostics")


@dataclasses.dataclass
class DiagnosticResult:
    focus_score: float  # 0 to 100
    focus_status: str   # "sharp", "soft", "blurry"
    focus_details: str

    lighting_status: str  # "optimal", "too_dim", "overexposed", "uneven"
    white_bed_luminance: float  # 0 to 255 DN
    highlight_clipping_pct: float  # 0 to 100%
    lighting_details: str

    geometry_status: str  # "aligned", "offset", "obstructed"
    geometry_confidence: float  # 0 to 1.0
    geometry_details: str

    is_ready_for_calibration: bool
    recommendations: List[str]


class HardwareDiagnostics:
    """Performs pre-flight checks on camera frames to guide physical setup."""

    def __init__(self, detector: Optional[BoardDetector] = None):
        self.detector = detector or BoardDetector()
        self.evaluator = QualityEvaluator()

    @staticmethod
    def compute_live_focus_metric(
        image: np.ndarray,
        wire_mask: Optional[np.ndarray] = None,
        target_roi: Optional[Tuple[int, int, int, int]] = None,
    ) -> float:
        """High-speed focus metric calculation (0 - 100) targeted on the furthest double field."""
        if image is None or image.size == 0:
            return 0.0
        if not target_roi:
            h, w = image.shape[:2]
            target_roi = (int(w * 0.25), int(h * 0.15), int(w * 0.50), int(h * 0.35))
        return QualityEvaluator.calculate_focus_score(image, target_roi=target_roi)

    def analyze_frame(
        self,
        frame: np.ndarray,
        roi: Optional[BoardROI] = None,
        dev_node: Optional[str] = None,
        gain: Optional[int] = None,
        auto_exposure: Optional[int] = None,
    ) -> DiagnosticResult:
        """Perform comprehensive pre-flight hardware diagnosis."""
        if frame is None or frame.size == 0:
            return DiagnosticResult(
                focus_score=0.0,
                focus_status="blurry",
                focus_details="No camera frame received.",
                lighting_status="too_dim",
                white_bed_luminance=0.0,
                highlight_clipping_pct=0.0,
                lighting_details="Camera stream is empty.",
                geometry_status="obstructed",
                geometry_confidence=0.0,
                geometry_details="Dartboard not detected.",
                is_ready_for_calibration=False,
                recommendations=["Check camera USB connection."],
            )

        # Query hardware gain / auto exposure if dev_node provided
        gain_val: Optional[int] = gain
        ae_val: Optional[int] = auto_exposure
        if dev_node and (gain_val is None or ae_val is None):
            try:
                from core.v4l2_device import V4L2Device
                vdev = V4L2Device(dev_node)
                if gain_val is None:
                    gain_val = vdev.get_control("gain")
                if ae_val is None:
                    ae_val = vdev.get_control("auto_exposure")
            except Exception:
                pass

        if roi is None:
            roi = self.detector.detect_board(frame, dev_node=dev_node)

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        recommendations: List[str] = []

        # --- 1. Focus Assessment (Targeted on Furthest Double Field) ---
        focus_score = self.compute_live_focus_metric(frame, roi.wire_mask, roi.target_roi)
        if focus_score >= 70.0:
            focus_status = "sharp"
            focus_details = f"Focus on furthest double is sharp ({focus_score:.0f}%)."
        elif focus_score >= 35.0:
            focus_status = "soft"
            focus_details = f"Focus on furthest double slightly soft ({focus_score:.0f}%)."
            recommendations.append("Rotate lens focus ring until the furthest double is sharp.")
        else:
            focus_status = "blurry"
            focus_details = f"Focus on furthest double blurry ({focus_score:.0f}%)."
            recommendations.append("Manually adjust lens focus on the furthest double bed.")

        # --- 2. Lighting Assessment ---
        uniform_pixels = gray[roi.uniform_mask > 0] if np.sum(roi.uniform_mask) > 50 else gray[roi.board_mask > 0]
        if len(uniform_pixels) == 0:
            uniform_pixels = gray.flatten()

        white_lum = float(np.percentile(uniform_pixels, 90))
        clipped_high = np.sum(gray[roi.board_mask > 0] >= 253) if np.sum(roi.board_mask) > 0 else np.sum(gray >= 253)
        total_board_pixels = max(1, np.sum(roi.board_mask > 0) if np.sum(roi.board_mask) > 0 else gray.size)
        hl_clip_pct = float((clipped_high / total_board_pixels) * 100.0)

        # --- 2. Lighting Assessment ---
        # In Auto-Exposure mode (auto_exposure != 1), the camera DSP automatically brightens the dark room,
        # which naturally pushes the center white beds high (e.g. 230-245). This is completely normal and
        # will be corrected by the software calibration (manual exposure locking).
        is_auto_exp = (ae_val is None) or (ae_val != 1)

        if is_auto_exp:
            if hl_clip_pct > 25.0:
                lighting_status = "overexposed"
                lighting_details = f"Severe overexposure ({hl_clip_pct:.1f}% clipped reflections)."
                recommendations.append("Dim the light source slightly.")
            elif white_lum < 70.0 or (white_lum <= 200.0 and gain_val is not None and gain_val >= 60):
                lighting_status = "too_dim"
                if gain_val is not None and gain_val >= 60:
                    lighting_details = f"Light too dim (Camera boosts gain to {gain_val}/128, high sensor noise)."
                else:
                    lighting_details = f"Too dark (Luminance: {white_lum:.0f}/255)."
                recommendations.append("Turn on or brighten LED ring illumination.")
            else:
                lighting_status = "optimal"
                lighting_details = f"Lighting ready ({white_lum:.0f}/255, will calibrate exposure)."
        else:
            # Under locked manual exposure:
            if hl_clip_pct > 3.5 or white_lum > 225.0:
                lighting_status = "overexposed"
                lighting_details = f"Overexposure / Glare ({hl_clip_pct:.1f}% clipped reflections)."
                recommendations.append("Dim LED ring slightly or adjust angle.")
            elif (gain_val is not None and gain_val > 40) or white_lum < 80.0:
                lighting_status = "too_dim"
                lighting_details = f"Lighting too dim (Luminance: {white_lum:.0f}/255)."
                recommendations.append("Brighten LED ring to reduce sensor noise.")
            elif (gain_val is not None and gain_val > 25) or white_lum < 115.0:
                lighting_status = "uneven"
                lighting_details = f"Lighting slightly dim (Luminance: {white_lum:.0f}/255)."
                recommendations.append("Brighten LED ring slightly for optimal SNR.")
            else:
                lighting_status = "optimal"
                lighting_details = f"Lighting optimal (Luminance: {white_lum:.0f}/255, {hl_clip_pct:.1f}% glare)."

        # --- 3. Geometry & Framing Assessment ---
        h, w = frame.shape[:2]
        cx, cy = roi.center
        dist_from_center = np.sqrt((cx - w / 2) ** 2 + (cy - h / 2) ** 2)
        max_dim = max(w, h)

        if roi.confidence < 0.4:
            geometry_status = "obstructed"
            geometry_details = "Dartboard geometry could not be reliably identified."
            recommendations.append("Ensure dartboard is unobstructed in camera view.")
        elif dist_from_center > (max_dim * 0.35) or roi.radius < (min(w, h) * 0.20):
            geometry_status = "offset"
            geometry_details = f"Dartboard is off-center or camera is too far away."
            recommendations.append("Adjust camera angle so board fills center.")
        else:
            geometry_status = "aligned"
            geometry_details = f"Dartboard is centered well (radius: {roi.radius}px)."

        is_ready = (focus_status in ("sharp", "soft")) and (lighting_status in ("optimal", "uneven"))

        return DiagnosticResult(
            focus_score=focus_score,
            focus_status=focus_status,
            focus_details=focus_details,
            lighting_status=lighting_status,
            white_bed_luminance=round(white_lum, 1),
            highlight_clipping_pct=round(hl_clip_pct, 2),
            lighting_details=lighting_details,
            geometry_status=geometry_status,
            geometry_confidence=round(roi.confidence, 2),
            geometry_details=geometry_details,
            is_ready_for_calibration=is_ready,
            recommendations=recommendations,
        )
