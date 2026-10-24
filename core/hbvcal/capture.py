"""Validated frame bursts and stream statistics."""

from __future__ import annotations

import dataclasses
from typing import List

import cv2
import numpy as np

from .v4l2 import Camera, Frame

START_DISCARD = 30  # frames dropped after stream start (startup fragments, AE/ISP settling)


def valid_jpeg(data: bytes) -> bool:
    return data[:2] == b"\xff\xd8" and data.rstrip(b"\x00")[-2:] == b"\xff\xd9"


@dataclasses.dataclass
class Burst:
    frames: List[Frame]
    invalid: int
    pixfmt: str = "MJPG"
    width: int = 0
    height: int = 0

    def stream_stats(self) -> dict:
        fr = self.frames
        seq = [f.sequence for f in fr]
        ts = np.array([f.timestamp for f in fr])
        sizes = np.array([len(f.data) for f in fr], dtype=np.float64)
        dt = np.diff(ts) if len(ts) > 1 else np.array([np.nan])
        fps = 1.0 / dt.mean() if len(ts) > 1 else float("nan")
        return {
            "frames": len(fr),
            "fps": float(fps),
            "dt_max_ms": float(dt.max() * 1000) if len(ts) > 1 else float("nan"),
            "dropped": int(sum(max(0, b - a - 1) for a, b in zip(seq, seq[1:]))),
            "invalid": self.invalid,
            "kb_mean": float(sizes.mean() / 1024) if len(sizes) else float("nan"),
            "kb_max": float(sizes.max() / 1024) if len(sizes) else float("nan"),
            "mbps": float(sizes.mean() * fps * 8 / 1e6) if len(sizes) else float("nan"),
        }

    def decode(self) -> np.ndarray:
        """(N, H, W, 3) uint8 BGR."""
        if self.pixfmt == "YUYV":
            return np.stack([yuyv_to_bgr(f.data, self.width, self.height) for f in self.frames])
        return np.stack([cv2.imdecode(np.frombuffer(f.data, np.uint8), cv2.IMREAD_COLOR) for f in self.frames])


def burst(cam: Camera, n: int, discard: int = 0, timeout: float = 2.0, max_reads: int = 0) -> Burst:
    """Read `discard` frames, then collect `n` complete frames (bad JPEG / short YUYV frames are counted and
    skipped; after a stream start the camera can send stale fragments for several seconds).
    Gives up after `max_reads` reads (default 4*n + 60)."""
    for _ in range(discard):
        cam.read(timeout)
    out, bad = [], 0
    limit = max_reads or 4 * n + 60
    while len(out) < n and len(out) + bad < limit:
        f = cam.read(timeout)
        if (cam.pixfmt == "MJPG" and not valid_jpeg(f.data)) or \
                (cam.pixfmt == "YUYV" and len(f.data) != cam.width * cam.height * 2):
            bad += 1
            continue
        out.append(f)
    if len(out) < n:
        raise RuntimeError(f"{cam.device}: only {len(out)} of {n} complete frames ({bad} broken)")
    return Burst(out, bad, cam.pixfmt, cam.width, cam.height)


def luma(bgr: np.ndarray) -> np.ndarray:
    """BT.601 luma like the JPEG Y channel (float32)."""
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)[..., 0].astype(np.float32) if bgr.ndim == 3 else bgr.astype(np.float32)


def yuyv_to_bgr(data: bytes, w: int, h: int) -> np.ndarray:
    a = np.frombuffer(data, np.uint8)[: w * h * 2].reshape(h, w, 2)
    return cv2.cvtColor(a, cv2.COLOR_YUV2BGR_YUYV)
