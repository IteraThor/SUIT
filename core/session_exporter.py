"""Calibration Session and Telemetry Exporter.

Automatically persists full-resolution frames, magnified zone crops,
Sobel edge gradient maps, and JSON metrics for before/after comparative analysis.
"""

from __future__ import annotations
import dataclasses
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
from typing import Dict, List, Optional, Tuple, Union
import cv2
import numpy as np

from core.cv_utils import apply_edge_contrast_filter
from core.optimizer import OptimizationResult
from core.metrics import QualityMetrics
from core.v4l2_device import CameraSettings

logger = logging.getLogger("suit.session_exporter")

DEFAULT_SESSIONS_DIR = Path.home() / ".local" / "share" / "suit" / "calibration_sessions"


def _sanitize_cam_name(dev_node: str) -> str:
    """Extract clean identifier like cam0, cam1, video0 from dev node."""
    match = re.search(r"video(\d+)", dev_node)
    if match:
        return f"cam{match.group(1)}"
    clean = re.sub(r"[^a-zA-Z0-9_-]", "_", dev_node.strip("/"))
    return clean or "cam"


def _extract_crop(
    frame: np.ndarray,
    center: Tuple[int, int],
    crop_size: Tuple[int, int] = (240, 240),
) -> Optional[np.ndarray]:
    """Safely extract bounded square crop around center point."""
    if frame is None or frame.size == 0:
        return None
    h, w = frame.shape[:2]
    cx, cy = center
    cw, ch = crop_size

    x1 = max(0, cx - cw // 2)
    y1 = max(0, cy - ch // 2)
    x2 = min(w, x1 + cw)
    y2 = min(h, y1 + ch)

    # Adjust if hitting border
    if x2 - x1 < cw and w >= cw:
        x1 = max(0, x2 - cw)
    if y2 - y1 < ch and h >= ch:
        y1 = max(0, y2 - ch)

    crop = frame[y1:y2, x1:x2]
    return crop if crop.size > 0 else None


class SessionExporter:
    """Exports structured calibration telemetry and comparative image samples."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or DEFAULT_SESSIONS_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def export_session(
        self,
        results: Dict[str, OptimizationResult],
        session_id: Optional[str] = None,
        log_text: Optional[str] = None,
    ) -> Path:
        """Export complete before/after telemetry for all calibrated cameras."""
        if not session_id:
            session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        session_dir = self.base_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        session_data = {
            "session_id": session_id,
            "timestamp": datetime.now().isoformat(),
            "camera_count": len(results),
            "cameras": {},
        }

        summary_lines = [
            f"=== SUIT CAM-librator Session Export: {session_id} ===",
            f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Calibrated Cameras: {len(results)}",
            "-" * 60,
        ]

        for dev_node, res in results.items():
            cam_name = _sanitize_cam_name(dev_node)
            cam_entry = {
                "dev_node": dev_node,
                "cam_name": cam_name,
                "target_kelvin": res.target_kelvin,
                "improvement_percentage": round(float(res.improvement_percentage), 2),
                "initial_settings": dataclasses.asdict(res.initial_settings) if res.initial_settings else {},
                "optimized_settings": dataclasses.asdict(res.optimized_settings) if res.optimized_settings else {},
                "initial_metrics": dataclasses.asdict(res.initial_metrics) if res.initial_metrics else {},
                "optimized_metrics": dataclasses.asdict(res.optimized_metrics) if res.optimized_metrics else {},
                "roi_geometry": {
                    "center": res.roi.center if res.roi else None,
                    "radius": res.roi.radius if res.roi else None,
                    "target_roi": res.roi.target_roi if res.roi else None,
                    "confidence": float(res.roi.confidence) if res.roi else 0.0,
                },
                "artifacts": {},
            }

            # 1. Full-Resolution PNG Frames
            if res.initial_frame is not None and res.initial_frame.size > 0:
                p_before = session_dir / f"{cam_name}_before.png"
                cv2.imwrite(str(p_before), res.initial_frame)
                cam_entry["artifacts"]["full_before"] = p_before.name

                edge_b = apply_edge_contrast_filter(res.initial_frame)
                p_edge_b = session_dir / f"{cam_name}_edge_map_before.png"
                cv2.imwrite(str(p_edge_b), edge_b)
                cam_entry["artifacts"]["edge_before"] = p_edge_b.name

            if res.optimized_frame is not None and res.optimized_frame.size > 0:
                p_after = session_dir / f"{cam_name}_after.png"
                cv2.imwrite(str(p_after), res.optimized_frame)
                cam_entry["artifacts"]["full_after"] = p_after.name

                edge_a = apply_edge_contrast_filter(res.optimized_frame)
                p_edge_a = session_dir / f"{cam_name}_edge_map_after.png"
                cv2.imwrite(str(p_edge_a), edge_a)
                cam_entry["artifacts"]["edge_after"] = p_edge_a.name

            # 2. Key Zonal Crops (Bullseye, Treble 20, Sweet Spot)
            center_pt = res.roi.center if (res.roi and res.roi.center) else (640, 360)
            radius = res.roi.radius if (res.roi and res.roi.radius) else 250
            treble_pt = (center_pt[0], max(0, center_pt[1] - int(radius * 0.55)))

            sweet_pt = center_pt
            if res.roi and res.roi.target_roi:
                tx, ty, tw, th = res.roi.target_roi
                sweet_pt = (tx + tw // 2, ty + th // 2)

            for zone_name, pt in [("bullseye", center_pt), ("treble20", treble_pt), ("sweetspot", sweet_pt)]:
                if res.initial_frame is not None:
                    crop_b = _extract_crop(res.initial_frame, pt, (240, 240))
                    if crop_b is not None:
                        p_crop_b = session_dir / f"{cam_name}_crop_{zone_name}_before.png"
                        cv2.imwrite(str(p_crop_b), crop_b)
                        cam_entry["artifacts"][f"crop_{zone_name}_before"] = p_crop_b.name

                if res.optimized_frame is not None:
                    crop_a = _extract_crop(res.optimized_frame, pt, (240, 240))
                    if crop_a is not None:
                        p_crop_a = session_dir / f"{cam_name}_crop_{zone_name}_after.png"
                        cv2.imwrite(str(p_crop_a), crop_a)
                        cam_entry["artifacts"][f"crop_{zone_name}_after"] = p_crop_a.name

            session_data["cameras"][dev_node] = cam_entry

            # Text summary breakdown
            summary_lines.extend([
                f"\n[Camera: {dev_node} ({cam_name})]",
                f"  Score:        {res.initial_metrics.composite_score:.1f}/100  ->  {res.optimized_metrics.composite_score:.1f}/100 (+{res.improvement_percentage:.1f}%)",
                f"  Wire FWHM:    {res.initial_metrics.wire_fwhm:.2f} px  ->  {res.optimized_metrics.wire_fwhm:.2f} px (thinner/sharper)",
                f"  Edge Steep:   {res.initial_metrics.edge_steepness:.2f}  ->  {res.optimized_metrics.edge_steepness:.2f}",
                f"  Halo Ringing: {res.initial_metrics.halo_overshoot:.3f}  ->  {res.optimized_metrics.halo_overshoot:.3f}",
                f"  Temporal Std: {res.initial_metrics.temporal_noise:.3f}  ->  {res.optimized_metrics.temporal_noise:.3f}",
                f"  CNR:          {res.initial_metrics.contrast_to_noise_ratio:.1f}  ->  {res.optimized_metrics.contrast_to_noise_ratio:.1f}",
                f"  Luminance:    {res.initial_metrics.mean_luminance:.1f}  ->  {res.optimized_metrics.mean_luminance:.1f}",
                f"  Gain:         {res.initial_settings.gain}  ->  {res.optimized_settings.gain}",
                f"  Contrast:     {res.initial_settings.contrast}  ->  {res.optimized_settings.contrast}",
                f"  Sharpness:    {res.initial_settings.sharpness}  ->  {res.optimized_settings.sharpness}",
                f"  Exposure:     {res.initial_settings.exposure_absolute}  ->  {res.optimized_settings.exposure_absolute}",
            ])

        # 3. Save JSON Metrics
        metrics_file = session_dir / "session_metrics.json"
        with open(metrics_file, "w", encoding="utf-8") as f:
            json.dump(session_data, f, indent=2)

        # 4. Save Text Summary
        summary_file = session_dir / "session_summary.txt"
        with open(summary_file, "w", encoding="utf-8") as f:
            f.write("\n".join(summary_lines) + "\n")

        # 5. Save Logs if provided
        if log_text:
            log_file = session_dir / "session_log.txt"
            with open(log_file, "w", encoding="utf-8") as f:
                f.write(log_text)

        # 6. Update latest symlink
        latest_link = self.base_dir / "latest"
        try:
            if latest_link.is_symlink() or latest_link.exists():
                latest_link.unlink()
            latest_link.symlink_to(session_dir.resolve(), target_is_directory=True)
        except Exception as e:
            logger.warning("Could not create 'latest' symlink: %s", e)

        logger.info("Successfully exported calibration session to %s", session_dir)
        return session_dir
