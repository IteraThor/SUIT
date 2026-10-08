"""Persistence service for camera calibration.

Generates /usr/local/bin/apply-cam-calibration.sh and manages the systemd boot service
to ensure 0% runtime CPU overhead during Autodarts gameplay.
"""

from __future__ import annotations
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, Optional
from core.v4l2_device import CameraSettings

logger = logging.getLogger("cam_librator.persistence")

CONFIG_DIR = Path("/etc/cam-librator")
USER_CONFIG_DIR = Path.home() / ".config" / "cam-librator"
SCRIPT_PATH = Path("/usr/local/bin/apply-cam-calibration.sh")
SERVICE_PATH = Path("/etc/systemd/system/apply-cam-calibration.service")


FACTORY_DEFAULTS = {
    "white_balance_automatic": 1,
    "auto_exposure": 3,
    "sharpness": 40,
    "contrast": 34,
    "gain": 64,
    "saturation": 68,
    "gamma": 300,
    "brightness": 0,
    "power_line_frequency": 1,
}


class PersistenceService:
    """Manages persistent boot script and systemd service generation."""

    def __init__(self):
        self.config_dir = CONFIG_DIR if os.access("/etc", os.W_OK) else USER_CONFIG_DIR
        self.config_file = self.config_dir / "calibration.json"

    def is_calibration_enabled(self) -> bool:
        """Check if custom calibrations are globally active."""
        if not self.config_file.exists():
            return False
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not data:
                return False
            # If explicitly disabled
            if data.get("_master_enabled") is False:
                return False
            # Check if at least one camera calibration entry exists
            cam_entries = [k for k in data.keys() if not k.startswith("_")]
            return len(cam_entries) > 0
        except Exception as e:
            logger.warning("Could not read calibration state: %s", e)
            return False

    def set_calibration_enabled(self, enabled: bool) -> Tuple[bool, str]:
        """Toggle global calibration active state and update boot scripts."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        data: Dict[str, dict] = {}
        if self.config_file.exists():
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logger.warning("Could not read config: %s", e)

        data["_master_enabled"] = enabled
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.exception("Failed to write config: %s", e)
            return False, f"Failed to save state: {e}"

        if enabled:
            return self.install_systemd_service()
        else:
            return self.disable_systemd_service()

    def save_calibration(self, camera_by_path: str, settings: CameraSettings) -> bool:
        """Save calibrated camera settings mapped to its physical USB by-path."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        data: Dict[str, dict] = {}
        if self.config_file.exists():
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logger.warning("Could not load existing config: %s", e)

        data["_master_enabled"] = True
        data[camera_by_path] = settings.to_dict()

        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            return True
        except Exception as e:
            logger.exception("Failed to write calibration config: %s", e)
            return False

    def clear_calibrations(self) -> Tuple[bool, str]:
        """Delete all saved calibrations, reset boot script, and disable calibration service."""
        try:
            if self.config_file.exists():
                self.config_file.unlink()
            ok, msg = self.disable_systemd_service()
            logger.info("All camera calibrations cleared and reset to factory defaults.")
            return True, "All calibrations deleted and reset to factory defaults."
        except Exception as e:
            logger.exception("Failed to clear calibrations: %s", e)
            return False, f"Error during reset: {e}"

    def load_calibrations(self) -> Dict[str, CameraSettings]:
        """Load all saved calibrations (ignoring internal metadata keys)."""
        if not self.config_file.exists():
            return {}
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return {
                path: CameraSettings.from_dict(cfg)
                for path, cfg in raw.items()
                if not path.startswith("_") and isinstance(cfg, dict)
            }
        except Exception as e:
            logger.warning("Could not read calibrations: %s", e)
            return {}

    def generate_boot_script(self) -> str:
        """Generate the shell script containing explicit v4l2-ctl parameter sets."""
        is_enabled = self.is_calibration_enabled()
        calibrations = self.load_calibrations()

        lines = [
            "#!/usr/bin/env bash",
            "# Auto-generated by CAM-librator for Autodarts",
            "# Applies hardware camera tuning with 0% runtime CPU overhead",
            "set -e",
            "",
        ]

        if not is_enabled or not calibrations:
            lines.append("echo '[CAM-librator] Hardware calibration is DISABLED. Applying standard factory defaults...'")
            lines.append("")
            # Reset all /dev/video* devices to defaults
            lines.append("for dev in /dev/v4l/by-path/*video-index0; do")
            lines.append("    if [ -e \"$dev\" ]; then")
            lines.append("        v4l2-ctl -d \"$dev\" --set-ctrl=auto_exposure=3 2>/dev/null || true")
            lines.append("        v4l2-ctl -d \"$dev\" --set-ctrl=gain=64 2>/dev/null || true")
            lines.append("        v4l2-ctl -d \"$dev\" --set-ctrl=white_balance_automatic=1 2>/dev/null || true")
            lines.append("        v4l2-ctl -d \"$dev\" --set-ctrl=sharpness=40 2>/dev/null || true")
            lines.append("        v4l2-ctl -d \"$dev\" --set-ctrl=contrast=34 2>/dev/null || true")
            lines.append("        v4l2-ctl -d \"$dev\" --set-ctrl=saturation=68 2>/dev/null || true")
            lines.append("        v4l2-ctl -d \"$dev\" --set-ctrl=gamma=300 2>/dev/null || true")
            lines.append("        v4l2-ctl -d \"$dev\" --set-ctrl=brightness=0 2>/dev/null || true")
            lines.append("    fi")
            lines.append("done")
            lines.append("echo '[CAM-librator] Standard camera defaults applied.'")
            return "\n".join(lines) + "\n"

        lines.append("echo '[CAM-librator] Applying persistent UVC camera parameters...'")
        lines.append("")

        for by_path, s in calibrations.items():
            lines.append(f"# Target Camera: {by_path}")
            lines.append(f"if [ -e '{by_path}' ]; then")
            lines.append(f"    v4l2-ctl -d '{by_path}' --set-ctrl=auto_exposure={s.exposure_auto} 2>/dev/null || true")
            lines.append(f"    v4l2-ctl -d '{by_path}' --set-ctrl=exposure_time_absolute={s.exposure_absolute} 2>/dev/null || true")
            lines.append(f"    v4l2-ctl -d '{by_path}' --set-ctrl=gain={s.gain} 2>/dev/null || true")
            lines.append(f"    v4l2-ctl -d '{by_path}' --set-ctrl=white_balance_automatic={s.wb_auto} 2>/dev/null || true")
            lines.append(f"    v4l2-ctl -d '{by_path}' --set-ctrl=white_balance_temperature={s.wb_temperature} 2>/dev/null || true")
            lines.append(f"    v4l2-ctl -d '{by_path}' --set-ctrl=sharpness={s.sharpness} 2>/dev/null || true")
            lines.append(f"    v4l2-ctl -d '{by_path}' --set-ctrl=contrast={s.contrast} 2>/dev/null || true")
            lines.append(f"    v4l2-ctl -d '{by_path}' --set-ctrl=brightness={s.brightness} 2>/dev/null || true")
            lines.append(f"    v4l2-ctl -d '{by_path}' --set-ctrl=gamma={s.gamma} 2>/dev/null || true")
            lines.append(f"    v4l2-ctl -d '{by_path}' --set-ctrl=saturation={s.saturation} 2>/dev/null || true")
            lines.append(f"    v4l2-ctl -d '{by_path}' --set-ctrl=power_line_frequency={s.power_line_frequency} 2>/dev/null || true")
            lines.append("fi")
            lines.append("")

        lines.append("echo '[CAM-librator] All camera parameters successfully applied.'")
        return "\n".join(lines) + "\n"

    def install_systemd_service(self) -> Tuple[bool, str]:
        """Write boot script and install systemd unit."""
        script_content = self.generate_boot_script()

        # If running without root, write to local user script path as fallback
        target_script = SCRIPT_PATH if os.access("/usr/local/bin", os.W_OK) else (Path.home() / ".local" / "bin" / "apply-cam-calibration.sh")
        target_script.parent.mkdir(parents=True, exist_ok=True)

        try:
            target_script.write_text(script_content, encoding="utf-8")
            target_script.chmod(0o755)
            logger.info("Successfully wrote boot script to %s", target_script)
        except Exception as e:
            logger.exception("Failed to write boot script: %s", e)
            return False, f"Failed to write boot script: {e}"

        unit_content = f"""[Unit]
Description=Apply CAM-librator UVC Camera Calibration for Autodarts
Before=autodarts.service
After=multi-user.target

[Service]
Type=oneshot
ExecStart={target_script}
RemainAfterExit=true

[Install]
WantedBy=multi-user.target
"""
        if os.access("/etc/systemd/system", os.W_OK):
            try:
                SERVICE_PATH.write_text(unit_content, encoding="utf-8")
                subprocess.run(["systemctl", "daemon-reload"], check=True)
                subprocess.run(["systemctl", "enable", "apply-cam-calibration.service"], check=True)
                logger.info("Successfully installed and enabled systemd service %s", SERVICE_PATH)
                return True, "Systemd service successfully enabled."
            except Exception as e:
                logger.exception("Failed to configure systemd unit: %s", e)
                return False, f"Failed to configure systemd unit: {e}"
        else:
            logger.info("Systemd unit creation skipped (no root access). Boot script ready at %s", target_script)
            return True, f"Script written to {target_script}."

    def disable_systemd_service(self) -> Tuple[bool, str]:
        """Update boot script to defaults and disable systemd service."""
        script_content = self.generate_boot_script()
        target_script = SCRIPT_PATH if os.access("/usr/local/bin", os.W_OK) else (Path.home() / ".local" / "bin" / "apply-cam-calibration.sh")
        target_script.parent.mkdir(parents=True, exist_ok=True)

        try:
            target_script.write_text(script_content, encoding="utf-8")
            target_script.chmod(0o755)
        except Exception as e:
            logger.warning("Could not update script: %s", e)

        if os.access("/etc/systemd/system", os.W_OK) and SERVICE_PATH.exists():
            try:
                subprocess.run(["systemctl", "disable", "apply-cam-calibration.service"], check=False)
                subprocess.run(["systemctl", "daemon-reload"], check=False)
                return True, "Systemd calibration service disabled."
            except Exception as e:
                logger.warning("Could not disable systemd unit: %s", e)
                return False, f"Could not disable unit: {e}"

        return True, "Calibration disabled. Standard defaults active."
