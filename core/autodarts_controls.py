"""Keep calibrated camera settings across Autodarts restarts.

Autodarts stores the controls of every camera in its config.toml, in a table per camera
(`[cam.controls.'vid=...&pid=...&serial=...&location=1-1']`), and writes them to the camera every
time it starts. A setting SUIT writes to the camera with V4L2 lasts until then: measured on a live
rig, `auto_exposure` went back from manual to auto and `gain` from 40 to 64 at the next Autodarts
start, whatever the boot service had set before it. With the same values in that table (exposureMode,
exposureTime, gain, ...) the camera came up manual and stayed so through restarts.

So the calibration is also written into Autodarts' own table. Only the keys of the one camera table
change; the new file must parse and every other table must be unchanged, or nothing is written.
Autodarts must not be running (it rewrites its config on exit): the wizard has it paused.
"""

from __future__ import annotations
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import tomllib
from typing import Dict, Optional

from core.v4l2_device import CameraSettings

logger = logging.getLogger("suit.autodarts_controls")

AUTODARTS_CONFIG = Path.home() / ".config" / "autodarts" / "config.toml"


def controls_from_settings(s: CameraSettings) -> Dict[str, int]:
    """CameraSettings as the control names of Autodarts' config (exposureMode 1 = manual, 3 = auto)."""
    ctrls = {
        "exposureMode": s.exposure_auto,
        "gain": s.gain,
        "whiteBalanceAuto": s.wb_auto,
        "sharpness": s.sharpness,
        "contrast": s.contrast,
        "brightness": s.brightness,
        "gamma": s.gamma,
        "saturation": s.saturation,
        "powerLineFrequency": s.power_line_frequency,
    }
    if s.exposure_auto == 1:
        ctrls["exposureTime"] = s.exposure_absolute
    if s.wb_auto == 0:
        ctrls["whiteBalanceTemperature"] = s.wb_temperature
    return ctrls


def camera_section_id(config: dict, dev_node: Optional[str], by_path: Optional[str] = None) -> Optional[str]:
    """The `vid=...&location=...` id of a camera's table, from the [cam] devices of the config."""
    cam = config.get("cam", {})
    devices = cam.get("devices") or cam.get("cams") or []
    entries = []
    for raw in devices:
        m = re.match(r"native=(/dev/[^&\s]+)&(.+)$", str(raw))
        if m:
            entries.append((m.group(1), m.group(2)))

    def real(p: Optional[str]) -> Optional[str]:
        return os.path.realpath(p) if p and os.path.exists(p) else p

    wanted = {real(dev_node), real(by_path)} - {None}
    for path, sid in entries:
        if real(path) in wanted:
            return sid
    # No node match (numbers swap between boots): the USB port in the by-path link names the camera
    port = re.search(r"usb-\d+:([\d.]+):", by_path or "")
    if port:
        hits = [sid for _p, sid in entries if (re.search(r"location=\d+-([\d.]+)", sid) or [None, None])[1] == port.group(1)]
        if len(hits) == 1:
            return hits[0]
    return None


def persist_camera_settings(dev_node: Optional[str], by_path: Optional[str], settings: CameraSettings,
                            config_path: Optional[Path] = None) -> bool:
    """Write `settings` into Autodarts' config for this camera; True when the file now holds them."""
    path = Path(config_path) if config_path else AUTODARTS_CONFIG
    try:
        text = path.read_text(encoding="utf-8")
        section = camera_section_id(tomllib.loads(text), dev_node, by_path)
        if section is None:
            logger.warning("No Autodarts camera table for %s: settings are not saved in Autodarts", dev_node)
            return False
        return _write_controls(path, text, section, controls_from_settings(settings))
    except Exception as e:
        logger.error("Could not save the camera settings in the Autodarts config: %s", e)
        return False


def _write_controls(path: Path, text: str, section: str, controls: Dict[str, int]) -> bool:
    new_text = _edit_table(text, section, controls)
    if new_text == text:
        return True
    old, new = tomllib.loads(text), tomllib.loads(new_text)          # raises on a broken edit
    table = new.get("cam", {}).get("controls", {}).get(section, {})
    if any(table.get(k) != v for k, v in controls.items()):
        raise ValueError("edited table does not hold the new values")
    old.get("cam", {}).get("controls", {}).pop(section, None)
    new.get("cam", {}).get("controls", {}).pop(section, None)
    if old != new:
        raise ValueError("the edit changed more than the camera table")
    shutil.copy2(path, path.with_name(path.name + ".suit-backup"))
    tmp = path.with_name(path.name + ".suit-tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))                   # the file holds keys: keep its mode
    os.replace(tmp, path)
    logger.info("Saved %s in the Autodarts config for %s", ", ".join(f"{k}={v}" for k, v in controls.items()), section)
    return True


def _edit_table(text: str, section: str, controls: Dict[str, int]) -> str:
    """Set keys in the camera's table, adding the keys (or the table) that are missing."""
    header = re.compile(r"^([ \t]*)\[cam\.controls\.'" + re.escape(section) + r"'\][ \t]*\n", re.M)
    m = header.search(text)
    if m is None:
        lines = "".join(f"    {k} = {v}\n" for k, v in controls.items())
        return text.rstrip("\n") + f"\n\n    [cam.controls.'{section}']\n{lines}"
    start = m.end()
    nxt = re.compile(r"^[ \t]*\[", re.M).search(text, start)
    end = nxt.start() if nxt else len(text)
    body = text[start:end]
    indent = m.group(1) + "    "
    for key, value in controls.items():
        assign = re.compile(r"^([ \t]*)" + re.escape(key) + r"([ \t]*=[ \t]*)[^\n]*$", re.M)
        found = assign.search(body)
        if found:
            body = body[:found.start()] + f"{found.group(1)}{key}{found.group(2)}{value}" + body[found.end():]
            continue
        # a new key goes after the last assignment, in the table's own indentation
        last = None
        for last in re.finditer(r"^([ \t]*)[A-Za-z_][A-Za-z0-9_]*[ \t]*=[^\n]*\n", body, re.M):
            pass
        at = last.end() if last else 0
        body = body[:at] + f"{last.group(1) if last else indent}{key} = {value}\n" + body[at:]
    return text[:start] + body + text[end:]
