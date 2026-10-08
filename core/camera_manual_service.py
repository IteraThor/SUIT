import re
import json
import subprocess
from pathlib import Path
from core.logger import get_logger

logger = get_logger("camera_manual")

DEFAULT_CONFIG_PATH = Path.home() / ".suit_camera_profiles.json"


class CameraManualService:
    DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_PATH

    @staticmethod
    def parse_v4l2_controls(raw_output: str) -> dict[str, dict]:
        """Parse the output of `v4l2-ctl --list-ctrls` into a structured dictionary."""
        controls: dict[str, dict] = {}
        for line in raw_output.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue

            # Format: <name> <hex_id> (<type>) : <attributes>
            left, right = line.split(":", 1)
            parts = left.strip().split()
            if len(parts) < 3:
                continue

            name = parts[0]
            hex_id = parts[1]
            ctrl_type_match = re.search(r"\(([^)]+)\)", parts[2])
            ctrl_type = ctrl_type_match.group(1) if ctrl_type_match else "int"

            attrs: dict = {
                "id": hex_id,
                "type": ctrl_type,
            }

            for token in right.strip().split():
                if "=" in token:
                    k, v = token.split("=", 1)
                    try:
                        attrs[k] = int(v)
                    except ValueError:
                        attrs[k] = v

            controls[name] = attrs

        return controls

    @classmethod
    def get_camera_controls(cls, device_path: str) -> dict[str, dict]:
        """Query supported V4L2 controls and their limits for a camera device."""
        try:
            res = subprocess.run(
                ["v4l2-ctl", "-d", device_path, "--list-ctrls"],
                capture_output=True,
                text=True,
                check=True,
            )
            return cls.parse_v4l2_controls(res.stdout)
        except Exception:
            logger.exception("Failed querying V4L2 controls for %s", device_path)
            return {}

    @classmethod
    def set_control(cls, device_path: str, control_name: str, value: int | bool) -> bool:
        """Set a single V4L2 control on a camera device."""
        try:
            val_str = str(int(value) if isinstance(value, bool) else value)
            cmd = ["v4l2-ctl", "-d", device_path, f"--set-ctrl={control_name}={val_str}"]
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            logger.debug("Set %s on %s to %s", control_name, device_path, val_str)
            return True
        except Exception:
            logger.exception("Failed setting %s=%s on %s", control_name, value, device_path)
            return False

    SERVICE_NAME = "suit-camera-settings.service"
    USER_UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME

    @classmethod
    def reset_defaults(cls, device_path: str) -> bool:
        """Reset all supported controls for the device to their factory defaults,
        skipping inactive dependent controls (e.g. shutter speed during auto exposure)."""
        ctrls = cls.get_camera_controls(device_path)
        if not ctrls:
            return False

        # Determine auto/manual modes first
        exp_auto_name = "auto_exposure" if "auto_exposure" in ctrls else ("exposure_auto" if "exposure_auto" in ctrls else None)
        wb_auto_name = "white_balance_automatic" if "white_balance_automatic" in ctrls else ("white_balance_temperature_auto" if "white_balance_temperature_auto" in ctrls else None)

        exp_default_auto = True
        if exp_auto_name and "default" in ctrls[exp_auto_name]:
            exp_default_auto = (ctrls[exp_auto_name]["default"] != 1)

        wb_default_auto = True
        if wb_auto_name and "default" in ctrls[wb_auto_name]:
            wb_default_auto = bool(ctrls[wb_auto_name]["default"])

        to_set: dict[str, int | bool] = {}

        # 1. Add mode controls first
        if exp_auto_name and "default" in ctrls[exp_auto_name]:
            to_set[exp_auto_name] = ctrls[exp_auto_name]["default"]
        if wb_auto_name and "default" in ctrls[wb_auto_name]:
            to_set[wb_auto_name] = ctrls[wb_auto_name]["default"]

        # 2. Add other controls
        for name, data in ctrls.items():
            if "default" not in data:
                continue
            if name in (exp_auto_name, wb_auto_name):
                continue
            if name == "exposure_time_absolute" and exp_default_auto:
                continue
            if name == "white_balance_temperature" and wb_default_auto:
                continue
            to_set[name] = data["default"]

        # Apply settings
        success_count = 0
        for name, val in to_set.items():
            if cls.set_control(device_path, name, val):
                success_count += 1
            else:
                logger.warning("Failed resetting control %s to %s on %s", name, val, device_path)

        return success_count > 0

    @classmethod
    def get_camera_control_settings_string(cls, device_path: str) -> str:
        """Format current camera controls into a v4l2-ctl --set-ctrl string."""
        ctrls = cls.get_camera_controls(device_path)
        if not ctrls:
            return ""

        parts = []
        # Exposure
        exp_key = "auto_exposure" if "auto_exposure" in ctrls else ("exposure_auto" if "exposure_auto" in ctrls else None)
        if exp_key:
            val = ctrls[exp_key].get("value", 3)
            parts.append(f"{exp_key}={val}")
            if val == 1 and "exposure_time_absolute" in ctrls:
                parts.append(f"exposure_time_absolute={ctrls['exposure_time_absolute'].get('value', 156)}")

        # White balance
        wb_key = "white_balance_automatic" if "white_balance_automatic" in ctrls else ("white_balance_temperature_auto" if "white_balance_temperature_auto" in ctrls else None)
        if wb_key:
            val = ctrls[wb_key].get("value", 1)
            parts.append(f"{wb_key}={val}")
            if not val and "white_balance_temperature" in ctrls:
                parts.append(f"white_balance_temperature={ctrls['white_balance_temperature'].get('value', 4600)}")

        # Additional standard controls
        for k in ("gain", "brightness", "contrast"):
            if k in ctrls and "value" in ctrls[k]:
                parts.append(f"{k}={ctrls[k]['value']}")

        return ",".join(parts)

    @classmethod
    def generate_service_unit_content(cls, cameras: list[str]) -> str:
        """Generate systemd user service unit content for camera settings on boot."""
        exec_lines = []
        v4l2_bin = "/usr/bin/v4l2-ctl"
        for dev in cameras:
            ctrls_str = cls.get_camera_control_settings_string(dev)
            if ctrls_str:
                exec_lines.append(f"ExecStart=-{v4l2_bin} -d {dev} --set-ctrl={ctrls_str}")

        exec_section = "\n".join(exec_lines) if exec_lines else f"ExecStart=-{v4l2_bin} --version"
        return f"""[Unit]
Description=SUIT Camera Settings on Boot
Before=autodarts.service
After=default.target

[Service]
Type=oneshot
{exec_section}
RemainAfterExit=yes

[Install]
WantedBy=default.target
"""

    @classmethod
    def is_boot_service_enabled(cls) -> bool:
        """Check if suit-camera-settings.service is enabled."""
        try:
            res = subprocess.run(
                ["systemctl", "--user", "is-enabled", cls.SERVICE_NAME],
                capture_output=True,
                text=True,
                timeout=2,
            )
            return res.returncode == 0 and "enabled" in res.stdout
        except Exception:
            return False

    @classmethod
    def enable_boot_service(cls, cameras: list[str], service_path: Path | None = None) -> bool:
        """Write camera settings systemd user unit and enable it."""
        target = service_path or cls.USER_UNIT_PATH
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            content = cls.generate_service_unit_content(cameras)
            target.write_text(content, encoding="utf-8")
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=5)
            res = subprocess.run(["systemctl", "--user", "enable", cls.SERVICE_NAME], capture_output=True, timeout=5)
            logger.info("Enabled camera boot service: %s", target)
            return res.returncode == 0
        except Exception:
            logger.exception("Failed enabling camera boot service")
            return False

    @classmethod
    def disable_boot_service(cls, service_path: Path | None = None) -> bool:
        """Disable and remove camera settings systemd user unit."""
        target = service_path or cls.USER_UNIT_PATH
        try:
            subprocess.run(["systemctl", "--user", "disable", "--now", cls.SERVICE_NAME], capture_output=True, timeout=5)
            if target.exists():
                target.unlink()
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=5)
            logger.info("Disabled camera boot service")
            return True
        except Exception:
            logger.exception("Failed disabling camera boot service")
            return False

    @classmethod
    def update_boot_service_if_enabled(cls, cameras: list[str], service_path: Path | None = None) -> bool:
        """Update systemd unit commands if the service is currently enabled."""
        if not cls.is_boot_service_enabled():
            return False
        target = service_path or cls.USER_UNIT_PATH
        try:
            content = cls.generate_service_unit_content(cameras)
            target.write_text(content, encoding="utf-8")
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=5)
            logger.debug("Updated camera boot service with current settings")
            return True
        except Exception:
            logger.exception("Failed updating camera boot service")
            return False

    @classmethod
    def save_preset(cls, device_path: str, settings: dict, config_path: Path | None = None) -> bool:
        """Deprecated: Save a set of control adjustments for a device into configuration."""
        target = config_path or DEFAULT_CONFIG_PATH
        try:
            data = cls.load_presets(config_path=target)
            data[device_path] = settings
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.info("Saved camera preset for %s to %s", device_path, target)
            return True
        except Exception:
            logger.exception("Failed saving camera preset to %s", target)
            return False

    @classmethod
    def load_presets(cls, config_path: Path | None = None) -> dict:
        """Deprecated: Load saved camera presets from configuration."""
        target = config_path or DEFAULT_CONFIG_PATH
        if target.exists():
            try:
                with open(target, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                logger.exception("Failed loading camera presets from %s", target)
        return {}

    @classmethod
    def apply_saved_presets(cls, config_path: Path | None = None) -> bool:
        """Deprecated: Apply all saved presets to their respective devices."""
        presets = cls.load_presets(config_path=config_path)
        if not presets:
            return True

        overall_ok = True
        for dev, settings in presets.items():
            for ctrl_name, val in settings.items():
                if not cls.set_control(dev, ctrl_name, val):
                    overall_ok = False
        return overall_ok

