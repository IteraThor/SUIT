"""Automated Dartboard ROI and segment wire detector.

Extracts wire contour masks for edge steepness and uniform patch masks for sensor noise estimation.
Uses Autodarts ground-truth geometry when available with a robust radial-ray CV fallback.
"""

from __future__ import annotations
import dataclasses
import json
import logging
import math
import os
from pathlib import Path
import re
import tomllib
from typing import Dict, List, Optional, Tuple, Union
import cv2
import numpy as np

logger = logging.getLogger("cam_librator.detector")


@dataclasses.dataclass
class BoardROI:
    center: Tuple[int, int]
    radius: int
    board_mask: np.ndarray  # uint8 binary mask of board
    wire_mask: np.ndarray   # uint8 binary mask of wire edges
    uniform_mask: np.ndarray  # uint8 binary mask of flat segment areas
    confidence: float
    target_roi: Optional[Tuple[int, int, int, int]] = None  # (x, y, w, h) bounding box of furthest double


class BoardDetector:
    """Detects dartboard position and extracts wire and uniform measurement masks."""

    def __init__(
        self,
        autodarts_config_path: Optional[Path] = None,
        autodarts_calibration_path: Optional[Path] = None,
    ):
        self.config_path = autodarts_config_path or (Path.home() / ".config" / "autodarts" / "config.toml")
        self.calibration_path = autodarts_calibration_path or (Path.home() / ".config" / "autodarts" / "calibration.json")
        self.locked_rois: Dict[str, Tuple[int, int, int, int]] = {}

    def load_calibration_homography(
        self,
        width: int,
        height: int,
        dev_node: Optional[str] = None,
        by_path: Optional[str] = None,
        cam_idx: Optional[int] = None,
    ) -> Optional[np.ndarray]:
        """Load 3x3 homography matrix from ~/.config/autodarts/calibration.json."""
        if not self.calibration_path.exists():
            return None

        try:
            cal_data = json.loads(self.calibration_path.read_text(encoding="utf-8"))
            cams = cal_data.get("cameras", {})
            if not cams:
                return None

            target_key = None

            # 1. Match dev_node / by_path against devices in config.toml
            if (dev_node or by_path) and self.config_path.exists():
                try:
                    with open(self.config_path, "rb") as f:
                        cfg_data = tomllib.load(f)
                    cam_section = cfg_data.get("cam", {})
                    devices = cam_section.get("devices") or cam_section.get("cams", [])

                    norm_dev = os.path.realpath(dev_node) if dev_node and os.path.exists(dev_node) else (dev_node or "")

                    for raw_d in devices:
                        if not raw_d:
                            continue
                        is_match = False
                        match = re.search(r"(?:native=)?(/dev/video\d+|/dev/v4l/[^\s&]+)", raw_d)
                        cand = match.group(1) if match else None
                        if cand:
                            cand_real = os.path.realpath(cand) if os.path.exists(cand) else cand
                            if norm_dev and (cand_real == norm_dev or cand == dev_node):
                                is_match = True

                        if not is_match and by_path:
                            loc_m = re.search(r"[?&]location=([^&]+)", raw_d)
                            if loc_m and loc_m.group(1) in by_path:
                                is_match = True

                        if is_match:
                            for k in cams.keys():
                                if k in raw_d or raw_d in k:
                                    target_key = k
                                    break
                            if target_key:
                                break
                except Exception:
                    pass

            # 2. Match directly against keys in calibration.json
            if not target_key and dev_node:
                norm_dev = os.path.realpath(dev_node) if os.path.exists(dev_node) else dev_node
                for k in cams.keys():
                    if k == dev_node or k == norm_dev or (by_path and k in by_path):
                        target_key = k
                        break

            # 3. Match by numeric index (e.g. cam_idx 1, 2, 3 -> index 0, 1, 2)
            if not target_key:
                cam_keys = list(cams.keys())
                idx = None
                if cam_idx is not None and 1 <= cam_idx <= len(cam_keys):
                    idx = cam_idx - 1
                elif dev_node:
                    match = re.search(r"video(\d+)", dev_node)
                    if match:
                        v_num = int(match.group(1))
                        calc_idx = v_num // 2
                        if calc_idx < len(cam_keys):
                            idx = calc_idx

                if idx is not None and 0 <= idx < len(cam_keys):
                    target_key = cam_keys[idx]

            if not target_key:
                return None

            cam_entry = cams.get(target_key, {})
            res_key = f"{width}x{height}"
            if res_key in cam_entry and "homography" in cam_entry[res_key]:
                return np.array(cam_entry[res_key]["homography"]).reshape(3, 3)

            for entry_val in cam_entry.values():
                if isinstance(entry_val, dict) and "homography" in entry_val:
                    return np.array(entry_val["homography"]).reshape(3, 3)

        except Exception as e:
            logger.debug("Failed loading calibration homography: %s", e)

        return None

    def _load_autodarts_geometry(
        self,
        frame_shape: Tuple[int, int],
        dev_node: Optional[str] = None,
        by_path: Optional[str] = None,
        cam_idx: Optional[int] = None,
    ) -> Optional[Tuple[Tuple[int, int], np.ndarray]]:
        """Load ground-truth dartboard geometry from Autodarts calibration.json or config.toml."""
        h, w = frame_shape[:2]

        # 1. Try modern Autodarts v2 homography from calibration.json
        h_mat = self.load_calibration_homography(w, h, dev_node=dev_node, by_path=by_path, cam_idx=cam_idx)
        if h_mat is not None:
            try:
                h_inv = np.linalg.inv(h_mat)
                b = h_inv @ np.array([500.0, 500.0, 1.0])
                scaled_bull = (int(b[0] / b[2]), int(b[1] / b[2]))

                angles = [math.radians(90 - i * 18) for i in range(20)]
                pts = []
                for a in angles:
                    p = h_inv @ np.array([500.0 + 323.0 * math.cos(a), 500.0 + 323.0 * math.sin(a), 1.0])
                    pts.append([int(p[0] / p[2]), int(p[1] / p[2])])

                scaled_outers = np.array(pts, dtype=np.int32)
                return scaled_bull, scaled_outers
            except Exception as e:
                logger.debug("Failed computing geometry from homography: %s", e)

        # 2. Try legacy config.toml double_outers
        if self.config_path.exists():
            try:
                with open(self.config_path, "rb") as f:
                    data = tomllib.load(f)

                cams = data.get("cam", {}).get("cams", ["/dev/video0", "/dev/video2", "/dev/video4"])
                c_idx = "0"
                if dev_node and dev_node in cams:
                    c_idx = str(cams.index(dev_node))
                elif cam_idx is not None:
                    c_idx = str(cam_idx - 1)

                db = data.get("dartboard", {}).get(c_idx, {})
                outers = db.get("double_outers", [])
                bull = db.get("bull", [640, 480])

                if len(outers) == 20:
                    scale_x = w / 1280.0
                    scale_y = h / 960.0

                    pts = np.array(outers, dtype=np.float32)
                    pts[:, 0] *= scale_x
                    pts[:, 1] *= scale_y
                    scaled_outers = np.round(pts).astype(np.int32)
                    scaled_bull = (int(bull[0] * scale_x), int(bull[1] * scale_y))
                    return scaled_bull, scaled_outers
            except Exception as e:
                logger.debug("Could not parse legacy Autodarts config: %s", e)

        return None

    def locate_bullseye(
        self,
        frame: Optional[np.ndarray] = None,
        dev_node: Optional[str] = None,
        by_path: Optional[str] = None,
        cam_idx: Optional[int] = None,
        frame_shape: Optional[Tuple[int, int]] = None,
    ) -> Tuple[int, int]:
        """Locate Bullseye coordinates using concentric red/green geometry or calibration."""
        if frame is not None:
            h, w = frame.shape[:2]
        elif frame_shape is not None:
            h, w = frame_shape[:2]
        else:
            h, w = 960, 1280

        # 1. Concentric red/green bullseye in central region of frame
        if frame is not None and frame.size > 0:
            roi_y1, roi_y2 = int(0.25 * h), int(0.58 * h)
            roi_x1, roi_x2 = int(0.32 * w), int(0.68 * w)
            crop = frame[roi_y1:roi_y2, roi_x1:roi_x2]
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)

            mask_red1 = cv2.inRange(hsv, (0, 70, 70), (14, 255, 255))
            mask_red2 = cv2.inRange(hsv, (166, 70, 70), (180, 255, 255))
            mask_red = mask_red1 | mask_red2
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
                return best_bull

        # 2. Calibration homography bullseye
        h_mat = self.load_calibration_homography(w, h, dev_node=dev_node, by_path=by_path, cam_idx=cam_idx)
        if h_mat is not None:
            try:
                h_inv = np.linalg.inv(h_mat)
                b = h_inv @ np.array([500.0, 500.0, 1.0])
                calib_bull = (int(b[0] / b[2]), int(b[1] / b[2]))
                if 0 < calib_bull[0] < w and 0 < calib_bull[1] < h:
                    return calib_bull
            except Exception:
                pass

        # 3. Geometric fallback
        return (int(0.5 * w), int(0.45 * h))

    def get_furthest_double_roi(
        self,
        frame_or_shape: Union[np.ndarray, Tuple[int, int]],
        dev_node: Optional[str] = None,
        by_path: Optional[str] = None,
        cam_idx: Optional[int] = None,
        center: Optional[Tuple[int, int]] = None,
        radius: Optional[int] = None,
    ) -> Tuple[int, int, int, int]:
        """Calculate bounding box (x, y, w, h) centered directly on the furthest double field.

        Identical to SUIT's optical target finder:
        Uses calibration homography prediction, refined by dynamic CV contour
        detection on the double segment wire, and locks the ROI to keep preview static.
        """
        key = dev_node or (f"cam_{cam_idx}" if cam_idx else None)
        if key and key in self.locked_rois:
            return self.locked_rois[key]

        if isinstance(frame_or_shape, np.ndarray):
            h, w = frame_or_shape.shape[:2]
            frame = frame_or_shape
        else:
            h, w = frame_or_shape[:2]
            frame = None

        # 1. Bullseye coordinates
        if center is not None:
            bx, by = center
        else:
            bx, by = self.locate_bullseye(
                frame=frame, dev_node=dev_node, by_path=by_path, cam_idx=cam_idx, frame_shape=(h, w)
            )

        # 2. Calibration target prediction
        calib_target = None
        h_mat = self.load_calibration_homography(w, h, dev_node=dev_node, by_path=by_path, cam_idx=cam_idx)
        if h_mat is not None:
            try:
                h_inv = np.linalg.inv(h_mat)
                pts = [
                    (h_inv @ np.array([500.0 + 323.0 * math.cos(math.radians(deg)),
                                      500.0 + 323.0 * math.sin(math.radians(deg)), 1.0]))
                    for deg in range(0, 360, 2)
                ]
                norm_pts = [(p[0] / p[2], p[1] / p[2]) for p in pts]
                top_pt = min(norm_pts, key=lambda p: p[1])
                calib_target = (int(top_pt[0]), int(top_pt[1]))
            except Exception:
                pass

        if calib_target is None:
            geo = self._load_autodarts_geometry((h, w), dev_node=dev_node, by_path=by_path, cam_idx=cam_idx)
            if geo:
                _, outers = geo
                top_pt = min(outers, key=lambda p: p[1])
                calib_target = (int(top_pt[0]), int(top_pt[1]))

        # 3. Dynamic CV contour detection on actual colored double field wire/segment
        cv_target = None
        if frame is not None and frame.size > 0:
            try:
                hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                mask_r = cv2.inRange(hsv, (0, 70, 70), (14, 255, 255)) | cv2.inRange(hsv, (166, 70, 70), (180, 255, 255))
                mask_g = cv2.inRange(hsv, (35, 60, 60), (88, 255, 255))

                mask_upper = np.zeros_like(mask_r)
                mask_upper[:by, :] = 255
                search_mask = cv2.bitwise_and(mask_r | mask_g, mask_upper)

                cnts, _ = cv2.findContours(search_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                min_dist = 0.150 * h
                max_dist = 0.210 * h

                candidates = []
                for c in cnts:
                    area = cv2.contourArea(c)
                    if 80 < area < 3500:
                        M = cv2.moments(c)
                        if M["m00"] > 0:
                            cx_c = M["m10"] / M["m00"]
                            cy_c = M["m01"] / M["m00"]
                            dist = math.hypot(cx_c - bx, cy_c - by)
                            if min_dist <= dist <= max_dist and cy_c < (by - 50):
                                candidates.append((cy_c, cx_c, dist, area))

                if candidates:
                    if calib_target:
                        candidates.sort(key=lambda item: math.hypot(item[1] - calib_target[0], item[0] - calib_target[1]))
                    else:
                        candidates.sort(key=lambda item: item[0])
                    cv_target = (int(candidates[0][1]), int(candidates[0][0]))
            except Exception:
                pass

        # 4. Target selection with priority: CV detected contour > Calibration target > Geometric fallback
        if cv_target:
            target_x, target_y = cv_target
        elif calib_target:
            target_x, target_y = calib_target
        else:
            exp_dist = int(0.187 * h)
            target_x = bx
            target_y = max(25, by - exp_dist)

        # Center target ROI bounding box directly on target coordinate (SUIT standard 160x46)
        roi_w = 160
        roi_h = 46
        x = max(0, min(w - roi_w, int(target_x - roi_w // 2)))
        y = max(0, min(h - roi_h, int(target_y - roi_h // 2)))
        roi = (x, y, roi_w, roi_h)

        if (cv_target is not None or calib_target is not None) and key:
            self.locked_rois[key] = roi

        return roi

    def unlock_target(self, dev_node: Optional[str] = None):
        """Reset locked target ROI so furthest double can be re-detected."""
        if dev_node:
            self.locked_rois.pop(dev_node, None)
        else:
            self.locked_rois.clear()

    def _find_bullseye(self, frame: np.ndarray) -> Optional[Tuple[int, int, float]]:
        """Locate the concentric red/green bullseye in the central region."""
        h, w = frame.shape[:2]
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        # Red inner bull mask
        mask_r1 = cv2.inRange(hsv, np.array([0, 60, 40]), np.array([12, 255, 255]))
        mask_r2 = cv2.inRange(hsv, np.array([168, 60, 40]), np.array([180, 255, 255]))
        mask_red = cv2.bitwise_or(mask_r1, mask_r2)

        # Green outer bull mask
        mask_green = cv2.inRange(hsv, np.array([35, 50, 40]), np.array([85, 255, 255]))

        # Search in center 50% of frame
        cx_min, cx_max = int(w * 0.25), int(w * 0.75)
        cy_min, cy_max = int(h * 0.25), int(h * 0.75)
        center_mask = np.zeros((h, w), dtype=np.uint8)
        cv2.rectangle(center_mask, (cx_min, cy_min), (cx_max, cy_max), 255, -1)

        mask_red_center = cv2.bitwise_and(mask_red, center_mask)
        cnts, _ = cv2.findContours(mask_red_center, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        best_bull = None
        best_score = -1.0

        for c in cnts:
            area = cv2.contourArea(c)
            if 30 < area < 2500:
                (cx, cy), radius = cv2.minEnclosingCircle(c)
                if not (6 <= radius <= 30):
                    continue

                # Verify outer green ring surrounding this red blob
                green_crop = cv2.bitwise_and(mask_green, mask_green, mask=center_mask)
                r_int = int(radius)
                circle_mask = np.zeros((h, w), dtype=np.uint8)
                cv2.circle(circle_mask, (int(cx), int(cy)), r_int * 3, 255, -1)
                cv2.circle(circle_mask, (int(cx), int(cy)), r_int + 2, 0, -1)

                green_overlap = cv2.countNonZero(cv2.bitwise_and(green_crop, circle_mask))
                score = area + (green_overlap * 2.0)

                if score > best_score:
                    best_score = score
                    best_bull = (int(cx), int(cy), radius)

        return best_bull

    def _detect_board_circle_from_rays(
        self,
        gray: np.ndarray,
        center: Tuple[int, int],
        min_radius: int,
        max_radius: int,
        num_rays: int = 36,
    ) -> Tuple[int, float]:
        """Trace radial scanlines outward from bullseye to find outer wire boundary."""
        h, w = gray.shape[:2]
        cx, cy = center
        detected_radii: List[float] = []

        # Compute vertical and horizontal Sobel gradients
        gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        grad_mag = cv2.magnitude(gx, gy)

        angles = np.linspace(0, 2 * np.pi, num_rays, endpoint=False)

        for angle in angles:
            cos_a = np.cos(angle)
            sin_a = np.sin(angle)

            r_samples = np.arange(min_radius, max_radius, 2)
            x_samples = np.round(cx + r_samples * cos_a).astype(int)
            y_samples = np.round(cy + r_samples * sin_a).astype(int)

            # Valid in-bounds mask
            valid = (x_samples >= 0) & (x_samples < w) & (y_samples >= 0) & (y_samples < h)
            if not np.any(valid):
                continue

            x_valid = x_samples[valid]
            y_valid = y_samples[valid]
            r_valid = r_samples[valid]

            grads = grad_mag[y_valid, x_valid]
            if len(grads) < 5:
                continue

            # Find outer edge peak
            peak_idx = np.argmax(grads)
            if grads[peak_idx] > 25.0:
                detected_radii.append(float(r_valid[peak_idx]))

        if len(detected_radii) < 8:
            return int((min_radius + max_radius) / 2), 0.3

        # Filter statistical outliers
        median_r = float(np.median(detected_radii))
        inliers = [r for r in detected_radii if abs(r - median_r) < 30.0]

        if not inliers:
            return int(median_r), 0.4

        final_r = int(np.mean(inliers))
        confidence = float(len(inliers) / num_rays)
        return final_r, min(1.0, confidence * 1.3)

    def detect_board(self, frame: np.ndarray, dev_node: Optional[str] = None) -> BoardROI:
        """Execute ground-truth or radial detection to compute complete board ROI."""
        h, w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)

        # 1. Check Autodarts Ground Truth
        auto_geo = self._load_autodarts_geometry(frame.shape, dev_node=dev_node)
        board_mask = np.zeros((h, w), dtype=np.uint8)

        if auto_geo is not None:
            center, outers = auto_geo
            cv2.fillPoly(board_mask, [outers], 255)
            # Approximate radius from polygon bounding box
            _, _, bw, bh = cv2.boundingRect(outers)
            radius = int(max(bw, bh) / 2)
            confidence = 1.0
        else:
            # 2. Fallback: Computer Vision Detection
            bull_res = self._find_bullseye(frame)
            if bull_res is not None:
                bx, by, r_bull = bull_res
                center = (bx, by)
                min_r = int(min(w, h) * 0.22)
                max_r = int(min(w, h) * 0.48)
                radius, conf = self._detect_board_circle_from_rays(gray, center, min_r, max_r)
                cv2.circle(board_mask, center, radius, 255, -1)
                confidence = conf
            else:
                # 2b. Contour fallback for high-contrast circular boards without standard colored bullseye
                thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
                cnts, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                best_circle = None
                best_area = 0.0
                min_area = (w * h) * 0.05
                for c in cnts:
                    area = cv2.contourArea(c)
                    if area > min_area:
                        (cx, cy), cr = cv2.minEnclosingCircle(c)
                        if 0.15 * min(w, h) <= cr <= 0.60 * min(w, h):
                            if area > best_area:
                                best_area = area
                                best_circle = ((int(cx), int(cy)), int(cr))

                if best_circle is not None:
                    center, radius = best_circle
                    cv2.circle(board_mask, center, radius, 255, -1)
                    confidence = 0.60
                else:
                    center = (int(w / 2), int(h * 0.45))
                    radius = int(min(w, h) * 0.35)
                    cv2.circle(board_mask, center, radius, 255, -1)
                    confidence = 0.25

        # Generate Wire Mask: Canny edges strictly within playable board mask (including outer double wire boundary)
        edges = cv2.Canny(blurred, 40, 120)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        dilated_board = cv2.dilate(board_mask, kernel, iterations=2)
        wire_mask = cv2.bitwise_and(edges, edges, mask=dilated_board)
        wire_mask_dilated = cv2.dilate(wire_mask, kernel, iterations=1)

        # Generate Uniform Segment Mask: Inside board, excluding wires and center bull
        uniform_mask = cv2.bitwise_and(board_mask, cv2.bitwise_not(wire_mask_dilated))
        eroded_board = cv2.erode(board_mask, kernel, iterations=2)
        uniform_mask = cv2.bitwise_and(uniform_mask, eroded_board)

        bull_mask = np.zeros((h, w), dtype=np.uint8)
        cv2.circle(bull_mask, center, int(radius * 0.12), 255, -1)
        uniform_mask = cv2.bitwise_and(uniform_mask, cv2.bitwise_not(bull_mask))

        target_roi = self.get_furthest_double_roi(frame, dev_node=dev_node, center=center, radius=radius)

        return BoardROI(
            center=center,
            radius=radius,
            board_mask=board_mask,
            wire_mask=wire_mask_dilated,
            uniform_mask=uniform_mask,
            confidence=confidence,
            target_roi=target_roi,
        )

    def create_roi_overlay(self, frame: np.ndarray, roi: BoardROI) -> np.ndarray:
        """Render a semi-transparent visual overlay showing wire edge and furthest double focus target."""
        overlay = frame.copy()
        h, w = frame.shape[:2]

        # 1. Dim irrelevant background outside board
        outside_mask = roi.board_mask == 0
        overlay[outside_mask] = (overlay[outside_mask] * 0.30).astype(np.uint8)

        # 2. Highlight uniform segment sampling areas in subtle blue
        uniform_pixels = roi.uniform_mask > 0
        overlay[uniform_pixels] = cv2.addWeighted(
            overlay[uniform_pixels], 0.75,
            np.full_like(overlay[uniform_pixels], (180, 100, 30)), 0.25, 0
        )

        # 3. Highlight wire edge sampling areas in bright cyan
        wire_pixels = roi.wire_mask > 0
        overlay[wire_pixels] = (0, 255, 200)

        # 4. Draw outer board ring contour and center marker
        contours, _ = cv2.findContours(roi.board_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(overlay, contours, -1, (0, 240, 255), 2, cv2.LINE_AA)
        cv2.drawMarker(overlay, roi.center, (0, 255, 255), markerType=cv2.MARKER_CROSS, markerSize=16, thickness=2)

        # 5. Draw Furthest Double Target Reticle (Sweet Spot) in bright neon green / amber
        if roi.target_roi:
            tx, ty, tw, th = roi.target_roi
            # Rounded target box
            cv2.rectangle(overlay, (tx, ty), (tx + tw, ty + th), (0, 255, 120), 2, cv2.LINE_AA)
            # Crosshair on center of furthest double
            fcx, fcy = tx + tw // 2, ty + th // 2
            cv2.drawMarker(overlay, (fcx, fcy), (0, 255, 120), markerType=cv2.MARKER_CROSS, markerSize=18, thickness=2)
            cv2.putText(overlay, "FOCUS TARGET (FURTHEST DOUBLE)", (tx, max(20, ty - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.50, (0, 255, 120), 2, cv2.LINE_AA)

        # 6. Add Legend Text
        src = "Autodarts Geometry" if roi.confidence >= 0.99 else "CV Radial Detection"
        cv2.putText(overlay, f"FOCUS MEASUREMENT [{src}]: Green = Focus Target (Double) | Cyan = Wires", (24, h - 24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.60, (0, 255, 255), 2, cv2.LINE_AA)

        return overlay
