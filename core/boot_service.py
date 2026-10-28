"""Fast Boot tweak: trims boot time and records every change so it can be reverted exactly."""

import json
import re
import subprocess
from pathlib import Path
from core.logger import get_logger

logger = get_logger("boot")

DEFAULT_STATE_PATH = Path.home() / ".config" / "suit" / "fast_boot.json"
ISCSI_NODES_DIR = Path("/var/lib/iscsi/nodes")

# NetworkManager-wait-online holds GDM (and so auto-login) until DHCP finishes,
# via iscsi.service -> remote-fs.target -> systemd-user-sessions. Autodarts copes
# with the network coming up after it starts, so the wait buys nothing here.
NETWORK_WAIT_UNITS = ["NetworkManager-wait-online.service"]

# Only units that actually start on a normal boot; condition-skipped ones
# (livesys, VM guest agents) cost nothing and are left alone.
UNUSED_SYSTEM_UNITS = [
    "abrtd.service",
    "abrt-journal-core.service",
    "abrt-oops.service",
    "abrt-vmcore.service",
    "abrt-xorg.service",
    "ModemManager.service",
    "cups.socket",
    "cups.path",
    "plocate-updatedb.timer",
    "dnf-makecache.timer",
]

# Skipped when iSCSI targets are configured on this machine.
ISCSI_UNITS = [
    "iscsi-starter.service",
    "iscsi-onboot.service",
    "iscsid.socket",
    "iscsiuio.socket",
]

# File indexer that starts with the session and competes with Autodarts for disk/CPU.
USER_MASK_UNITS = ["localsearch-3.service"]


class BootService:
    @staticmethod
    def _run(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, capture_output=True, text=True)

    @classmethod
    def _unit_state(cls, unit: str, user: bool = False) -> str:
        cmd = ["systemctl"] + (["--user"] if user else []) + ["is-enabled", unit]
        try:
            return cls._run(cmd).stdout.strip() or "not-found"
        except Exception:
            logger.debug(f"Failed reading enablement state of {unit}")
            return "unknown"

    @staticmethod
    def _iscsi_in_use(nodes_dir: Path = ISCSI_NODES_DIR) -> bool:
        try:
            return nodes_dir.is_dir() and any(nodes_dir.iterdir())
        except PermissionError:
            # Can't tell, so treat it as in use
            return True

    @classmethod
    def _grub_fastboot_set(cls) -> bool:
        try:
            res = cls._run(["sudo", "-n", "grub2-editenv", "list"])
            return any(line.strip() == "fastboot=1" for line in res.stdout.splitlines())
        except Exception:
            logger.debug("Failed reading grubenv")
            return False

    @staticmethod
    def _load_state(state_path: Path) -> dict | None:
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception:
            logger.exception(f"Corrupt Fast Boot state file: {state_path}")
            return None

    @staticmethod
    def _save_state(state: dict, state_path: Path):
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    @classmethod
    def is_fast_boot_enabled(cls, state_path: Path | None = None) -> bool:
        return (state_path or DEFAULT_STATE_PATH).exists()

    @classmethod
    def get_last_boot_time(cls) -> float | None:
        try:
            res = cls._run(["systemd-analyze"])
            match = re.search(r"=\s*(?:(\d+)min\s*)?([\d.]+)s", res.stdout)
            if not match:
                return None
            return int(match.group(1) or 0) * 60 + float(match.group(2))
        except Exception:
            logger.debug("Failed reading systemd-analyze boot time")
            return None

    @classmethod
    def enable_fast_boot(cls, state_path: Path | None = None) -> tuple[bool, str]:
        path = state_path or DEFAULT_STATE_PATH
        state = cls._load_state(path) or {"system_units": [], "user_units": [], "grub_fastboot": False}

        candidates = NETWORK_WAIT_UNITS + UNUSED_SYSTEM_UNITS
        if not cls._iscsi_in_use():
            candidates += ISCSI_UNITS

        failed = []
        # Only touch what is enabled now, so revert never enables something the user had off
        for unit in candidates:
            if unit in state["system_units"] or cls._unit_state(unit) != "enabled":
                continue
            if cls._run(["sudo", "-n", "systemctl", "disable", unit]).returncode == 0:
                state["system_units"].append(unit)
            else:
                failed.append(unit)

        for unit in USER_MASK_UNITS:
            if unit in state["user_units"] or cls._unit_state(unit, user=True) in ("masked", "not-found"):
                continue
            if cls._run(["systemctl", "--user", "mask", unit]).returncode == 0:
                state["user_units"].append(unit)
            else:
                failed.append(unit)

        if not state["grub_fastboot"] and not cls._grub_fastboot_set():
            if cls._run(["sudo", "-n", "grub2-editenv", "-", "set", "fastboot=1"]).returncode == 0:
                state["grub_fastboot"] = True
            else:
                failed.append("GRUB menu timeout")

        cls._save_state(state, path)
        logger.info(f"Fast Boot enabled: {state}, failed: {failed}")
        if failed:
            return False, f"Fast Boot partly applied, failed: {', '.join(failed)}"
        return True, "Fast Boot enabled. Takes effect on next boot."

    @classmethod
    def disable_fast_boot(cls, state_path: Path | None = None) -> tuple[bool, str]:
        path = state_path or DEFAULT_STATE_PATH
        state = cls._load_state(path)
        if state is None:
            path.unlink(missing_ok=True)
            return True, "Fast Boot already off."

        remaining = {"system_units": [], "user_units": [], "grub_fastboot": False}

        for unit in state.get("system_units", []):
            if cls._run(["sudo", "-n", "systemctl", "enable", unit]).returncode != 0:
                remaining["system_units"].append(unit)

        for unit in state.get("user_units", []):
            if cls._run(["systemctl", "--user", "unmask", unit]).returncode != 0:
                remaining["user_units"].append(unit)

        if state.get("grub_fastboot"):
            if cls._run(["sudo", "-n", "grub2-editenv", "-", "unset", "fastboot"]).returncode != 0:
                remaining["grub_fastboot"] = True

        if any(remaining.values()):
            # Keep what is still applied so the switch stays on and a retry finishes the job
            cls._save_state(remaining, path)
            logger.error(f"Fast Boot revert incomplete: {remaining}")
            return False, "Could not fully revert Fast Boot. Try again."

        path.unlink(missing_ok=True)
        logger.info("Fast Boot reverted")
        return True, "Fast Boot disabled. Original boot settings restored."

    @classmethod
    def set_fast_boot(cls, enabled: bool, state_path: Path | None = None) -> tuple[bool, str]:
        if enabled:
            return cls.enable_fast_boot(state_path)
        return cls.disable_fast_boot(state_path)
