"""Stream several cameras at once and record stability (fps, drops, broken frames, gaps, bandwidth).

Used by autocal.calibrate_all to pick the best MJPEG quality all cameras can stream together.
Kernel USB/UVC errors logged during the run are counted from `journalctl -k`.
"""

from __future__ import annotations

import subprocess
import threading
import time

import numpy as np

from .capture import START_DISCARD, valid_jpeg
from .settings import Settings, apply_prestream, apply_streaming
from .v4l2 import Camera
from .xu import XU


SETTLE_S = 2.0


class Worker(threading.Thread):
    def __init__(self, dev: str, s: Settings, width: int, height: int, fps: int, seconds: float, start_evt):
        super().__init__(daemon=True)
        self.dev, self.s, self.w, self.h, self.fps, self.seconds = dev, s, width, height, fps, seconds
        self.start_evt = start_evt
        self.stats: dict = {}
        self.error = None

    def run(self):
        try:
            with XU(self.dev) as xu:
                apply_prestream(xu, self.s, self.w)
                with Camera(self.dev) as cam:
                    cam.configure(self.w, self.h, "MJPG", self.fps)
                    self.start_evt.wait()
                    cam.start()
                    try:
                        for _ in range(START_DISCARD):
                            cam.read(5.0)
                        apply_streaming(cam, xu, self.s)
                        # a mode switch causes a one-time 1–2 frame hiccup right after apply: settle first
                        t_settle = time.monotonic() + SETTLE_S
                        while time.monotonic() < t_settle:
                            cam.read(5.0)
                        seqs, ts, sizes, bad = [], [], [], 0
                        t_end = time.monotonic() + self.seconds
                        while time.monotonic() < t_end:
                            f = cam.read(5.0)
                            if not valid_jpeg(f.data):
                                bad += 1
                            seqs.append(f.sequence)
                            ts.append(f.timestamp)
                            sizes.append(len(f.data))
                    finally:
                        cam.stop()
            ts = np.array(ts)
            dt = np.diff(ts)
            self.stats = {
                "fps": float((len(ts) - 1) / (ts[-1] - ts[0])),
                "dropped": int(sum(max(0, b - a - 1) for a, b in zip(seqs, seqs[1:]))),
                "broken": bad,
                "gap_max_ms": float(dt.max() * 1000),
                "gaps_over_1.5x": int((dt > 1.5 / self.fps).sum()),
                "kb_mean": float(np.mean(sizes) / 1024),
                "kb_max": float(np.max(sizes) / 1024),
                "mbps": float(np.mean(sizes) * 8 / 1e6 * len(ts) / (ts[-1] - ts[0])),
                "frames": len(ts),
            }
        except Exception as e:  # report, don't hang the others
            self.error = f"{type(e).__name__}: {e}"


ERROR_KEYS = ("uvcvideo", "xhci", "-71", "disconnect", "reset", "URB")


def kernel_error_lines() -> int:
    """Number of USB/UVC problem lines in this boot's kernel log (compare before/after a run;
    `--since` is unreliable here because of inconsistent journal timestamps)."""
    out = subprocess.run(["journalctl", "-k", "-b", "--no-pager", "-q"], capture_output=True, text=True).stdout
    return sum(1 for line in out.splitlines()
               if any(k in line for k in ERROR_KEYS) and "Found UVC" not in line and "registered" not in line)
