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

logger = get_logger("camera_focus")


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


class CameraFocusService:
    def __init__(self):
        self.cap: cv2.VideoCapture | None = None
        self.current_device: str | None = None
        self.peak_score: float = 0.0
        self.last_score: float = 0.0
        self.last_bullseye: tuple[int, int] | None = None
        self.last_target_roi: tuple[int, int, int, int] | None = None
        self.smoothed_focal_y: float | None = None
        self._accum_zoom_crop: np.ndarray | None = None
        self._cam_lock = threading.Lock()

    def start_camera(self, dev_path: str, width: int = 1280, height: int = 960) -> bool:
        """Open raw V4L2 camera device for high-framerate capture."""
        with self._cam_lock:
            self._stop_camera_unlocked()
            self.reset_peak()
            try:
                logger.info("Opening camera device %s (%dx%d)", dev_path, width, height)
                self.cap = cv2.VideoCapture(dev_path, cv2.CAP_V4L2)
                if not self.cap.isOpened():
                    # Fallback to default backend
                    self.cap = cv2.VideoCapture(dev_path)
                
                if not self.cap.isOpened():
                    logger.error("Failed to open camera device %s", dev_path)
                    return False

                # Configure high-speed MJPG stream to preserve USB bandwidth
                self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc(*"MJPG"))
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                self.current_device = dev_path
                return True
            except Exception:
                logger.exception("Exception while opening camera %s", dev_path)
                return False

    def read_frame(self) -> tuple[bool, np.ndarray | None]:
        """Read a single raw frame from the active camera."""
        with self._cam_lock:
            if not self.cap or not self.cap.isOpened():
                return False, None
            try:
                ret, frame = self.cap.read()
                return ret, frame
            except Exception:
                logger.exception("Error reading frame from camera")
                return False, None

    def _stop_camera_unlocked(self) -> None:
        """Release hardware video capture locks (internal unlocked)."""
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                logger.exception("Error releasing camera device")
            self.cap = None
        self.current_device = None

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

    def locate_bullseye(self, image: np.ndarray, existing_calib: dict | None = None) -> tuple[int, int]:
        """Locate Bullseye coordinates using concentric red/green geometry or calibration."""
        h, w = image.shape[:2]
        
        # Check existing Autodarts calibration first if valid
        if existing_calib and "bull" in existing_calib:
            bx, by = existing_calib["bull"]
            if 0 < bx < w and 0 < by < h:
                self.last_bullseye = (int(bx), int(by))
                return self.last_bullseye

        # Search central region for concentric red/green bullseye
        roi_y1, roi_y2 = int(0.25 * h), int(0.58 * h)
        roi_x1, roi_x2 = int(0.32 * w), int(0.68 * w)
        crop = image[roi_y1:roi_y2, roi_x1:roi_x2]
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)

        # Red mask for inner bull
        mask_red1 = cv2.inRange(hsv, (0, 70, 70), (14, 255, 255))
        mask_red2 = cv2.inRange(hsv, (166, 70, 70), (180, 255, 255))
        mask_red = mask_red1 | mask_red2

        # Green mask for outer bull ring
        mask_green = cv2.inRange(hsv, (35, 55, 55), (88, 255, 255))

        cnts_red, _ = cv2.findContours(mask_red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cnts_green, _ = cv2.findContours(mask_green, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        red_centers = []
        for c in cnts_red:
            if 30 < cv2.contourArea(c) < 1500:
                M = cv2.moments(c)
                if M["m00"] > 0:
                    red_centers.append((M["m10"] / M["m00"], M["m01"] / M["m00"]))

        green_centers = []
        for c in cnts_green:
            if 70 < cv2.contourArea(c) < 3500:
                M = cv2.moments(c)
                if M["m00"] > 0:
                    green_centers.append((M["m10"] / M["m00"], M["m01"] / M["m00"]))

        best_bull = None
        min_dist = 999.0
        for rx, ry in red_centers:
            for gx, gy in green_centers:
                d = math.hypot(rx - gx, ry - gy)
                if d < 15.0 and d < min_dist:
                    min_dist = d
                    best_bull = (int(rx + roi_x1), int(ry + roi_y1))

        if best_bull:
            self.last_bullseye = best_bull
            return best_bull

        # Fallback to cached or standard center
        if self.last_bullseye:
            return self.last_bullseye
        return (int(0.5 * w), int(0.42 * h))

    def get_target_roi(
        self, image: np.ndarray, bullseye: tuple[int, int], existing_calib: dict | None = None
    ) -> tuple[int, int, int, int]:
        """Calculate the bounding box of the furthest double field at the top of the board."""
        h, w = image.shape[:2]
        bx, by = bullseye

        if existing_calib and "doubleOuters" in existing_calib:
            outers = existing_calib["doubleOuters"]
            if outers and len(outers) >= 20:
                # Furthest segment is at minimum y
                top_pt = min(outers, key=lambda p: p[1])
                tx, ty = int(top_pt[0]), int(top_pt[1])
                roi_w = 180
                roi_h = 50
                x = max(0, min(w - roi_w, tx - roi_w // 2))
                y = max(0, min(h - roi_h, ty - 10))
                self.last_target_roi = (x, y, roi_w, roi_h)
                return self.last_target_roi

        # Top double is directly above bullseye along the vertical perspective tilt axis
        roi_w = 180
        roi_h = 50
        x = max(0, min(w - roi_w, bx - roi_w // 2))
        y = max(0, min(h - roi_h, by - 180))
        self.last_target_roi = (x, y, roi_w, roi_h)
        return self.last_target_roi

    def compute_sharpness(self, roi: np.ndarray) -> float:
        """Compute normalized sharpness score (0–100) tuned for 2170 CMOS sensors on dartboard wires."""
        if roi is None or roi.size == 0:
            return 0.0
        
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if len(roi.shape) == 3 else roi
        
        # High-frequency edge variance
        lap = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        
        # Gradient energy (Tenengrad)
        gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        ten = float(np.mean(gx**2 + gy**2))

        # Logarithmic normalization calibrated to real dartboard blade wire optics (2170 sensor)
        log_lap = math.log10(lap + 1e-6)
        log_ten = math.log10(ten + 1e-6)

        norm_lap = np.clip((log_lap - 1.2) / (3.35 - 1.2), 0.0, 1.0)
        norm_ten = np.clip((log_ten - 2.2) / (4.70 - 2.2), 0.0, 1.0)

        blended = 0.5 * norm_lap + 0.5 * norm_ten
        return float(np.clip(blended * 100.0, 0.0, 100.0))

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
        )
