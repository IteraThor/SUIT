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
            return True, "SUIT updated successfully."
        except subprocess.TimeoutExpired:
            return False, "Update timed out. Please check internet connection."
        except Exception as e:
            logger.exception("Update execution failed")
            return False, f"Update failed: {str(e)}"

    @classmethod
    def ensure_dependencies(cls) -> None:
        """Check for essential packages like python3-opencv and install if missing."""
        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            try:
                from core.privileges import check_sudo_privileges
                if check_sudo_privileges():
                    logger.info("Installing missing python3-opencv package via DNF")
                    subprocess.run(
                        ["sudo", "dnf", "install", "-y", "--setopt=install_weak_deps=False", "python3-opencv"],
                        capture_output=True,
                        text=True,
                        timeout=180
                    )
            except Exception:
                logger.exception("Failed installing python3-opencv during update")

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
