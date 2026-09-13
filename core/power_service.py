import subprocess
import re
from pathlib import Path
from core.logger import get_logger

logger = get_logger("power")

DEFAULT_GDM_CONFIG = Path("/etc/gdm/custom.conf")


class PowerService:
    @staticmethod
    def set_screen_blanking(disable_blanking: bool) -> bool:
        return PowerService.set_power_and_sleep(disable_sleep=disable_blanking)

    @staticmethod
    def is_blanking_disabled() -> bool:
        return PowerService.is_power_and_sleep_optimized()

    @staticmethod
    def is_performance_profile_active(profile: str = "throughput-performance") -> bool:
        try:
            res = subprocess.run(["tuned-adm", "active"], capture_output=True, text=True)
            return profile in res.stdout
        except Exception:
            logger.debug("Failed checking tuned-adm active profile")
            return False

    @staticmethod
    def set_performance_profile(profile: str = "throughput-performance") -> bool:
        try:
            subprocess.run(["sudo", "-n", "tuned-adm", "profile", profile], check=True, capture_output=True)
            logger.info(f"Set tuned-adm profile to {profile}")
            return True
        except Exception:
            logger.exception(f"Failed setting tuned-adm profile to {profile}")
            return False

    @staticmethod
    def is_power_and_sleep_optimized() -> bool:
        try:
            res1 = subprocess.run(["gsettings", "get", "org.gnome.desktop.session", "idle-delay"], capture_output=True, text=True)
            if not ("uint32 0" in res1.stdout or res1.stdout.strip() == "0"):
                return False
            res2 = subprocess.run(["gsettings", "get", "org.gnome.settings-daemon.plugins.power", "idle-dim"], capture_output=True, text=True)
            if "false" not in res2.stdout.lower():
                return False
            res3 = subprocess.run(["gsettings", "get", "org.gnome.settings-daemon.plugins.power", "sleep-inactive-ac-type"], capture_output=True, text=True)
            if "nothing" not in res3.stdout.lower():
                return False
            return True
        except Exception:
            logger.debug("Failed checking GNOME power and sleep settings")
            return False

    @staticmethod
    def set_power_and_sleep(disable_sleep: bool = True) -> bool:
        try:
            if disable_sleep:
                subprocess.run(["gsettings", "set", "org.gnome.desktop.session", "idle-delay", "0"], check=True)
                subprocess.run(["gsettings", "set", "org.gnome.settings-daemon.plugins.power", "idle-dim", "false"], check=True)
                subprocess.run(["gsettings", "set", "org.gnome.settings-daemon.plugins.power", "sleep-inactive-ac-type", "'nothing'"], check=True)
                subprocess.run(["gsettings", "set", "org.gnome.settings-daemon.plugins.power", "sleep-inactive-battery-type", "'nothing'"], check=True)
                subprocess.run(["gsettings", "set", "org.gnome.settings-daemon.plugins.power", "power-button-action", "'interactive'"], check=True)
                subprocess.run(["sudo", "-n", "rm", "-f", "/etc/systemd/logind.conf.d/99-suit-power.conf"], check=False)
            else:
                subprocess.run(["gsettings", "set", "org.gnome.desktop.session", "idle-delay", "900"], check=True)
                subprocess.run(["gsettings", "set", "org.gnome.settings-daemon.plugins.power", "idle-dim", "true"], check=True)
                subprocess.run(["gsettings", "set", "org.gnome.settings-daemon.plugins.power", "sleep-inactive-ac-type", "'suspend'"], check=True)
                subprocess.run(["gsettings", "set", "org.gnome.settings-daemon.plugins.power", "sleep-inactive-battery-type", "'suspend'"], check=True)
            logger.info(f"Configured power and sleep settings: disable_sleep={disable_sleep}")
            return True
        except Exception:
            logger.exception("Failed configuring power and sleep settings")
            return False

    @staticmethod
    def generate_gdm_config(config_path: Path, enabled: bool, user: str) -> str:
        content = config_path.read_text(encoding="utf-8") if config_path.exists() else "[daemon]\n"
        lines = [
            line for line in content.splitlines()
            if not line.strip().startswith("AutomaticLoginEnable=") and not line.strip().startswith("AutomaticLogin=")
        ]
        
        daemon_idx = -1
        for i, line in enumerate(lines):
            if line.strip() == "[daemon]":
                daemon_idx = i
                break
                
        if enabled:
            insert_lines = [
                "AutomaticLoginEnable=True",
                f"AutomaticLogin={user}"
            ]
        else:
            insert_lines = [
                "AutomaticLoginEnable=False"
            ]
            
        if daemon_idx != -1:
            lines = lines[:daemon_idx + 1] + insert_lines + lines[daemon_idx + 1:]
        else:
            lines = ["[daemon]"] + insert_lines + [""] + lines
            
        return "\n".join(lines) + "\n"

    @classmethod
    def get_step1_status(cls, user: str) -> dict:
        profile_ok = cls.is_performance_profile_active("throughput-performance")
        power_ok = cls.is_power_and_sleep_optimized()
        autologin_enabled, active_user = cls.is_auto_login_enabled()
        autologin_ok = autologin_enabled and (not user or active_user == user)
        return {
            "profile_ok": profile_ok,
            "power_ok": power_ok,
            "autologin_ok": autologin_ok,
            "all_ok": profile_ok and power_ok and autologin_ok,
            "user": active_user or user
        }

    @classmethod
    def apply_all_system_energy_and_login(cls, user: str) -> bool:
        ok1 = cls.set_performance_profile("throughput-performance")
        ok2 = cls.set_power_and_sleep(disable_sleep=True)
        ok3 = cls.set_auto_login(enabled=True, user=user)
        return ok1 and ok2 and ok3

    @classmethod
    def set_auto_login(cls, enabled: bool, user: str, config_path: Path | None = None) -> bool:
        target_path = config_path or DEFAULT_GDM_CONFIG
        try:
            new_content = cls.generate_gdm_config(target_path, enabled, user)
            cmd = f"cat << 'EOF' > {target_path}\n{new_content}\nEOF"
            subprocess.run(["sudo", "-n", "bash", "-c", cmd], check=True)
            logger.info(f"Updated GDM auto-login to enabled={enabled} for user {user}")
            return True
        except Exception:
            logger.exception("Failed configuring GDM auto-login")
            return False

    @classmethod
    def is_auto_login_enabled(cls, config_path: Path | None = None) -> tuple[bool, str]:
        target_path = config_path or DEFAULT_GDM_CONFIG
        if not target_path.exists():
            return False, ""
        try:
            content = target_path.read_text(encoding="utf-8")
            enabled_match = re.search(r"AutomaticLoginEnable\s*=\s*(True|true|1)", content)
            user_match = re.search(r"AutomaticLogin\s*=\s*([^\s\n]+)", content)
            enabled = bool(enabled_match)
            user = user_match.group(1).strip() if user_match else ""
            return enabled, user
        except Exception:
            logger.exception("Error checking GDM auto-login")
            return False, ""
