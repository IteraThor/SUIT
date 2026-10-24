"""User service that keeps the saved camera settings applied (runs daemon.py).

The keeper starts before Autodarts (the MJPEG quality must be in camera RAM before a stream starts) and
re-applies the controls once Autodarts has opened the cameras. It runs while any camera has a saved profile
(an optimization, or settings kept on the Camera Controls page); sync() switches it on and off.

Older setups ran their own watchers on the same cameras; they would fight the keeper, so migrate_legacy()
removes them: SUIT's previous exposure lock and the standalone HBV calibration app's services.
migrate_manual_controls() moves the old Manual Controls page's presets and boot settings into presets.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("suit.camera_keeper")

SERVICE_NAME = "suit-camera-keeper.service"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
USER_UNIT_PATH = UNIT_DIR / SERVICE_NAME
LEGACY_UNITS = ("suit-exposure-lock.service", "hbv-camera-optimizer.service", "hbvcal.service", "hbvcal-webui.service")
LEGACY_FILES = (Path.home() / ".config" / "suit" / "exposure_lock.json",  # old exposure lock entries
                Path.home() / ".config" / "suit" / "camera_profile.json")  # old optimizer's camera brand choice


def _systemctl(*args: str, timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=timeout)


def unit_content() -> str:
    root = Path(__file__).resolve().parents[2]
    return f"""[Unit]
Description=SUIT camera keeper (keeps the optimized HBV camera settings applied)
Before=autodarts.service

[Service]
Type=simple
WorkingDirectory={root}
ExecStart={sys.executable} -m core.hbvcal.daemon
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""


def is_enabled() -> bool:
    try:
        return _systemctl("is-enabled", SERVICE_NAME, timeout=5).stdout.strip() == "enabled"
    except Exception:
        return False


def enable() -> bool:
    try:
        USER_UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        USER_UNIT_PATH.write_text(unit_content(), encoding="utf-8")
        _systemctl("daemon-reload", timeout=5)
        res = _systemctl("enable", SERVICE_NAME)
        _systemctl("restart", SERVICE_NAME)
        logger.info("Enabled %s", SERVICE_NAME)
        return res.returncode == 0
    except Exception:
        logger.exception("Failed enabling %s", SERVICE_NAME)
        return False


def disable() -> bool:
    try:
        _systemctl("disable", "--now", SERVICE_NAME)
        if USER_UNIT_PATH.exists():
            USER_UNIT_PATH.unlink()
        _systemctl("daemon-reload", timeout=5)
        return True
    except Exception:
        logger.exception("Failed disabling %s", SERVICE_NAME)
        return False


def sync() -> None:
    """Keeper on while any camera has a saved profile, off (and removed) otherwise."""
    from . import profiles
    if profiles.load_all():
        if not is_enabled():
            enable()
        else:
            start()
    elif USER_UNIT_PATH.exists() or is_enabled():
        disable()


def stop() -> None:
    """Pause the keeper (an optimization needs the cameras to itself); start() resumes it."""
    try:
        _systemctl("stop", SERVICE_NAME)
    except Exception:
        logger.exception("Failed stopping %s", SERVICE_NAME)


def start() -> None:
    try:
        _systemctl("start", SERVICE_NAME)
    except Exception:
        logger.exception("Failed starting %s", SERVICE_NAME)


def heal() -> None:
    """At start and after an update: migrate old watchers and the Manual Controls data, and point the keeper
    unit at this checkout. Profiles made with the standalone HBV calibration app (same store) get a keeper
    too, since its service is gone now."""
    from . import profiles
    migrate_legacy()
    migrate_manual_controls()
    if USER_UNIT_PATH.exists():
        if USER_UNIT_PATH.read_text(encoding="utf-8") != unit_content():
            enable()
    elif profiles.load_all():
        enable()


# --- old Manual Controls page --------------------------------------------------------------------------

MANUAL_PRESETS = Path.home() / ".suit_camera_profiles.json"
MANUAL_BOOT_UNIT = "suit-camera-settings.service"
MANUAL_BOOT_PRESET = "Old boot settings"
# CameraSettings field names an early optimizer wrote into Manual Controls presets -> V4L2 control names
LEGACY_PRESET_KEYS = {"exposure_auto": "auto_exposure", "exposure_absolute": "exposure_time_absolute",
                      "wb_auto": "white_balance_automatic", "wb_temperature": "white_balance_temperature"}


