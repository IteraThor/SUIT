from pathlib import Path
import subprocess
import sys
import os
from core.logger import get_logger

logger = get_logger("update")


class UpdateService:
    """Service to check for GitHub updates, apply git pulls, and restart SUIT."""

    PROJECT_DIR = Path(__file__).resolve().parent.parent

    @classmethod
    def check_update(cls, project_dir: Path | None = None) -> dict:
        target_dir = project_dir or cls.PROJECT_DIR
        git_dir = target_dir / ".git"

        if not git_dir.exists():
            return {
                "has_update": False,
                "current_commit": "",
                "remote_commit": "",
                "commits_behind": 0,
                "latest_message": "",
                "error": "Git repository not found. SUIT must be installed via git to update.",
            }

        try:
            # 1. Fetch origin main with a short timeout
            fetch_res = subprocess.run(
                ["git", "fetch", "origin", "main"],
                cwd=str(target_dir),
                capture_output=True,
                text=True,
                timeout=8
            )
            if fetch_res.returncode != 0:
                logger.debug("Git fetch failed: %s", fetch_res.stderr.strip())
                return {
                    "has_update": False,
                    "current_commit": "",
                    "remote_commit": "",
                    "commits_behind": 0,
                    "latest_message": "",
                    "error": "Unable to connect to GitHub. Check internet connection.",
                }

            # 2. Local HEAD short commit
            head_res = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(target_dir),
                capture_output=True,
                text=True,
                check=True
            )
            local_commit = head_res.stdout.strip()

            # 3. Remote origin/main short commit
            remote_res = subprocess.run(
                ["git", "rev-parse", "--short", "origin/main"],
                cwd=str(target_dir),
                capture_output=True,
                text=True,
                check=True
            )
            remote_commit = remote_res.stdout.strip()

            if local_commit == remote_commit:
                return {
                    "has_update": False,
                    "current_commit": local_commit,
                    "remote_commit": remote_commit,
                    "commits_behind": 0,
                    "latest_message": "",
                    "error": None,
                }

            # 4. Count of commits behind
            count_res = subprocess.run(
                ["git", "rev-list", "--count", "HEAD..origin/main"],
                cwd=str(target_dir),
                capture_output=True,
                text=True,
                check=True
            )
            commits_behind = int(count_res.stdout.strip() or "1")

            # 5. Latest remote commit summary message
            msg_res = subprocess.run(
                ["git", "log", "-1", "--pretty=format:%s", "origin/main"],
                cwd=str(target_dir),
                capture_output=True,
                text=True,
                check=True
            )
            latest_msg = msg_res.stdout.strip()

            return {
                "has_update": commits_behind > 0,
                "current_commit": local_commit,
                "remote_commit": remote_commit,
                "commits_behind": commits_behind,
                "latest_message": latest_msg,
                "error": None,
            }

        except subprocess.TimeoutExpired:
            logger.debug("Git fetch timed out")
            return {
                "has_update": False,
                "current_commit": "",
                "remote_commit": "",
                "commits_behind": 0,
                "latest_message": "",
                "error": "Check for updates timed out. Check internet connection.",
            }
        except Exception as e:
            logger.exception("Error checking for updates")
            return {
                "has_update": False,
                "current_commit": "",
                "remote_commit": "",
                "commits_behind": 0,
                "latest_message": "",
                "error": f"Failed checking updates: {str(e)}",
            }

    @classmethod
    def apply_update(cls, project_dir: Path | None = None) -> tuple[bool, str]:
        target_dir = project_dir or cls.PROJECT_DIR
        try:
            # 1. Fetch latest changes from origin main
            fetch_res = subprocess.run(
                ["git", "fetch", "origin", "main"],
                cwd=str(target_dir),
                capture_output=True,
                text=True,
                timeout=15
            )
            if fetch_res.returncode != 0:
                err = fetch_res.stderr.strip() or "Network error"
                return False, f"Failed fetching updates: {err}"

            # 2. Fast-forward pull with hard reset fallback for rewritten history
            pull_res = subprocess.run(
                ["git", "pull", "--ff-only", "origin", "main"],
                cwd=str(target_dir),
                capture_output=True,
                text=True,
                timeout=15
            )
            if pull_res.returncode != 0:
                logger.warning("Fast-forward pull failed, attempting hard reset to origin/main: %s", pull_res.stderr.strip())
                reset_res = subprocess.run(
                    ["git", "reset", "--hard", "origin/main"],
                    cwd=str(target_dir),
                    capture_output=True,
                    text=True,
                    timeout=15
                )
                if reset_res.returncode != 0:
                    err = reset_res.stderr.strip()
                    logger.warning("Hard reset failed: %s", err)
                    return False, f"Unable to update: {err}"

            logger.info("Successfully updated SUIT from origin main")
            cls.ensure_dependencies()
            cls.ensure_autodarts_service()
            cls.ensure_camera_keeper()
            cls.ensure_display_autostart()
            return True, "SUIT updated successfully."
        except subprocess.TimeoutExpired:
            return False, "Update timed out. Please check internet connection."
        except Exception as e:
            logger.exception("Update execution failed")
            return False, f"Update failed: {str(e)}"

    @classmethod
    def ensure_camera_keeper(cls) -> None:
        """Migrate old camera watchers and Manual Controls data; keep the camera keeper unit on this checkout."""
        try:
            from core.hbvcal import service
            service.heal()
        except Exception as e:
            logger.warning("Post-update camera keeper check notice: %s", e)

    @classmethod
    def ensure_display_autostart(cls) -> None:
        """Ensure rotation and touch autostart is up-to-date with current repository path."""
        try:
            from core.display_service import DisplayService
            DisplayService.ensure_autostart()
        except Exception as e:
            logger.warning("Post-update display autostart check notice: %s", e)

    @classmethod
    def ensure_autodarts_service(cls) -> None:
        """Ensure Autodarts systemd service is provisioned and enabled if Autodarts is installed."""
        try:
            from core.autodarts_service import (
                is_autodarts_installed,
                is_service_enabled,
                set_service_enabled,
                ensure_service_unit
            )
            if is_autodarts_installed():
                ensure_service_unit()
                if not is_service_enabled():
                    logger.info("Post-update: enabling autodarts.service for boot startup")
                    set_service_enabled(True)
        except Exception as e:
            logger.warning("Post-update autodarts service check notice: %s", e)

    @classmethod
    def check_requirements(cls) -> list[str]:
        """Check for essential SUIT dependencies and return list of missing DNF package names."""
        missing = []
        import shutil
        import importlib.util

        if not shutil.which("v4l2-ctl"):
            missing.append("v4l-utils")

        if not importlib.util.find_spec("cv2"):
            missing.append("python3-opencv")

        if not importlib.util.find_spec("evdev"):
            missing.append("python3-evdev")

        if not importlib.util.find_spec("qrcode"):
            missing.append("python3-qrcode")

        if not shutil.which("git"):
            missing.append("git")

        return missing

    @classmethod
    def install_requirements(cls, packages: list[str] | None = None) -> tuple[bool, str]:
        """Install missing requirement packages via DNF."""
        to_install = packages if packages is not None else cls.check_requirements()
        if not to_install:
            return True, "All requirements are already installed."

        try:
            from core.privileges import check_sudo_privileges
            if check_sudo_privileges():
                cmd = ["sudo", "dnf", "install", "-y", "--setopt=install_weak_deps=False"] + to_install
                logger.info("Installing requirements via passwordless sudo: %s", cmd)
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            else:
                pkexec_cmd = f"pkexec dnf install -y --setopt=install_weak_deps=False {' '.join(to_install)}"
                logger.info("Installing requirements via pkexec: %s", pkexec_cmd)
                res = subprocess.run(pkexec_cmd, shell=True, capture_output=True, text=True, timeout=180)

            if res.returncode == 0:
                logger.info("Successfully installed requirement packages: %s", to_install)
                return True, "Requirements installed successfully."
            else:
                err = res.stderr.strip() or "Installation failed."
                logger.error("Failed installing requirements: %s", err)
                return False, f"Installation failed: {err}"
        except subprocess.TimeoutExpired:
            return False, "Installation timed out."
        except Exception as e:
            logger.exception("Error installing requirements")
            return False, f"Installation failed: {e}"

    @classmethod
    def ensure_dependencies(cls) -> None:
        """Check for essential packages and install if missing."""
        missing = cls.check_requirements()
        if missing:
            cls.install_requirements(missing)

    @classmethod
    def restart_application(cls, project_dir: Path | None = None):
        target_dir = project_dir or cls.PROJECT_DIR
        app_path = target_dir / "app_gtk.py"
        args = [sys.executable, str(app_path)] + [a for a in sys.argv[1:] if a != str(app_path)]
        logger.info("Restarting SUIT process: %s", args)
        try:
            # Replace the current process with newly started instance
            os.execv(sys.executable, args)
        except Exception:
            logger.exception("Failed executing process reload via execv, falling back to Popen")
            try:
                subprocess.Popen(args, cwd=str(target_dir))
                sys.exit(0)
            except Exception:
                logger.exception("Fallback process restart failed")


__all__ = ["UpdateService"]
