"""Repairs after a Fedora upgrade (new GNOME Shell, new Python, gnome-keyring -> oo7, new systemd).

SUIT remembers the system versions it last saw working in ~/.config/suit/system_versions.json.
At start-up `find_problems()` looks for what an upgrade typically leaves broken; each Problem carries its
own fix, which the "Fix All" page runs one after the other. The versions are stored again once nothing
is left to fix, so a later upgrade is reported as "Fedora 45 -> 46".
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.logger import get_logger
from core.i18n import tr

logger = get_logger("upgrade")

STATE_PATH = Path.home() / ".config" / "suit" / "system_versions.json"
PROJECT_DIR = Path(__file__).resolve().parent.parent
SUIT_VENV = PROJECT_DIR / "venv"
SYSTEM_PYTHON = Path("/usr/bin/python3")
KEEPER_UNIT = Path.home() / ".config" / "systemd" / "user" / "suit-camera-keeper.service"
# Packages a venv gets from `python -m venv` itself
VENV_BASE_PACKAGES = {"pip", "setuptools", "wheel"}


@dataclass
class Problem:
    id: str
    title: str
    detail: str
    fix: Callable[[], tuple[bool, str]]
    relogin: bool = False       # the fix takes effect at the next login
    interactive: bool = False   # the fix opens a system prompt the user has to answer
    waiting_for_login: bool = False  # already fixed, only a login/restart is missing


# ------------------------------------------------------------------ versions

def _os_release() -> dict:
    out = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            key, _, val = line.partition("=")
            out[key.strip()] = val.strip().strip('"')
    except OSError:
        pass
    return out


def gnome_shell_version() -> str | None:
    """Version of the running GNOME Shell ("51.1"), else of the installed one."""
    try:
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        res = bus.call_sync("org.gnome.Shell", "/org/gnome/Shell", "org.freedesktop.DBus.Properties", "Get",
                            GLib.Variant("(ss)", ("org.gnome.Shell", "ShellVersion")), GLib.VariantType("(v)"),
                            Gio.DBusCallFlags.NONE, 2000, None)
        return str(res.unpack()[0])
    except Exception:
        pass
    try:
        out = subprocess.run(["gnome-shell", "--version"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"(\d+(?:\.\w+)*)", out)
        return m.group(1) if m else None
    except Exception:
        return None


def _major(version: str | None) -> str | None:
    m = re.match(r"(\d+)", version or "")
    return m.group(1) if m else None


def _interpreter_version(path) -> str | None:
    """'3.15' for a python binary (symlinks resolved), None if it is missing."""
    real = os.path.realpath(path)
    if not os.path.exists(real):
        return None
    m = re.search(r"python(\d+\.\d+)$", os.path.basename(real))
    if m:
        return m.group(1)
    try:
        out = subprocess.run([real, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return out or None
    except Exception:
        return None


def current_versions() -> dict:
    osr = _os_release()
    return {"os_name": osr.get("NAME", "Linux"), "os": osr.get("VERSION_ID", ""),
            "gnome": _major(gnome_shell_version()) or "", "python": _interpreter_version(SYSTEM_PYTHON) or ""}


def load_seen() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("Failed reading %s", STATE_PATH)
        return {}


def save_seen(versions: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(versions, indent=2), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except Exception:
        logger.exception("Failed saving %s", STATE_PATH)


def guess_previous(current: dict) -> dict:
    """Versions before an upgrade SUIT did not see coming (no record yet), from what the upgrade left behind:
    kernels of the older Fedora release in /lib/modules and the Python version stale venvs were built for."""
    prev = {}
    releases = set()
    for d in Path("/lib/modules").glob("*"):
        m = re.search(r"\.fc(\d+)\.", d.name)
        if m:
            releases.add(int(m.group(1)))
    older = [r for r in releases if current.get("os", "").isdigit() and r < int(current["os"])]
    if older:
        prev["os"] = str(max(older))
    venvs = [SUIT_VENV]
    try:
        autoglow = _autoglow_dir()
    except Exception:
        autoglow = None
    if autoglow:
        venvs.append(autoglow / "venv")
    for venv in venvs:
        built = venv_built_for(venv)
        if built and built != current.get("python"):
            prev["python"] = built
            break
    return prev


def describe_changes(seen: dict, current: dict) -> list[str]:
    """['Fedora Linux 44 → 45', 'GNOME 50 → 51', 'Python 3.14 → 3.15'] for what changed since SUIT last looked."""
    out = []
    for key, label in (("os", current.get("os_name", "Linux")), ("gnome", "GNOME"), ("python", "Python")):
        old, new = seen.get(key), current.get(key)
        if old and new and old != new:
            out.append(f"{label} {old} → {new}")
    return out


# ------------------------------------------------------------------ helpers

def _run(cmd: list[str], timeout: int = 120, **kw) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(cmd, 124, "", f"timed out after {e.timeout:.0f} s")
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def _last_line(res: subprocess.CompletedProcess) -> str:
    lines = (res.stderr.strip() or res.stdout.strip()).splitlines()
    return lines[-1] if lines else f"exit code {res.returncode}"


# ------------------------------------------------------------------ Python environments

def venv_built_for(venv: Path) -> str | None:
    """Python version a venv was created with, from its pyvenv.cfg ('3.14')."""
    try:
        cfg = (venv / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"^version(?:_info)?\s*=\s*(\d+\.\d+)", cfg, re.M)
    return m.group(1) if m else None


def venv_is_stale(venv: Path) -> bool:
    """True when a venv was built for another Python than the one it now runs: its packages are not found."""
    built = venv_built_for(venv)
    if built is None:
        return False
    runs = _interpreter_version(venv / "bin" / "python3")
    return runs is None or runs != built


def _venv_packages(venv: Path) -> list[str]:
    """Names of the packages pip installed into a venv (any Python version folder)."""
    names = set()
    for info in venv.glob("lib/python*/site-packages/*.dist-info"):
        try:
            installer = (info / "INSTALLER").read_text(encoding="utf-8").strip()
        except OSError:
            installer = "pip"
        if installer != "pip":
            continue
        name = info.name.split("-")[0]
        try:
            m = re.search(r"^Name:\s*(\S+)", (info / "METADATA").read_text(encoding="utf-8", errors="ignore"), re.M)
            name = m.group(1) if m else name
        except OSError:
            pass
        if name.lower() not in VENV_BASE_PACKAGES:
            names.add(name)
    return sorted(names, key=str.lower)


def rebuild_venv(venv: Path) -> tuple[bool, str]:
    """Recreate a venv for the current Python and reinstall the packages pip had put into it."""
    try:
        system_site = bool(re.search(r"^include-system-site-packages\s*=\s*true",
                                     (venv / "pyvenv.cfg").read_text(encoding="utf-8"), re.M | re.I))
    except OSError:
        system_site = True
    packages = _venv_packages(venv)
    cmd = [str(SYSTEM_PYTHON), "-m", "venv", "--clear"] + (["--system-site-packages"] if system_site else []) + [str(venv)]
    res = _run(cmd, timeout=300)
    if res.returncode != 0:
        logger.warning("Rebuilding %s failed: %s", venv, res.stderr)
        return False, tr("Could not rebuild the Python environment: {error}").format(error=_last_line(res))
    logger.info("Rebuilt %s for Python %s", venv, _interpreter_version(SYSTEM_PYTHON))
    if packages:
        res = _run([str(venv / "bin" / "python3"), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                    *packages], timeout=900)
        if res.returncode != 0:
            logger.warning("Reinstalling %s into %s failed: %s", packages, venv, res.stderr[-2000:])
            return True, tr("Python environment rebuilt, but these packages could not be reinstalled: {pkgs}").format(
                pkgs=", ".join(packages))
    return True, tr("Python environment rebuilt.")


# ------------------------------------------------------------------ problem: SUIT's own venv

def _check_suit_venv() -> Problem | None:
    if not venv_is_stale(SUIT_VENV):
        return None

    def fix() -> tuple[bool, str]:
        ok, msg = rebuild_venv(SUIT_VENV)
        if ok:
            try:
                if KEEPER_UNIT.is_file() and str(SUIT_VENV) in KEEPER_UNIT.read_text(encoding="utf-8"):
                    _run(["systemctl", "--user", "try-restart", KEEPER_UNIT.name])
            except OSError:
                pass
        return ok, msg

    return Problem(
        "suit_venv", tr("SUIT Python environment"),
        tr("Built for Python {old}, the system now runs Python {new}. Rotation at boot and the camera keeper use it.").format(
            old=venv_built_for(SUIT_VENV), new=_interpreter_version(SUIT_VENV / "bin" / "python3") or "?"),
        fix)


# ------------------------------------------------------------------ problem: AutoGlow 2's venv

def _autoglow_dir() -> Path | None:
    from core.autoglow_service import AutoGlowService
    d = AutoGlowService.get_install_dir() or AutoGlowService._unit_dir()
    return d if d and AutoGlowService._is_autoglow2_dir(d) else None


def _check_autoglow() -> Problem | None:
    install_dir = _autoglow_dir()
    if not install_dir:
        return None
    venv = install_dir / "venv"
    if not venv_is_stale(venv):
        return None
    old = venv_built_for(venv)

    def fix() -> tuple[bool, str]:
        from core.autoglow_service import AutoGlowService
        # The old venv cannot run anymore; AutoGlow's setup builds a fresh one, installs its packages
        # (and the Rust LED engine) and restarts the service.
        shutil.rmtree(venv, ignore_errors=True)
        ok, msg = AutoGlowService._run_setup(install_dir)
        if not ok:
            return False, msg
        active = _run(["systemctl", "is-active", "autoglow.service"]).stdout.strip()
        if active != "active":
            return False, tr("AutoGlow 2 was set up again but its service is {state}.").format(state=active or "?")
        return True, tr("AutoGlow 2 is running again.")

    return Problem(
        "autoglow_venv", tr("AutoGlow 2"),
        tr("Its Python environment was built for Python {old} and cannot start. AutoGlow's setup runs again "
           "(takes a few minutes).").format(old=old),
        fix)


# ------------------------------------------------------------------ problem: SUIT shell extensions

def _suit_extensions() -> list[tuple[str, Path, Path]]:
    from core.desktop_service import DesktopService as D
    return [(D.EXTENSION_UUID, D.EXTENSION_SOURCE_DIR, D.USER_EXTENSION_DIR),
            (D.HW_OVERLAY_UUID, D.HW_OVERLAY_SOURCE_DIR, D.HW_OVERLAY_USER_DIR),
            (D.MINIMAL_DESKTOP_UUID, D.MINIMAL_DESKTOP_SOURCE_DIR, D.MINIMAL_DESKTOP_USER_DIR)]


def _shell_versions(ext_dir: Path) -> list[str]:
    try:
        return [str(v) for v in json.loads((ext_dir / "metadata.json").read_text(encoding="utf-8")).get("shell-version", [])]
    except (OSError, ValueError):
        return []


def extension_state(uuid: str) -> str | None:
    """GNOME Shell's state for an extension ('ACTIVE', 'OUT OF DATE', 'ERROR', ...), None if it does not know it."""
    res = _run(["gnome-extensions", "info", uuid], timeout=10, env={**os.environ, "LC_ALL": "C"})
    if res.returncode != 0:
        return None
    m = re.search(r"^\s*State:\s*(.+)$", res.stdout, re.M)
    return m.group(1).strip() if m else None