def _port_of(device: str):
    """Port key for a by-path link or a /dev/videoN node (resolved through the by-path links), or None."""
    from . import profiles
    if device.startswith("/dev/v4l/by-path/"):
        return profiles.port_key(device)
    real = os.path.realpath(device)
    for link in sorted(glob.glob("/dev/v4l/by-path/*-usb-*")):
        if os.path.realpath(link) == real:
            return profiles.port_key(link)
    return None


def _uvc_settings(ctrls: dict) -> dict:
    """Standard controls only: old presets also carried keys no camera knows (e.g. the optimizer's exposure_lock)."""
    from .settings import Settings
    from .v4l2 import CID
    uvc = {}
    for k, v in ctrls.items():
        name = LEGACY_PRESET_KEYS.get(k, k)
        if name in CID:
            try:
                uvc[name] = int(v)
            except (TypeError, ValueError):
                pass
    return Settings(uvc=uvc).to_json()


def migrate_manual_controls() -> None:
    """Old Manual Controls presets and its "Load on boot" values become presets of the Camera Controls page.
    The boot values are NOT applied: Autodarts overwrote them at every start, so keeping them now would change
    the picture. They stay available as a preset to load."""
    from . import profiles
    presets = profiles.load_presets()
    changed = False
    if MANUAL_PRESETS.exists():
        try:
            data = json.loads(MANUAL_PRESETS.read_text())
            if not isinstance(data, dict):
                data = {}
            if "profiles" in data:
                old = data["profiles"] or {}
            else:  # oldest format: {"/dev/videoN": {...}} written by an early optimizer
                old = {"CAM-librator": {k: v for k, v in data.items() if k.startswith("/dev/")}}
            for name, cams in old.items():
                entry = {}
                for dev, ctrls in (cams or {}).items():
                    port = _port_of(dev)
                    if port and isinstance(ctrls, dict):
                        entry[port] = _uvc_settings(ctrls)
                if entry and name not in presets:
                    presets[name] = entry
                    changed = True
            MANUAL_PRESETS.rename(MANUAL_PRESETS.with_name(MANUAL_PRESETS.name + ".migrated"))
            logger.info("Migrated %d Manual Controls preset(s)", len(old))
        except (OSError, ValueError):
            logger.exception("Failed migrating %s", MANUAL_PRESETS)
    unit = UNIT_DIR / MANUAL_BOOT_UNIT
    if unit.exists():
        try:
            entry = {}
            for dev, ctrls in re.findall(r"^ExecStart=-?\S*v4l2-ctl -d (\S+) --set-ctrl=(\S+)", unit.read_text(), re.M):
                port = _port_of(dev)
                if port:
                    entry[port] = _uvc_settings(dict(kv.split("=", 1) for kv in ctrls.split(",") if "=" in kv))
            if entry and MANUAL_BOOT_PRESET not in presets:
                presets[MANUAL_BOOT_PRESET] = entry
                changed = True
            _systemctl("disable", "--now", MANUAL_BOOT_UNIT)
            unit.unlink()
            _systemctl("daemon-reload", timeout=5)
            logger.info("Removed %s (its values are the preset '%s')", MANUAL_BOOT_UNIT, MANUAL_BOOT_PRESET)
        except (OSError, ValueError):
            logger.exception("Failed migrating %s", MANUAL_BOOT_UNIT)
    if changed:
        profiles.save_presets(presets)


def migrate_legacy() -> None:
    for unit in LEGACY_UNITS:
        try:
            active = _systemctl("is-active", unit, timeout=5).stdout.strip() == "active"
            enabled = _systemctl("is-enabled", unit, timeout=5).stdout.strip() == "enabled"
            if active or enabled:
                _systemctl("disable", "--now", unit)
                logger.info("Disabled legacy %s", unit)
            # SUIT's own old unit file can go (also when already disabled); the standalone app's files
            # belong to its own uninstaller
            if unit == "suit-exposure-lock.service" and (UNIT_DIR / unit).exists():
                (UNIT_DIR / unit).unlink()
                _systemctl("daemon-reload", timeout=5)
        except Exception:
            logger.exception("Failed migrating %s", unit)
    for f in LEGACY_FILES:
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass
