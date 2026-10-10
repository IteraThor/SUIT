"""Camera focus analysis service for Autodarts.

Provides headless computer vision for locating dartboard geometry,
tracking relative peak sharpness via the Oscilloscope Method,
supporting Furthest Double, T20, and custom click ROI targeting.
"""

from dataclasses import dataclass, field
from enum import Enum
import math
import time
import numpy as np
import cv2
import threading

from core.logger import get_logger
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


class TargetMode(Enum):
    NONE = "none"
    FURTHEST_DOUBLE = "furthest_double"
    T20 = "t20"
    CUSTOM = "custom"


@dataclass
class FocusAnalysis:
    score: float  # Normalized relative score 0-100
    raw_score: float  # Raw metric value
    peak_score: float  # Peak normalized score
    peak_raw: float  # Peak raw metric value
    trend: FocusTrend
    target_roi: tuple[int, int, int, int]  # (x, y, width, height)
    target_name: str
    target_mode: TargetMode
    bullseye: tuple[int, int] = (0, 0)
    focal_line_y: int = 0
    lighting_state: LightingState = LightingState.OPTIMAL
    history: list[float] = field(default_factory=list)  # 0-100 history
    peak_history_idx: int = -1
    fps: float = 0.0


class CameraFocusService:
    def __init__(self, detector: BoardDetector | None = None):
        self.detector = detector or BoardDetector()
        self.cap: cv2.VideoCapture | None = None
        self.current_device: str | None = None
        self.current_cam_uri: str | None = None

        # Targeting State
        self.target_mode: TargetMode = TargetMode.NONE
        self.target_name: str = "Unselected"
        self.custom_roi: tuple[int, int, int, int] | None = None
        self.locked_target_roi: tuple[int, int, int, int] | None = None
        self.last_bullseye: tuple[int, int] | None = None

        # Oscilloscope & Relative Tracking State
        self.raw_history: list[float] = []
        self.norm_history: list[float] = []
        self.smoothed_raw: float = 0.0
        self.baseline_min: float = float("inf")
        self.baseline_max: float = 0.0
        self.peak_raw: float = 0.0
        self.peak_norm: float = 0.0
        self.peak_history_idx: int = -1

        self.last_sample_time: float = 0.0
        self.sample_interval: float = 0.10  # 100ms / 10 Hz
        self.fps_tracker: list[float] = []
        self.current_fps: float = 0.0

        self._accum_zoom_crop: np.ndarray | None = None
        self._cam_lock = threading.Lock()

    def set_target_mode(self, mode: TargetMode, custom_point: tuple[int, int] | None = None, frame_shape: tuple[int, int] = (960, 1280)) -> None:
        """Set active focus target (Furthest Double, T20, or custom tap)."""
        self.target_mode = mode
        self.reset_peak()

        if mode == TargetMode.FURTHEST_DOUBLE:
            self.target_name = "Furthest Double"
            self.custom_roi = None
            self.locked_target_roi = None
            if self.detector and self.current_device:
                self.detector.unlock_target(self.current_device)
        elif mode == TargetMode.T20:
            self.target_name = "Triple 20 (T20)"
            self.custom_roi = None
            self.locked_target_roi = None
            if self.detector and self.current_device:
                self.detector.unlock_target(self.current_device)
        elif mode == TargetMode.CUSTOM and custom_point is not None:
            cx, cy = custom_point
            h, w = frame_shape[:2]
            roi_size = 90
            x = max(0, min(w - roi_size, int(cx - roi_size // 2)))
            y = max(0, min(h - roi_size, int(cy - roi_size // 2)))
            self.custom_roi = (x, y, roi_size, roi_size)
            self.locked_target_roi = self.custom_roi
            self.target_name = "Custom Point"

    def start_camera(self, dev_path: str, width: int = 1280, height: int = 960, retries: int = 3, uri: str | None = None) -> bool:
        """Open raw V4L2 camera device for capture with retry backoff."""
        with self._cam_lock:
            self._stop_camera_unlocked()
            self.reset_peak()
            self.target_mode = TargetMode.NONE
            self.locked_target_roi = None
            self.custom_roi = None
            self.last_bullseye = None

            import os
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
        """Reset the latched peak score and oscilloscope tracking state."""
        self.raw_history.clear()
        self.norm_history.clear()
        self.smoothed_raw = 0.0
        self.baseline_min = float("inf")
        self.baseline_max = 0.0
        self.peak_raw = 0.0
        self.peak_norm = 0.0
        self.peak_history_idx = -1
        self.last_sample_time = 0.0
        self._accum_zoom_crop = None

    def unlock_target(self) -> None:
        """Reset locked target ROI so target can be re-selected."""
        self.locked_target_roi = None
        self.custom_roi = None
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
        out_size: tuple[int, int] = (260, 260),
        peaking_enabled: bool = True,
    ) -> np.ndarray | None:
        """Extract clean 2.5x magnified crop on target ROI with temporal smoothing and optional focus peaking."""
        fh, fw = frame.shape[:2]
        tx, ty, tw, th = target_roi
        if tw > 0 and th > 0:
            cx = tx + tw // 2
            cy = ty + th // 2
        else:
            cx = fw // 2
            cy = fh // 2

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

        if self._accum_zoom_crop is None or self._accum_zoom_crop.shape != crop.shape:
            self._accum_zoom_crop = crop.astype(np.float32)
        else:
            self._accum_zoom_crop = cv2.addWeighted(
                crop.astype(np.float32), 0.40, self._accum_zoom_crop, 0.60, 0
            )
        clean_crop = np.clip(self._accum_zoom_crop, 0, 255).astype(np.uint8)

        # Focus peaking / contour edge highlights
        if peaking_enabled:
            gray = cv2.cvtColor(clean_crop, cv2.COLOR_BGR2GRAY)
            gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
            gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
            mag = cv2.magnitude(gx, gy)
            peak_mask = mag > 200.0
            if np.any(peak_mask):
                peaked = clean_crop.copy()
                peaked[peak_mask] = cv2.addWeighted(
                    clean_crop[peak_mask], 0.35, np.full_like(clean_crop[peak_mask], [40, 255, 60]), 0.65, 0
                )
                render_crop = peaked
            else:
                render_crop = clean_crop
        else:
            render_crop = clean_crop

        resized = cv2.resize(render_crop, out_size, interpolation=cv2.INTER_LINEAR)
        return cv2.cvtColor(resized, cv2.COLOR_BGR2BGRA)

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
        """Calculate target bounding box based on active target mode."""
        if self.locked_target_roi is not None:
            return self.locked_target_roi

        h, w = image.shape[:2]

        if self.target_mode == TargetMode.CUSTOM and self.custom_roi is not None:
            self.locked_target_roi = self.custom_roi
            return self.custom_roi

        if self.target_mode == TargetMode.T20:
            roi = self.detector.get_t20_roi(
                image,
                dev_node=self.current_device,
                by_path=self.current_cam_uri,
                center=bullseye,
                roi_size=90,
            )
            self.locked_target_roi = roi
            return roi

        # Furthest double or default
        roi = self.detector.get_furthest_double_roi(
            image,
            dev_node=self.current_device,
            by_path=self.current_cam_uri,
            center=bullseye,
        )
        self.locked_target_roi = roi
        return roi

    @staticmethod
    def compute_patch_sharpness(patch: np.ndarray) -> float:
        """Compute combined Laplacian Variance + Tenengrad focus metric on patch."""
        if patch is None or patch.size == 0:
            return 0.0

        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY) if len(patch.shape) == 3 else patch
        
        lap = cv2.Laplacian(gray, cv2.CV_64F)
        lap_var = float(lap.var())

        gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        tenengrad = float(np.mean(gx**2 + gy**2))

        raw = math.sqrt(max(0.0, lap_var)) + math.sqrt(max(0.0, tenengrad))
        return raw

    def check_lighting_quality(self, roi: np.ndarray) -> LightingState:
        """Evaluate if target patch has acceptable exposure."""
        if roi is None or roi.size == 0:
            return LightingState.TOO_DARK
            
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if len(roi.shape) == 3 else roi
        mean_luma = float(np.mean(gray))
        
        if mean_luma < 40.0:
            return LightingState.TOO_DARK
        elif mean_luma > 185.0:
            return LightingState.OVEREXPOSED
        return LightingState.OPTIMAL

    def analyze_frame(self, image: np.ndarray, existing_calib: dict | None = None) -> FocusAnalysis:
        """Perform real-time Oscilloscope focus analysis on camera frame."""
        now = time.time()
        self.fps_tracker.append(now)
        self.fps_tracker = [t for t in self.fps_tracker if now - t <= 1.0]
        self.current_fps = float(len(self.fps_tracker))

        bullseye = self.locate_bullseye(image, existing_calib)
        target_roi = self.get_target_roi(image, bullseye, existing_calib)

        rx, ry, rw, rh = target_roi
        patch = image[ry : ry + rh, rx : rx + rw]
        
        lighting_state = self.check_lighting_quality(patch)
        
        if lighting_state != LightingState.OPTIMAL:
            raw_val = 0.0
        else:
            raw_val = self.compute_patch_sharpness(patch)

        # Oscilloscope sampling
        if self.smoothed_raw == 0.0:
            self.smoothed_raw = raw_val
        else:
            self.smoothed_raw = self.smoothed_raw * 0.65 + raw_val * 0.35

        s_val = self.smoothed_raw

        if s_val > 0.0:
            if s_val < self.baseline_min:
                self.baseline_min = s_val
            if s_val > self.baseline_max:
                self.baseline_max = s_val

        if s_val > self.peak_raw:
            self.peak_raw = s_val

        # Update history
        if (now - self.last_sample_time) >= self.sample_interval or len(self.raw_history) == 0:
            self.last_sample_time = now
            self.raw_history.append(s_val)
            if len(self.raw_history) > 100:
                self.raw_history.pop(0)

            span = max(10.0, self.baseline_max - self.baseline_min)
            self.norm_history = [
                min(100.0, max(0.0, ((v - self.baseline_min) / span) * 100.0))
                if span > 0 else 0.0
                for v in self.raw_history
            ]

            if self.baseline_max > self.baseline_min:
                self.peak_norm = min(100.0, max(0.0, ((self.peak_raw - self.baseline_min) / span) * 100.0))
            else:
                self.peak_norm = 0.0

            if self.raw_history and self.peak_raw > 0:
                try:
                    self.peak_history_idx = self.raw_history.index(self.peak_raw)
                except ValueError:
                    self.peak_history_idx = len(self.raw_history) - 1
            else:
                self.peak_history_idx = -1

        span = max(10.0, self.baseline_max - self.baseline_min)
        if span > 0 and self.smoothed_raw > 0:
            current_norm = min(100.0, max(0.0, ((self.smoothed_raw - self.baseline_min) / span) * 100.0))
        else:
            current_norm = 0.0

        if self.peak_raw > 0 and self.smoothed_raw >= (self.peak_raw * 0.98):
            trend = FocusTrend.SHARPENING
        elif self.peak_raw > 0 and (self.peak_raw - self.smoothed_raw) / max(1.0, self.peak_raw) > 0.06:
            trend = FocusTrend.OVERSHOT
        else:
            trend = FocusTrend.STABLE

        focal_line_y = int(ry + rh // 2)

        return FocusAnalysis(
            score=round(current_norm, 1),
            raw_score=round(self.smoothed_raw, 1),
            peak_score=round(self.peak_norm, 1),
            peak_raw=round(self.peak_raw, 1),
            trend=trend,
            target_roi=target_roi,
            target_name=self.target_name,
            target_mode=self.target_mode,
            bullseye=bullseye,
            focal_line_y=focal_line_y,
            lighting_state=lighting_state,
            history=list(self.norm_history),
            peak_history_idx=self.peak_history_idx,
            fps=round(self.current_fps, 1),
        )
