"""GTK4 and Libadwaita Pixbuf conversion utilities for OpenCV frame buffers."""

from __future__ import annotations
from typing import Optional, Tuple
import cv2
import numpy as np

import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib

from core.cv_utils import (
    apply_difference_filter,
    apply_edge_contrast_filter,
    apply_noise_inspector_filter,
)


def frame_to_pixbuf(
    frame: np.ndarray,
    zoom_factor: float = 1.0,
    center_x: Optional[int] = None,
    center_y: Optional[int] = None,
    max_w: int = 560,
    max_h: int = 380,
) -> Optional[GdkPixbuf.Pixbuf]:
    """Convert an OpenCV BGR frame to a scaled GdkPixbuf with optional ROI zoom."""
    if frame is None or frame.size == 0:
        return None
    h, w = frame.shape[:2]

    if zoom_factor > 1.05 and center_x is not None and center_y is not None:
        crop_w = int(w / zoom_factor)
        crop_h = int(h / zoom_factor)
        x1 = max(0, min(w - crop_w, int(center_x - crop_w / 2)))
        y1 = max(0, min(h - crop_h, int(center_y - crop_h / 2)))
        x2 = min(w, x1 + crop_w)
        y2 = min(h, y1 + crop_h)
        cropped = frame[y1:y2, x1:x2]
    else:
        cropped = frame

    ch, cw = cropped.shape[:2]
    scale = min(max_w / cw, max_h / ch)
    nw, nh = max(1, int(cw * scale)), max(1, int(ch * scale))
    resized = cv2.resize(cropped, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(rgb.tobytes()),
        GdkPixbuf.Colorspace.RGB,
        False,
        8,
        nw,
        nh,
        nw * 3,
    )


def extract_magnified_pixbuf(
    frame: np.ndarray,
    center: Optional[Tuple[int, int]] = None,
    zoom_factor: float = 2.5,
    max_w: int = 560,
    max_h: int = 380,
    filter_mode: str = "normal",
    show_reticle: bool = True,
    diff_reference_frame: Optional[np.ndarray] = None,
) -> Tuple[Optional[GdkPixbuf.Pixbuf], Tuple[int, int, int, int]]:
    """Extract a magnified ROI crop, apply optional filter, and convert to Pixbuf.

    Returns:
        (pixbuf, (crop_x1, crop_y1, crop_w, crop_h)) in original frame coordinates.
    """
    if frame is None or frame.size == 0:
        return None, (0, 0, 0, 0)
    fh, fw = frame.shape[:2]

    if zoom_factor > 1.05 and center is not None:
        crop_w = max(20, int(fw / zoom_factor))
        crop_h = max(20, int(fh / zoom_factor))
        cx, cy = center
        x1 = max(0, min(fw - crop_w, int(cx - crop_w / 2)))
        y1 = max(0, min(fh - crop_h, int(cy - crop_h / 2)))
        x2 = min(fw, x1 + crop_w)
        y2 = min(fh, y1 + crop_h)
        cropped = frame[y1:y2, x1:x2].copy()
        diff_ref_cropped = (
            diff_reference_frame[y1:y2, x1:x2].copy()
            if (diff_reference_frame is not None and diff_reference_frame.size > 0)
            else None
        )
    else:
        x1, y1, crop_w, crop_h = 0, 0, fw, fh
        cropped = frame.copy()
        diff_ref_cropped = (
            diff_reference_frame.copy()
            if (diff_reference_frame is not None and diff_reference_frame.size > 0)
            else None
        )

    if filter_mode == "diff" and diff_ref_cropped is not None:
        filtered = apply_difference_filter(diff_ref_cropped, cropped)
    elif filter_mode == "edge":
        filtered = apply_edge_contrast_filter(cropped)
    elif filter_mode == "noise":
        filtered = apply_noise_inspector_filter(cropped)
    else:
        filtered = cropped

    ch, cw = filtered.shape[:2]
    scale = min(max_w / cw, max_h / ch)
    nw, nh = max(1, int(cw * scale)), max(1, int(ch * scale))
    resized = cv2.resize(filtered, (nw, nh), interpolation=cv2.INTER_LINEAR if zoom_factor > 1.05 else cv2.INTER_AREA)

    if show_reticle and zoom_factor > 1.05:
        rcx, rcy = nw // 2, nh // 2
        cv2.drawMarker(resized, (rcx, rcy), (18, 20, 24), markerType=cv2.MARKER_CROSS, markerSize=20, thickness=3, line_type=cv2.LINE_AA)
        cv2.drawMarker(resized, (rcx, rcy), (0, 255, 200), markerType=cv2.MARKER_CROSS, markerSize=18, thickness=1, line_type=cv2.LINE_AA)

    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
    pix = GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(rgb.tobytes()),
        GdkPixbuf.Colorspace.RGB,
        False,
        8,
        nw,
        nh,
        nw * 3,
    )
    return pix, (x1, y1, crop_w, crop_h)
