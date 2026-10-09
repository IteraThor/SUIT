"""Computer vision utilities and image filters for dartboard calibration.

Provides pure NumPy/OpenCV image transforms:
- Difference image subtraction & heatmapping for inserted dart testing
- Dart tip localization via morphological background subtraction
- Wire edge steepness contrast gradient maps
- Sensor high-frequency noise & grain amplification
- Peaking and HUD rendering for zoomed camera crops
"""

from __future__ import annotations
import math
from typing import Optional, Tuple
import cv2
import numpy as np

from core.board_detector import BoardROI


def apply_difference_filter(empty_frame: np.ndarray, dart_frame: np.ndarray) -> np.ndarray:
    """Isolate dart difference signal and colorize with high-contrast heatmap."""
    if empty_frame is None or dart_frame is None or empty_frame.size == 0 or dart_frame.size == 0:
        return dart_frame if dart_frame is not None else empty_frame

    if empty_frame.shape[:2] != dart_frame.shape[:2]:
        empty_frame = cv2.resize(empty_frame, (dart_frame.shape[1], dart_frame.shape[0]))

    g_empty = cv2.cvtColor(empty_frame, cv2.COLOR_BGR2GRAY) if len(empty_frame.shape) == 3 else empty_frame
    g_dart = cv2.cvtColor(dart_frame, cv2.COLOR_BGR2GRAY) if len(dart_frame.shape) == 3 else dart_frame

    diff = cv2.absdiff(g_empty, g_dart)
    amplified = np.clip(diff.astype(np.float32) * 3.5, 0, 255).astype(np.uint8)
    heatmap = cv2.applyColorMap(amplified, cv2.COLORMAP_TURBO)

    dart_rgb = cv2.cvtColor(g_dart, cv2.COLOR_GRAY2BGR)
    blended = cv2.addWeighted(heatmap, 0.65, dart_rgb, 0.35, 0)
    return blended


def detect_dart_tip(
    empty_frame: np.ndarray,
    dart_frame: np.ndarray,
    roi: Optional[BoardROI] = None,
) -> Optional[Tuple[int, int]]:
    """Locate the exact pixel coordinate of the inserted dart tip using background subtraction."""
    if empty_frame is None or dart_frame is None or empty_frame.size == 0 or dart_frame.size == 0:
        return None

    if empty_frame.shape[:2] != dart_frame.shape[:2]:
        empty_frame = cv2.resize(empty_frame, (dart_frame.shape[1], dart_frame.shape[0]))

    g_empty = cv2.cvtColor(empty_frame, cv2.COLOR_BGR2GRAY) if len(empty_frame.shape) == 3 else empty_frame
    g_dart = cv2.cvtColor(dart_frame, cv2.COLOR_BGR2GRAY) if len(dart_frame.shape) == 3 else dart_frame

    diff = cv2.absdiff(g_empty, g_dart)
    if roi and roi.board_mask is not None:
        diff[roi.board_mask == 0] = 0

    _, thresh = cv2.threshold(diff, 18, 255, cv2.THRESH_BINARY)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)
    cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    valid_contours = [c for c in contours if cv2.contourArea(c) > 60]
    if not valid_contours:
        return None

    largest_cnt = max(valid_contours, key=cv2.contourArea)
    pts = largest_cnt.reshape(-1, 2)

    if roi and roi.center:
        bcx, bcy = roi.center
        tip_pt = min(pts, key=lambda p: (p[0] - bcx) ** 2 + (p[1] - bcy) ** 2)
        return (int(tip_pt[0]), int(tip_pt[1]))
    else:
        M = cv2.moments(largest_cnt)
        if M["m00"] > 0:
            return (int(M["m10"] / M["m00"]), int(M["m01"] / M["m00"]))
        return (int(pts[0][0]), int(pts[0][1]))


def apply_edge_contrast_filter(frame: np.ndarray) -> np.ndarray:
    """Generate colorized Sobel edge gradient map highlighting wire steepness."""
    if frame is None or frame.size == 0:
        return frame
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    mag = np.clip(mag * 2.2, 0, 255).astype(np.uint8)
    return cv2.applyColorMap(mag, cv2.COLORMAP_VIRIDIS)


