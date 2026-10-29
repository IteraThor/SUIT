"""Per-camera profile store (~/.config/hbvcal/profiles.json), keyed by USB port path.

All HBV cameras report the same serial (HB2026070001, hard-coded in firmware), so a camera is identified by the
port it is plugged into (`/dev/v4l/by-path/...`). If serials ever differ, the serial is stored too and used to
warn when a known camera moved to another port.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, Optional

from .settings import Settings

STORE = Path(os.environ.get("HBVCAL_PROFILES", Path.home() / ".config" / "hbvcal" / "profiles.json"))
SHARED_SERIAL = "HB2026070001"


def port_key(device: str) -> str:
    """'/dev/v4l/by-path/pci-0000:00:14.0-usb-0:1:1.0-video-index0' -> 'pci-0000:00:14.0-usb-0:1:1.0'."""
    return Path(device).name.split("-video-index")[0]


def serial_of(device: str) -> Optional[str]:
    """USB serial of the camera behind a V4L2 node (from sysfs), or None."""
    try:
        real = Path(os.path.realpath(device)).name  # videoN
        p = Path(os.path.realpath(f"/sys/class/video4linux/{real}/device"))
        for _ in range(4):
            if (p / "serial").exists():
                return (p / "serial").read_text().strip()
            p = p.parent
    except OSError:
        pass
    return None


def load_all() -> Dict[str, dict]:
    try:
        return json.loads(STORE.read_text()).get("cameras", {})
    except (OSError, ValueError):
        return {}


def save_all(cams: Dict[str, dict]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "cameras": cams}, indent=1))
    os.replace(tmp, STORE)


def get(device: str) -> Optional[dict]:
    return load_all().get(port_key(device))


def get_settings(device: str) -> Optional[Settings]:
    p = get(device)
    return Settings.from_json(p["settings"]) if p else None


def put(device: str, settings: Settings, meta: Optional[dict] = None) -> None:
    cams = load_all()
    cams[port_key(device)] = {
        "settings": settings.to_json(),
        "serial": serial_of(device),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "meta": meta or {},
    }
    save_all(cams)


def set_q(device: str, q: int, note: str) -> None:
    """Change only the saved MJPEG Q of a camera's profile (the keeper's frame-overflow fix), noting why."""
    cams = load_all()
    p = cams.get(port_key(device))
    if not p:
        return
    p["settings"]["mjpeg_q"] = q
    meta = p.setdefault("meta", {})
    if "mjpeg_q" in meta:
        meta["mjpeg_q"] = q
    meta["q_raised"] = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "to": q, "why": note}
    p["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_all(cams)


def usb_id(device: str) -> Optional[str]:
    """'vvvv:pppp' USB vendor:product of the camera behind a V4L2 node (sysfs), or None."""
    try:
        real = Path(os.path.realpath(device)).name
        p = Path(os.path.realpath(f"/sys/class/video4linux/{real}/device"))
        for _ in range(4):
            if (p / "idVendor").exists():
                return f"{(p / 'idVendor').read_text().strip()}:{(p / 'idProduct').read_text().strip()}"
            p = p.parent
    except OSError:
        pass
    return None


def remove(device: str) -> None:
    cams = load_all()
    cams.pop(port_key(device), None)
    save_all(cams)


CONSENT = STORE.parent / "consent.json"


def consent() -> Optional[dict]:
    try:
        return json.loads(CONSENT.read_text())
    except (OSError, ValueError):
        return None


def set_consent(hbv_confirmed: bool) -> None:
    CONSENT.parent.mkdir(parents=True, exist_ok=True)
    CONSENT.write_text(json.dumps({"hbv_confirmed": hbv_confirmed, "time": time.strftime("%Y-%m-%d %H:%M:%S")}))


# --- who may use the hidden layers --------------------------------------------------------------------

HBV_USB_ID = "0bdc:2710"


def hbv(device: str) -> bool:
    return usb_id(device) == HBV_USB_ID


def hidden_ok(device: str) -> bool:
    """May the tuning back door (MJPEG quality, sensor registers) be used on this camera? Only on HBV cameras,
    and only after the user confirmed once that they use HBV OV2710 cameras."""
    c = consent()
    return bool(c and c.get("hbv_confirmed")) and hbv(device)


def is_calibrated(prof: Optional[dict]) -> bool:
    """A profile made by the optimizer (autocal stamps the calibration time), not one kept from manual changes."""
    return bool(prof and "calibrated" in prof.get("meta", {}))


def uvc_only(s: Settings) -> Settings:
    return Settings(uvc=dict(s.uvc))


# --- named presets: a snapshot of every camera's settings, loaded all at once ---------------------------

PRESETS = STORE.parent / "presets.json"


def load_presets() -> Dict[str, Dict[str, dict]]:
    """{preset name: {port key: settings json}}"""
    try:
        return json.loads(PRESETS.read_text()).get("presets", {})
    except (OSError, ValueError):
        return {}


def save_presets(presets: Dict[str, Dict[str, dict]]) -> None:
    PRESETS.parent.mkdir(parents=True, exist_ok=True)
    tmp = PRESETS.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "presets": presets}, indent=1))
    os.replace(tmp, PRESETS)


# --- the default preset: loaded into the profiles once per boot (by the keeper) ---------------------------

DEFAULT_PRESET_FILE = STORE.parent / "default_preset.json"
_BOOT_MARK = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "hbvcal-default-preset-loaded"


def default_preset() -> Optional[str]:
    try:
        name = json.loads(DEFAULT_PRESET_FILE.read_text()).get("name")
    except (OSError, ValueError):
        return None
    return name if name in load_presets() else None


def set_default_preset(name: Optional[str]) -> None:
    DEFAULT_PRESET_FILE.parent.mkdir(parents=True, exist_ok=True)
    if name is None:
        DEFAULT_PRESET_FILE.unlink(missing_ok=True)
    else:
        DEFAULT_PRESET_FILE.write_text(json.dumps({"name": name, "time": time.strftime("%Y-%m-%d %H:%M:%S")}))


def preset_to_profiles(name: str) -> int:
    """Make a preset's settings the kept profiles (the keeper applies them). Like loading it on Camera Controls,
    but without touching the cameras, so it works before they stream. Returns the number of cameras."""
    entry = load_presets().get(name, {})
    cams = load_all()
    for port, js in entry.items():
        p = cams.get(port)
        meta = dict(p.get("meta", {})) if p else {"source": "preset"}
        if p and "calibrated" in meta:  # as live._save_edit: remember what the optimizer picked
            meta.setdefault("calibrated_settings", p["settings"])
        meta.pop("manual_edit", None)
        meta["preset"] = name
        cams[port] = {"settings": js, "serial": (p or {}).get("serial"),
                      "updated": time.strftime("%Y-%m-%d %H:%M:%S"), "meta": meta}
    save_all(cams)
    return len(entry)


def load_default_preset_once() -> Optional[str]:
    """At the first keeper start of a boot: the default preset becomes the kept profiles. Its name, if loaded."""
    name = default_preset()
    if name is None or _BOOT_MARK.exists():
        return None
    preset_to_profiles(name)
    try:
        _BOOT_MARK.write_text(name)
    except OSError:
        pass
    return name
