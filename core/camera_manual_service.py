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

    @classmethod
    def reset_defaults(cls, device_path: str) -> bool:
        """Reset all supported controls for the device to their factory defaults."""
        ctrls = cls.get_camera_controls(device_path)
        if not ctrls:
            return False

        all_ok = True
        for name, data in ctrls.items():
            if "default" in data:
                ok = cls.set_control(device_path, name, data["default"])
                if not ok:
                    all_ok = False
        return all_ok

    @classmethod
    def save_preset(cls, device_path: str, settings: dict, config_path: Path | None = None) -> bool:
        """Save a set of control adjustments for a device into configuration."""
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
        """Load saved camera presets from configuration."""
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
        """Apply all saved presets to their respective devices."""
        presets = cls.load_presets(config_path=config_path)
        if not presets:
            return True

        overall_ok = True
        for dev, settings in presets.items():
            for ctrl_name, val in settings.items():
                if not cls.set_control(dev, ctrl_name, val):
                    overall_ok = False
        return overall_ok
