"""Exposure lock watcher: freezes the calibrated auto exposure once Autodarts streams.

The camera optimizer stores its exposure as a lock: auto exposure metered with a backlight
compensation level, then switched to manual WITHOUT writing an exposure value. On these cameras that
keeps ~15 % less noise than any written exposure of the same brightness. A lock does not survive a
stream reopen, and Autodarts reopens its cameras at every start and after standby (its config has
them on auto exposure, which is a good picture on its own). This watcher re-creates the lock when:
  * Autodarts reports its cameras running (SUIT stops Autodarts while it calibrates, so the watcher
    never interferes with a calibration),
  * the camera is on auto exposure with the calibrated backlight compensation and gain (a camera
    the user changed since then is left alone),
  * Autodarts' own frames of that camera held still for STEADY_READINGS readings (auto exposure has
    settled) within LEVEL_TOL of the level measured at calibration (nobody standing in the view).

Run as a user service: python -m core.exposure_lock
"""

from __future__ import annotations
import dataclasses
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("suit.exposure_lock")

LOCK_FILE = Path.home() / ".config" / "suit" / "exposure_lock.json"
AUTODARTS_CONFIG = Path.home() / ".config" / "autodarts" / "config.toml"
SERVICE_NAME = "suit-exposure-lock.service"
USER_UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME

POLL_S = 1.0
IDLE_POLL_S = 5.0  # all cameras locked (or nothing to do): look less often
STEADY_READINGS = 3  # consecutive frame levels within STEADY_TOL of each other
STEADY_TOL = 0.015
LEVEL_TOL = 0.15  # frame level within this share of the calibrated one (Autodarts' re-encoded frames read ~5 % brighter)


@dataclasses.dataclass
class LockEntry:
    path: str  # /dev/v4l/by-path link (survives re-enumeration)
    backlight_compensation: int
    gain: int
    reference: float  # mean frame level (DN) with the calibrated settings


class LockTracker:
    """Decides, reading by reading, when one camera may be locked."""

    def __init__(self, entry: LockEntry):
        self.entry = entry
        self.levels: List[float] = []

    def reset(self) -> None:
        self.levels = []

    def update(self, level: float) -> bool:
        """Feed one frame level; True when the camera should be locked now."""
        self.levels = (self.levels + [level])[-STEADY_READINGS:]
        if len(self.levels) < STEADY_READINGS:
            return False
        lo, hi = min(self.levels), max(self.levels)
        if hi - lo > STEADY_TOL * max(hi, 1.0):
            return False
        return abs(level - self.entry.reference) <= LEVEL_TOL * self.entry.reference


def load_entries(path: Optional[Path] = None) -> List[LockEntry]:
    path = path or LOCK_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [LockEntry(**c) for c in data.get("cameras", [])]
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.error("Could not read %s: %s", path, e)
        return []


def save_entries(entries: List[LockEntry], path: Optional[Path] = None) -> None:
    path = path or LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"cameras": [dataclasses.asdict(e) for e in entries]}, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def update_entries(locked: Dict[str, LockEntry], unlocked: List[str], path: Optional[Path] = None) -> List[LockEntry]:
    """Replace the entries of `locked` cameras and drop the `unlocked` ones; returns the new list."""
    gone = set(locked) | set(unlocked)
    entries = [e for e in load_entries(path) if e.path not in gone] + list(locked.values())
    save_entries(entries, path)
    return entries


# ----------------------------------------------------------------------------------------------
# Camera and Autodarts access
# ----------------------------------------------------------------------------------------------

def _v4l2(*args: str) -> str:
    return subprocess.run(["v4l2-ctl", *args], capture_output=True, text=True, timeout=5).stdout


def read_controls(path: str) -> Dict[str, int]:
    out = _v4l2("-d", path, "-C", "auto_exposure,backlight_compensation,gain")
    return {k: int(v) for k, v in re.findall(r"^(\w+):\s*(-?\d+)", out, re.M)}


