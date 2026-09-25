import os
import socket
import re
import json
import shutil
import time
import tomllib
import urllib.request
import urllib.error
import subprocess
import getpass
from pathlib import Path
from core.logger import get_logger

logger = get_logger("autodarts")

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "autodarts" / "config.toml"
USER_UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / "autodarts.service"
SYSTEM_UNIT_PATH = Path("/etc/systemd/system/autodarts.service")

DEFAULT_HOST = os.environ.get("AUTODARTS_HOST", "127.0.0.1")
DEFAULT_V1_PORT = 3180
DEFAULT_V2_PORT = 3182
DEFAULT_PORT = int(os.environ.get("AUTODARTS_PORT", "3182"))
DEFAULT_API_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/api"



def ensure_autodarts_dirs() -> None:
    """Ensure ~/.config/autodarts and ~/.local/state/autodarts exist and are valid directories.
    Removes any broken or stale symlinks at ~/.local/state/autodarts that would cause
    systemd StateDirectory setup to fail with status 238/STATE_DIRECTORY.
    """
    home = Path.home()
    config_dir = home / ".config" / "autodarts"
    state_dir = home / ".local" / "state" / "autodarts"

    try:
        config_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning("Could not create config directory %s: %s", config_dir, e)

    try:
        if state_dir.is_symlink() or (state_dir.exists() and not state_dir.is_dir()):
            state_dir.unlink(missing_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(state_dir, 0o700)
        except Exception:
            pass
    except Exception as e:
        logger.warning("Could not setup state directory %s: %s", state_dir, e)



def get_autodarts_port(config_path: Path | None = None, host: str = DEFAULT_HOST) -> int:
    """Dynamically resolve the Autodarts API port:
    1. AUTODARTS_PORT environment variable if set.
    2. config.toml [api] port or [host] port.
    3. Active listening socket (probe 3182 first, then 3180).
    4. Fallback to 3182.
    """
    if "AUTODARTS_PORT" in os.environ:
        try:
            return int(os.environ["AUTODARTS_PORT"])
        except ValueError:
            pass

    path = config_path or DEFAULT_CONFIG_PATH
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            if "api" in data and isinstance(data["api"], dict) and "port" in data["api"]:
                return int(data["api"]["port"])
            if "host" in data and isinstance(data["host"], dict) and "port" in data["host"]:
                return int(data["host"]["port"])
        except Exception:
            pass

    # Probe live ports (fast socket check) - prioritize V2 (3182)
    if is_port_open(host, DEFAULT_V2_PORT, timeout=0.05):
        return DEFAULT_V2_PORT
    if is_port_open(host, DEFAULT_V1_PORT, timeout=0.05):
        return DEFAULT_V1_PORT

    return DEFAULT_PORT


def get_api_url(host: str = DEFAULT_HOST, port: int | None = None) -> str:
    target_port = port if port is not None else get_autodarts_port(host=host)
    return f"http://{host}:{target_port}/api"


def is_port_open(host: str = DEFAULT_HOST, port: int | None = None, timeout: float = 0.2) -> bool:
    target_port = port if port is not None else get_autodarts_port(host=host)
    try:
        with socket.create_connection((host, target_port), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def read_stored_auth(config_path: Path | None = None) -> tuple[str, str]:
    path = config_path or DEFAULT_CONFIG_PATH
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            upstream = data.get("upstream", data.get("auth", {}))
            if isinstance(upstream, dict):
                return str(upstream.get("board_id", "")).strip(), str(upstream.get("api_key", "")).strip()
        except Exception:
            logger.exception("Error reading config.toml auth block")
    return "", ""


def patch_engine_config(patch_dict: dict, host: str = DEFAULT_HOST, port: int | None = None) -> bool:
    """Send PATCH /api/config with partial JSON to live update engine settings."""
    target_port = port if port is not None else get_autodarts_port(host=host)
    if not is_port_open(host, target_port):
        return False
    try:
        payload = json.dumps(patch_dict).encode("utf-8")
        req = urllib.request.Request(
            f"http://{host}:{target_port}/api/config",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "SUIT/2.0"},
            method="PATCH"
        )
        with urllib.request.urlopen(req, timeout=1.5):
            return True
    except Exception as e:
        logger.debug(f"PATCH /api/config failed: {e}")
        return False


def save_stored_auth(
    board_id: str,
    api_key: str,
    config_path: Path | None = None,
    host: str = DEFAULT_HOST,
    port: int | None = None,
    refresh_token: str = "",
) -> bool:
    path = config_path or DEFAULT_CONFIG_PATH
    board_id = board_id.strip()
    api_key = api_key.strip()
    refresh_token = refresh_token.strip()
    target_port = port if port is not None else get_autodarts_port(config_path=path, host=host)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = path.read_text(encoding="utf-8") if path.exists() else ""

        # Update or add [upstream] (v2 native) — include refresh_token if available so
        # the autodarts binary can use /auth/v1/refresh to keep the session alive.
        if refresh_token:
            upstream_block = (
                f"[upstream]\n"
                f"api_key = '{api_key}'\n"
                f"refresh_token = '{refresh_token}'\n"
                f"board_id = '{board_id}'"
            )
        else:
            upstream_block = f"[upstream]\napi_key = '{api_key}'\nboard_id = '{board_id}'"
        if "[upstream]" in content:
            content = re.sub(r"(?ms)^\[upstream\].*?(?=(^\[|\Z))", upstream_block + "\n\n", content)
        else:
            content = content.rstrip() + f"\n\n{upstream_block}\n"

        # Update or add [auth] (v1 compatibility)
        auth_block = f"[auth]\napi_key = '{api_key}'\nboard_id = '{board_id}'"
        if "[auth]" in content:
            content = re.sub(r"(?ms)^\[auth\].*?(?=(^\[|\Z))", auth_block + "\n\n", content)
        else:
            content = f"{auth_block}\n\n" + content

        path.write_text(content.strip() + "\n", encoding="utf-8")
        logger.info("Saved auth credentials to config.toml ([upstream] & [auth])")

        # Live PATCH to engine if online
        patch_engine_config({"auth": {"board_id": board_id, "api_key": api_key}}, host=host, port=target_port)
        return True
    except Exception:
        logger.exception("Failed saving auth to config.toml")
        return False


def unlink_stored_auth(config_path: Path | None = None, host: str = DEFAULT_HOST, port: int | None = None) -> bool:
    target_port = port if port is not None else get_autodarts_port(config_path=config_path, host=host)
    if is_port_open(host, target_port):
        for endpoint in ("/api/config/reset", "/api/config/auth/reset"):
            try:
                req = urllib.request.Request(f"http://{host}:{target_port}{endpoint}", data=b"", method="POST")
                with urllib.request.urlopen(req, timeout=0.8):
                    break
            except Exception:
                pass

    return save_stored_auth("", "", config_path=config_path, host=host, port=target_port)


# --- Autodarts CLI Discovery ---

def get_autodarts_cli_binary() -> str | None:
    """Find the path to the autodarts CLI binary ('ad' or 'autodarts')."""
    for name in ["ad", "autodarts"]:
        found = shutil.which(name)
        if found:
            return found
    candidates = [
        Path.home() / ".local" / "bin" / "ad",
        Path.home() / ".local" / "bin" / "autodarts",
        Path.home() / ".local" / "share" / "autodarts" / "autodarts",
        Path("/usr/local/bin/ad"),
        Path("/usr/local/bin/autodarts"),
    ]
    for p in candidates:
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    return None



# --- V2 Service & Detection Controls ---


SPEED_PRESETS = {
    "Very low": {"detection": {"threshold": 24, "kernel": 7}, "motion": {"threshold": 24, "scale": 2.0}},
    "Low": {"detection": {"threshold": 20, "kernel": 5}, "motion": {"threshold": 20, "scale": 3.0}},
    "Default": {"detection": {"threshold": 16, "kernel": 5}, "motion": {"threshold": 16, "scale": 4.0}},
    "High": {"detection": {"threshold": 12, "kernel": 3}, "motion": {"threshold": 12, "scale": 5.0}},
    "Very high": {"detection": {"threshold": 8, "kernel": 3}, "motion": {"threshold": 8, "scale": 6.0}},
}


def set_detection_speed(speed: str, host: str = DEFAULT_HOST, port: int | None = None) -> bool:
    """Live patch detection speed sensitivity preset."""
    preset = SPEED_PRESETS.get(speed, SPEED_PRESETS["Default"])
    return patch_engine_config(preset, host=host, port=port)


def set_standby_minutes(minutes: int, host: str = DEFAULT_HOST, port: int | None = None) -> bool:
    """Live patch camera standby timeout."""
    return patch_engine_config({"motion": {"standby_minutes": minutes}}, host=host, port=port)


def control_detection_start(host: str = DEFAULT_HOST, port: int | None = None) -> bool:
    """Send PUT /api/start to resume camera motion detection."""
    target_port = port if port is not None else get_autodarts_port(host=host)
    if not is_port_open(host, target_port):
        return False
    try:
        req = urllib.request.Request(f"http://{host}:{target_port}/api/start", data=b"", method="PUT")
        with urllib.request.urlopen(req, timeout=2.0):
            return True
    except Exception:
        logger.exception("Failed to start Autodarts detection")
        return False


def control_detection_stop(host: str = DEFAULT_HOST, port: int | None = None) -> bool:
    """Send PUT /api/stop to pause camera motion detection."""
    target_port = port if port is not None else get_autodarts_port(host=host)
    if not is_port_open(host, target_port):
        return False
    try:
        req = urllib.request.Request(f"http://{host}:{target_port}/api/stop", data=b"", method="PUT")
        with urllib.request.urlopen(req, timeout=2.0):
            return True
    except Exception:
        logger.exception("Failed to stop Autodarts detection")
        return False


def control_detection_reset(host: str = DEFAULT_HOST, port: int | None = None) -> bool:
    """Send POST /api/reset to reset detection and clear calibration/throw state."""
    target_port = port if port is not None else get_autodarts_port(host=host)
    if not is_port_open(host, target_port):
        return False
    try:
        req = urllib.request.Request(f"http://{host}:{target_port}/api/reset", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=2.0):
            return True
    except Exception:
        logger.exception("Failed to reset Autodarts detection")
        return False


def control_calibrate(host: str = DEFAULT_HOST, port: int | None = None) -> tuple[bool, str]:
    """Trigger POST /api/config/calibration/auto to run auto calibration across cameras."""
    target_port = port if port is not None else get_autodarts_port(host=host)
    if not is_port_open(host, target_port):
        return False, "Autodarts engine is offline."

    # Pre-check: if the engine is blocked by no upstream connection, give a useful message.
    try:
        req_sys = urllib.request.Request(f"http://{host}:{target_port}/api/system", headers={"User-Agent": "SUIT/2.0"})
        with urllib.request.urlopen(req_sys, timeout=1.5) as resp:
            sys_data = json.loads(resp.read().decode("utf-8"))
            blocker = sys_data.get("blocker", "")
            connected = sys_data.get("state", {}).get("connected", False)
            if blocker == "no-connection" or not connected:
                return False, "Board not connected to Autodarts. Re-link your board first."
    except Exception:
        pass  # If /api/system fails, fall through and try calibration anyway.

    try:
        req = urllib.request.Request(f"http://{host}:{target_port}/api/config/calibration/auto", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            return True, "Calibration started."
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore").strip() or str(e)
        return False, f"Calibration failed: {err_msg}"
    except Exception as e:
        logger.exception("Failed triggering calibration")
        return False, f"Calibration error: {e}"



# --- Telemetry & Status Parsing ---

def fetch_cams_stats(host: str = DEFAULT_HOST, port: int | None = None) -> dict:
    target_port = port if port is not None else get_autodarts_port(host=host)
    try:
        req = urllib.request.Request(f"http://{host}:{target_port}/api/cams/stats", headers={"User-Agent": "SUIT/2.0"})
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def fetch_engine_config(host: str = DEFAULT_HOST, port: int | None = None) -> dict:
    target_port = port if port is not None else get_autodarts_port(host=host)
    try:
        req_cfg = urllib.request.Request(f"http://{host}:{target_port}/api/config", headers={"User-Agent": "SUIT/2.0"})
        with urllib.request.urlopen(req_cfg, timeout=1.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def fetch_system_telemetry(config_path: Path | None = None, host: str = DEFAULT_HOST, port: int | None = None) -> dict:
    """Fetch live telemetry from Autodarts v2 engine (/api/system)."""
    b_id, a_key = read_stored_auth(config_path=config_path)
    target_port = port if port is not None else get_autodarts_port(config_path=config_path, host=host)
    data = {
        "online": False,
        "version": "v2.0.0",
        "update_available": "",
        "state": "Stopped",
        "connected": False,
        "running": False,
        "event": "",
        "num_throws": 0,
        "fps": 0,
        "cpu_percent": 0.0,
        "memory_mb": 0.0,
        "resolution": "",
        "calibrated": False,
        "calibrating": False,
        "motion_state": "waiting",
        "cam_states": ["-", "-", "-"],
        "cams_count": 0,
        "board_id": b_id,
        "has_api_key": bool(a_key),
        "blocker": "",
    }

    if not is_port_open(host, target_port):
        return data

    try:
        req = urllib.request.Request(f"http://{host}:{target_port}/api/system", headers={"User-Agent": "SUIT/2.0"})
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            sys_json = json.loads(resp.read().decode("utf-8"))
            data["online"] = True
            ver = sys_json.get("version", "2.0.0")
            data["version"] = f"v{ver}" if not str(ver).startswith("v") else str(ver)
            data["update_available"] = sys_json.get("updateAvailable", "")
            data["blocker"] = sys_json.get("blocker", "")

            st = sys_json.get("state", {})
            data["state"] = st.get("status", "Running")
            data["connected"] = st.get("connected", False)
            data["running"] = st.get("running", False)
            data["event"] = st.get("event", "")
            data["num_throws"] = st.get("numThrows", 0)

            stats = sys_json.get("stats", {})
            data["fps"] = round(stats.get("fps", 0))
            data["cpu_percent"] = round(float(stats.get("cpuPercent", 0.0)), 1)
            mem_bytes = stats.get("memoryBytes", 0)
            data["memory_mb"] = round(mem_bytes / (1024 * 1024), 1)
            res = stats.get("resolution", {})
            if res.get("width") and res.get("height"):
                data["resolution"] = f"{res['width']}x{res['height']}"

            cal = sys_json.get("calibration", {})
            data["calibrated"] = bool(cal.get("calibrated", False) or sys_json.get("calibrated", False))
            data["calibrating"] = (sys_json.get("calibratingCams", 0) > 0)

            mot = sys_json.get("motion", {})
            if mot.get("isDart"):
                data["motion_state"] = "dart"
            elif mot.get("isHand"):
                data["motion_state"] = "hand"
            elif mot.get("isTakeoutFull") or mot.get("isTakeoutPartial"):
                data["motion_state"] = "takeout"
            elif mot.get("isStable"):
                data["motion_state"] = "stable"
            else:
                data["motion_state"] = "waiting"

            cam_stats = sys_json.get("camStats", [])
            data["cams_count"] = len(cam_stats)
    except Exception as e:
        logger.debug("Failed fetching /api/system: %s", e)

    return data


# Compatibility alias for callers and tests
fetch_telemetry = fetch_system_telemetry


# --- Configuration Reading & Writing ---

def parse_cam_device(cam_str: str) -> str:
    """Extract and resolve a local /dev/videoX or /dev/v4l node from a camera config entry.

    Supports:
      - Autodarts v2 URIs: 'native=/dev/video0&vid=0bdc&pid=2710&serial=...&location=1-1'
      - Standard Linux device paths: '/dev/video0', '/dev/v4l/by-path/...'
      - Numeric index strings: '0' -> '/dev/video0'
    """
    if not cam_str:
        return ""
    cam_str = str(cam_str).strip()
    if not cam_str:
        return ""

    # 1. If it's already an existing device node, return it directly
    if cam_str.startswith("/") and os.path.exists(cam_str):
        if "/dev/v4l/" in cam_str:
            try:
                return str(Path(cam_str).resolve())
            except Exception:
                return cam_str
        return cam_str

    # 2. Check for native=/dev/... in URI or raw /dev/video path in string
    match = re.search(r"(?:native=)?(/dev/video\d+|/dev/v4l/[^\s&]+)", cam_str)
    candidate = match.group(1) if match else None
    if candidate and os.path.exists(candidate):
        if "/dev/v4l/" in candidate:
            try:
                return str(Path(candidate).resolve())
            except Exception:
                return candidate
        return candidate

    # 3. Check for location= in URI if candidate path doesn't exist on system
    loc_match = re.search(r"[?&]location=([^&]+)", cam_str)
    if loc_match:
        loc = loc_match.group(1)
        by_path_dir = Path("/dev/v4l/by-path")
        if by_path_dir.exists():
            for p in by_path_dir.glob(f"*{loc}*video-index0"):
                if p.exists():
                    try:
                        return str(p.resolve())
                    except Exception:
                        return str(p)

    # 4. If candidate was extracted from URI, return it even if not currently plugged in
    if candidate:
        return candidate

    # 5. Check if numeric index like "0" -> "/dev/video0"
    if cam_str.isdigit():
        return f"/dev/video{cam_str}"

    return cam_str


def read_cam_config(config_path: Path | None = None, host: str = DEFAULT_HOST, port: int | None = None) -> dict:
    """Read camera and video settings from running engine or config.toml."""
    cfg = {
        "cams": ["", "", ""],
        "devices": ["", "", ""],
        "width": 1280,
        "height": 720,
        "fps": 30,
        "fps_max": 30,
        "auto_distortion": False,
        "standby_minutes": 15,
        "detection_speed": "Default"
    }

    def _apply_dict(data: dict):
        if "cam" in data:
            cam_data = data["cam"]
            raw_cams = cam_data.get("devices") or cam_data.get("cams", ["", "", ""])
            cams_list = (raw_cams + ["", "", ""])[:3]
            cfg["cams"] = [parse_cam_device(c) if c else "" for c in cams_list]
            cfg["devices"] = cams_list
            cfg["width"] = int(cam_data.get("width", 1280))
            cfg["height"] = int(cam_data.get("height", 720))
            cfg["fps"] = int(cam_data.get("fps", 30))
            cfg["fps_max"] = int(cam_data.get("fps_max", 30))
            cfg["auto_distortion"] = bool(cam_data.get("auto_distortion", False))
            if "standby_minutes" in cam_data:
                cfg["standby_minutes"] = int(cam_data["standby_minutes"])
        if "motion" in data and "standby_minutes" in data["motion"]:
            cfg["standby_minutes"] = int(data["motion"]["standby_minutes"])

    target_port = port if port is not None else get_autodarts_port(config_path=config_path, host=host)
    if is_port_open(host, target_port):
        data = fetch_engine_config(host, target_port)
        if data:
            _apply_dict(data)
            return cfg

    path = config_path or DEFAULT_CONFIG_PATH
    if path.exists():
        try:
            _apply_dict(tomllib.loads(path.read_text(encoding="utf-8")))
        except Exception:
            logger.exception("Error parsing config.toml for cam section")

    return cfg


def save_cam_config(
    cams: list[str],
    width: int,
    height: int,
    fps: int,
    auto_distortion: bool = False,
    standby_minutes: int = 15,
    config_path: Path | None = None,
    host: str = DEFAULT_HOST,
    port: int | None = None
) -> bool:
    """Save camera, video, lens correction, and standby settings to config.toml and live engine."""
    path = config_path or DEFAULT_CONFIG_PATH
    target_port = port if port is not None else get_autodarts_port(config_path=path, host=host)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = path.read_text(encoding="utf-8") if path.exists() else ""

        cams_formatted = (cams + ["", "", ""])[:3]

        # Preserve rich v2 URIs if the underlying camera node matches existing config
        if path.exists() and content:
            try:
                existing_toml = tomllib.loads(content)
                existing_cam = existing_toml.get("cam", {})
                existing_entries = existing_cam.get("devices") or existing_cam.get("cams", [])
                mapped_cams = []
                for c in cams_formatted:
                    if not c:
                        mapped_cams.append("")
                        continue
                    matched_entry = c
                    for old_entry in existing_entries:
                        if old_entry and parse_cam_device(old_entry) == c:
                            matched_entry = old_entry
                            break
                    mapped_cams.append(matched_entry)
                cams_formatted = mapped_cams
            except Exception:
                pass

        cams_str = "[" + ", ".join([f"'{c}'" for c in cams_formatted]) + "]"
        dist_str = "true" if auto_distortion else "false"

        cam_block = (
            f"[cam]\n"
            f"auto_distortion = {dist_str}\n"
            f"cams = {cams_str}\n"
            f"devices = {cams_str}\n"
            f"fps = {fps}\n"
            f"fps_max = {fps}\n"
            f"height = {height}\n"
            f"standby_minutes = {standby_minutes}\n"
            f"width = {width}"
        )

        if "[cam]" in content:
            content = re.sub(r"(?ms)^\[cam\].*?(?=(^\[|\Z))", cam_block + "\n\n", content)
        else:
            content = content.rstrip() + f"\n\n{cam_block}\n"

        # Ensure [motion] standby_minutes stays synchronized
        motion_block = f"[motion]\nstandby_minutes = {standby_minutes}"
        if "[motion]" in content:
            content = re.sub(r"(?ms)^\[motion\].*?(?=(^\[|\Z))", motion_block + "\n\n", content)
        else:
            content = content.rstrip() + f"\n\n{motion_block}\n"

        path.write_text(content.strip() + "\n", encoding="utf-8")
        logger.info("Saved camera configuration to config.toml")

        # Live PATCH if engine is running
        if is_port_open(host, target_port):
            patch_dict = {
                "cam": {
                    "cams": cams_formatted,
                    "devices": cams_formatted,
                    "width": width,
                    "height": height,
                    "fps": fps,
                    "fps_max": fps,
                    "auto_distortion": auto_distortion,
                },
                "motion": {
                    "standby_minutes": standby_minutes
                }
            }
            patch_engine_config(patch_dict, host=host, port=target_port)

        return True
    except Exception:
        logger.exception("Failed saving camera configuration")
        return False


# --- Camera Device & Resolution Discovery ---

def format_short_camera_label(card: str, bus: str = "", path: str = "") -> str:
    """Format a clean, concise camera label for dropdown UI selectors."""
    name = card.strip() if card else "Camera"
    if name.startswith("usb-"):
        name = re.sub(r"-video-index[0-9]+$", "", name)
        name = name.replace("usb-", "").replace("_", " ")
        name = re.sub(r"\s+", " ", name).strip()

    if ":" in name:
        parts = [p.strip() for p in name.split(":")]
        if len(parts) == 2 and parts[0] == parts[1]:
            name = parts[0]

    if any(k in name.lower() for k in ("usb camera", "usb 2.0 camera", "usb2.0 camera")):
        name = "USB Cam"
    elif name.lower() == "camera":
        name = "Cam"
    elif len(name) > 18:
        name = name[:16] + "…"

    short_id = ""
    if bus:
        m = re.search(r"-([0-9]+(?:\.[0-9]+)*)$", bus)
        if m:
            short_id = m.group(1)
        else:
            short_id = bus.replace("usb-", "")[-6:]
    elif path:
        dev_m = re.search(r"(video[0-9]+)", path)
        if dev_m:
            short_id = dev_m.group(1)

    if short_id:
        return f"{name} ({short_id})"
    return name


def get_available_cameras(host: str = DEFAULT_HOST, port: int | None = None) -> list[dict]:
    cams = [{"path": "", "label": "- none -", "full_name": "Unassigned"}]
    seen_paths = {""}
    target_port = port if port is not None else get_autodarts_port(host=host)

    # 1. Query Autodarts API /api/devices if online (authoritative source)
    if is_port_open(host, target_port):
        try:
            req = urllib.request.Request(f"http://{host}:{target_port}/api/devices", headers={"User-Agent": "SUIT/2.0"})
            with urllib.request.urlopen(req, timeout=0.8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list):
                    for dev in data:
                        formats = dev.get("formats", [])
                        path = formats[0].get("path", "") if formats else ""
                        card = dev.get("card", "Camera")
                        bus = dev.get("bus", "")
                        label = format_short_camera_label(card, bus, path)
                        full_name = f"{card} ({bus})" if bus else card
                        if path and path not in seen_paths:
                            cams.append({"path": path, "label": label, "full_name": full_name})
                            seen_paths.add(path)
                    if len(cams) > 1:
                        return cams
        except Exception:
            pass

    # 2. Fallback: Query system v4l devices when Autodarts is offline
    try:
        from core.usb_service import UsbService
        usb_cams = UsbService.get_camera_devices()
        for c in usb_cams:
            dev_name = c.get("dev", "")
            dev_path = f"/dev/{dev_name}"
            name = c.get("name", "Camera")
            label = format_short_camera_label(name, path=dev_path)
            full_name = f"{name} ({dev_path})"
            if dev_path not in seen_paths:
                cams.append({"path": dev_path, "label": label, "full_name": full_name})
                seen_paths.add(dev_path)
                try:
                    seen_paths.add(str(Path(dev_path).resolve()))
                except Exception:
                    pass

        # Also check /dev/v4l/by-id only for devices not already found
        by_id = Path("/dev/v4l/by-id")
        if by_id.exists():
            for link in sorted(by_id.iterdir()):
                if "index0" in link.name or not any(x in link.name for x in ["index1", "index2", "index3"]):
                    target = str(link.resolve())
                    if target not in seen_paths and str(link) not in seen_paths:
                        label = format_short_camera_label(link.name, path=target)
                        full_name = f"{link.name} ({target})"
                        cams.append({"path": str(link), "label": label, "full_name": full_name})
                        seen_paths.add(str(link))
                        seen_paths.add(target)
    except Exception:
        logger.exception("Error discovering camera devices")

    return cams


def get_camera_supported_resolutions(cam_path: str, host: str = DEFAULT_HOST, port: int | None = None) -> set[tuple[int, int]]:
    """Query supported resolutions (w, h) >= 640x480 for a specific camera device."""
    if not cam_path or not cam_path.strip():
        return set()

    res_set: set[tuple[int, int]] = set()
    norm_path = parse_cam_device(cam_path)
    real_path = ""
    try:
        real_path = str(Path(norm_path or cam_path).resolve())
    except Exception:
        pass

    target_port = port if port is not None else get_autodarts_port(host=host)

    # 1. Try Autodarts API /api/devices first
    if is_port_open(host, target_port):
        try:
            req = urllib.request.Request(f"http://{host}:{target_port}/api/devices", headers={"User-Agent": "SUIT/2.0"})
            with urllib.request.urlopen(req, timeout=0.8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list):
                    for dev in data:
                        for fmt in dev.get("formats", []):
                            p = fmt.get("path", "")
                            p_norm = parse_cam_device(p)
                            p_real = ""
                            try:
                                p_real = str(Path(p_norm or p).resolve())
                            except Exception:
                                pass
                            if p in (cam_path, norm_path) or (real_path and (p == real_path or p_real == real_path or p_norm == norm_path)):
                                for r in fmt.get("resolutions", []):
                                    w, h = r.get("width", 0), r.get("height", 0)
                                    if w >= 640 and h >= 480:
                                        res_set.add((w, h))
                                if res_set:
                                    return res_set
        except Exception:
            pass

    # 2. Query direct V4L ioctl on target device
    import fcntl
    import struct
    target_paths = [p for p in [norm_path, cam_path, real_path] if p and p.startswith("/dev/")]

    for p in target_paths:
        try:
            fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
            for fmt in [0x47504a4d, 0x56595559]:
                idx = 0
                while True:
                    buf = bytearray(44)
                    struct.pack_into('II', buf, 0, idx, fmt)
                    try:
                        fcntl.ioctl(fd, 0xc02c564a, buf)
                        f_type = struct.unpack_from('I', buf, 8)[0]
                        if f_type == 1:
                            w, h = struct.unpack_from('II', buf, 12)
                            if w >= 640 and h >= 480:
                                res_set.add((w, h))
                        idx += 1
                    except Exception:
                        break
            os.close(fd)
            if res_set:
                break
        except Exception:
            pass

    return res_set


def get_supported_resolutions(
    cam_paths: list[str] | None = None,
    host: str = DEFAULT_HOST,
    port: int | None = None
) -> list[tuple[int, int]]:
    """Returns sorted list of common resolutions (w >= 640, h >= 480).
    If cam_paths is provided, requires 3 distinct cameras and returns their intersection.
    """
    target_port = port if port is not None else get_autodarts_port(host=host)

    if cam_paths is not None:
        valid_paths = [p.strip() for p in cam_paths if p and p.strip()]
        if len(valid_paths) < 3 or len(set(valid_paths)) < 3:
            return []
        check_paths = valid_paths
    else:
        check_paths = [c["path"] for c in get_available_cameras(host=host, port=target_port) if c.get("path")]

    common: set[tuple[int, int]] | None = None
    for p in check_paths:
        s = get_camera_supported_resolutions(p, host=host, port=target_port)
        if not s:
            if cam_paths is not None:
                return []
            continue
        common = set(s) if common is None else (common & set(s))

    if common:
        return sorted(list(common), key=lambda x: (x[0], x[1]), reverse=True)

    if cam_paths is not None:
        return []

    # Fallback when no cameras are connected
    cur_cfg = read_cam_config(host=host, port=target_port)
    cur_w = cur_cfg.get("width", 1280)
    cur_h = cur_cfg.get("height", 720)
    candidates = {(cur_w, cur_h), (1920, 1080), (1280, 720), (640, 480)}
    return sorted(list(candidates), key=lambda x: (x[0], x[1]), reverse=True)


# --- Systemd Service State & Installation ---

def is_service_enabled() -> bool:
    """Check if systemd user unit autodarts.service is enabled."""
    user_unit = Path.home() / ".config" / "systemd" / "user" / "default.target.wants" / "autodarts.service"
    if user_unit.exists():
        return True
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-enabled", "autodarts.service"],
            capture_output=True,
            text=True,
            timeout=2
        )
        return proc.stdout.strip() == "enabled"
    except Exception:
        return False


def set_service_enabled(enabled: bool) -> bool:
    """Enable or disable systemd user unit and user lingering."""
    user = getpass.getuser()
    action = "enable" if enabled else "disable"
    try:
        subprocess.run(["systemctl", "--user", action, "autodarts.service"], capture_output=True, timeout=5)
        if enabled:
            subprocess.run(["loginctl", "enable-linger", user], capture_output=True, timeout=5)
        return True
    except Exception as e:
        logger.exception("Failed setting service enabled=%s: %s", enabled, e)
        return False


def is_autodarts_installed() -> bool:
    """Check if Autodarts binary or systemd service exists on the system."""
    user_unit = Path.home() / ".config" / "systemd" / "user" / "autodarts.service"
    sys_unit = Path("/etc/systemd/system/autodarts.service")
    v2_bin = Path.home() / ".local" / "share" / "autodarts" / "autodarts"
    user_bin = Path.home() / ".local" / "bin" / "autodarts"
    local_opt = Path.home() / ".local" / "opt" / "autodarts" / "autodarts"
    usr_bin = Path("/usr/local/bin/autodarts")

    return bool(
        user_unit.exists()
        or sys_unit.exists()
        or v2_bin.exists()
        or user_bin.exists()
        or local_opt.exists()
        or usr_bin.exists()
        or shutil.which("autodarts")
    )


def install_autodarts() -> tuple[bool, str]:
    """Install official Autodarts v2 release and configure user systemd service."""
    user = getpass.getuser()
    home = Path.home()

    # 1. Preserve existing credentials and camera settings
    preserved_bid, preserved_key = read_stored_auth()
    preserved_cam = read_cam_config()

    # 2. Stop and remove legacy v1 system service if present
    cleanup_v1_cmd = (
        "sudo -n systemctl stop autodarts.service autodartsupdater.service 2>/dev/null || true; "
        "sudo -n systemctl disable autodarts.service autodartsupdater.service 2>/dev/null || true; "
        "sudo -n rm -f /etc/systemd/system/autodarts.service "
        "/etc/systemd/system/autodartsupdater.service "
        "/etc/systemd/system/*.wants/autodarts.service "
        "/etc/systemd/system/*.wants/autodartsupdater.service 2>/dev/null || true; "
        "sudo -n systemctl daemon-reload 2>/dev/null || true"
    )
    try:
        subprocess.run(cleanup_v1_cmd, shell=True, capture_output=True, text=True, timeout=10)
    except Exception as e:
        logger.warning("Legacy v1 cleanup check warning: %s", e)

    # 3. Stop any stray foreground or background autodarts processes
    try:
        subprocess.run("pkill -9 -x autodarts 2>/dev/null || true", shell=True, timeout=5)
        for _ in range(6):
            if not is_port_open(DEFAULT_HOST, DEFAULT_V2_PORT, timeout=0.05) and not is_port_open(DEFAULT_HOST, DEFAULT_V1_PORT, timeout=0.05):
                break
            time.sleep(0.5)
    except Exception:
        pass

    # 4. Run official installer
    install_cmd = (
        f"curl -fsSL autodarts.sh | bash -s -- --headless && "
        f"sudo -n usermod -aG video {user} 2>/dev/null || true"
    )
    try:
        proc = subprocess.run(install_cmd, shell=True, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            logger.warning("Installer script returned %s: %s", proc.returncode, proc.stderr)
            return False, f"Installation script failed: {proc.stderr.strip() or 'Unknown error'}"
    except Exception as e:
        logger.exception("Failed executing autodarts.sh installer")
        return False, str(e)

    # 5. Locate binary
    bin_candidates = [
        home / ".local" / "share" / "autodarts" / "autodarts",
        home / ".local" / "bin" / "autodarts",
        Path("/usr/local/bin/autodarts"),
    ]
    autodarts_bin = ""
    for candidate in bin_candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            autodarts_bin = str(candidate.resolve())
            break
    if not autodarts_bin:
        which_bin = shutil.which("autodarts")
        if which_bin:
            autodarts_bin = str(Path(which_bin).resolve())

    if not autodarts_bin:
        return False, "Installation completed, but autodarts binary could not be found."

    # 6. Configure systemd user service (~/.config/systemd/user/autodarts.service)
    user_unit_dir = home / ".config" / "systemd" / "user"
    user_unit_file = user_unit_dir / "autodarts.service"
    unit_content = (
        "# Generated by SUIT for Autodarts v2\n"
        "[Unit]\n"
        "Description=Autodarts board\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "StartLimitIntervalSec=60\n"
        "StartLimitBurst=5\n\n"
        "[Service]\n"
        "Type=notify\n"
        "NotifyAccess=main\n"
        f"ExecStart={autodarts_bin} run\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "TimeoutStopSec=15\n"
        "WatchdogSec=30\n"
        "StateDirectory=autodarts\n"
        "StateDirectoryMode=0700\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )

    try:
        user_unit_dir.mkdir(parents=True, exist_ok=True)
        user_unit_file.write_text(unit_content, encoding="utf-8")
        logger.info("Wrote user systemd unit: %s", user_unit_file)
    except Exception as e:
        logger.exception("Failed writing user systemd unit")
        return False, f"Failed creating systemd user service: {e}"

    # Ensure required configuration and state directories exist cleanly
    ensure_autodarts_dirs()

    # 7. Restore preserved credentials and camera configuration before starting
    if preserved_bid or preserved_key:
        save_stored_auth(preserved_bid, preserved_key)
    if any(preserved_cam.get("cams", [])):
        save_cam_config(
            preserved_cam["cams"],
            preserved_cam.get("width", 1280),
            preserved_cam.get("height", 720),
            preserved_cam.get("fps", 30),
            auto_distortion=preserved_cam.get("auto_distortion", False),
            standby_minutes=preserved_cam.get("standby_minutes", 15)
        )

    # 8. Enable user lingering so service runs across reboots without GUI login
    try:
        subprocess.run(f"loginctl enable-linger {user} 2>/dev/null || true", shell=True, timeout=5)
    except Exception:
        pass

    # 9. Reload user systemd daemon, reset failure count, enable and start service
    service_cmd = (
        "systemctl --user daemon-reload && "
        "systemctl --user reset-failed autodarts.service 2>/dev/null || true; "
        "systemctl --user enable autodarts.service && "
        "systemctl --user restart autodarts.service"
    )
    try:
        svc_proc = subprocess.run(service_cmd, shell=True, capture_output=True, text=True, timeout=15)
        if svc_proc.returncode != 0:
            logger.warning("systemctl --user failed: %s", svc_proc.stderr)
    except Exception as e:
        logger.warning("Failed enabling systemd user service: %s", e)

    # 10. Verify API listener
    target_port = get_autodarts_port()
    api_ready = False
    for _ in range(10):
        time.sleep(0.5)
        if is_port_open(DEFAULT_HOST, target_port, timeout=0.1):
            api_ready = True
            break
    if not api_ready:
        logger.warning("Autodarts API port %s not yet listening, triggering second restart", target_port)
        subprocess.run("systemctl --user restart autodarts.service 2>/dev/null || true", shell=True, timeout=10)

    logger.info("Autodarts installation/upgrade completed successfully")
    return True, "Autodarts v2 installation completed and service started."


def uninstall_autodarts() -> tuple[bool, str]:
    """Stop, disable service, remove systemd unit and local config/binaries."""
    home = str(Path.home())
    cmd = (
        "systemctl --user stop autodarts.service 2>/dev/null || true; "
        "systemctl --user disable autodarts.service 2>/dev/null || true; "
        f"rm -f {home}/.config/systemd/user/autodarts.service "
        f"{home}/.config/systemd/user/default.target.wants/autodarts.service; "
        "systemctl --user daemon-reload 2>/dev/null || true; "
        "sudo -n systemctl stop autodarts 2>/dev/null || true; "
        "sudo -n systemctl disable autodarts 2>/dev/null || true; "
        "sudo -n rm -f /etc/systemd/system/autodarts.service /etc/systemd/system/autodartsupdater.service; "
        "sudo -n systemctl daemon-reload 2>/dev/null || true; "
        f"rm -rf {home}/.local/share/autodarts {home}/.local/opt/autodarts "
        f"{home}/.local/bin/autodarts {home}/.local/bin/ad {home}/.config/autodarts "
        f"{home}/.local/state/autodarts "
        f"/usr/local/bin/autodarts /usr/bin/autodarts; "
        "pkill -9 -f autodarts 2>/dev/null || true"
    )
    try:
        subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        logger.info("Autodarts uninstallation completed")
        return True, "Autodarts has been uninstalled."
    except Exception as e:
        logger.exception("Failed uninstalling Autodarts")
        return False, str(e)