def _enabled_extensions() -> list[str]:
    from core.desktop_service import DesktopService
    try:
        return DesktopService._get_enabled_extensions()
    except Exception:
        return []


def _check_extensions() -> Problem | None:
    shell = _major(gnome_shell_version())
    if not shell:
        return None
    enabled = _enabled_extensions()
    outdated, waiting = [], []
    for uuid, _src, dest in _suit_extensions():
        if not (dest / "metadata.json").is_file():
            continue
        if shell not in _shell_versions(dest):
            outdated.append(uuid)
        elif uuid in enabled and extension_state(uuid) == "OUT OF DATE":
            waiting.append(uuid)  # files already fixed, GNOME Shell reads them at the next login
    if not outdated and not waiting:
        return None

    def fix() -> tuple[bool, str]:
        from core.desktop_service import DesktopService
        untested = []
        for uuid, src, dest in _suit_extensions():
            if not (dest / "metadata.json").is_file():
                continue
            DesktopService.install_extension_files(src, dest)
            versions = _shell_versions(dest)
            if shell not in versions:
                # A GNOME Shell newer than this SUIT knows: let it load the extension anyway. GNOME Shell
                # catches errors per extension; a SUIT update brings a tested version later.
                meta_path = dest / "metadata.json"
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta["shell-version"] = versions + [shell]
                meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
                untested.append(uuid)
                logger.warning("%s: added untested GNOME Shell %s to the installed metadata", uuid, shell)
        if untested:
            return True, tr("Extensions updated (not yet tested with GNOME {version}). Restart or log out to load them.").format(
                version=shell)
        return True, tr("Extensions updated for GNOME {version}. Restart or log out to load them.").format(version=shell)

    names = ", ".join(outdated + waiting)
    if outdated:
        detail = tr("GNOME {version} switched off: {names}. They get a version for this GNOME.").format(
            version=shell, names=names)
    else:
        detail = tr("Already updated, GNOME loads them at the next login: {names}.").format(names=names)
    return Problem("extensions", tr("Touch, overlay and desktop extensions"), detail, fix, relogin=True,
                   waiting_for_login=not outdated)


