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
    def __init__(self):
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

    def unlock_target(self) -> None:
        """Reset locked target ROI so furthest double can be re-detected."""
        self.locked_target_roi = None
        self.last_target_roi = None
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

    def load_calibration_homography(self, width: int, height: int) -> np.ndarray | None:
        """Load 3x3 homography matrix from ~/.config/autodarts/calibration.json for active camera."""
        try:
            from pathlib import Path
            import json
            cal_file = Path.home() / ".config" / "autodarts" / "calibration.json"
            if not cal_file.exists():
                return None
            cal_data = json.loads(cal_file.read_text(encoding="utf-8"))
            cams = cal_data.get("cameras", {})

            target_key = None
            if self.current_cam_uri:
                for k in cams.keys():
                    if k in self.current_cam_uri or self.current_cam_uri in k:
                        target_key = k
                        break
            if not target_key and self.current_device:
                from core.autodarts_service import read_cam_config, parse_cam_device
                cfg = read_cam_config()
                for raw_d in cfg.get("devices", []):
                    if raw_d and parse_cam_device(raw_d) == self.current_device:
                        for k in cams.keys():
                            if k in raw_d or raw_d in k:
                                target_key = k
                                break
                        if target_key:
                            break

            if not target_key:
                return None

            res_key = f"{width}x{height}"
            cam_entry = cams.get(target_key, {})
            if res_key in cam_entry and "homography" in cam_entry[res_key]:
                return np.array(cam_entry[res_key]["homography"]).reshape(3, 3)
        except Exception:
            logger.debug("Failed loading calibration homography", exc_info=True)
        return None

    def locate_bullseye(self, image: np.ndarray, existing_calib: dict | None = None) -> tuple[int, int]:
        """Locate Bullseye coordinates using robust concentric geometry, color-agnostic clustering, or calibration."""
        h, w = image.shape[:2]

        # 1. Check existing Autodarts calibration first if valid
        if existing_calib and "bull" in existing_calib:
            bx, by = existing_calib["bull"]
            if 0 < bx < w and 0 < by < h:
                self.last_bullseye = (int(bx), int(by))
                return self.last_bullseye

        # Check homography from calibration
        if existing_calib and "homography" in existing_calib:
            try:
                h_mat = np.array(existing_calib["homography"]).reshape(3, 3)
                h_inv = np.linalg.inv(h_mat)
                b = h_inv @ np.array([500.0, 500.0, 1.0])
                calib_bull = (int(b[0] / b[2]), int(b[1] / b[2]))
                if 0 < calib_bull[0] < w and 0 < calib_bull[1] < h:
                    self.last_bullseye = calib_bull
                    return calib_bull
            except Exception:
                pass

        h_mat = self.load_calibration_homography(w, h)
        if h_mat is not None:
            try:
                h_inv = np.linalg.inv(h_mat)
                b = h_inv @ np.array([500.0, 500.0, 1.0])
                calib_bull = (int(b[0] / b[2]), int(b[1] / b[2]))
                if 0 < calib_bull[0] < w and 0 < calib_bull[1] < h:
                    self.last_bullseye = calib_bull
                    return calib_bull
            except Exception:
                pass

        # 2. Concentric Multi-Channel & Color-Agnostic Contour Clustering
        roi_y1, roi_y2 = int(0.15 * h), int(0.85 * h)
        roi_x1, roi_x2 = int(0.15 * w), int(0.85 * w)
        crop = image[roi_y1:roi_y2, roi_x1:roi_x2]
        ch, cw = crop.shape[:2]

        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)

        accum = np.zeros((ch, cw), dtype=np.float32)

        # A. Red/Green Concentric Detector (Standard dartboard color bonus)
        mask_red = cv2.inRange(hsv, (0, 55, 55), (14, 255, 255)) | cv2.inRange(hsv, (166, 55, 55), (180, 255, 255))
        mask_green = cv2.inRange(hsv, (35, 40, 40), (88, 255, 255))
        cnts_r, _ = cv2.findContours(mask_red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cnts_g, _ = cv2.findContours(mask_green, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        red_pts = []
        for c in cnts_r:
            area = cv2.contourArea(c)
            if 20 < area < 2500:
                M = cv2.moments(c)
                if M["m00"] > 0:
                    red_pts.append((M["m10"] / M["m00"], M["m01"] / M["m00"], area))
        green_pts = []
        for c in cnts_g:
            area = cv2.contourArea(c)
            if 50 < area < 4500:
                M = cv2.moments(c)
                if M["m00"] > 0:
                    green_pts.append((M["m10"] / M["m00"], M["m01"] / M["m00"], area))

        for rx, ry, ra in red_pts:
            for gx, gy, ga in green_pts:
                d = math.hypot(rx - gx, ry - gy)
                ratio = ga / (ra + 1e-5)
                if d < 12.0 and 1.5 < ratio < 15.0:
                    bx_c = int((rx + gx) / 2)
                    by_c = int((ry + gy) / 2)
                    if 0 <= bx_c < cw and 0 <= by_c < ch:
                        accum[by_c, bx_c] += 5000.0 / (1.0 + d * 2.0)

        # B. Color-Agnostic Concentric Edge / Grayscale Ring Detector
        blurred = cv2.GaussianBlur(gray, (5, 5), 1.0)
        thresh_adapt = cv2.adaptiveThreshold(blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 25, 4)
        edges = cv2.Canny(blurred, 30, 100)

        for bin_img in [thresh_adapt, edges]:
            cnts, _ = cv2.findContours(bin_img, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
            valid_cnts = []
            for c in cnts:
                area = cv2.contourArea(c)
                if 80 < area < 4000:
                    peri = cv2.arcLength(c, True)
                    if peri > 0:
                        circularity = 4 * math.pi * area / (peri * peri)
                        if circularity > 0.35:
                            M = cv2.moments(c)
                            if M["m00"] > 0:
                                cx = M["m10"] / M["m00"]
                                cy = M["m01"] / M["m00"]
                                valid_cnts.append((cx, cy, area, circularity))

            for i in range(len(valid_cnts)):
                for j in range(i + 1, len(valid_cnts)):
                    c1, c2 = valid_cnts[i], valid_cnts[j]
                    d = math.hypot(c1[0] - c2[0], c1[1] - c2[1])
                    inner_area = min(c1[2], c2[2])
                    outer_area = max(c1[2], c2[2])
                    ratio = outer_area / (inner_area + 1e-5)
                    if d < 4.5 and 1.8 < ratio < 9.0 and (80 < inner_area < 700) and (450 < outer_area < 3500):
                        bx_c = int((c1[0] + c2[0]) / 2)
                        by_c = int((c1[1] + c2[1]) / 2)
                        if 0 <= bx_c < cw and 0 <= by_c < ch:
                            accum[by_c, bx_c] += 1500.0 / (1.0 + d * 2.0)

        accum_smooth = cv2.GaussianBlur(accum, (21, 21), 0)
        y_g, x_g = np.ogrid[:ch, :cw]
        prior = 1.0 - 0.25 * ((x_g - cw / 2) ** 2 + (y_g - ch / 2) ** 2) / ((cw / 2) ** 2 + (ch / 2) ** 2)
        accum_smooth *= np.clip(prior, 0.5, 1.0)

        _, max_v, _, max_loc = cv2.minMaxLoc(accum_smooth)
        if max_v > 0:
            best_bull = (int(max_loc[0] + roi_x1), int(max_loc[1] + roi_y1))
            self.last_bullseye = best_bull
            return best_bull

        # Fallback to cached or standard center
        if self.last_bullseye:
            return self.last_bullseye
        return (int(0.5 * w), int(0.45 * h))

    def get_target_roi(
        self, image: np.ndarray, bullseye: tuple[int, int], existing_calib: dict | None = None
    ) -> tuple[int, int, int, int]:
        """Calculate bounding box centered directly on the furthest double field.

        Once a target is identified via calibration or dynamic CV detection,
        the ROI is locked to remain static so the digital zoom magnifier does not wiggle.
        """
        if self.locked_target_roi is not None:
            return self.locked_target_roi

        h, w = image.shape[:2]
        bx, by = bullseye

        # 1. Calibration target prediction (from existing_calib or calibration.json homography)
        calib_target = None
        if existing_calib and "doubleOuters" in existing_calib:
            outers = existing_calib["doubleOuters"]
            if outers and len(outers) >= 20:
                top_pt = min(outers, key=lambda p: p[1])
                calib_target = (int(top_pt[0]), int(top_pt[1]))
        elif existing_calib and "homography" in existing_calib:
            try:
                h_mat = np.array(existing_calib["homography"]).reshape(3, 3)
                h_inv = np.linalg.inv(h_mat)
                angles = np.linspace(0, 2 * np.pi, 180, endpoint=False)
                canonical_pts = np.stack([
                    500.0 + 323.0 * np.cos(angles),
                    500.0 + 323.0 * np.sin(angles),
                    np.ones_like(angles)
                ], axis=0)
                proj_pts = h_inv @ canonical_pts
                u = proj_pts[0] / proj_pts[2]
                v = proj_pts[1] / proj_pts[2]
                w_depth = proj_pts[2]
                furthest_idx = np.argmax(w_depth)
                fx, fy = int(u[furthest_idx]), int(v[furthest_idx])
                if 0 < fx < w and 0 < fy < h:
                    calib_target = (fx, fy)
            except Exception:
                pass

        if calib_target is None:
            h_mat = self.load_calibration_homography(w, h)
            if h_mat is not None:
                try:
                    h_inv = np.linalg.inv(h_mat)
                    angles = np.linspace(0, 2 * np.pi, 180, endpoint=False)
                    canonical_pts = np.stack([
                        500.0 + 323.0 * np.cos(angles),
                        500.0 + 323.0 * np.sin(angles),
                        np.ones_like(angles)
                    ], axis=0)
                    proj_pts = h_inv @ canonical_pts
                    u = proj_pts[0] / proj_pts[2]
                    v = proj_pts[1] / proj_pts[2]
                    w_depth = proj_pts[2]
                    furthest_idx = np.argmax(w_depth)
                    fx, fy = int(u[furthest_idx]), int(v[furthest_idx])
                    if 0 < fx < w and 0 < fy < h:
                        calib_target = (fx, fy)
                except Exception:
                    pass

        # 2. Dynamic Color-Agnostic & Color-Enhanced CV Double Wire Detection
        cv_target = None
        try:
            min_dist = int(0.155 * h)
            max_dist = int(0.195 * h)

            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

            mask_r = cv2.inRange(hsv, (0, 60, 60), (14, 255, 255)) | cv2.inRange(hsv, (166, 60, 60), (180, 255, 255))
            mask_g = cv2.inRange(hsv, (35, 45, 45), (88, 255, 255))
            color_mask = mask_r | mask_g

            blurred = cv2.GaussianBlur(gray, (5, 5), 1.0)
            edges = cv2.Canny(blurred, 35, 100)

            candidates = []
            for bin_mask, is_color in [(color_mask, True), (edges, False)]:
                cnts, _ = cv2.findContours(bin_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                for c in cnts:
                    area = cv2.contourArea(c)
                    if (is_color and 50 < area < 4000) or (not is_color and 20 < area < 5000):
                        M = cv2.moments(c)
                        if M["m00"] > 0:
                            cx = M["m10"] / M["m00"]
                            cy = M["m01"] / M["m00"]
                            dist = math.hypot(cx - bx, cy - by)
                            if min_dist <= dist <= max_dist and cy < by:
                                dist_diff = abs(dist - 0.175 * h)
                                bonus = 300.0 if is_color else 0.0
                                score = bonus - dist_diff * 3.0 - abs(cx - bx) * 0.5 + (by - cy) * 0.2
                                candidates.append((score, (int(cx), int(cy))))

            if candidates:
                candidates.sort(key=lambda item: item[0], reverse=True)
                cv_target = candidates[0][1]
        except Exception:
            pass

        # 3. Target selection priority: Calibration > CV Contour > Geometry Fallback
        if calib_target:
            target_x, target_y = calib_target
        elif cv_target:
            target_x, target_y = cv_target
        else:
            exp_dist = int(0.175 * h)
            target_x = bx
            target_y = max(25, by - exp_dist)

        # Center the target ROI bounding box directly on the target coordinate
        roi_w = 160
        roi_h = 46
        x = max(0, min(w - roi_w, int(target_x - roi_w // 2)))
        y = max(0, min(h - roi_h, int(target_y - roi_h // 2)))
        roi = (x, y, roi_w, roi_h)
        self.last_target_roi = roi

        # Lock the ROI if it was identified by calibration or dynamic CV detection
        # to ensure the zoom window and HUD reticle stay completely static.
        if (cv_target is not None) or (calib_target is not None):
            self.locked_target_roi = roi

        return roi

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
