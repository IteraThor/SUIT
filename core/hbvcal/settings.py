"""Camera settings across all control layers, applied in the order the firmware requires.

Layers (verified on the cameras):
  1. `mjpeg_q`  XDATA Q byte for the stream resolution: must be written BEFORE the stream starts.
  2. `uvc`      standard V4L2/UVC controls (exposure writes re-assert sensor gain 0x80).
  3. `sensor`   OV2710 registers, written last, while streaming.
  4. `isp`      raw XDATA ISP registers, written last, while streaming (experimental).
"""

from __future__ import annotations

import dataclasses
import json
import time
from typing import Dict, Optional

from .v4l2 import Camera
from .xu import XU

# Per-resolution MJPEG Q bytes in XDATA, per firmware build (from the code that loads 0xC104 at stream start).
# "mid" is the one used at 1280x960/1280x720 (verified); "large" is presumably 1920x1080; "small" <= 640.
Q_ADDR = {
    "07/11/26": {"large": 0xC06F, "mid": 0xC094, "small": 0xC079},
    "07/28/26": {"large": 0xC070, "mid": 0xC095, "small": 0xC07A},
    "02/15/25": {"large": 0xC072, "mid": 0xC097, "small": 0xC07C},  # Autodarts DIY Cam image, same code at 0x420B
}
Q_DEFAULT = {"large": 24, "mid": 22, "small": 18}
Q_LEVELS = (1, 2, 3, 4, 5, 6, 7, 8, 11, 16, 22)  # the Q steps the optimizer uses, least compression first; 22 = stock
# Largest MJPEG frame a build streams reliably (KB). 02/15/25 has a smaller frame buffer: a bigger frame arrives cut
# off (~165-195 KB fragments), and if every frame is that big the camera sends nothing usable (Autodarts: 0 fps,
# "stalled"/"reconnected" loop). Measured 2026-10-29 on 3 cameras at 1280x960: 319-324 KB clean, ~340 KB all broken
# (an earlier session saw up to 365 KB pass, so the cliff varies per camera/scene; the lower value is used).
# The limit depends on the resolution: gain swept up at a fixed Q on the same 3 cameras, last clean frame before all
# broken was 303-323 KB at 1280x960 and 215-229 KB at 1280x720 (one gain step, ~7-11 KB, from clean to all broken).
# Frame size also grows with the scene (light, darts in the board), so the Q calibration keeps the largest frame of
# its static test scene under FRAME_HEADROOM of the limit, and the keeper raises Q if a camera stalls anyway.
FRAME_LIMIT_KB = {"02/15/25": {960: 300, 720: 215}}  # by frame height; other heights use the lowest
FRAME_HEADROOM = 0.8

# Order matters: mode switches before the values they enable.
UVC_ORDER = [
    "auto_exposure", "exposure_dynamic_framerate", "exposure_time_absolute", "gain",
    "white_balance_automatic", "white_balance_temperature", "power_line_frequency",
    "backlight_compensation", "brightness", "contrast", "saturation", "hue", "gamma", "sharpness",
]


@dataclasses.dataclass
class Settings:
    uvc: Dict[str, int] = dataclasses.field(default_factory=dict)
    mjpeg_q: Optional[int] = None
    sensor: Dict[int, int] = dataclasses.field(default_factory=dict)
    isp: Dict[int, int] = dataclasses.field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "uvc": self.uvc,
            "mjpeg_q": self.mjpeg_q,
            "sensor": {f"0x{k:04X}": v for k, v in self.sensor.items()},
            "isp": {f"0x{k:04X}": v for k, v in self.isp.items()},
        }

    @classmethod
    def from_json(cls, d: dict) -> "Settings":
        return cls(
            uvc=dict(d.get("uvc", {})),
            mjpeg_q=d.get("mjpeg_q"),
            sensor={int(k, 16): int(v) for k, v in d.get("sensor", {}).items()},
            isp={int(k, 16): int(v) for k, v in d.get("isp", {}).items()},
        )

    def merged(self, other: "Settings") -> "Settings":
        """self overridden by other."""
        return Settings(
            uvc={**self.uvc, **other.uvc},
            mjpeg_q=other.mjpeg_q if other.mjpeg_q is not None else self.mjpeg_q,
            sensor={**self.sensor, **other.sensor},
            isp={**self.isp, **other.isp},
        )

    def label(self) -> str:
        parts = [f"{k}={v}" for k, v in self.uvc.items()]
        if self.mjpeg_q is not None:
            parts.append(f"Q={self.mjpeg_q}")
        parts += [f"s{k:04X}={v:#04x}" for k, v in self.sensor.items()]
        parts += [f"x{k:04X}={v:#04x}" for k, v in self.isp.items()]
        return " ".join(parts) or "current"


