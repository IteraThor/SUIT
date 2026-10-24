"""Board geometry: Autodarts' homography (image -> 1000x1000 board view) and board-area masks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

AUTODARTS_CALIBRATION = Path.home() / ".config" / "autodarts" / "calibration.json"
BOARD_SIZE = 1000
BOARD_CENTER = (500.0, 500.0)
# Outer edge of the double ring in the board view, measured on the warped image (px).
DOUBLE_OUTER_R = 335.0
# Board area that matters for scoring: playing field plus a margin for darts hitting the double.
BOARD_R = 345.0


def port_of(device: str) -> str:
    """'/dev/v4l/by-path/pci-0000:00:14.0-usb-0:1:1.0-video-index0' -> 'pci-0000:00:14.0-usb-0:1:1.0'."""
    name = Path(device).name
    return name.split("-video-index")[0]


def load_homography(device: str, width: int, height: int, path: Path = AUTODARTS_CALIBRATION) -> Optional[np.ndarray]:
    """Autodarts' image->board homography for the camera on `device` (by-path) at this resolution."""
    try:
        cal = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    port = port_of(device)
    for key, cam in cal.get("cameras", {}).items():
        if key.endswith("location=" + port):
            entry = cam.get(f"{width}x{height}")
            if entry and "homography" in entry:
                return np.array(entry["homography"], dtype=np.float64).reshape(3, 3)
    return None


def warp(img: np.ndarray, H: np.ndarray, interp: int = cv2.INTER_LINEAR) -> np.ndarray:
    return cv2.warpPerspective(img, H, (BOARD_SIZE, BOARD_SIZE), flags=interp)


def board_mask_view(r: float = BOARD_R) -> np.ndarray:
    """Boolean mask of the board area in the 1000x1000 board view."""
    yy, xx = np.mgrid[0:BOARD_SIZE, 0:BOARD_SIZE]
    return (xx - BOARD_CENTER[0]) ** 2 + (yy - BOARD_CENTER[1]) ** 2 <= r * r


def board_mask_image(H: np.ndarray, shape: tuple, r: float = BOARD_R) -> np.ndarray:
    """Boolean mask of the board area in camera-image coordinates (for native-resolution metrics)."""
    m = board_mask_view(r).astype(np.uint8) * 255
    back = cv2.warpPerspective(m, np.linalg.inv(H), (shape[1], shape[0]), flags=cv2.INTER_NEAREST)
    return back > 0