# ------------------------------------------------------------------ problem: keyring (gnome-keyring -> oo7)

def _check_keyring() -> Problem | None:
    from core.keyring_service import KeyringService
    from core.power_service import PowerService
    if not KeyringService.uses_oo7():
        return None
    try:
        autologin, _ = PowerService.is_auto_login_enabled()
    except Exception:
        autologin = False
    # Only a problem for an automatic login: nothing types the keyring password then. Do not trust "unlocked
    # right now": right after the upgrade oo7 keeps the migrated keyring open until the next reboot.
    if not autologin or KeyringService.oo7_autounlock_ok()[0]:
        return None
    return Problem(
        "keyring", tr("Login keyring"),
        tr("The new keyring service (oo7) cannot unlock the keyring after the automatic login and asks for a "
           "password at every start. SUIT stores a keyring password protected by this computer's TPM, so oo7 "
           "unlocks it by itself. Saved entries are kept."),
        KeyringService.setup_oo7_autounlock, interactive=KeyringService.oo7_login_locked() is True)


# ------------------------------------------------------------------ problem: Remote Desktop starts before oo7

def _check_remote_desktop() -> Problem | None:
    from core.remote_desktop_service import RemoteDesktopService
    if not RemoteDesktopService.needs_secret_service_order():
        return None
    return Problem(
        "remote_desktop", tr("Remote Desktop"),
        tr("It starts at the same moment as the new keyring service (oo7) and fails when oo7 is not ready "
           "yet. It is set to wait for oo7."),
        RemoteDesktopService.ensure_secret_service_order)


