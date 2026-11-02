import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from core.logger import get_logger
from core.systemd_service import SystemdService
from core.update_service import UpdateService
from core.i18n import tr

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
# Everything AutoGlow 2 (2.1 and newer) writes for the user lives in its own folder: config.json holds the
# profiles and settings, presets/ mirrors the profiles, the key keeps the browser extension's ID
USER_DATA = ["config.json", "config.json.bak", "game_flows.json", "game_flows.json.bak",
             "presets", "studio", "backups", "chrome_extension.key"]
# Untracked files AutoGlow itself creates (user data, venv, logs, caches); anything else in a git
# checkout is someone's own work
RUNTIME_FILES = ["venv", ".venv", "__pycache__", "*.pyc", "*.log", "*.log.*", "config.json", "config.json.*",
                 "game_flows.json", "game_flows.json.*", "presets", "studio", "backups", "scratch",
                 "chrome_extension.key", "online_signals.jsonl", ".sync_status.json", "build", "dist",
                 "target"]  # rust_core/target: setup.sh's build of the Rust LED engine
# The new version is cloned here first, so a failed download leaves an older install untouched
STAGING_DIR = DEFAULT_DIR.with_name(".Autoglow-2.suit-new")
BACKUP_ROOT = Path.home() / ".local" / "share" / "suit" / "autoglow-backups"
# Where older AutoGlow 2 copies (git clones, unpacked release ZIPs) are looked for
SEARCH_ROOTS = [(Path.home(), 4), (Path("/opt"), 2)]
SKIP_DIRS = {"venv", "node_modules", "__pycache__", "site-packages", "snap"}


