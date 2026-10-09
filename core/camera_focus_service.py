"""Camera focus analysis service for Autodarts.

Provides headless computer vision for locating dartboard geometry,
tracking peak focus on the furthest double field, computing dynamic
focal line progression, and managing V4L2 camera capture.
"""

from dataclasses import dataclass
from enum import Enum
import math
import numpy as np
import cv2
import threading

from core.logger import get_logger
from core.metrics import QualityEvaluator
from core.board_detector import BoardDetector

logger = get_logger("camera_focus")


class LightingState(Enum):
    TOO_DARK = "too_dark"
    OPTIMAL = "optimal"
    OVEREXPOSED = "overexposed"


class FocusTrend(Enum):
    SHARPENING = "sharpening"
    STABLE = "stable"
    OVERSHOT = "overshot"


@dataclass
class FocusAnalysis:
    score: float
    peak_score: float
    trend: FocusTrend
    target_roi: tuple[int, int, int, int]  # (x, y, width, height)
    focal_line_y: int
    bullseye: tuple[int, int]
    target_name: str = "Furthest Double"
    lighting_state: LightingState = LightingState.OPTIMAL


class CameraFocusService:
    def __init__(self, detector: BoardDetector | None = None):
        self.detector = detector or BoardDetector()
        self.cap: cv2.VideoCapture | None = None
        self.current_device: str | None = None
        self.current_cam_uri: str | None = None
        self.peak_score: float = 0.0
        self.last_score: float = 0.0
        self.last_bullseye: tuple[int, int] | None = None
        self.last_target_roi: tuple[int, int, int, int] | None = None
        self.locked_target_roi: tuple[int, int, int, int] | None = None
        self.smoothed_focal_y: float | None = None
        self._accum_zoom_crop: np.ndarray | None = None
        self._cam_lock = threading.Lock()

    def start_camera(self, dev_path: str, width: int = 1280, height: int = 960, retries: int = 3, uri: str | None = None) -> bool:
        """Open raw V4L2 camera device for high-framerate capture with retry backoff."""
        with self._cam_lock:
            self._stop_camera_unlocked()
            self.reset_peak()
            self.unlock_target()
            self.last_bullseye = None

            import os, time
            from core.autodarts_service import parse_cam_device

            target_dev = parse_cam_device(dev_path)
            if not target_dev or not os.path.exists(target_dev):
                logger.warning("Camera device path does not exist: %s (resolved from %s)", target_dev, dev_path)
                return False

            self.current_cam_uri = uri or (dev_path if "location=" in dev_path or "serial=" in dev_path else None)

            for attempt in range(retries):
                try:
                    logger.info("Opening camera device %s (%dx%d) [attempt %d/%d]", target_dev, width, height, attempt + 1, retries)
                    cap = cv2.VideoCapture(target_dev, cv2.CAP_V4L2)
                    if not cap.isOpened():
                        cap.release()
                        cap = cv2.VideoCapture(target_dev)

                    # Also try numeric index fallback if target is /dev/videoX
                    if not cap.isOpened() and target_dev.startswith("/dev/video"):
                        try:
                            idx = int(target_dev.replace("/dev/video", ""))
                            cap.release()
                            cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
                            if not cap.isOpened():
                                cap.release()
                                cap = cv2.VideoCapture(idx)
                        except Exception:
                            pass

                    if cap.isOpened():
                        # Configure high-speed MJPG stream to preserve USB bandwidth
                        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc(*"MJPG"))
                        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                        self.cap = cap
                        self.current_device = target_dev
                        return True
                    else:
                        cap.release()
                except Exception:
                    logger.exception("Exception while opening camera %s", target_dev)

                if attempt < retries - 1:
                    time.sleep(0.3)

            logger.error("Failed to open camera device %s after %d attempts", target_dev, retries)
            return False

    def read_frame(self) -> tuple[bool, np.ndarray | None]:
        """Read a single raw frame from the active camera."""
        with self._cam_lock:
            if self.cap is None or not self.cap.isOpened():
                return False, None
            try:
                ret, frame = self.cap.read()
                if ret and frame is not None and frame.size > 0:
                    return True, frame
                return False, None
            except Exception:
                logger.exception("Error reading frame from camera %s", self.current_device)
                return False, None

    def _stop_camera_unlocked(self) -> None:
        """Internal helper to release capture resource (must hold _cam_lock)."""
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                logger.exception("Error releasing camera %s", self.current_device)
            self.cap = None
        self.current_device = None
        self.current_cam_uri = None

    def stop_camera(self) -> None:
        """Release hardware video capture locks."""
        with self._cam_lock:
            self._stop_camera_unlocked()

    def reset_peak(self) -> None:
        """Reset the latched peak score and tracking state."""
        self.peak_score = 0.0
        self.last_score = 0.0
        self.smoothed_focal_y = None
        self._accum_zoom_crop = None

    def unlock_target(self) -> None:
        """Reset locked target ROI so furthest double can be re-detected."""
        self.locked_target_roi = None
        self.last_target_roi = None
        self._accum_zoom_crop = None
        if self.detector:
            self.detector.unlock_target(self.current_device)

    @staticmethod
    def frame_to_bgra(frame: np.ndarray) -> np.ndarray:
        """Convert BGR camera frame to BGRA for Cairo surface rendering."""
        return cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)

    def process_zoom_crop(
        self,
        frame: np.ndarray,
        target_roi: tuple[int, int, int, int],
        zoom_factor: float = 2.5,
        out_size: tuple[int, int] = (260, 260)
    ) -> np.ndarray | None:
        """
        Extract digital zoom crop on target ROI with temporal accumulation
        and Sobel focus peaking edge highlights. Returns BGRA numpy buffer.
        """
        fh, fw = frame.shape[:2]
        tx, ty, tw, th = target_roi
        if tw > 0 and th > 0:
            cx = tx + tw // 2
            cy = ty + th // 2
        else:
            cx = fw // 2
            cy = fh // 4

        target_w, target_h = out_size
        cw = max(20, int(target_w / zoom_factor))
        ch = max(20, int(target_h / zoom_factor))

        x1 = cx - cw // 2
        y1 = cy - ch // 2
        x2 = x1 + cw
        y2 = y1 + ch

        pad_top = max(0, -y1)
        pad_bottom = max(0, y2 - fh)
        pad_left = max(0, -x1)
        pad_right = max(0, x2 - fw)

        if pad_top > 0 or pad_bottom > 0 or pad_left > 0 or pad_right > 0:
            padded = cv2.copyMakeBorder(
                frame, pad_top, pad_bottom, pad_left, pad_right, cv2.BORDER_CONSTANT, value=[10, 12, 15]
            )
            crop = padded[y1 + pad_top : y2 + pad_top, x1 + pad_left : x2 + pad_left]
        else:
            crop = frame[y1:y2, x1:x2]

        if crop.size == 0:
            return None

        # Temporal smoothing to stabilize sensor noise
        if self._accum_zoom_crop is None or self._accum_zoom_crop.shape != crop.shape:
            self._accum_zoom_crop = crop.astype(np.float32)
        else:
            self._accum_zoom_crop = cv2.addWeighted(
                crop.astype(np.float32), 0.35, self._accum_zoom_crop, 0.65, 0
            )
        clean_crop = np.clip(self._accum_zoom_crop, 0, 255).astype(np.uint8)

        # Focus peaking edge filter
        gray_crop = cv2.cvtColor(clean_crop, cv2.COLOR_BGR2GRAY)
        gx = cv2.Sobel(gray_crop, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray_crop, cv2.CV_32F, 0, 1, ksize=3)
        mag = cv2.magnitude(gx, gy)
        peak_mask = mag > 260.0

        if np.any(peak_mask):
            peaked_crop = clean_crop.copy()
            peaked_crop[peak_mask] = cv2.addWeighted(
                clean_crop[peak_mask], 0.35, np.full_like(clean_crop[peak_mask], [40, 255, 60]), 0.65, 0
            )
            render_crop = peaked_crop
        else:
            render_crop = clean_crop

        resized = cv2.resize(render_crop, out_size, interpolation=cv2.INTER_LINEAR)
        return cv2.cvtColor(resized, cv2.COLOR_BGR2BGRA)

    def load_calibration_homography(self, width: int, height: int) -> np.ndarray | None:
        """Load 3x3 homography matrix from ~/.config/autodarts/calibration.json for active camera."""
        return self.detector.load_calibration_homography(
            width, height, dev_node=self.current_device, by_path=self.current_cam_uri
        )

    def locate_bullseye(self, image: np.ndarray, existing_calib: dict | None = None) -> tuple[int, int]:
        """Locate Bullseye coordinates using BoardDetector concentric geometry or calibration."""
        h, w = image.shape[:2]
        if existing_calib and "bull" in existing_calib:
            bx, by = existing_calib["bull"]
            if 0 < bx < w and 0 < by < h:
                self.last_bullseye = (int(bx), int(by))
                return self.last_bullseye

        bull = self.detector.locate_bullseye(
            frame=image,
            dev_node=self.current_device,
            by_path=self.current_cam_uri,
        )
        self.last_bullseye = bull
        return bull

    def get_target_roi(
        self, image: np.ndarray, bullseye: tuple[int, int], existing_calib: dict | None = None
    ) -> tuple[int, int, int, int]:
        """Calculate bounding box centered directly on the furthest double field.

        Once a target is identified via calibration or dynamic CV detection,
        the ROI is locked to remain static so the digital zoom magnifier does not wiggle.
        """
        if self.locked_target_roi is not None:
            return self.locked_target_roi

        roi = self.detector.get_furthest_double_roi(
            image,
            dev_node=self.current_device,
            by_path=self.current_cam_uri,
            center=bullseye,
        )
        self.last_target_roi = roi
        self.locked_target_roi = roi
        return roi

    def compute_sharpness(self, roi: np.ndarray) -> float:
        """Compute normalized sharpness score (0–100) tuned for 2170 CMOS sensors on dartboard wires."""
        return QualityEvaluator.calculate_focus_score(roi)

    def check_lighting_quality(self, roi: np.ndarray) -> LightingState:
        """Evaluate if the target patch has acceptable exposure for sharpness scoring."""
        if roi is None or roi.size == 0:
            return LightingState.TOO_DARK
            
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if len(roi.shape) == 3 else roi
        mean_luma = float(np.mean(gray))
        
        if mean_luma < 45.0:
            return LightingState.TOO_DARK
        elif mean_luma > 185.0:
            return LightingState.OVEREXPOSED
        return LightingState.OPTIMAL

    def find_dynamic_focal_line(
        self, image: np.ndarray, bullseye: tuple[int, int], target_roi: tuple[int, int, int, int]
    ) -> int:
        """Return the target sweet spot center y-coordinate on the furthest double field."""
        _, ty, _, th = target_roi
        return int(ty + th // 2)

    def analyze_frame(self, image: np.ndarray, existing_calib: dict | None = None) -> FocusAnalysis:
        """Perform complete optical focus analysis on a camera frame."""
        h, w = image.shape[:2]
        bullseye = self.locate_bullseye(image, existing_calib)
        target_roi = self.get_target_roi(image, bullseye, existing_calib)

        # Extract target ROI patch and compute sharpness score
        rx, ry, rw, rh = target_roi
        patch = image[ry : ry + rh, rx : rx + rw]
        
        lighting_state = self.check_lighting_quality(patch)
        
        if lighting_state != LightingState.OPTIMAL:
            score = 0.0
        else:
            score = self.compute_sharpness(patch)

        # Locate current focal line
        focal_line_y = self.find_dynamic_focal_line(image, bullseye, target_roi)

        # Peak tracking and trend evaluation
        if score > self.peak_score:
            self.peak_score = score
            trend = FocusTrend.SHARPENING
        elif self.peak_score > 0 and (self.peak_score - score) >= 4.0:
            trend = FocusTrend.OVERSHOT
        elif abs(score - self.last_score) <= 1.0:
            trend = FocusTrend.STABLE
        elif score < self.last_score:
            trend = FocusTrend.OVERSHOT
        else:
            trend = FocusTrend.SHARPENING

        self.last_score = score

        return FocusAnalysis(
            score=round(score, 1),
            peak_score=round(self.peak_score, 1),
            trend=trend,
            target_roi=target_roi,
            focal_line_y=focal_line_y,
            bullseye=bullseye,
            target_name="Furthest Double",
            lighting_state=lighting_state,
        )
