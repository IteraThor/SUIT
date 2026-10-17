import json
import re
import shutil
import subprocess
from pathlib import Path
from core.logger import get_logger
from core.v4l2_device import V4L2Device

logger = get_logger("camera_manual")

DEFAULT_CONFIG_PATH = Path.home() / ".suit_camera_profiles.json"


class CameraManualService:
    DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_PATH
    SERVICE_NAME = "suit-camera-settings.service"
    USER_UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME

    @classmethod
    def parse_v4l2_controls(cls, raw_output: str) -> dict[str, dict]:
        """Parse the output of v4l2-ctl into a structured dictionary."""
        controls = V4L2Device.parse_v4l2_ctl_output(raw_output)
        return {name: ctrl.to_dict() for name, ctrl in controls.items()}

    @classmethod
    def get_camera_controls(cls, device_path: str) -> dict[str, dict]:
        """Query supported V4L2 controls and their limits for a camera device via V4L2Device."""
        try:
            dev = V4L2Device(device_path)
            controls = dev.query_controls()
            return {name: ctrl.to_dict() for name, ctrl in controls.items()}
        except Exception:
            logger.exception("Failed querying V4L2 controls for %s", device_path)
            return {}

    @classmethod
    def set_control(cls, device_path: str, control_name: str, value: int | bool) -> bool:
        """Set a single V4L2 control on a camera device via V4L2Device."""
        try:
            dev = V4L2Device(device_path)
            val = int(value) if isinstance(value, bool) else int(value)
            return dev.set_control(control_name, val)
        except Exception:
            logger.exception("Failed setting %s=%s on %s", control_name, value, device_path)
            return False

    @classmethod
    def reset_defaults(cls, device_path: str) -> bool:
        """Reset all supported controls for the device to their factory defaults via V4L2Device."""
        try:
            dev = V4L2Device(device_path)
            return dev.reset_to_defaults()
        except Exception:
            logger.exception("Failed resetting camera defaults on %s", device_path)
            return False

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
        for k in ("gain", "sharpness", "contrast", "gamma", "brightness", "saturation", "power_line_frequency"):
            if k in ctrls and "value" in ctrls[k]:
                parts.append(f"{k}={ctrls[k]['value']}")

        return ",".join(parts)

    @classmethod
    def generate_service_unit_content(cls, cameras: list[str]) -> str:
        """Generate systemd user service unit content for camera settings on boot."""
        exec_lines = []
        v4l2_bin = shutil.which("v4l2-ctl") or "/usr/bin/v4l2-ctl"
        for dev in cameras:
            ctrls_str = cls.get_camera_control_settings_string(dev)
            if ctrls_str:
                exec_lines.append(f"ExecStart=-{v4l2_bin} -d {dev} --set-ctrl={ctrls_str}")

        exec_section = "\n".join(exec_lines) if exec_lines else "ExecStart=/bin/true"
        return f"""[Unit]
Description=SUIT Camera Settings on Boot
Before=autodarts.service

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
    def heal_boot_service_if_needed(cls, service_path: Path | None = None) -> bool:
        """Inspect suit-camera-settings.service and auto-repair ordering cycles or defects.

        Prior to commit 7555d79, suit-camera-settings.service contained 'After=default.target'
        combined with 'Before=autodarts.service', causing a transaction cycle that prevented
        autodarts.service from starting on boot.
        """
        target = service_path or cls.USER_UNIT_PATH
        if not target.exists():
            return False

        try:
            content = target.read_text(encoding="utf-8")
            healed = False

            # Check for legacy cyclic ordering
            def _strip_default_target(match: re.Match) -> str:
                tokens = [t for t in match.group(1).split() if t != "default.target"]
                return f"After={' '.join(tokens)}\n" if tokens else ""

            new_content = re.sub(
                r"(?m)^[ \t]*After[ \t]*=([^\r\n]*\bdefault\.target\b[^\r\n]*)\r?\n?",
                _strip_default_target,
                content,
            )
            if new_content != content:
                content = new_content
                healed = True

            # Ensure Before=autodarts.service is present so camera controls apply before Autodarts
            if "[Unit]" in content and "Before=autodarts.service" not in content:
                content = content.replace("[Unit]", "[Unit]\nBefore=autodarts.service")
                healed = True

            if healed:
                target.write_text(content, encoding="utf-8")
                subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=5)
                logger.info("Auto-repaired %s: eliminated systemd boot ordering cycle", target)
                return True
            return False
        except Exception:
            logger.exception("Failed checking/healing camera boot service")
            return False

    # =========================================================================
    # PROFILE MANAGEMENT APIs
    # =========================================================================

    @classmethod
    def get_profiles_data(cls, config_path: Path | None = None) -> dict:
        """Load full profiles document."""
        target = config_path or DEFAULT_CONFIG_PATH
        if target.exists():
            try:
                with open(target, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        # Migrate flat dictionary structure if needed
                        if "profiles" not in data and any(k.startswith("/dev/video") for k in data.keys()):
                            cams_dict = {k: v for k, v in data.items() if not k.startswith("_")}
                            return {
                                "_active_profile": "CAM-librator",
                                "profiles": {
                                    "CAM-librator": cams_dict
                                }
                            }
                        return data
            except Exception:
                logger.exception("Failed loading camera profiles from %s", target)
        return {"_active_profile": "Default", "profiles": {}}

    @classmethod
    def get_profile_names(cls, config_path: Path | None = None) -> list[str]:
        """List all available profile names with 'Default' always first."""
        data = cls.get_profiles_data(config_path)
        names = ["Default"]
        custom = [k for k in data.get("profiles", {}).keys() if k != "Default"]
        return names + sorted(custom)

    @classmethod
    def get_active_profile_name(cls, config_path: Path | None = None) -> str:
        """Get the currently selected profile name."""
        data = cls.get_profiles_data(config_path)
        return data.get("_active_profile", "Default")

    @classmethod
    def get_current_settings_for_cameras(cls, cameras: list[str]) -> dict[str, dict]:
        """Query current control values for all given cameras."""
        res = {}
        for dev in cameras:
            ctrls = cls.get_camera_controls(dev)
            if ctrls:
                settings = {}
                for k, v in ctrls.items():
                    if isinstance(v, dict) and "value" in v:
                        settings[k] = v["value"]
                if settings:
                    res[dev] = settings
        return res

    @classmethod
    def save_profile(cls, name: str, camera_settings: dict, config_path: Path | None = None, set_active: bool = True) -> bool:
        """Save/overwrite a named multi-camera profile."""
        if not name or name.strip() == "" or name.strip() == "Default":
            return False
        name = name.strip()
        target = config_path or DEFAULT_CONFIG_PATH
        try:
            data = cls.get_profiles_data(target)
            if "profiles" not in data:
                data["profiles"] = {}
            data["profiles"][name] = camera_settings
            if set_active:
                data["_active_profile"] = name
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.info("Saved camera profile '%s' to %s", name, target)
            return True
        except Exception:
            logger.exception("Failed saving camera profile '%s' to %s", name, target)
            return False

    @classmethod
    def rename_profile(cls, old_name: str, new_name: str, config_path: Path | None = None) -> bool:
        """Rename an existing custom profile."""
        if old_name == "Default" or not new_name or new_name.strip() in ("", "Default", old_name):
            return False
        new_name = new_name.strip()
        target = config_path or DEFAULT_CONFIG_PATH
        try:
            data = cls.get_profiles_data(target)
            profiles = data.get("profiles", {})
            if old_name in profiles:
                profiles[new_name] = profiles.pop(old_name)
                if data.get("_active_profile") == old_name:
                    data["_active_profile"] = new_name
                with open(target, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                return True
        except Exception:
            logger.exception("Failed renaming profile '%s' to '%s'", old_name, new_name)
        return False

    @classmethod
    def delete_profile(cls, name: str, config_path: Path | None = None) -> bool:
        """Delete a custom profile (Default cannot be deleted)."""
        if name == "Default":
            return False
        target = config_path or DEFAULT_CONFIG_PATH
        try:
            data = cls.get_profiles_data(target)
            profiles = data.get("profiles", {})
            if name in profiles:
                del profiles[name]
                if data.get("_active_profile") == name:
                    data["_active_profile"] = "Default"
                with open(target, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                return True
        except Exception:
            logger.exception("Failed deleting profile '%s'", name)
        return False

    @classmethod
    def apply_profile(cls, name: str, cameras: list[str], config_path: Path | None = None) -> bool:
        """Apply a profile across all given cameras and set it active."""
        target = config_path or DEFAULT_CONFIG_PATH
        data = cls.get_profiles_data(target)

        if name == "Default":
            for dev in cameras:
                cls.reset_defaults(dev)
            data["_active_profile"] = "Default"
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                pass
            cls.update_boot_service_if_enabled(cameras)
            return True

        profiles = data.get("profiles", {})
        if name not in profiles:
            return False

        prof_cams = profiles[name]
        for dev in cameras:
            settings = prof_cams.get(dev)
            if settings:
                vdev = V4L2Device(dev)
                vdev.set_controls_dict(settings)
            else:
                cls.reset_defaults(dev)

        data["_active_profile"] = name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass
        cls.update_boot_service_if_enabled(cameras)
        return True

    @classmethod
    def clear_presets(cls, config_path: Path | None = None) -> bool:
        """Clear all saved camera profiles and reset active profile to Default."""
        target = config_path or DEFAULT_CONFIG_PATH
        try:
            if target.exists():
                target.unlink()
            return True
        except Exception:
            logger.exception("Failed clearing camera presets from %s", target)
            return False

    # Backwards compatibility methods
    @classmethod
    def save_preset(cls, device_path: str, settings: dict, config_path: Path | None = None) -> bool:
        target = config_path or DEFAULT_CONFIG_PATH
        data = cls.get_profiles_data(target)
        active_name = data.get("_active_profile", "CAM-librator")
        if active_name == "Default":
            active_name = "CAM-librator"
        cams = data.get("profiles", {}).get(active_name, {})
        cams[device_path] = settings
        return cls.save_profile(active_name, cams, config_path=target, set_active=True)

    @classmethod
    def load_presets(cls, config_path: Path | None = None) -> dict:
        data = cls.get_profiles_data(config_path)
        active_name = data.get("_active_profile", "Default")
        if active_name == "Default":
            return {}
        return data.get("profiles", {}).get(active_name, {})
