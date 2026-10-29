"""What Autodarts reports about its cameras: local API (http://127.0.0.1:3180) and camera order in its config."""

from __future__ import annotations

import json
import re
import tomllib
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

from . import profiles

AUTODARTS_API = "http://127.0.0.1:3180"
AUTODARTS_CONFIG = Path.home() / ".config" / "autodarts" / "config.toml"
DEFAULT_SIZE = (1280, 960)


def stream_size() -> Tuple[int, int]:
    """(width, height) Autodarts streams its cameras at; DEFAULT_SIZE where the config does not say.
    The camera reserves USB bandwidth by resolution, not by fps or frame size (1280x960: 2x768 B per microframe,
    1280x720 and below: 1x1024 B), so the optimizer must test the resolution Autodarts really uses: three cameras
    behind one USB 2 root port fit at 720, not at 960."""
    try:
        cam = tomllib.loads(AUTODARTS_CONFIG.read_text()).get("cam", {})
        size = (int(cam.get("width", DEFAULT_SIZE[0])), int(cam.get("height", DEFAULT_SIZE[1])))
    except (OSError, ValueError, TypeError):
        return DEFAULT_SIZE
    return size if min(size) > 0 else DEFAULT_SIZE


def set_stream_size(width: int, height: int) -> None:
    """Write the [cam] width/height Autodarts streams at, leaving every other line of its config as it is.
    Only while Autodarts is stopped: it rewrites config.toml from its own values when it exits."""
    text = AUTODARTS_CONFIG.read_text()
    m = re.search(r"(?ms)^\[cam\]\n.*?(?=^\s*\[|\Z)", text)  # up to the first sub-table or next table
    if m is None:
        raise RuntimeError(f"no [cam] section in {AUTODARTS_CONFIG}")
    sec = m.group(0)
    for key, v in (("width", width), ("height", height)):
        sec, n = re.subn(rf"(?m)^{key}\s*=.*$", f"{key} = {v}", sec)
        if not n:
            sec = sec.replace("[cam]\n", f"[cam]\n{key} = {v}\n", 1)
    AUTODARTS_CONFIG.write_text(text[:m.start()] + sec + text[m.end():])


def autodarts_index(dev: str) -> Optional[int]:
    """Position of this camera in Autodarts' camera list (its image endpoint index)."""
    try:
        devs = tomllib.loads(AUTODARTS_CONFIG.read_text()).get("cam", {}).get("devices", [])
    except (OSError, ValueError):
        return None
    port = profiles.port_key(dev)
    for i, uri in enumerate(devs):
        if f"location={port}" in uri:
            return i
    return None


def get_json(path: str, timeout: float = 2) -> Optional[dict]:
    try:
        with urllib.request.urlopen(f"{AUTODARTS_API}{path}", timeout=timeout) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def calibrate_board() -> bool:
    """Start Autodarts' automatic board calibration (it needs one per resolution). True if Autodarts took it."""
    req = urllib.request.Request(f"{AUTODARTS_API}/api/config/calibration/auto", data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=4):
            return True
    except OSError:
        return False


def camera_fps() -> Optional[List[float]]:
    """Frames per second Autodarts gets from each camera (its config order), or None when Autodarts is not
    running detection (stopped, unreachable, or in camera standby, where every camera reads 0)."""
    state = get_json("/api/state", timeout=1)
    if not state or not state.get("running"):
        return None
    stats = get_json("/api/cams/stats", timeout=1)
    fps = (stats or {}).get("fps")
    return [float(f or 0) for f in fps] if isinstance(fps, list) else None
