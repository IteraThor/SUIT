"""Fast Boot: individually toggleable boot-time tweaks, each recording its changes for an exact revert,
plus a firmware (BIOS) analysis with a vendor-specific guide."""

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from core.logger import get_logger
from core.i18n import tr

logger = get_logger("boot")

DEFAULT_STATE_PATH = Path.home() / ".config" / "suit" / "fast_boot.json"
ISCSI_NODES_DIR = Path("/var/lib/iscsi/nodes")
FSTAB_PATH = Path("/etc/fstab")
DMI_DIR = Path("/sys/class/dmi/id")
EFI_DIR = Path("/sys/firmware/efi")
DRACUT_CONF = Path("/etc/dracut.conf.d/90-suit-fast-boot.conf")
# The graphics driver (i915 here: ~2 s) only loads in the initrd so the splash can draw;
# without these modules the root disk mounts as soon as it appears.
DRACUT_CONF_TEXT = '# Written by SUIT Fast Boot\nomit_dracutmodules+=" plymouth drm "\n'


@dataclass(frozen=True)
class Tweak:
    id: str
    title: str
    summary: str
    kind: str = "units"            # "units", "grub" or "dracut"
    system_units: tuple = ()       # disabled, only if enabled now
    user_units: tuple = ()         # masked for the user session
    default_s: float = 0.2         # estimate when the last boot gives nothing to measure
    blocks_login: bool = False     # on the critical path to the desktop, not just background load
    recommended: bool = True
    warning: str = ""


# NetworkManager-wait-online holds GDM (and so auto-login) until DHCP finishes,
# via iscsi.service -> remote-fs.target -> systemd-user-sessions. Autodarts copes
# with the network coming up after it starts, so the wait buys nothing here.
# Only units that actually start on a normal boot are listed; condition-skipped ones
# (livesys, VM guest agents) cost nothing and are left alone.
TWEAKS = [
    Tweak("network_wait", "Skip Network Wait",
          "Start the desktop without waiting for the network to connect.",
          system_units=("NetworkManager-wait-online.service",), default_s=3.0, blocks_login=True),
    Tweak("remote_fs", "Skip Network Drives",
          "Don't wait for NFS network drives before login.",
          system_units=("nfs-client.target", "remote-fs.target"), default_s=0.3, blocks_login=True),
    Tweak("iscsi", "Skip iSCSI Storage",
          "Stop network storage services that nothing here uses.",
          system_units=("iscsi-starter.service", "iscsi-onboot.service", "iscsid.socket", "iscsiuio.socket"),
          default_s=0.3, blocks_login=True),
    Tweak("boot_menu", "Hide Boot Menu",
          "Skip the boot menu countdown. Hold Shift during boot to get it back.",
          kind="grub", default_s=1.0, blocks_login=True),
    Tweak("unused_services", "Unused Background Services",
          "Crash reporter, modem manager, printing, file search database and package cache refresh.",
          system_units=("abrtd.service", "abrt-journal-core.service", "abrt-oops.service",
                        "abrt-vmcore.service", "abrt-xorg.service", "ModemManager.service",
                        "cups.socket", "cups.path", "plocate-updatedb.timer", "dnf-makecache.timer")),
    Tweak("file_indexer", "File Indexer",
          "Stop desktop file indexing, which competes with Autodarts for disk and CPU.",
          user_units=("localsearch-3.service",), default_s=0.5),
    Tweak("maintenance", "Maintenance Daemons",
          "Disk health (smartd), RAID monitor, CPU error log and the at job scheduler.",
          system_units=("smartd.service", "mdmonitor.service", "mcelog.service", "atd.service")),
    Tweak("rsyslog", "Duplicate System Log",
          "Turn off rsyslog. The system journal keeps every log anyway.",
          system_units=("rsyslog.service",), default_s=0.5),
    Tweak("splash", "Skip Boot Splash",
          "Leave the logo animation and graphics driver out of the early boot image so the disk mounts sooner.",
          kind="dracut", default_s=1.5, blocks_login=True, recommended=False,
          warning="Rebuilds the boot image (about a minute). The screen stays black or shows text until login."),
    Tweak("bluetooth", "Bluetooth and Network Discovery",
          "Turn off Bluetooth and Avahi device discovery.",
          system_units=("bluetooth.service", "avahi-daemon.service", "avahi-daemon.socket"),
          recommended=False, warning="Bluetooth keyboards, controllers and speakers stop working."),
    Tweak("firewall", "Firewall",
          "The network only starts after the firewall is up, and login waits for the network.",
          system_units=("firewalld.service",), default_s=1.5, blocks_login=True, recommended=False,
          warning="Less secure. Only on a trusted home network behind a router."),
]
TWEAKS_BY_ID = {t.id: t for t in TWEAKS}