# ------------------------------------------------------------------ problem: Fast Boot's login unit copy

def _check_login_override() -> Problem | None:
    from core.boot_service import BootService, USER_SESSIONS_OVERRIDE
    if not USER_SESSIONS_OVERRIDE.is_file():
        return None
    try:
        installed = USER_SESSIONS_OVERRIDE.read_text(encoding="utf-8")
    except OSError:
        return None
    if "Written by SUIT Fast Boot" not in installed:
        return None
    wanted = BootService._user_sessions_text()
    if wanted is None or wanted == installed:
        return None

    def fix() -> tuple[bool, str]:
        res = _run(["sudo", "-n", "tee", str(USER_SESSIONS_OVERRIDE)], input=wanted)
        if res.returncode != 0:
            return False, tr("Could not write {path}: {error}").format(path=USER_SESSIONS_OVERRIDE, error=_last_line(res))
        _run(["sudo", "-n", "systemctl", "daemon-reload"])
        return True, tr("Login unit updated to the new systemd version.")

    return Problem(
        "login_override", tr("Fast Boot: login before network"),
        tr("SUIT's copy of systemd-user-sessions.service is older than the new systemd's. It is copied again."),
        fix)


# ------------------------------------------------------------------ public API

CHECKS = (_check_autoglow, _check_extensions, _check_suit_venv, _check_login_override, _check_keyring,
          _check_remote_desktop)


def find_problems() -> list[Problem]:
    """Everything an upgrade left broken, in the order Fix All runs it (interactive fixes last)."""
    problems = []
    for check in CHECKS:
        try:
            p = check()
        except Exception:
            logger.exception("Upgrade check %s failed", check.__name__)
            continue
        if p:
            problems.append(p)
    problems.sort(key=lambda p: p.interactive)
    return problems


def startup_check() -> dict:
    """Run at SUIT start (background thread). Records the versions when all is well."""
    current = current_versions()
    seen = load_seen()
    problems = find_problems()
    if problems and not seen:
        seen = guess_previous(current)
    if seen != current and all(p.waiting_for_login for p in problems):
        save_seen(current)
    if problems:
        logger.info("After the system upgrade (%s) %d thing(s) need fixing: %s",
                    ", ".join(describe_changes(seen, current)) or "versions unknown", len(problems),
                    ", ".join(p.id for p in problems))
    return {"problems": problems, "changes": describe_changes(seen, current), "current": current}


def mark_fixed() -> None:
    save_seen(current_versions())


def reboot() -> tuple[bool, str]:
    res = _run(["systemctl", "reboot"])
    return res.returncode == 0, _last_line(res) if res.returncode else ""


__all__ = ["Problem", "find_problems", "startup_check", "mark_fixed", "current_versions", "describe_changes",
           "venv_is_stale", "rebuild_venv", "extension_state", "reboot"]
