import re
import shutil
import subprocess
from pathlib import Path
from core.logger import get_logger
from core.systemd_service import SystemdService
from core.update_service import UpdateService

logger = get_logger("autoglow")

REPO_URL = "https://github.com/IteraThor/Autoglow-2.git"
SERVICE_NAME = "autoglow.service"
UNIT_PATH = Path("/etc/systemd/system/autoglow.service")
DEFAULT_DIR = Path.home() / "Autoglow-2"
WEB_URL = "http://localhost:8080"
# Browser policy files AutoGlow's setup.sh writes to force-install the AutoGlow Bridge extension
POLICY_FILES = [
    "/etc/opt/chrome/policies/managed/autoglow.json",
    "/etc/chromium/policies/managed/autoglow.json",
    "/etc/chromium-browser/policies/managed/autoglow.json",
    "/etc/brave/policies/managed/autoglow.json",
    "/etc/opt/edge/policies/managed/autoglow.json",
]
# Files 2.1 tracked in git that are user data since 2.2; local edits to them block `git pull`
LEGACY_TRACKED_FILES = ["game_flows.json", "presets/Default.json"]
SETUP_TIMEOUT = 900


class AutoGlowService:
    """Installs, updates and controls AutoGlow 2 (github.com/IteraThor/Autoglow-2) as its systemd service."""

    @staticmethod
    def _is_autoglow2_dir(path: Path) -> bool:
        # AutoGlow 1 used the same service name and also had a server.py, but no core/version.py
        return (path / "server.py").is_file() and (path / "core" / "version.py").is_file()

    @classmethod
    def get_install_dir(cls) -> Path | None:
        """The AutoGlow 2 folder the service runs from, else the default clone folder; None when not installed."""
        try:
            if UNIT_PATH.is_file():
                m = re.search(r"^WorkingDirectory=(.+)$", UNIT_PATH.read_text(encoding="utf-8"), re.MULTILINE)
                if m:
                    unit_dir = Path(m.group(1).strip())
                    if cls._is_autoglow2_dir(unit_dir):
                        return unit_dir
        except Exception:
            logger.debug("Failed reading %s", UNIT_PATH)
        if cls._is_autoglow2_dir(DEFAULT_DIR):
            return DEFAULT_DIR
        return None

    @staticmethod
    def get_version(install_dir: Path) -> str:
        try:
            text = (install_dir / "core" / "version.py").read_text(encoding="utf-8")
            m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
            if m:
                return m.group(1)
        except Exception:
            pass
        return ""

    @classmethod
    def get_status(cls) -> dict:
        install_dir = cls.get_install_dir()
        unit = SystemdService.get_status(SERVICE_NAME, scope="system")
        return {
            "installed": install_dir is not None,
            "legacy": UNIT_PATH.is_file() and install_dir is None,
            "install_dir": install_dir,
            "version": cls.get_version(install_dir) if install_dir else "",
            "is_git": bool(install_dir and (install_dir / ".git").exists()),
            "service_present": unit.get("active_state") != "nofile" and UNIT_PATH.is_file(),
            "running": unit.get("active_state") == "active",
            "active_state": unit.get("active_state", "nofile"),
            "enabled": unit.get("unit_file_state") == "enabled",
        }

    @staticmethod
    def _run_setup(install_dir: Path) -> tuple[bool, str]:
        """AutoGlow's own setup.sh: system packages, venv, browser extension policy and the service."""
        try:
            res = subprocess.run(
                ["sudo", "-n", "bash", str(install_dir / "setup.sh")],
                cwd=str(install_dir), capture_output=True, text=True, timeout=SETUP_TIMEOUT
            )
            if res.returncode != 0:
                tail = (res.stderr.strip() or res.stdout.strip()).splitlines()[-1:] or ["unknown error"]
                logger.warning("AutoGlow setup.sh failed: %s\n%s", res.stderr, res.stdout[-2000:])
                return False, f"AutoGlow setup failed: {tail[0]}"
            return True, "AutoGlow 2 is installed and running."
        except subprocess.TimeoutExpired:
            return False, "AutoGlow setup timed out."
        except Exception as e:
            logger.exception("Failed running AutoGlow setup.sh")
            return False, f"AutoGlow setup failed: {e}"

    @classmethod
    def install(cls) -> tuple[bool, str]:
        install_dir = cls.get_install_dir()
        if install_dir is None:
            if DEFAULT_DIR.exists() and any(DEFAULT_DIR.iterdir()):
                return False, f"{DEFAULT_DIR} already exists and is not an AutoGlow 2 folder."
            try:
                res = subprocess.run(
                    ["git", "clone", "--depth", "1", REPO_URL, str(DEFAULT_DIR)],
                    capture_output=True, text=True, timeout=180
                )
                if res.returncode != 0:
                    logger.warning("AutoGlow clone failed: %s", res.stderr.strip())
                    return False, "Download failed. Check internet connection."
            except Exception as e:
                logger.exception("Failed cloning AutoGlow")
                return False, f"Download failed: {e}"
            install_dir = DEFAULT_DIR
        return cls._run_setup(install_dir)

    @classmethod
    def check_update(cls) -> dict:
        install_dir = cls.get_install_dir()
        if install_dir is None or not (install_dir / ".git").exists():
            return {"has_update": False, "error": "AutoGlow was not installed with git, so it cannot update here."}
        return UpdateService.check_update(project_dir=install_dir)

    @classmethod
    def update(cls) -> tuple[bool, str]:
        install_dir = cls.get_install_dir()
        if install_dir is None or not (install_dir / ".git").exists():
            return False, "AutoGlow was not installed with git, so it cannot update here."
        cwd = str(install_dir)
        try:
            # Coming from 2.1: keep the user's flows, then drop the local edits that block the pull
            backups = {}
            for rel in LEGACY_TRACKED_FILES:
                tracked = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=cwd, capture_output=True).returncode == 0
                if tracked and subprocess.run(["git", "diff", "--quiet", "--", rel], cwd=cwd).returncode != 0:
                    backups[rel] = (install_dir / rel).read_bytes()
                    subprocess.run(["git", "checkout", "--", rel], cwd=cwd, check=True, capture_output=True)

            res = subprocess.run(["git", "pull", "--ff-only", "origin", "main"], cwd=cwd, capture_output=True, text=True, timeout=120)

            # presets/ is regenerated from config.json, only the flows are restored
            if "game_flows.json" in backups:
                (install_dir / "game_flows.json").write_bytes(backups["game_flows.json"])

            if res.returncode != 0:
                logger.warning("AutoGlow git pull failed: %s", res.stderr.strip())
                return False, "Update download failed. Local changes in the AutoGlow folder may block it."
        except Exception as e:
            logger.exception("Failed updating AutoGlow")
            return False, f"Update failed: {e}"

        ok, msg = cls._run_setup(install_dir)
        return ok, ("AutoGlow 2 updated to " + (cls.get_version(install_dir) or "the latest version") + ".") if ok else msg

    @staticmethod
    def start() -> bool:
        SystemdService.reset_failed(SERVICE_NAME, scope="system")
        return SystemdService.start_unit(SERVICE_NAME, scope="system")

    @staticmethod
    def stop() -> bool:
        return SystemdService.stop_unit(SERVICE_NAME, scope="system")

    @staticmethod
    def restart() -> bool:
        SystemdService.reset_failed(SERVICE_NAME, scope="system")
        return SystemdService.restart_unit(SERVICE_NAME, scope="system")

    @staticmethod
    def set_enabled(enabled: bool) -> bool:
        try:
            res = subprocess.run(
                ["sudo", "-n", "systemctl", "enable" if enabled else "disable", SERVICE_NAME],
                capture_output=True, text=True, timeout=10
            )
            if res.returncode != 0:
                logger.warning("systemctl %s autoglow failed: %s", "enable" if enabled else "disable", res.stderr.strip())
            return res.returncode == 0
        except Exception:
            logger.exception("Failed toggling AutoGlow autostart")
            return False

    @classmethod
    def uninstall(cls, remove_files: bool) -> tuple[bool, str]:
        install_dir = cls.get_install_dir()
        script = (
            f"systemctl disable --now {SERVICE_NAME} 2>/dev/null || true; "
            f"rm -f {UNIT_PATH} {' '.join(POLICY_FILES)}; "
            "systemctl daemon-reload"
        )
        try:
            res = subprocess.run(["sudo", "-n", "bash", "-c", script], capture_output=True, text=True, timeout=30)
            if res.returncode != 0:
                logger.warning("AutoGlow uninstall failed: %s", res.stderr.strip())
                return False, "Failed removing the AutoGlow service."
            # Only a folder SUIT cloned itself is deleted, never a copy the user set up elsewhere
            if remove_files and install_dir == DEFAULT_DIR:
                shutil.rmtree(install_dir)
            if remove_files and install_dir == DEFAULT_DIR:
                return True, "AutoGlow 2 removed."
            return True, "AutoGlow 2 service removed, its folder was kept."
        except Exception as e:
            logger.exception("Failed uninstalling AutoGlow")
            return False, f"Uninstall failed: {e}"
