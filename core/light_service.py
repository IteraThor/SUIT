import json
import urllib.request
import urllib.error
from pathlib import Path
from core.logger import get_logger

logger = get_logger("light")

CONFIG_DIR = Path.home() / ".config" / "suit"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "kiosk_config.json"

DEFAULT_LIGHT_CONFIG = {
    "light_enabled": False,
    "light_device_type": "wled",  # "wled" | "smart_plug"
    "light_ip": "",
    "light_port": 80,
}

class LightService:
    @classmethod
    def get_config_path(cls, custom_path: Path | None = None) -> Path:
        return custom_path or DEFAULT_CONFIG_PATH

    @classmethod
    def read_config(cls, custom_path: Path | None = None) -> dict:
        path = cls.get_config_path(custom_path)
        cfg = dict(DEFAULT_LIGHT_CONFIG)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for k in DEFAULT_LIGHT_CONFIG:
                        if k in data:
                            cfg[k] = data[k]
            except Exception as e:
                logger.warning("Failed reading light config from %s: %s", path, e)
        return cfg

    @classmethod
    def save_config(
        cls,
        enabled: bool,
        device_type: str = "wled",
        ip: str = "",
        port: int = 80,
        custom_path: Path | None = None
    ) -> bool:
        path = cls.get_config_path(custom_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = {}
            if path.exists():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(existing, dict):
                        existing = {}
                except Exception:
                    existing = {}

            existing["light_enabled"] = bool(enabled)
            existing["light_device_type"] = str(device_type).strip().lower()
            existing["light_ip"] = str(ip).strip()
            existing["light_port"] = int(port) if port else 80

            path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
            logger.info("Saved light config: enabled=%s, type=%s, ip=%s:%d", enabled, device_type, ip, port)
            return True
        except Exception as e:
            logger.exception("Failed saving light config to %s: %s", path, e)
            return False

    @classmethod
    def query_status(cls, config: dict | None = None, timeout: float = 0.8) -> tuple[bool, bool, str]:
        """
        Queries whether the device is reachable and whether it is currently turned ON.
        Returns: (is_reachable: bool, is_on: bool, message_or_error: str)
        """
        cfg = config or cls.read_config()
        if not cfg.get("light_enabled"):
            return False, False, "Dartboard light is disabled in settings"

        ip = cfg.get("light_ip", "").strip()
        if not ip:
            return False, False, "No device IP address configured"

        port = cfg.get("light_port", 80) or 80
        dev_type = cfg.get("light_device_type", "wled")

        if dev_type == "wled":
            return cls._query_wled_status(ip, port, timeout=timeout)
        elif dev_type in ("tasmota", "smart_plug"):
            return cls._query_tasmota_status(ip, port, timeout=timeout)
        elif dev_type == "shelly":
            return cls._query_shelly_status(ip, port, timeout=timeout)
        else:
            return False, False, f"Unsupported device type: {dev_type}"

    @classmethod
    def _is_tasmota_on(cls, data: dict) -> bool:
        if not isinstance(data, dict):
            return False
        for k, v in data.items():
            if k.upper().startswith("POWER"):
                return str(v).upper() in ("ON", "1", "TRUE")
        status_sec = data.get("Status", {})
        if isinstance(status_sec, dict) and "Power" in status_sec:
            return str(status_sec["Power"]) in ("1", "ON", "true", "True")
        return False

    @classmethod
    def _query_wled_status(cls, ip: str, port: int, timeout: float = 0.8) -> tuple[bool, bool, str]:
        url = f"http://{ip}:{port}/json/state"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SUIT-Kiosk"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                is_on = bool(data.get("on", False))
                return True, is_on, "OK"
        except urllib.error.URLError as e:
            logger.debug("WLED reachability error at %s: %s", url, e)
            return False, False, f"Unreachable ({e.reason})"
        except Exception as e:
            logger.debug("WLED query error at %s: %s", url, e)
            return False, False, str(e)

    @classmethod
    def _query_tasmota_status(cls, ip: str, port: int, timeout: float = 0.8) -> tuple[bool, bool, str]:
        url = f"http://{ip}:{port}/cm?cmnd=Power"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SUIT-Kiosk"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                is_on = cls._is_tasmota_on(data)
                return True, is_on, "OK"
        except urllib.error.URLError as e:
            logger.debug("Tasmota reachability error at %s: %s", url, e)
            return False, False, f"Unreachable ({e.reason})"
        except Exception as e:
            logger.debug("Tasmota query error at %s: %s", url, e)
            return False, False, str(e)

    @classmethod
    def _query_shelly_status(cls, ip: str, port: int, timeout: float = 0.8) -> tuple[bool, bool, str]:
        # Try Gen 1 /relay/0 first
        url = f"http://{ip}:{port}/relay/0"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SUIT-Kiosk"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return True, bool(data.get("ison", False)), "OK"
        except Exception:
            pass

        # Try Gen 2 /rpc/Switch.GetStatus?id=0
        url_gen2 = f"http://{ip}:{port}/rpc/Switch.GetStatus?id=0"
        try:
            req = urllib.request.Request(url_gen2, headers={"User-Agent": "SUIT-Kiosk"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return True, bool(data.get("output", False)), "OK"
        except Exception as e:
            return False, False, f"Shelly query error ({e})"

    @classmethod
    def set_power(cls, on: bool, config: dict | None = None, timeout: float = 1.2) -> tuple[bool, bool, str]:
        """
        Sets device power explicitly to ON or OFF.
        Returns: (success: bool, is_now_on: bool, message: str)
        """
        cfg = config or cls.read_config()
        ip = cfg.get("light_ip", "").strip()
        if not ip:
            return False, False, "No device IP address configured"

        port = cfg.get("light_port", 80) or 80
        dev_type = cfg.get("light_device_type", "wled")

        if dev_type == "wled":
            return cls._set_wled_power(ip, port, on=on, timeout=timeout)
        elif dev_type in ("tasmota", "smart_plug"):
            return cls._set_tasmota_power(ip, port, on=on, timeout=timeout)
        elif dev_type == "shelly":
            return cls._set_shelly_power(ip, port, on=on, timeout=timeout)
        else:
            return False, False, f"Unsupported device type: {dev_type}"

    @classmethod
    def _set_wled_power(cls, ip: str, port: int, on: bool, timeout: float = 1.2) -> tuple[bool, bool, str]:
        url = f"http://{ip}:{port}/json/state"
        payload = json.dumps({"on": bool(on), "v": True}).encode("utf-8")
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "SUIT-Kiosk"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                new_state = bool(data.get("on", False))
                return True, new_state, "Light turned ON" if new_state else "Light turned OFF"
        except Exception as e:
            cmd = "1" if on else "0"
            fallback_url = f"http://{ip}:{port}/win&T={cmd}"
            try:
                req_fallback = urllib.request.Request(fallback_url, headers={"User-Agent": "SUIT-Kiosk"})
                with urllib.request.urlopen(req_fallback, timeout=timeout) as _:
                    return True, bool(on), "Light turned ON" if on else "Light turned OFF"
            except Exception as e2:
                logger.error("Failed setting WLED power at %s: %s (fallback: %s)", url, e, e2)
                return False, False, f"Failed setting power ({e})"

    @classmethod
    def _set_tasmota_power(cls, ip: str, port: int, on: bool, timeout: float = 1.2) -> tuple[bool, bool, str]:
        cmd = "ON" if on else "OFF"
        url = f"http://{ip}:{port}/cm?cmnd=Power%20{cmd}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SUIT-Kiosk"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                is_on = cls._is_tasmota_on(data)
                return True, is_on, "Light turned ON" if is_on else "Light turned OFF"
        except urllib.error.URLError as e:
            return False, False, f"Unreachable ({e.reason})"
        except Exception as e:
            return False, False, f"Tasmota error ({e})"

    @classmethod
    def _set_shelly_power(cls, ip: str, port: int, on: bool, timeout: float = 1.2) -> tuple[bool, bool, str]:
        turn_cmd = "on" if on else "off"
        # Try Gen 1
        url = f"http://{ip}:{port}/relay/0?turn={turn_cmd}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SUIT-Kiosk"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                new_state = bool(data.get("ison", False))
                return True, new_state, "Light turned ON" if new_state else "Light turned OFF"
        except Exception:
            pass

        # Try Gen 2
        url_gen2 = f"http://{ip}:{port}/rpc/Switch.Set?id=0&on={'true' if on else 'false'}"
        try:
            req = urllib.request.Request(url_gen2, headers={"User-Agent": "SUIT-Kiosk"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return True, bool(on), "Light turned ON" if on else "Light turned OFF"
        except Exception as e:
            return False, False, f"Shelly error ({e})"

    @classmethod
    def toggle_light(cls, config: dict | None = None, timeout: float = 1.2) -> tuple[bool, bool, str]:
        """
        Toggles device power.
        Returns: (success: bool, is_now_on: bool, message: str)
        """
        cfg = config or cls.read_config()
        if not cfg.get("light_enabled"):
            return False, False, "Light is disabled in settings"

        ip = cfg.get("light_ip", "").strip()
        if not ip:
            return False, False, "No device IP address configured"

        port = cfg.get("light_port", 80) or 80
        dev_type = cfg.get("light_device_type", "wled")

        if dev_type == "wled":
            return cls._toggle_wled(ip, port, timeout=timeout)
        elif dev_type in ("tasmota", "smart_plug"):
            return cls._toggle_tasmota(ip, port, timeout=timeout)
        elif dev_type == "shelly":
            return cls._toggle_shelly(ip, port, timeout=timeout)
        else:
            return False, False, f"Unsupported device type: {dev_type}"

    @classmethod
    def _toggle_wled(cls, ip: str, port: int, timeout: float = 1.2) -> tuple[bool, bool, str]:
        url = f"http://{ip}:{port}/json/state"
        # In WLED JSON API, {"on": "t", "v": True} toggles power and returns the new state
        payload = json.dumps({"on": "t", "v": True}).encode("utf-8")
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "SUIT-Kiosk"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                new_state = bool(data.get("on", False))
                return True, new_state, "Light turned on" if new_state else "Light turned off"
        except Exception as e:
            # Fallback to HTTP GET toggle endpoint /win&T=2
            fallback_url = f"http://{ip}:{port}/win&T=2"
            try:
                req_fallback = urllib.request.Request(fallback_url, headers={"User-Agent": "SUIT-Kiosk"})
                with urllib.request.urlopen(req_fallback, timeout=timeout) as _:
                    # Verify new state
                    _, new_state, _ = cls._query_wled_status(ip, port, timeout=timeout)
                    return True, new_state, "Light turned on" if new_state else "Light turned off"
            except Exception as e2:
                logger.error("Failed toggling WLED at %s: %s (fallback error: %s)", url, e, e2)
                return False, False, f"Failed to toggle light ({e})"

    @classmethod
    def _toggle_tasmota(cls, ip: str, port: int, timeout: float = 1.2) -> tuple[bool, bool, str]:
        url = f"http://{ip}:{port}/cm?cmnd=Power%20TOGGLE"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SUIT-Kiosk"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                is_on = cls._is_tasmota_on(data)
                return True, is_on, "Light turned on" if is_on else "Light turned off"
        except urllib.error.URLError as e:
            return False, False, f"Unreachable ({e.reason})"
        except Exception as e:
            return False, False, f"Tasmota toggle error ({e})"

    @classmethod
    def _toggle_shelly(cls, ip: str, port: int, timeout: float = 1.2) -> tuple[bool, bool, str]:
        # Try Gen 1
        url = f"http://{ip}:{port}/relay/0?turn=toggle"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SUIT-Kiosk"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                new_state = bool(data.get("ison", False))
                return True, new_state, "Light turned on" if new_state else "Light turned off"
        except Exception:
            pass

        # Try Gen 2
        url_gen2 = f"http://{ip}:{port}/rpc/Switch.Toggle?id=0"
        try:
            req = urllib.request.Request(url_gen2, headers={"User-Agent": "SUIT-Kiosk"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                _, is_on, _ = cls._query_shelly_status(ip, port, timeout=timeout)
                return True, is_on, "Light turned on" if is_on else "Light turned off"
        except Exception as e:
            return False, False, f"Shelly toggle error ({e})"

    @classmethod
    def test_connection(cls, ip: str, device_type: str = "wled", port: int = 80, timeout: float = 1.2) -> tuple[bool, str]:
        """
        Tests reachability of the device and retrieves its model / version info if possible.
        Returns: (success: bool, info_message: str)
        """
        clean_ip = ip.strip()
        if not clean_ip:
            return False, "IP address cannot be empty"

        if device_type == "wled":
            url = f"http://{clean_ip}:{port}/json/info"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "SUIT-Kiosk"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    name = data.get("name", "WLED")
                    ver = data.get("ver", "")
                    led_count = data.get("leds", {}).get("count", "")
                    extra = f" ({name}, v{ver}"
                    if led_count:
                        extra += f", {led_count} LEDs"
                    extra += ")"
                    return True, f"Connected to WLED successfully{extra}"
            except urllib.error.URLError as e:
                return False, f"Connection failed: {e.reason}"
            except Exception as e:
                return False, f"Connection failed: {e}"
        elif device_type in ("tasmota", "smart_plug"):
            return cls._test_tasmota(clean_ip, port, timeout=timeout)
        elif device_type == "shelly":
            return cls._test_shelly(clean_ip, port, timeout=timeout)
        else:
            return False, f"Unsupported device type: {device_type}"

    @classmethod
    def _test_tasmota(cls, ip: str, port: int, timeout: float = 1.2) -> tuple[bool, str]:
        url = f"http://{ip}:{port}/cm?cmnd=Status%202"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SUIT-Kiosk"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                fw_ver = data.get("StatusFWR", {}).get("Version", "")
                ver_str = f" (v{fw_ver})" if fw_ver else ""
                return True, f"Connected to Tasmota Smart Plug{ver_str}"
        except Exception:
            try:
                url_basic = f"http://{ip}:{port}/cm?cmnd=Status"
                req_basic = urllib.request.Request(url_basic, headers={"User-Agent": "SUIT-Kiosk"})
                with urllib.request.urlopen(req_basic, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    dev_name = data.get("Status", {}).get("DeviceName", "Tasmota")
                    return True, f"Connected to {dev_name} Smart Plug"
            except Exception as e:
                return False, f"Connection failed: {e}"

    @classmethod
    def _test_shelly(cls, ip: str, port: int, timeout: float = 1.2) -> tuple[bool, str]:
        shelly_info = f"http://{ip}:{port}/shelly"
        try:
            req = urllib.request.Request(shelly_info, headers={"User-Agent": "SUIT-Kiosk"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                model = data.get("type", "Shelly")
                return True, f"Connected to {model} Smart Plug"
        except Exception as e:
            return False, f"Connection failed: {e}"