def apply_noise_inspector_filter(frame: np.ndarray) -> np.ndarray:
    """Isolate high-frequency sensor grain / noise by subtracting low-pass structure and amplifying 7x."""
    if frame is None or frame.size == 0:
        return frame
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
    blurred = cv2.GaussianBlur(gray, (5, 5), 1.2)
    residual = gray.astype(np.float32) - blurred.astype(np.float32)
    amplified = np.clip(128.0 + residual * 7.0, 0, 255).astype(np.uint8)
    return cv2.cvtColor(amplified, cv2.COLOR_GRAY2BGR)


def process_zoom_crop_suit(
    frame: np.ndarray,
    target_roi: Tuple[int, int, int, int],
    accum_crop: Optional[np.ndarray] = None,
    zoom_factor: float = 2.5,
    out_size: Tuple[int, int] = (440, 275),
    score: float = 0.0,
) -> Tuple[np.ndarray, np.ndarray]:
    """Extract digital zoom crop with temporal smoothing, neon-green wire focus peaking, and reticle."""
    fh, fw = frame.shape[:2]
    tx, ty, tw, th = target_roi
    cx = tx + tw // 2 if (tw > 0 and th > 0) else fw // 2
    cy = ty + th // 2 if (tw > 0 and th > 0) else fh // 4

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
        return frame, crop

    # Temporal noise rejection smoothing
    if accum_crop is None or accum_crop.shape != crop.shape:
        accum_crop = crop.astype(np.float32)
    else:
        accum_crop = cv2.addWeighted(crop.astype(np.float32), 0.35, accum_crop, 0.65, 0)
    clean_crop = np.clip(accum_crop, 0, 255).astype(np.uint8)

    # Focus peaking edge highlights
    gray_crop = cv2.cvtColor(clean_crop, cv2.COLOR_BGR2GRAY)
    gx = cv2.Sobel(gray_crop, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray_crop, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    peak_mask = mag > 250.0

    if np.any(peak_mask):
        peaked_crop = clean_crop.copy()
        peaked_crop[peak_mask] = cv2.addWeighted(
            clean_crop[peak_mask], 0.35, np.full_like(clean_crop[peak_mask], [40, 255, 60]), 0.65, 0
        )
        render_crop = peaked_crop
    else:
        render_crop = clean_crop

    resized = cv2.resize(render_crop, out_size, interpolation=cv2.INTER_LINEAR)

    # Crosshair reticle
    rw, rh = out_size
    rcx, rcy = rw // 2, rh // 2
    cv2.drawMarker(resized, (rcx, rcy), (18, 20, 24), markerType=cv2.MARKER_CROSS, markerSize=24, thickness=3, line_type=cv2.LINE_AA)
    cv2.drawMarker(resized, (rcx, rcy), (0, 255, 200), markerType=cv2.MARKER_CROSS, markerSize=22, thickness=1, line_type=cv2.LINE_AA)

    return resized, accum_crop


def draw_sweet_spot_overlay_suit(
    frame: np.ndarray,
    target_roi: Tuple[int, int, int, int],
    score: float,
) -> np.ndarray:
    """Draw SUIT-style Sweet-Spot Target Box and Reticle on the full frame."""
    out = frame.copy()
    tx, ty, tw, th = target_roi
    is_locked = score >= 85.0

    if is_locked:
        color = (46, 194, 78)  # Emerald green
        thickness = 3
    elif score >= 55.0:
        color = (10, 165, 229)  # Amber
        thickness = 2
    else:
        color = (229, 132, 53)  # Blue
        thickness = 2

    # Bounding box
    cv2.rectangle(out, (tx, ty), (tx + tw, ty + th), color, thickness)

    # Reticle crosshairs
    tcx, tcy = tx + tw // 2, ty + th // 2
    cv2.line(out, (tcx - 16, tcy), (tcx + 16, tcy), color, 1)
    cv2.line(out, (tcx, tcy - 12), (tcx, tcy + 12), color, 1)

    # Label box
    lbl = "SWEET SPOT" if is_locked else f"TARGET ({score:.0f}%)"
    font = cv2.FONT_HERSHEY_SIMPLEX
    (tw_t, th_t), _ = cv2.getTextSize(lbl, font, 0.45, 1)
    ly1 = max(0, ty - th_t - 8)
    ly2 = ty
    lx1 = tx
    lx2 = tx + tw_t + 12
    cv2.rectangle(out, (lx1, ly1), (lx2, ly2), (18, 20, 24), -1)
    cv2.putText(out, lbl, (lx1 + 6, ly2 - 4), font, 0.45, color, 1, cv2.LINE_AA)

    return out