# Key that opens firmware setup at power-on, by DMI vendor
SETUP_KEYS = {
    "hp": "F10", "hewlett-packard": "F10", "dell": "F2", "lenovo": "F1 or F2", "asus": "Del or F2",
    "asustek": "Del or F2", "msi": "Del", "micro-star": "Del", "gigabyte": "Del", "asrock": "F2 or Del",
    "intel": "F2", "acer": "F2", "fujitsu": "F2", "medion": "F2", "beelink": "Del", "minisforum": "Del",
}

# Generic BIOS guide; vendors with a known menu layout override the "where" hints
BIOS_STEPS = [
    ("fast_boot", "Turn on Fast Boot",
     "Skips most hardware checks at power-on. Often called Fast Boot or Quick Boot."),
    ("pxe", "Turn off Network (PXE) Boot",
     "Stops the firmware from preparing to boot over the network."),
    ("order", "Put Fedora first in the boot order",
     "The firmware then goes straight to the disk instead of trying USB or network first."),
    ("delay", "Set boot and POST delays to 0",
     "Some boards wait a few seconds for a key press before booting."),
    ("legacy", "Turn off Legacy / CSM support",
     "Pure UEFI mode skips loading old option ROMs."),
    ("logo", "Turn off the full-screen logo",
     "Often called Quiet Boot or Full Screen Logo."),
    ("devices", "Turn off unused onboard devices",
     "Serial ports, card readers or a second network card each add a little start-up time."),
]
VENDOR_HINTS = {
    "hp": {
        "fast_boot": "Advanced > Boot Options > Fast Boot.",
        "pxe": "Advanced > Boot Options > untick Network (PXE) Boot.",
        "order": "Advanced > Boot Options > UEFI Boot Sources: move the Fedora entry to the top.",
        "delay": "Advanced > Boot Options > POST Delay and Startup Menu Delay to 0.",
        "legacy": "Advanced > Secure Boot Configuration > Legacy Support Disable.",
        "logo": None,
        "devices": "Advanced > Built-In Device Options and Port Options.",
    },
}


