import os
import socket
import re
import json
import shutil
import time
import tomllib
import urllib.request
from pathlib import Path
from core.logger import get_logger

logger = get_logger("autodarts")

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "autodarts" / "config.toml"
USER_UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / "autodarts.service"
SYSTEM_UNIT_PATH = Path("/etc/systemd/system/autodarts.service")

DEFAULT_HOST = os.environ.get("AUTODARTS_HOST", "127.0.0.1")
DEFAULT_V1_PORT = 3180
DEFAULT_V2_PORT = 3182
DEFAULT_PORT = int(os.environ.get("AUTODARTS_PORT", "3180"))
DEFAULT_API_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/api"


def get_autodarts_port(config_path: Path | None = None, host: str = DEFAULT_HOST) -> int:
    """Dynamically resolve the Autodarts API port:
    1. AUTODARTS_PORT environment variable if set.
    2. config.toml [api] port or [host] port.
    3. Active listening socket (probe 3180 first, then 3182).
    4. Fallback to 3180.
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

    # Probe live ports (fast socket check)
    if is_port_open(host, DEFAULT_V1_PORT, timeout=0.05):
        return DEFAULT_V1_PORT
    if is_port_open(host, DEFAULT_V2_PORT, timeout=0.05):
        return DEFAULT_V2_PORT

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
    board_id = ""
    api_key = ""
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            # Prioritize [upstream] (v2), fallback to [auth] (v1)
            upstream = data.get("upstream", {})
            if isinstance(upstream, dict):
                board_id = str(upstream.get("board_id", "")).strip()
                api_key = str(upstream.get("api_key", "")).strip()

            if not board_id or not api_key:
                auth = data.get("auth", {})
                if isinstance(auth, dict):
                    if not board_id:
                        board_id = str(auth.get("board_id", "")).strip()
                    if not api_key:
                        api_key = str(auth.get("api_key", "")).strip()
            if board_id or api_key:
                return board_id, api_key
        except Exception:
            pass

        # Regex fallback
        try:
            content = path.read_text(encoding="utf-8")
            b_match = re.search(r"board_id\s*=\s*['\"]([^'\"]*)['\"]", content)
            k_match = re.search(r"api_key\s*=\s*['\"]([^'\"]*)['\"]", content)
            if b_match:
                board_id = b_match.group(1).strip()
            if k_match:
                api_key = k_match.group(1).strip()
        except Exception:
            logger.exception("Error reading config.toml auth block")
    return board_id, api_key


def save_stored_auth(
    board_id: str,
    api_key: str,
    config_path: Path | None = None,
    host: str = DEFAULT_HOST,
    port: int | None = None
) -> bool:
    path = config_path or DEFAULT_CONFIG_PATH
    board_id = board_id.strip()
    api_key = api_key.strip()
    target_port = port if port is not None else get_autodarts_port(config_path=path, host=host)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = path.read_text(encoding="utf-8") if path.exists() else ""

        # Update or add [auth] (v1 compatibility)
        auth_block = f"[auth]\napi_key = '{api_key}'\nboard_id = '{board_id}'"
        if "[auth]" in content:
            content = re.sub(r"(?ms)^\[auth\].*?(?=(^\[|\Z))", auth_block + "\n\n", content)
        else:
            content = f"{auth_block}\n\n" + content

        # Update or add [upstream] (v2 native)
        upstream_block = f"[upstream]\napi_key = '{api_key}'\nboard_id = '{board_id}'"
        if "[upstream]" in content:
            content = re.sub(r"(?ms)^\[upstream\].*?(?=(^\[|\Z))", upstream_block + "\n\n", content)
        else:
            content = content.rstrip() + f"\n\n{upstream_block}\n"

        path.write_text(content.strip() + "\n", encoding="utf-8")
        logger.info("Saved auth credentials to config.toml ([auth] & [upstream])")

        # Live PATCH to engine if alive
        if is_port_open(host, target_port):
            try:
                payload = json.dumps({"auth": {"board_id": board_id, "api_key": api_key}}).encode("utf-8")
                req = urllib.request.Request(
                    f"http://{host}:{target_port}/api/config",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="PATCH"
                )
                with urllib.request.urlopen(req, timeout=0.8):
                    pass
            except Exception as e:
                logger.debug(f"Engine PATCH skipped/failed: {e}")
        return True
    except Exception:
        logger.exception("Failed saving auth to config.toml")
        return False


def unlink_stored_auth(config_path: Path | None = None, host: str = DEFAULT_HOST, port: int | None = None) -> bool:
    target_port = port if port is not None else get_autodarts_port(config_path=config_path, host=host)
    if is_port_open(host, target_port):
        # 1. Try PATCH empty credentials
        try:
            payload = json.dumps({"auth": {"board_id": "", "api_key": ""}}).encode("utf-8")
            req = urllib.request.Request(
                f"http://{host}:{target_port}/api/config",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="PATCH"
            )
            with urllib.request.urlopen(req, timeout=0.8):
                pass
        except Exception:
            pass

        # 2. Try POST /api/config/reset (v2) or /api/config/auth/reset (v1)
        for endpoint in ("/api/config/reset", "/api/config/auth/reset"):
            try:
                req = urllib.request.Request(f"http://{host}:{target_port}{endpoint}", data=b"", method="POST")
                with urllib.request.urlopen(req, timeout=0.8):
                    break
            except Exception:
                pass

    return save_stored_auth("", "", config_path=config_path, host=host, port=target_port)


def control_detection_start(host: str = DEFAULT_HOST, port: int | None = None) -> bool:
    """Send PUT /api/start to resume camera motion detection."""
    target_port = port if port is not None else get_autodarts_port(host=host)
    if not is_port_open(host, target_port):
        return False
    try:
        req = urllib.request.Request(f"http://{host}:{target_port}/api/start", data=b"", method="PUT")
        with urllib.request.urlopen(req, timeout=1.5):
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
        with urllib.request.urlopen(req, timeout=1.5):
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
        with urllib.request.urlopen(req, timeout=1.5):
            return True
    except Exception:
        logger.exception("Failed to reset Autodarts detection")
        return False


def fetch_cams_stats(host: str = DEFAULT_HOST, port: int | None = None) -> dict:
    """Fetch live camera telemetry (/api/cams/stats) from running Autodarts engine."""
    target_port = port if port is not None else get_autodarts_port(host=host)
    try:
        req_stats = urllib.request.Request(f"http://{host}:{target_port}/api/cams/stats", headers={"User-Agent": "SUIT"})
        with urllib.request.urlopen(req_stats, timeout=0.8) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.debug("Failed to fetch camera stats from %s:%s: %s", host, target_port, e)
        return {}


def fetch_engine_config(host: str = DEFAULT_HOST, port: int | None = None) -> dict:
    """Fetch running configuration (/api/config) from Autodarts engine."""
    target_port = port if port is not None else get_autodarts_port(host=host)
    try:
        req_cfg = urllib.request.Request(f"http://{host}:{target_port}/api/config", headers={"User-Agent": "SUIT"})
        with urllib.request.urlopen(req_cfg, timeout=1.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.debug("Failed to fetch engine config from %s:%s: %s", host, target_port, e)
        return {}


def fetch_telemetry(config_path: Path | None = None, host: str = DEFAULT_HOST, port: int | None = None) -> dict:
    b_id, a_key = read_stored_auth(config_path=config_path)
    target_port = port if port is not None else get_autodarts_port(config_path=config_path, host=host)
    data = {
        "online": False,
        "version": "Unknown",
        "state": "Stopped",
        "connected": False,
        "running": False,
        "event": "",
        "cams_count": 0,
        "resolution": "",
        "fps": 0,
        "board_id": b_id,
        "has_api_key": bool(a_key),
        "num_throws": 0
    }

    if not is_port_open(host, target_port):
        return data

    try:
        req_state = urllib.request.Request(f"http://{host}:{target_port}/api/state", headers={"User-Agent": "SUIT"})
        with urllib.request.urlopen(req_state, timeout=1.0) as resp:
            state_json = json.loads(resp.read().decode("utf-8"))
            data["online"] = True
            data["state"] = state_json.get("status", "Running")
            data["connected"] = state_json.get("connected", False)
            data["running"] = state_json.get("running", False)
            data["event"] = state_json.get("event", "")
            data["num_throws"] = state_json.get("numThrows", 0)
    except Exception:
        return data

    # 1. Fetch official engine version
    try:
        req_ver = urllib.request.Request(f"http://{host}:{target_port}/api/version", headers={"User-Agent": "SUIT"})
        with urllib.request.urlopen(req_ver, timeout=0.8) as resp:
            ver_text = resp.read().decode("utf-8").strip()
            if ver_text and not ver_text.startswith("<"):
                data["version"] = f"v{ver_text}" if not ver_text.startswith("v") else ver_text
    except Exception:
        pass

    # 2. Fetch live camera stats (active resolution and per-camera FPS)
    stats = fetch_cams_stats(host, target_port)
    fps_list = stats.get("fps", [])
    if fps_list:
        data["cams_count"] = len(fps_list)
        data["fps"] = round(sum(fps_list) / len(fps_list))
    res = stats.get("resolution", {})
    if res.get("width") and res.get("height"):
        data["resolution"] = f"{res['width']}x{res['height']}"

    # 3. Fallback to /api/config if stats didn't populate resolution/cams
    if not data["resolution"] or data["cams_count"] == 0:
        cfg_json = fetch_engine_config(host, target_port)
        cams = [c for c in cfg_json.get("cam", {}).get("cams", []) if c and c.strip()]
        if not data["cams_count"]:
            data["cams_count"] = len(cams)
        w = cfg_json.get("cam", {}).get("width", 0)
        h = cfg_json.get("cam", {}).get("height", 0)
        if not data["resolution"] and w and h:
            data["resolution"] = f"{w}x{h}"
        if not data["fps"]:
            data["fps"] = cfg_json.get("cam", {}).get("fps", 0)

    return data


def read_cam_config(config_path: Path | None = None, host: str = DEFAULT_HOST, port: int | None = None) -> dict:
    cfg = {
        "cams": ["", "", ""],
        "width": 1280,
        "height": 720,
        "fps": 30,
        "fps_max": 30
    }
    target_port = port if port is not None else get_autodarts_port(config_path=config_path, host=host)

    # 1. Try reading from running API first
    if is_port_open(host, target_port):
        data = fetch_engine_config(host, target_port)
        if "cam" in data:
            cam_data = data["cam"]
            raw_cams = cam_data.get("devices") or cam_data.get("cams", ["", "", ""])
            cams_list = (raw_cams + ["", "", ""])[:3]
            return {
                "cams": cams_list,
                "width": int(cam_data.get("width", 1280)),
                "height": int(cam_data.get("height", 720)),
                "fps": int(cam_data.get("fps", 30)),
                "fps_max": int(cam_data.get("fps_max", 30))
            }

    # 2. Fallback to ~/.config/autodarts/config.toml
    path = config_path or DEFAULT_CONFIG_PATH
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            if "cam" in data:
                cam_data = data["cam"]
                raw_cams = cam_data.get("devices") or cam_data.get("cams", ["", "", ""])
                cams_list = (raw_cams + ["", "", ""])[:3]
                return {
                    "cams": cams_list,
                    "width": int(cam_data.get("width", 1280)),
                    "height": int(cam_data.get("height", 720)),
                    "fps": int(cam_data.get("fps", 30)),
                    "fps_max": int(cam_data.get("fps_max", 30))
                }
        except Exception:
            logger.exception("Error parsing config.toml for cam section")

    return cfg


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
    elif len(name) > 16:
        name = name[:14] + "…"

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
    cams = [{"path": "", "label": "Select camera", "full_name": "Select camera"}]
    seen_paths = {""}
    target_port = port if port is not None else get_autodarts_port(host=host)

    # 1. Query Autodarts API /api/devices if online
    if is_port_open(host, target_port):
        try:
            req = urllib.request.Request(f"http://{host}:{target_port}/api/devices", headers={"User-Agent": "SUIT"})
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
        except Exception:
            pass

    # 2. Query system v4l devices
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

        by_id = Path("/dev/v4l/by-id")
        if by_id.exists():
            for link in sorted(by_id.iterdir()):
                if "index0" in link.name or not any(x in link.name for x in ["index1", "index2", "index3"]):
                    target = str(link.resolve())
                    label = format_short_camera_label(link.name, path=target)
                    full_name = f"{link.name} ({target})"
                    if str(link) not in seen_paths:
                        cams.append({"path": str(link), "label": label, "full_name": full_name})
                        seen_paths.add(str(link))
    except Exception:
        logger.exception("Error discovering camera devices")

    return cams


def get_camera_supported_resolutions(cam_path: str, host: str = DEFAULT_HOST, port: int | None = None) -> set[tuple[int, int]]:
    """Query supported resolutions (w, h) >= 640x480 for a specific camera device."""
    if not cam_path or not cam_path.strip():
        return set()

    res_set: set[tuple[int, int]] = set()
    real_path = ""
    try:
        real_path = str(Path(cam_path).resolve())
    except Exception:
        pass

    target_port = port if port is not None else get_autodarts_port(host=host)

    # 1. Try Autodarts API /api/devices first
    if is_port_open(host, target_port):
        try:
            req = urllib.request.Request(f"http://{host}:{target_port}/api/devices", headers={"User-Agent": "SUIT"})
            with urllib.request.urlopen(req, timeout=0.8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list):
                    for dev in data:
                        for fmt in dev.get("formats", []):
                            p = fmt.get("path", "")
                            p_real = ""
                            try:
                                p_real = str(Path(p).resolve())
                            except Exception:
                                pass
                            if p == cam_path or (real_path and (p == real_path or p_real == real_path)):
                                for r in fmt.get("resolutions", []):
                                    w, h = r.get("width", 0), r.get("height", 0)
                                    if w >= 640 and h >= 480:
                                        res_set.add((w, h))
                                if res_set:
                                    return res_set
        except Exception:
            pass

    # 2. Query direct V4L ioctl on target device
    import fcntl, struct
    target_paths = [cam_path]
    if real_path and real_path != cam_path:
        target_paths.append(real_path)

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
    """
    Returns sorted list of common resolutions (w >= 640, h >= 480).
    If cam_paths is provided:
      - Requires all 3 camera slots to be selected with valid, non-empty, distinct paths.
      - Returns only the intersection of supported resolutions across the 3 selected cameras.
      - If fewer than 3 cameras are provided/valid, returns an empty list [].
    If cam_paths is None:
      - Fallback discovery across all detected devices.
    """
    target_port = port if port is not None else get_autodarts_port(host=host)

    if cam_paths is not None:
        valid_paths = [p.strip() for p in cam_paths if p and p.strip()]
        # Require all 3 cameras to be selected and distinct
        if len(valid_paths) < 3 or len(set(valid_paths)) < 3:
            return []

        common: set[tuple[int, int]] | None = None
        for p in valid_paths:
            s = get_camera_supported_resolutions(p, host=host, port=target_port)
            if not s:
                return []
            if common is None:
                common = set(s)
            else:
                common = common.intersection(s)

        if common:
            return sorted(list(common), key=lambda x: (x[0], x[1]), reverse=True)
        return []

    # 1. Query /api/devices from Autodarts engine (exact firmware resolutions)
    devices = []
    if is_port_open(host, target_port):
        try:
            req = urllib.request.Request(f"http://{host}:{target_port}/api/devices", headers={"User-Agent": "SUIT"})
            with urllib.request.urlopen(req, timeout=0.8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list):
                    devices = data
        except Exception:
            pass

    device_resolutions = []
    for dev in devices:
        formats = dev.get("formats", [])
        if formats:
            dev_res = []
            for r in formats[0].get("resolutions", []):
                w, h = r.get("width", 0), r.get("height", 0)
                if w >= 640 and h >= 480:
                    dev_res.append((w, h))
            if dev_res:
                device_resolutions.append(set(dev_res))

    # 2. If API was offline/empty, query direct V4L ioctl on connected cameras
    if not device_resolutions:
        import fcntl, struct
        dev_paths = []
        by_id = Path("/dev/v4l/by-id")
        if by_id.exists():
            for p in by_id.iterdir():
                if "index0" in p.name or not any(x in p.name for x in ["index1", "index2", "index3"]):
                    dev_paths.append(str(p))
        if not dev_paths:
            v4l_dir = Path("/dev")
            dev_paths = [str(p) for p in sorted(v4l_dir.glob("video*"))]

        for d_path in dev_paths:
            res_set = set()
            try:
                fd = os.open(d_path, os.O_RDONLY | os.O_NONBLOCK)
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
            except Exception:
                pass
            if res_set:
                device_resolutions.append(res_set)

    # Calculate intersection across cameras if multiple cameras are detected
    if device_resolutions:
        common = device_resolutions[0]
        for s in device_resolutions[1:]:
            if s:
                common = common.intersection(s)
        if common:
            return sorted(list(common), key=lambda x: (x[0], x[1]), reverse=True)

    # Fallback when no cameras are connected: show current resolution + standard fallbacks
    cur_cfg = read_cam_config(host=host, port=target_port)
    cur_w = cur_cfg.get("width", 1280)
    cur_h = cur_cfg.get("height", 720)
    candidates = {(cur_w, cur_h), (1920, 1080), (1280, 720), (640, 480)}
    return sorted(list(candidates), key=lambda x: (x[0], x[1]), reverse=True)


def save_cam_config(
    cams: list[str],
    width: int,
    height: int,
    fps: int,
    config_path: Path | None = None,
    host: str = DEFAULT_HOST,
    port: int | None = None
) -> bool:
    path = config_path or DEFAULT_CONFIG_PATH
    target_port = port if port is not None else get_autodarts_port(config_path=path, host=host)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = path.read_text(encoding="utf-8") if path.exists() else ""

        cams_formatted = (cams + ["", "", ""])[:3]
        cams_str = "[" + ", ".join([f"'{c}'" for c in cams_formatted]) + "]"
        # Save both cams (v1) and devices (v2) for full interoperability
        cam_block = (
            f"[cam]\n"
            f"cams = {cams_str}\n"
            f"devices = {cams_str}\n"
            f"width = {width}\n"
            f"height = {height}\n"
            f"fps = {fps}\n"
            f"fps_max = {fps}"
        )

        if "[cam]" in content:
            content = re.sub(r"(?ms)^\[cam\].*?(?=(^\[|\Z))", cam_block + "\n\n", content)
        else:
            content = content.rstrip() + f"\n\n{cam_block}\n"

        path.write_text(content.strip() + "\n", encoding="utf-8")
        logger.info("Saved camera configuration to config.toml")

        # Live PATCH if engine is running
        if is_port_open(host, target_port):
            try:
                payload = json.dumps({
                    "cam": {
                        "cams": cams_formatted,
                        "width": width,
                        "height": height,
                        "fps": fps,
                        "fps_max": fps
                    }
                }).encode("utf-8")
                req = urllib.request.Request(
                    f"http://{host}:{target_port}/api/config",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="PATCH"
                )
                with urllib.request.urlopen(req, timeout=1.0):
                    pass
                logger.info("Sent live camera PATCH to engine")
            except Exception as e:
                logger.debug(f"Live camera PATCH skipped: {e}")

        return True
    except Exception:
        logger.exception("Failed saving camera configuration")
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
    """
    Install official Autodarts v2 release or upgrade from v1:
    - Preserves existing cloud board credentials and camera configuration.
    - Cleans up legacy v1 system-wide systemd units (/etc/systemd/system/autodarts.service).
    - Runs official headless installer: curl -fsSL autodarts.sh | bash -s -- --headless
    - Sets video group permissions.
    - Configures native systemd user unit (~/.config/systemd/user/autodarts.service).
    - Enables systemd user lingering via loginctl so daemon starts on boot.
    - Enables and restarts the user service.
    """
    import getpass
    import subprocess

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
        # Allow kernel sockets to clear TIME_WAIT
        for _ in range(6):
            if not is_port_open(DEFAULT_HOST, DEFAULT_V1_PORT, timeout=0.05) and not is_port_open(DEFAULT_HOST, DEFAULT_V2_PORT, timeout=0.05):
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

    # 7. Enable user lingering so service runs across reboots without GUI login
    try:
        subprocess.run(f"loginctl enable-linger {user} 2>/dev/null || true", shell=True, timeout=5)
    except Exception:
        pass

    # 8. Reload user systemd daemon, enable and start service
    service_cmd = (
        "systemctl --user daemon-reload && "
        "systemctl --user enable autodarts.service && "
        "systemctl --user restart autodarts.service"
    )
    try:
        svc_proc = subprocess.run(service_cmd, shell=True, capture_output=True, text=True, timeout=15)
        if svc_proc.returncode != 0:
            logger.warning("systemctl --user failed: %s", svc_proc.stderr)
    except Exception as e:
        logger.warning("Failed enabling systemd user service: %s", e)

    # 9. Restore preserved credentials and camera configuration
    if preserved_bid or preserved_key:
        save_stored_auth(preserved_bid, preserved_key)
    if any(preserved_cam.get("cams", [])):
        save_cam_config(
            preserved_cam["cams"],
            preserved_cam.get("width", 1280),
            preserved_cam.get("height", 720),
            preserved_cam.get("fps", 30)
        )

    # 10. Verify API listener bound cleanly; retry restart if socket had lingering collision
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
    import subprocess
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
        f"/usr/local/bin/autodarts /usr/bin/autodarts; "
        "pkill -9 -f autodarts 2>/dev/null || true"
    )
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        logger.info("Autodarts uninstallation completed")
        return True, "Autodarts has been uninstalled."
    except Exception as e:
        logger.exception("Failed uninstalling Autodarts")
        return False, str(e)