class AutoGlowService:
    """Installs, updates and controls AutoGlow 2 (github.com/IteraThor/Autoglow-2) as its systemd service."""

    @staticmethod
    def _is_autoglow2_dir(path: Path) -> bool:
        # AutoGlow 1 used the same service name and also had a server.py, but no core/version.py
        return (path / "server.py").is_file() and (path / "core" / "version.py").is_file()

    @classmethod
    def _unit_dir(cls) -> Path | None:
        """WorkingDirectory of autoglow.service, whatever it belongs to."""
        try:
            if UNIT_PATH.is_file():
                m = re.search(r"^WorkingDirectory=(.+)$", UNIT_PATH.read_text(encoding="utf-8"), re.MULTILINE)
                if m:
                    return Path(m.group(1).strip())
        except Exception:
            logger.debug("Failed reading %s", UNIT_PATH)
        return None

    @classmethod
    def get_install_dir(cls) -> Path | None:
        """The AutoGlow 2 install SUIT manages: a git clone in the default folder. None when there is none."""
        if cls._is_autoglow2_dir(DEFAULT_DIR) and (DEFAULT_DIR / ".git").exists():
            return DEFAULT_DIR
        return None

    @staticmethod
    def _autoglow_processes() -> list[tuple[int, Path]]:
        """(pid, folder) of python processes running a server.py or start.py, e.g. a hand-started copy."""
        found = []
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                args = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="ignore").split("\0")
                if "python" not in Path(args[0]).name:
                    continue
                script = next((a for a in args[1:] if Path(a).name in ("server.py", "start.py")), None)
                if script is None:
                    continue
                path = Path(script) if os.path.isabs(script) else Path(os.readlink(f"/proc/{pid}/cwd")) / script
                found.append((int(pid), path.parent))
            except OSError:
                continue
        return found

    @classmethod
    def find_old_installs(cls) -> list[Path]:
        """Every AutoGlow 2 copy on this computer except the one SUIT manages: the service's folder,
        hand-started copies, and folders under the home folder and /opt."""
        managed = cls.get_install_dir()
        candidates = []
        unit_dir = cls._unit_dir()
        if unit_dir:
            candidates.append(unit_dir)
        candidates += [d for _, d in cls._autoglow_processes()]
        if managed is None:
            candidates.append(DEFAULT_DIR)
        for root, max_depth in SEARCH_ROOTS:
            if not root.is_dir():
                continue
            base_depth = len(root.parts)
            for dirpath, dirnames, filenames in os.walk(root):
                here = Path(dirpath)
                if "server.py" in filenames and cls._is_autoglow2_dir(here):
                    candidates.append(here)
                    dirnames[:] = []
                    continue
                if len(here.parts) - base_depth >= max_depth:
                    dirnames[:] = []
                    continue
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]

        result, seen = [], set()
        for c in candidates:
            try:
                key = c.resolve()
            except OSError:
                continue
            if key in seen or not cls._is_autoglow2_dir(c):
                continue
            seen.add(key)
            if managed is not None and key == managed.resolve():
                continue
            if key == STAGING_DIR.resolve():
                continue
            result.append(c)
        return result

    @staticmethod
    def _has_local_work(path: Path) -> bool:
        """True for a git checkout someone works in: edits or commits not on its remote, or files that are
        neither AutoGlow's nor written by it at runtime (AutoGlow keeps its own tests/docs/dev.py local only).
        Such a folder is never deleted. The files 2.1 checkouts edit themselves do not count."""
        if not (path / ".git").exists():
            return False
        try:
            status = subprocess.run(["git", "status", "--porcelain", "--ignored", "--untracked-files=normal"],
                                    cwd=str(path), capture_output=True, text=True, timeout=15)
            unpushed = subprocess.run(["git", "rev-list", "--count", "HEAD", "--not", "--remotes"],
                                      cwd=str(path), capture_output=True, text=True, timeout=15)
            if status.returncode != 0 or unpushed.returncode != 0:
                return True
            for line in status.stdout.splitlines():
                code, rel = line[:2], line[3:].strip().strip('"').rstrip("/")
                if code in ("??", "!!"):
                    if not any(fnmatch(part, pat) for part in rel.split("/") for pat in RUNTIME_FILES):
                        return True
                elif rel not in LEGACY_TRACKED_FILES:
                    return True
            return int(unpushed.stdout.strip() or 0) > 0
        except Exception:
            logger.debug("git check failed in %s", path)
            return True

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
        unit_dir = cls._unit_dir()
        running = {d.resolve() for _, d in cls._autoglow_processes()}
        old = [{"path": d, "version": cls.get_version(d), "keep": cls._has_local_work(d),
                "active": d.resolve() in running or bool(unit_dir and d.resolve() == unit_dir.resolve())}
               for d in cls.find_old_installs()]
        return {
            "installed": install_dir is not None,
            # AutoGlow 1 used the same service name
            "legacy": UNIT_PATH.is_file() and not (unit_dir and cls._is_autoglow2_dir(unit_dir)),
            "old_installs": old,
            "install_dir": install_dir,
            "version": cls.get_version(install_dir) if install_dir else "",
            "is_git": bool(install_dir and (install_dir / ".git").exists()),
            "service_present": (unit.get("active_state") != "nofile" and UNIT_PATH.is_file()
                                and install_dir is not None and unit_dir == install_dir),
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
                return False, tr("AutoGlow setup failed: {error}").format(error=tail[0])
            return True, "AutoGlow 2 is installed and running."
        except subprocess.TimeoutExpired:
            return False, "AutoGlow setup timed out."
        except Exception as e:
            logger.exception("Failed running AutoGlow setup.sh")
            return False, tr("AutoGlow setup failed: {error}").format(error=e)

    @classmethod
    def install(cls) -> tuple[bool, str]:
        """Install (or repair) AutoGlow 2 in the default folder. Older AutoGlow 2 copies are removed first;
        their profiles, flows and settings move into the new install and are backed up."""
        install_dir = cls.get_install_dir()
        old = cls.find_old_installs()
        if install_dir is None and DEFAULT_DIR.exists() and any(DEFAULT_DIR.iterdir()):
            if DEFAULT_DIR not in old:
                return False, tr("{path} already exists and is not an AutoGlow 2 folder.").format(path=DEFAULT_DIR)
            if cls._has_local_work(DEFAULT_DIR):
                return False, tr("{path} has changes that are not on GitHub. Move the folder and try again.").format(path=DEFAULT_DIR)

        target = install_dir
        if target is None:
            ok, msg = cls._clone(STAGING_DIR)
            if not ok:
                return ok, msg
            target = STAGING_DIR

        if old:
            ok, msg = cls._replace_old_installs(old, target, has_own_data=install_dir is not None)
            if not ok:
                return ok, msg

        if target == STAGING_DIR:
            try:
                STAGING_DIR.rename(DEFAULT_DIR)
            except OSError as e:
                logger.exception("Failed moving the new AutoGlow into place")
                return False, tr("AutoGlow setup failed: {error}").format(error=e)
        ok, msg = cls._run_setup(DEFAULT_DIR)
        if ok and old:
            msg = tr("AutoGlow 2 is installed. The older version was removed, your profiles were kept.")
        return ok, msg

    @staticmethod
    def _clone(dest: Path) -> tuple[bool, str]:
        shutil.rmtree(dest, ignore_errors=True)
        try:
            res = subprocess.run(
                ["git", "clone", "--depth", "1", REPO_URL, str(dest)],
                capture_output=True, text=True, timeout=180
            )
            if res.returncode != 0:
                logger.warning("AutoGlow clone failed: %s", res.stderr.strip())
                shutil.rmtree(dest, ignore_errors=True)
                return False, "Download failed. Check internet connection."
            return True, ""
        except Exception as e:
            logger.exception("Failed cloning AutoGlow")
            shutil.rmtree(dest, ignore_errors=True)
            return False, tr("Download failed: {error}").format(error=e)

    @classmethod
    def _replace_old_installs(cls, old: list[Path], target: Path, has_own_data: bool) -> tuple[bool, str]:
        """Back up and carry over the user data of the older copies, then remove them completely."""
        # The copy the service ran is the one in use; otherwise the most recently saved settings win.
        # A SUIT install that already has its own settings keeps them and only gains missing profiles.
        unit_dir = cls._unit_dir()
        primary = next((d for d in old if unit_dir and d.resolve() == unit_dir.resolve()), None)
        if primary is None and not (has_own_data and (target / "config.json").exists()):
            with_config = [d for d in old if (d / "config.json").is_file()]
            if with_config:
                primary = max(with_config, key=lambda d: (d / "config.json").stat().st_mtime)

        try:
            backup = BACKUP_ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")
            sources = list(old) + ([target] if has_own_data and primary else [])
            for i, d in enumerate(sources, 1):
                cls._copy_user_data(d, backup / f"{i}-{d.name}", overwrite=True)
                (backup / f"{i}-{d.name}" / "ORIGIN.txt").write_text(f"{d}\n", encoding="utf-8")
            logger.info("Backed up AutoGlow user data from %s to %s", ", ".join(map(str, sources)), backup)
        except Exception as e:
            logger.exception("Failed backing up AutoGlow user data")
            return False, tr("Could not back up the older AutoGlow profiles: {error}").format(error=e)

        ok = cls._remove_system_parts(old)
        cls._stop_processes(old)
        if not ok:
            return False, "Failed removing the AutoGlow service."

        try:
            if primary:
                cls._copy_user_data(primary, target, overwrite=True)
            # Profiles that exist only in the other copies: AutoGlow imports presets/*.json on start
            for d in old:
                if d is not primary and (d / "presets").is_dir():
                    (target / "presets").mkdir(exist_ok=True)
                    for f in (d / "presets").glob("*.json"):
                        if not (target / "presets" / f.name).exists():
                            shutil.copy2(f, target / "presets" / f.name)
        except Exception as e:
            logger.exception("Failed carrying over AutoGlow user data")
            return False, tr("Could not carry over the older AutoGlow profiles: {error}").format(error=e)

        for d in old:
            if cls._has_local_work(d):
                logger.warning("Keeping %s: it has git changes not on its remote", d)
                continue
            cls._remove_folder(d)
        return True, ""

    @staticmethod
    def _copy_user_data(src: Path, dest: Path, overwrite: bool):
        dest.mkdir(parents=True, exist_ok=True)
        for name in USER_DATA:
            s, d = src / name, dest / name
            if not s.exists() or (d.exists() and not overwrite):
                continue
            if s.is_dir():
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)

    @staticmethod
    def _remove_system_parts(old: list[Path]) -> bool:
        """Stop and remove the service, the browser extension policies and the SELinux labels of old copies."""
        selinux = " ".join(
            f"semanage fcontext -d {shlex.quote(str(d) + '/venv/bin(/.*)?')} 2>/dev/null || true;" for d in old)
        script = (
            f"systemctl disable --now {SERVICE_NAME} 2>/dev/null || true; "
            f"rm -f {UNIT_PATH} {' '.join(POLICY_FILES)}; "
            f"if command -v semanage >/dev/null 2>&1; then {selinux or 'true;'} fi; "
            "systemctl daemon-reload"
        )
        try:
            res = subprocess.run(["sudo", "-n", "bash", "-c", script], capture_output=True, text=True, timeout=60)
            if res.returncode != 0:
                logger.warning("Removing the old AutoGlow service failed: %s", res.stderr.strip())
            return res.returncode == 0
        except Exception:
            logger.exception("Failed removing the old AutoGlow service")
            return False

    @classmethod
    def _stop_processes(cls, old: list[Path]):
        """Hand-started copies would keep port 8080 busy for the new service."""
        targets = {d.resolve() for d in old}
        pids = []
        for pid, folder in cls._autoglow_processes():
            try:
                if folder.resolve() in targets:
                    os.kill(pid, signal.SIGTERM)
                    pids.append(pid)
            except OSError:
                continue
        deadline = time.monotonic() + 5
        while pids and time.monotonic() < deadline:
            pids = [p for p in pids if Path(f"/proc/{p}").exists()]
            time.sleep(0.2)
        for p in pids:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass

    @classmethod
    def _remove_folder(cls, path: Path):
        # Guard against ever deleting anything but an AutoGlow 2 folder
        if not cls._is_autoglow2_dir(path) or path.resolve() in (Path.home().resolve(), Path("/")) or len(path.resolve().parts) < 3:
            logger.warning("Refusing to delete %s", path)
            return
        try:
            shutil.rmtree(path)
        except PermissionError:
            # Files created by an older setup run as root
            subprocess.run(["sudo", "-n", "rm", "-rf", "--", str(path)], capture_output=True, timeout=60)
        except Exception:
            logger.exception("Failed deleting %s", path)
        if path.exists():
            logger.warning("Could not fully delete %s", path)
        else:
            logger.info("Removed older AutoGlow 2 copy %s", path)

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
            return False, tr("Update failed: {error}").format(error=e)

        ok, msg = cls._run_setup(install_dir)
        if not ok:
            return ok, msg
        version = cls.get_version(install_dir)
        return ok, tr("AutoGlow 2 updated to {version}.").format(version=version) if version else tr("AutoGlow 2 updated to the latest version.")

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
        try:
            if not cls._remove_system_parts([install_dir] if install_dir else []):
                return False, "Failed removing the AutoGlow service."
            # Only a folder SUIT cloned itself is deleted, never a copy the user set up elsewhere
            if remove_files and install_dir == DEFAULT_DIR:
                shutil.rmtree(install_dir)
            if remove_files and install_dir == DEFAULT_DIR:
                return True, "AutoGlow 2 removed."
            return True, "AutoGlow 2 service removed, its folder was kept."
        except Exception as e:
            logger.exception("Failed uninstalling AutoGlow")
            return False, tr("Uninstall failed: {error}").format(error=e)
