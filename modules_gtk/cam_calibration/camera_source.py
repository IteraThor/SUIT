"""Camera access for the Camera Optimizer wizard.

The wizard never opens /dev/video* itself: everything that touches hardware goes through a
CameraSource. The default one talks to V4L2; tests and the screenshot harness pass a stub
(fixture frames, fake optimizer) to CamCalibrationView(camera_source=...).
"""

from __future__ import annotations
import logging
import time
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from core.board_detector import get_autodarts_resolution
from core.dart_tip_detector import CameraModel, load_camera_models
from core.optimizer import CameraOptimizer, OptimizationResult
from core.v4l2_device import V4L2Device
from core.v4l2_topology import CameraDevice, V4L2Topology

logger = logging.getLogger("cam_librator.source")

LIVE_SIZE = (640, 480)


def cam_key(cam) -> str:
    """Stable camera id: the USB port by-path link, falling back to the device node.

    Same id CameraModel uses, so detections, tip ROIs and results share one key.
    """
    return getattr(cam, "by_path", None) or cam.dev_node


class LiveStream:
    """A 640x480 preview stream of one camera."""

    def __init__(self, cap: cv2.VideoCapture):
        self._cap = cap

    def read(self) -> Optional[np.ndarray]:
        ret, frame = self._cap.read()
        return frame if ret and frame is not None else None

    def release(self) -> None:
        try:
            self._cap.release()
        except Exception:
            pass


class CameraSource:
    """V4L2-backed camera access (scan, live preview, bursts, calibration, optimizer)."""

    def scan(self) -> List[CameraDevice]:
        cams = V4L2Topology().scan_cameras()
        cams.sort(key=lambda c: c.index)
        return cams

    def reset_camera_profile(self, cams: Sequence[CameraDevice]) -> None:
        """Apply the Default profile of the manual controls: factory control values, active profile reset."""
        from core.camera_manual_service import CameraManualService
        CameraManualService.apply_profile("Default", [c.dev_node for c in cams])

    def capture_resolution(self) -> Tuple[int, int]:
        return get_autodarts_resolution()

    def open_live(self, cam: CameraDevice, width: int = LIVE_SIZE[0], height: int = LIVE_SIZE[1]) -> Optional[LiveStream]:
        cap = V4L2Device(cam.dev_node).open_stream(width=width, height=height)
        if cap is None or not cap.isOpened():
            return None
        return LiveStream(cap)

    def capture_burst(self, cam: CameraDevice, count: int, width: int, height: int) -> List[np.ndarray]:
        frames = V4L2Device(cam.dev_node).capture_burst(count=count, width=width, height=height, warmup_frames=6)
        return [f for f in frames if f is not None]

    def camera_models(self, cams: Sequence[CameraDevice], width: int, height: int) -> Dict[str, CameraModel]:
        return load_camera_models(list(cams), width, height)

    def optimizer(self, cam: CameraDevice) -> CameraOptimizer:
        return CameraOptimizer(V4L2Device(cam.dev_node))

    def export_session(self, results: Dict[str, OptimizationResult], log_text: str) -> None:
        """Save before/after telemetry of a finished run (best effort, worker thread)."""
        from core.session_exporter import SessionExporter
        try:
            path = SessionExporter().export_session(results, log_text=log_text)
            logger.info("Saved calibration session telemetry to %s", path)
        except Exception as e:
            logger.warning("Failed to save calibration session telemetry: %s", e)

    def export_sample(self, cams: Sequence[CameraDevice], empty: Dict[str, List[np.ndarray]],
                      darts: Dict[str, List[np.ndarray]], capture_size: Tuple[int, int],
                      detection=None, tip_rois=None, edited_darts=None):
        """Save a dart sample zip for the SUIT team (worker thread); returns its path."""
        from core.camera_manual_service import CameraManualService
        from core.sample_exporter import SampleCamera, export_sample
        sample_cams = [SampleCamera(key=cam_key(c), number=c.index, dev_node=c.dev_node, name=c.name,
                                    sensor_model=c.sensor_model,
                                    controls=CameraManualService.get_camera_controls(c.dev_node))
                       for c in cams]
        return export_sample(sample_cams, empty, darts, capture_size, detection=detection, tip_rois=tip_rois,
                             edited_darts=edited_darts)

    @staticmethod
    def settle(seconds: float) -> None:
        # Gives the kernel a moment to release a stream before the next open
        time.sleep(seconds)