def autodarts_index(path: str, config_path: Optional[Path] = None) -> Optional[int]:
    """Position of the camera in Autodarts' camera list (its image endpoint index)."""
    try:
        cam = tomllib.loads((config_path or AUTODARTS_CONFIG).read_text(encoding="utf-8")).get("cam", {})
    except Exception:
        return None
    real = os.path.realpath(path)
    for i, raw in enumerate(cam.get("devices") or cam.get("cams") or []):
        m = re.match(r"native=(/dev/[^&\s]+)", str(raw))
        if m and os.path.realpath(m.group(1)) == real:
            return i
    return None


def _api(path: str, port: int, timeout: float = 3.0) -> bytes:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return r.read()


def cameras_running(port: int) -> bool:
    try:
        return bool(json.loads(_api("/api/cams/state", port)).get("isRunning"))
    except Exception:
        return False


def frame_level(index: int, port: int) -> Optional[float]:
    """Mean grey level of Autodarts' current frame of one camera."""
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(_api(f"/api/img/cams/{index}", port), np.uint8), cv2.IMREAD_GRAYSCALE)
        return float(img.mean()) if img is not None else None
    except Exception:
        return None


def _port() -> int:
    try:
        from core.autodarts_service import get_autodarts_port
        return int(get_autodarts_port())
    except Exception:
        return 3180


def run(stop_after: Optional[float] = None) -> None:
    trackers: Dict[str, LockTracker] = {}
    started = time.monotonic()
    while stop_after is None or time.monotonic() - started < stop_after:
        entries = load_entries()
        port = _port()
        busy = False
        if entries and cameras_running(port):
            for e in entries:
                t = trackers.get(e.path)
                if t is None or t.entry != e:
                    t = trackers[e.path] = LockTracker(e)
                ctrls = read_controls(e.path)
                if ctrls.get("auto_exposure") != 3:
                    t.reset()  # locked already, or set to manual by someone else
                    continue
                if ctrls.get("backlight_compensation") != e.backlight_compensation or ctrls.get("gain") != e.gain:
                    t.reset()  # not the calibrated settings any more: leave the camera alone
                    continue
                busy = True
                idx = autodarts_index(e.path)
                level = frame_level(idx, port) if idx is not None else None
                if level is None:
                    t.reset()
                    continue
                if t.update(level):
                    _v4l2("-d", e.path, "-c", "auto_exposure=1")
                    logger.info("Locked exposure on %s (frame level %.1f, calibrated %.1f)", e.path, level, e.reference)
                    t.reset()
        else:
            for t in trackers.values():
                t.reset()
        time.sleep(POLL_S if busy else IDLE_POLL_S)


# ----------------------------------------------------------------------------------------------
# User service
# ----------------------------------------------------------------------------------------------

def service_unit_content() -> str:
    root = Path(__file__).resolve().parents[1]
    return f"""[Unit]
Description=SUIT exposure lock (freezes the calibrated camera exposure while Autodarts runs)
After=autodarts.service

[Service]
Type=simple
WorkingDirectory={root}
ExecStart={sys.executable} -m core.exposure_lock
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""


def enable_service(unit_path: Optional[Path] = None) -> bool:
    unit_path = unit_path or USER_UNIT_PATH
    try:
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(service_unit_content(), encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=5)
        res = subprocess.run(["systemctl", "--user", "enable", "--now", SERVICE_NAME], capture_output=True, timeout=10)
        subprocess.run(["systemctl", "--user", "restart", SERVICE_NAME], capture_output=True, timeout=10)
        logger.info("Enabled %s", SERVICE_NAME)
        return res.returncode == 0
    except Exception:
        logger.exception("Failed enabling %s", SERVICE_NAME)
        return False


def disable_service(unit_path: Optional[Path] = None) -> bool:
    unit_path = unit_path or USER_UNIT_PATH
    try:
        subprocess.run(["systemctl", "--user", "disable", "--now", SERVICE_NAME], capture_output=True, timeout=10)
        if unit_path.exists():
            unit_path.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=5)
        return True
    except Exception:
        logger.exception("Failed disabling %s", SERVICE_NAME)
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    if shutil.which("v4l2-ctl") is None:
        logger.error("v4l2-ctl not found")
        sys.exit(1)
    run()