class BootService:
    @staticmethod
    def _run(cmd: list[str], input: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, capture_output=True, text=True, input=input)

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

    @staticmethod
    def _network_mounts_in_fstab() -> bool:
        try:
            for line in FSTAB_PATH.read_text(encoding="utf-8").splitlines():
                fields = line.split()
                if len(fields) < 4 or fields[0].startswith("#"):
                    continue
                if fields[2].startswith(("nfs", "cifs", "smb")) or "_netdev" in fields[3]:
                    return True
        except OSError:
            return True
        return False

    @classmethod
    def _disk_encrypted(cls) -> bool:
        try:
            return "crypt" in cls._run(["lsblk", "-rno", "TYPE"]).stdout.split()
        except Exception:
            return True

    @classmethod
    def _grub_fastboot_set(cls) -> bool:
        try:
            res = cls._run(["sudo", "-n", "grub2-editenv", "list"])
            return any(line.strip() == "fastboot=1" for line in res.stdout.splitlines())
        except Exception:
            logger.debug("Failed reading grubenv")
            return False

    # ------------------------------------------------------------------ state

    @staticmethod
    def _load_state(state_path: Path) -> dict:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"tweaks": {}}
        except Exception:
            logger.exception(f"Corrupt Fast Boot state file: {state_path}")
            return {"tweaks": {}}
        if "tweaks" not in state:
            state = BootService._migrate_v1(state)
        return state

    @staticmethod
    def _migrate_v1(old: dict) -> dict:
        """The single Fast Boot switch stored flat lists; spread them over the tweaks they belong to."""
        tweaks = {}
        for unit in old.get("system_units", []):
            owner = next((t for t in TWEAKS if unit in t.system_units), None)
            if owner:
                tweaks.setdefault(owner.id, {"system_units": []})["system_units"].append(unit)
        for unit in old.get("user_units", []):
            owner = next((t for t in TWEAKS if unit in t.user_units), None)
            if owner:
                tweaks.setdefault(owner.id, {}).setdefault("user_units", []).append(unit)
        if old.get("grub_fastboot"):
            tweaks["boot_menu"] = {"grub_fastboot": True}
        return {"tweaks": tweaks}

    @staticmethod
    def _save_state(state: dict, state_path: Path):
        if not state["tweaks"]:
            state_path.unlink(missing_ok=True)
            return
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    @classmethod
    def is_fast_boot_enabled(cls, state_path: Path | None = None) -> bool:
        return cls.applied_count(state_path) > 0

    @classmethod
    def applied_count(cls, state_path: Path | None = None) -> int:
        return len(cls._load_state(state_path or DEFAULT_STATE_PATH)["tweaks"])

    # ------------------------------------------------------------------ measurements

    @staticmethod
    def _seconds(text: str) -> float:
        """'1min 2.5s', '743ms' or '3.2s' -> seconds."""
        total = 0.0
        for value, unit in re.findall(r"([\d.]+)\s*(min|ms|s)\b", text):
            total += float(value) * {"min": 60, "s": 1, "ms": 0.001}[unit]
        return total

    @classmethod
    def get_boot_report(cls) -> dict | None:
        """Per-phase times of the last boot from systemd-analyze, e.g. {'firmware': 12.4, ..., 'total': 33.3}."""
        try:
            out = cls._run(["systemd-analyze"]).stdout
        except Exception:
            logger.debug("Failed running systemd-analyze")
            return None
        line = next((l for l in out.splitlines() if l.startswith("Startup finished in")), "")
        if "=" not in line:
            return None
        parts, total = line[len("Startup finished in"):].rsplit("=", 1)
        report = {}
        for chunk in parts.split("+"):
            match = re.match(r"\s*(.+?)\s*\((\w+)\)", chunk)
            if match:
                report[match.group(2)] = cls._seconds(match.group(1))
        report["total"] = cls._seconds(total)
        return report

    @classmethod
    def get_last_boot_time(cls) -> float | None:
        report = cls.get_boot_report()
        return report["total"] if report else None

    @classmethod
    def _blame(cls) -> dict[str, float]:
        try:
            out = cls._run(["systemd-analyze", "blame"]).stdout
        except Exception:
            return {}
        times = {}
        for line in out.splitlines():
            match = re.match(r"\s*(.+?)\s+(\S+\.(?:service|socket|timer|target|path|mount))\s*$", line)
            if match:
                times[match.group(2)] = cls._seconds(match.group(1))
        return times

    @classmethod
    def _estimate(cls, tweak: Tweak, blame: dict[str, float]) -> float:
        measured = sum(blame.get(u, 0.0) for u in tweak.system_units)
        return round(measured, 1) if measured >= 0.1 else tweak.default_s

    # ------------------------------------------------------------------ tweaks

    @classmethod
    def _unavailable_reason(cls, tweak: Tweak) -> str:
        if tweak.id == "iscsi" and cls._iscsi_in_use():
            return "iSCSI storage is set up on this machine."
        if tweak.id == "remote_fs" and cls._network_mounts_in_fstab():
            return "Network drives are listed in /etc/fstab."
        if tweak.kind == "dracut" and cls._disk_encrypted():
            return "Disk encryption needs the boot splash for its password prompt."
        if tweak.kind == "grub" and cls._grub_fastboot_set():
            return "Already active on this system."
        if tweak.kind == "units":
            active = [u for u in tweak.system_units if cls._unit_state(u) == "enabled"]
            active += [u for u in tweak.user_units if cls._unit_state(u, user=True) not in ("masked", "not-found")]
            if not active:
                return "Already off on this system."
        return ""

    @classmethod
    def get_tweaks(cls, state_path: Path | None = None) -> list[dict]:
        applied = cls._load_state(state_path or DEFAULT_STATE_PATH)["tweaks"]
        blame = cls._blame()
        rows = []
        for t in TWEAKS:
            entry = applied.get(t.id)
            rows.append({
                "id": t.id, "title": t.title, "summary": t.summary, "warning": t.warning,
                "recommended": t.recommended, "blocks_login": t.blocks_login,
                "applied": entry is not None,
                "unavailable": "" if entry is not None else cls._unavailable_reason(t),
                "estimate_s": entry.get("saved_s", t.default_s) if entry is not None else cls._estimate(t, blame),
            })
        return rows

    @classmethod
    def _apply(cls, tweak: Tweak, entry: dict) -> list[str]:
        """Apply what is not applied yet, record it in entry, and return what failed."""
        failed = []
        # Only touch what is enabled now, so revert never enables something the user had off
        for unit in tweak.system_units:
            if unit in entry.setdefault("system_units", []) or cls._unit_state(unit) != "enabled":
                continue
            if cls._run(["sudo", "-n", "systemctl", "disable", unit]).returncode == 0:
                entry["system_units"].append(unit)
            else:
                failed.append(unit)
        for unit in tweak.user_units:
            if unit in entry.setdefault("user_units", []) or \
                    cls._unit_state(unit, user=True) in ("masked", "not-found"):
                continue
            if cls._run(["systemctl", "--user", "mask", unit]).returncode == 0:
                entry["user_units"].append(unit)
            else:
                failed.append(unit)
        if tweak.kind == "grub" and not entry.get("grub_fastboot") and not cls._grub_fastboot_set():
            if cls._run(["sudo", "-n", "grub2-editenv", "-", "set", "fastboot=1"]).returncode == 0:
                entry["grub_fastboot"] = True
            else:
                failed.append("boot menu timeout")
        if tweak.kind == "dracut" and not entry.get("dracut_conf"):
            wrote = cls._run(["sudo", "-n", "tee", str(DRACUT_CONF)], input=DRACUT_CONF_TEXT).returncode == 0
            if wrote and cls._run(["sudo", "-n", "dracut", "-f"]).returncode == 0:
                entry["dracut_conf"] = True
            else:
                cls._run(["sudo", "-n", "rm", "-f", str(DRACUT_CONF)])
                failed.append("boot image rebuild")
        return failed

    @classmethod
    def _revert(cls, entry: dict) -> dict:
        """Undo what entry recorded; return what is still applied (empty when fully reverted)."""
        remaining = {}
        for unit in entry.get("system_units", []):
            if cls._run(["sudo", "-n", "systemctl", "enable", unit]).returncode != 0:
                remaining.setdefault("system_units", []).append(unit)
        for unit in entry.get("user_units", []):
            if cls._run(["systemctl", "--user", "unmask", unit]).returncode != 0:
                remaining.setdefault("user_units", []).append(unit)
        if entry.get("grub_fastboot"):
            if cls._run(["sudo", "-n", "grub2-editenv", "-", "unset", "fastboot"]).returncode != 0:
                remaining["grub_fastboot"] = True
        if entry.get("dracut_conf"):
            removed = cls._run(["sudo", "-n", "rm", "-f", str(DRACUT_CONF)]).returncode == 0
            if not (removed and cls._run(["sudo", "-n", "dracut", "-f"]).returncode == 0):
                remaining["dracut_conf"] = True
        return remaining

    @classmethod
    def set_tweak(cls, tweak_id: str, enabled: bool, state_path: Path | None = None) -> tuple[bool, str]:
        tweak = TWEAKS_BY_ID[tweak_id]
        path = state_path or DEFAULT_STATE_PATH
        state = cls._load_state(path)

        if enabled:
            estimate = cls._estimate(tweak, cls._blame())
            entry = state["tweaks"].setdefault(tweak_id, {})
            failed = cls._apply(tweak, entry)
            entry.setdefault("saved_s", estimate)
            cls._save_state(state, path)
            logger.info(f"Fast Boot tweak {tweak_id} applied: {entry}, failed: {failed}")
            if failed:
                return False, tr("{title} partly applied, failed: {failed}").format(title=tr(tweak.title), failed=", ".join(failed))
            return True, tr("{title} on. Takes effect on next boot.").format(title=tr(tweak.title))

        entry = state["tweaks"].get(tweak_id)
        if entry is None:
            return True, tr("{title} already off.").format(title=tr(tweak.title))
        remaining = cls._revert(entry)
        if remaining:
            # Keep what is still applied so the switch stays on and a retry finishes the job
            state["tweaks"][tweak_id] = {**remaining, "saved_s": entry.get("saved_s", tweak.default_s)}
            cls._save_state(state, path)
            logger.error(f"Fast Boot tweak {tweak_id} revert incomplete: {remaining}")
            return False, tr("Could not fully revert {title}. Try again.").format(title=tr(tweak.title))
        del state["tweaks"][tweak_id]
        cls._save_state(state, path)
        logger.info(f"Fast Boot tweak {tweak_id} reverted")
        return True, tr("{title} off. Original setting restored.").format(title=tr(tweak.title))

    @classmethod
    def set_fast_boot(cls, enabled: bool, state_path: Path | None = None) -> tuple[bool, str]:
        """On: apply every recommended tweak that is available. Off: revert everything applied."""
        if enabled:
            ids = [t["id"] for t in cls.get_tweaks(state_path)
                   if t["recommended"] and not t["applied"] and not t["unavailable"]]
        else:
            ids = list(cls._load_state(state_path or DEFAULT_STATE_PATH)["tweaks"])
        failed = [TWEAKS_BY_ID[i].title for i in ids if not cls.set_tweak(i, enabled, state_path)[0]]
        if failed:
            return False, tr("Some tweaks failed: {failed}").format(failed=", ".join(failed))
        if enabled:
            return True, "Recommended tweaks on. Takes effect on next boot."
        return True, "All Fast Boot tweaks reverted."

    # ------------------------------------------------------------------ firmware

    @classmethod
    def _efi_boot_entries(cls) -> dict:
        """Parse efibootmgr: {'current': '000B', 'order': [...], 'timeout': 0, 'entries': {id: (name, active)}}."""
        try:
            out = cls._run(["efibootmgr"]).stdout
        except Exception:
            return {}
        info = {"entries": {}, "order": []}
        for line in out.splitlines():
            if line.startswith("BootCurrent:"):
                info["current"] = line.split(":", 1)[1].strip()
            elif line.startswith("BootOrder:"):
                info["order"] = [x.strip() for x in line.split(":", 1)[1].split(",") if x.strip()]
            elif line.startswith("Timeout:"):
                match = re.search(r"\d+", line)
                info["timeout"] = int(match.group()) if match else 0
            else:
                match = re.match(r"Boot([0-9A-Fa-f]{4})(\*?)\s+(.*?)(?:\t|\s{2,}|$)", line)
                if match:
                    info["entries"][match.group(1)] = (match.group(3).strip(), match.group(2) == "*")
        return info

    @staticmethod
    def _dmi(field: str) -> str:
        try:
            return (DMI_DIR / field).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @classmethod
    def analyze_firmware(cls, report: dict | None = None) -> dict:
        """How long the firmware took, what this tool can see that slows it, and a BIOS guide for this board."""
        report = report if report is not None else cls.get_boot_report()
        firmware_s = (report or {}).get("firmware")
        vendor, model = cls._dmi("sys_vendor"), cls._dmi("product_name")
        vendor_key = next((k for k in SETUP_KEYS if vendor.lower().startswith(k)), "")
        hints = VENDOR_HINTS.get("hp" if vendor_key in ("hp", "hewlett-packard") else vendor_key, {})

        findings = []
        efi = cls._efi_boot_entries() if EFI_DIR.exists() else {}
        entries = efi.get("entries", {})
        network = [name for name, active in entries.values()
                   if active and re.search(r"\b(ipv4|ipv6|pxe|network)\b", name, re.I)]
        if network:
            findings.append("Network (PXE) boot is on, so the network card is prepared every start.")
        order = efi.get("order", [])
        if order and efi.get("current") and order[0] != efi["current"]:
            first = entries.get(order[0], (order[0], True))[0]
            findings.append(tr("'{name}' is tried before Fedora in the boot order.").format(name=first))
        if efi.get("timeout"):
            findings.append(tr("The firmware boot menu waits {seconds} s.").format(seconds=efi["timeout"]))
        if not EFI_DIR.exists():
            findings.append("Boots in Legacy mode, usually slower than UEFI.")

        if firmware_s is None:
            rating = "unknown"
        elif firmware_s <= 4:
            rating = "good"
        elif firmware_s <= 8:
            rating = "ok"
        else:
            rating = "slow"

        steps = []
        for key, title, detail in BIOS_STEPS:
            where = hints.get(key, "")
            if where is None:
                continue  # this vendor has no such option
            steps.append({"title": title, "detail": detail, "where": where})

        return {
            "firmware_s": firmware_s, "rating": rating, "vendor": vendor, "model": model,
            "setup_key": SETUP_KEYS.get(vendor_key, "Del or F2"), "is_efi": EFI_DIR.exists(),
            "findings": findings, "steps": steps,
        }

    @classmethod
    def reboot_to_firmware(cls) -> tuple[bool, str]:
        res = cls._run(["systemctl", "reboot", "--firmware-setup"])
        if res.returncode != 0:
            logger.error(f"Reboot into firmware setup failed: {res.stderr.strip()}")
            return False, "Could not restart into the BIOS. Press the setup key at power-on instead."
        return True, "Restarting into the BIOS..."