def q_class(width: int) -> str:
    return "large" if width >= 1920 else ("mid" if width > 640 else "small")


def q_addr(xu: XU, width: int) -> int:
    """XDATA address of the Q byte for this camera's firmware build and stream width.
    Not cached: a camera can be re-flashed under the same device path."""
    build = xu.firmware_build()
    if build not in Q_ADDR:
        raise RuntimeError(f"{xu.device}: unknown firmware build {build!r}; Q address not mapped")
    return Q_ADDR[build][q_class(width)]


def q_above(q: int, steps: int = 1) -> int:
    """The Q level `steps` coarser than q (more compression, smaller frames), at most stock."""
    higher = [lv for lv in Q_LEVELS if lv > q]
    return higher[min(steps, len(higher)) - 1] if higher else max(q, Q_LEVELS[-1])


def apply_prestream(xu: XU, s: Settings, width: int) -> None:
    """Layer 1: must run while no one is streaming (or before the next stream start)."""
    if s.mjpeg_q is not None:
        xu.unlock()
        xu.mem_write(q_addr(xu, width), bytes([int(s.mjpeg_q)]))


def frame_budget_kb(xu: XU, height: int) -> float:
    """Largest frame (KB) a Q level may produce on this camera's firmware build at this frame height; inf where no
    limit is known."""
    limits = FRAME_LIMIT_KB.get(xu.firmware_build())
    if not limits:
        return float("inf")
    return limits.get(height, min(limits.values())) * FRAME_HEADROOM


def inactive(uvc: Dict[str, int], name: str) -> bool:
    """Controls an auto mode in the same settings makes inactive (the driver refuses writes to them)."""
    if name == "exposure_time_absolute":
        return uvc.get("auto_exposure", 1) != 1
    if name == "white_balance_temperature":
        return bool(uvc.get("white_balance_automatic", 0))
    return False


def set_uvc(cam: Camera, name: str, value: int) -> None:
    """One UVC write. Some cameras (GXI/Sonix) refuse exposure writes after a plain switch to manual
    until auto exposure is toggled once more."""
    try:
        cam.set(name, value)
    except PermissionError:
        if name != "exposure_time_absolute":
            raise
        cam.set("auto_exposure", 3)
        cam.set("auto_exposure", 1)
        cam.set(name, value)


def apply_uvc(cam: Camera, uvc: Dict[str, int], strict: bool = True) -> Dict[str, str]:
    """Layer 2 in the order the firmware needs, skipping controls an auto mode makes inactive.
    strict: raise on the first failure; otherwise return {control: error} and carry on."""
    failed = {}
    for name in sorted(uvc, key=lambda n: UVC_ORDER.index(n) if n in UVC_ORDER else 99):
        if inactive(uvc, name):
            continue
        try:
            set_uvc(cam, name, uvc[name])
        except (OSError, KeyError) as e:
            if strict:
                raise
            failed[name] = str(e)
    return failed


def apply_streaming(cam: Camera, xu: XU, s: Settings, settle_frames: int = 0) -> None:
    """Layers 2–4: while streaming."""
    apply_uvc(cam, s.uvc)
    if s.sensor or s.isp:
        xu.unlock()
        for reg, val in s.sensor.items():
            xu.sensor_write(reg, val)
        for addr, val in s.isp.items():
            xu.mem_write(addr, bytes([val]))
    for _ in range(settle_frames):
        cam.read()


def snapshot(cam: Camera, xu: Optional[XU] = None, width: int = 1280,
             sensor_regs=(0x3500, 0x3501, 0x3502, 0x350A, 0x350B)) -> Settings:
    """Read back the current state of every layer (sensor regs only if streaming)."""
    from .v4l2 import CID
    uvc = {}
    for name in CID:
        try:
            uvc[name] = cam.get(name)
        except OSError:
            pass
    s = Settings(uvc=uvc)
    if xu is not None:
        try:
            xu.unlock()
            s.mjpeg_q = xu.mem_read(q_addr(xu, width), 1)[0]
            if cam.streaming:
                s.sensor = {r: xu.sensor_read(r) for r in sensor_regs}
        except OSError:
            pass
    return s


def save(s: Settings, path) -> None:
    with open(path, "w") as f:
        json.dump(s.to_json(), f, indent=1)


def load(path) -> Settings:
    with open(path) as f:
        return Settings.from_json(json.load(f))
