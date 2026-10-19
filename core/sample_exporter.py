"""Dart samples for the SUIT team: one zip a user can send to improve dart tip detection.

A sample is what step 2 of the Camera Optimizer worked with, losslessly:
- the empty-board burst and the darts burst of every camera (PNG, capture resolution)
- Autodarts' calibration.json and the camera part of config.toml (never keys or board ids)
- the V4L2 controls each camera had
- the detection and the tip positions per camera; `manual` marks a tip the user dragged into place

Pure Python / OpenCV, no GTK. Camera controls are passed in by the caller (hardware stays in
CameraSource).
"""

from __future__ import annotations
import dataclasses
from datetime import datetime
import json
import logging
from pathlib import Path
import subprocess
import tomllib
from typing import Dict, List, Optional, Sequence
import zipfile

import cv2
import numpy as np

logger = logging.getLogger("suit.sample_exporter")

SAMPLE_FORMAT = 1
AUTODARTS_DIR = Path.home() / ".config" / "autodarts"
# Keys of config.toml [cam] that describe the cameras; everything else (api keys, board ids,
# network settings) stays on the machine.
CAM_CONFIG_KEYS = ("width", "height", "fps", "auto_distortion", "devices", "controls")


@dataclasses.dataclass
class SampleCamera:
    key: str                                 # cam_key: the USB port by-path link
    number: int                              # 1, 2, 3 as shown in the wizard
    dev_node: str
    name: str = ""
    sensor_model: str = ""
    controls: Dict[str, dict] = dataclasses.field(default_factory=dict)


def default_sample_dir() -> Path:
    """The user's Downloads folder (XDG), falling back to the home folder."""
    try:
        out = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True, timeout=3)
        path = Path(out.stdout.strip())
        if out.returncode == 0 and path.is_dir() and path != Path.home():
            return path
    except Exception:
        pass
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


def autodarts_cam_settings(config_path: Optional[Path] = None) -> dict:
    """The camera part of Autodarts' config.toml; {} when it cannot be read."""
    try:
        with open(config_path or AUTODARTS_DIR / "config.toml", "rb") as f:
            cam = tomllib.load(f).get("cam", {})
        return {k: cam[k] for k in CAM_CONFIG_KEYS if k in cam}
    except Exception as e:
        logger.debug("Could not read Autodarts camera settings: %s", e)
        return {}


def suit_version(project_dir: Optional[Path] = None) -> Optional[str]:
    """Short git commit of the SUIT checkout, None outside git."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=3,
                             cwd=project_dir or Path(__file__).resolve().parent.parent)
        if out.returncode != 0:
            return None
        return out.stdout.strip() or None
    except Exception:
        return None


def _png(frame: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", frame)
    if not ok:
        raise ValueError("PNG encoding failed")
    return buf.tobytes()


def _detection_entry(detection, numbers: Dict[str, int]) -> Optional[dict]:
    if detection is None:
        return None
    return {
        "warnings": list(detection.warnings),
        "runtime_s": round(float(detection.runtime_s), 3),
        "tips": [{
            "id": t.id,
            "board": [round(float(v), 2) for v in t.board],
            "board_mm": [round(float(v), 2) for v in t.board_mm],
            "ring_hint": t.ring_hint,
            "confidence": round(float(t.confidence), 3),
            "n_cams": t.n_cams,
            "line_residual_mm": round(float(t.line_residual_mm), 3),
            "per_cam": {str(numbers[k]): {"px": [round(float(v), 2) for v in ct.px],
                                          "direction": [round(float(v), 4) for v in ct.direction],
                                          "seen_axis": ct.seen_axis, "occluded": ct.occluded}
                        for k, ct in t.per_cam.items() if k in numbers},
        } for t in detection.tips],
    }


def _tip_entries(rois) -> List[dict]:
    return [{
        "dart_id": r.dart_id,
        "tip": [round(float(v), 2) for v in r.tip],
        "direction": [round(float(v), 4) for v in r.direction],
        "visible": r.visible,
        "manual": r.manual,
        "hidden_reason": r.hidden_reason,
    } for r in rois or []]


def export_sample(cameras: Sequence[SampleCamera],
                  empty: Dict[str, List[np.ndarray]],
                  darts: Dict[str, List[np.ndarray]],
                  capture_size: Sequence[int],
                  detection=None,
                  tip_rois: Optional[Dict[str, list]] = None,
                  out_dir: Optional[Path] = None,
                  autodarts_dir: Optional[Path] = None,
                  now: Optional[datetime] = None) -> Path:
    """Write one sample zip and return its path.

    `empty` / `darts` map camera key -> burst (BGR). Cameras without a darts burst are left out.
    """
    now = now or datetime.now()
    name = f"suit-sample-{now.strftime('%Y%m%d-%H%M%S')}"
    out_dir = Path(out_dir) if out_dir else default_sample_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.zip"
    ad_dir = Path(autodarts_dir) if autodarts_dir else AUTODARTS_DIR
    cams = [c for c in sorted(cameras, key=lambda c: c.number) if darts.get(c.key)]
    if not cams:
        raise ValueError("No camera has a darts picture to save")
    numbers = {c.key: c.number for c in cams}

    manifest = {
        "format": SAMPLE_FORMAT,
        "created": now.isoformat(timespec="seconds"),
        "suit_version": suit_version(),
        "capture": {"width": int(capture_size[0]), "height": int(capture_size[1]), "fourcc": "MJPG"},
        "cameras": [],
        "autodarts": {"cam": autodarts_cam_settings(ad_dir / "config.toml"), "calibration": None},
        "detection": _detection_entry(detection, numbers),
    }

    tmp = path.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w") as zf:
        for c in cams:
            entry = {"number": c.number, "key": c.key, "dev_node": c.dev_node, "name": c.name,
                     "sensor_model": c.sensor_model, "controls": c.controls,
                     "empty": [], "darts": [], "tips": _tip_entries((tip_rois or {}).get(c.key))}
            for kind, bursts in (("empty", empty), ("darts", darts)):
                for i, frame in enumerate(bursts.get(c.key) or []):
                    arc = f"{name}/{kind}/cam{c.number}_{i:02d}.png"
                    zf.writestr(arc, _png(frame), compress_type=zipfile.ZIP_STORED)   # PNG is compressed
                    entry[kind].append(arc.split("/", 1)[1])
            manifest["cameras"].append(entry)
        cal = ad_dir / "calibration.json"
        if cal.is_file():
            zf.write(cal, f"{name}/autodarts/calibration.json", compress_type=zipfile.ZIP_DEFLATED)
            manifest["autodarts"]["calibration"] = "autodarts/calibration.json"
        zf.writestr(f"{name}/manifest.json", json.dumps(manifest, indent=1), compress_type=zipfile.ZIP_DEFLATED)
    tmp.replace(path)
    logger.info("Saved dart sample to %s (%.1f MB)", path, path.stat().st_size / 1e6)
    return path
