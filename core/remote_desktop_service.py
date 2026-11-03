import subprocess
import re
import socket
from pathlib import Path
from core.logger import get_logger
from core.i18n import tr

logger = get_logger("remote_desktop")

GRD_UNIT = "gnome-remote-desktop.service"
OO7_DAEMON = Path("/usr/libexec/oo7-daemon")
# oo7 (Fedora 45+) has no D-Bus activation file for org.freedesktop.secrets: GNOME Remote Desktop starts at
# the same moment as oo7 and fails ("The name is not activatable") unless it waits for it. oo7-daemon.service
# is Type=dbus, so After= waits until the Secret Service is on the bus.
SECRETS_DROPIN = Path.home() / ".config" / "systemd" / "user" / f"{GRD_UNIT}.d" / "50-suit-secret-service.conf"
SECRETS_DROPIN_TEXT = ("# Written by SUIT: Remote Desktop reads its login from the keyring, so wait for oo7\n"
                       "[Unit]\nWants=oo7-daemon.service\nAfter=oo7-daemon.service\n")


class RemoteDesktopService:
    """Service to configure GNOME Remote Desktop (RDP sharing and remote control)."""

    @staticmethod
    def _systemctl(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)

    @classmethod
    def needs_secret_service_order(cls) -> bool:
        """True when Remote Desktop is switched on under oo7 but does not wait for it yet."""
        if not OO7_DAEMON.exists():
            return False
        if cls._systemctl("is-enabled", GRD_UNIT).stdout.strip() != "enabled":
            return False
        try:
            return SECRETS_DROPIN.read_text(encoding="utf-8") != SECRETS_DROPIN_TEXT
        except OSError:
            return True

    @classmethod
    def ensure_secret_service_order(cls) -> tuple[bool, str]:
        """Let Remote Desktop start after oo7, then (re)start it if it is switched on."""
        if not OO7_DAEMON.exists():
            return True, ""
        try:
            SECRETS_DROPIN.parent.mkdir(parents=True, exist_ok=True)
            if not SECRETS_DROPIN.is_file() or SECRETS_DROPIN.read_text(encoding="utf-8") != SECRETS_DROPIN_TEXT:
                SECRETS_DROPIN.write_text(SECRETS_DROPIN_TEXT, encoding="utf-8")
                logger.info("Remote Desktop now waits for the oo7 Secret Service (%s)", SECRETS_DROPIN)
            cls._systemctl("daemon-reload")
        except OSError as e:
            return False, tr("Could not write {path}: {error}").format(path=SECRETS_DROPIN, error=e)
        if cls._systemctl("is-enabled", GRD_UNIT).stdout.strip() != "enabled":
            return True, tr("Remote Desktop waits for the keyring service from now on.")
        if cls._systemctl("is-active", GRD_UNIT).stdout.strip() == "active":
            # Running fine: the order counts from the next boot. A restart would drop the RDP session
            # the user may be running this repair through.
            return True, tr("Remote Desktop waits for the keyring service from now on.")
        cls._systemctl("reset-failed", GRD_UNIT)
        cls._systemctl("restart", GRD_UNIT)
        state = cls._systemctl("is-active", GRD_UNIT).stdout.strip()
        if state != "active":
            return False, tr("Remote Desktop does not start ({state}). Details are in its journal.").format(state=state)
        return True, tr("Remote Desktop is running again.")

    @staticmethod
    def get_local_ip() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            try:
                return socket.gethostname()
            except Exception:
                return "127.0.0.1"

    @classmethod
    def get_remote_desktop_status(cls) -> dict:
        try:
            res = subprocess.run(["grdctl", "status", "--show-credentials"], capture_output=True, text=True, check=False)
            out = res.stdout or ""

            # Isolate RDP block to prevent matching VNC section's View-only setting
            rdp_block = out
            if "RDP:" in out:
                rdp_block = out.split("RDP:", 1)[1]
                if "VNC:" in rdp_block:
                    rdp_block = rdp_block.split("VNC:", 1)[0]

            enabled = bool(re.search(r"Status:\s*enabled", rdp_block, re.I))
            view_only = bool(re.search(r"View-only:\s*yes", rdp_block, re.I))

            # Cross-reference with gsettings authoritative source
            try:
                gset_vo = subprocess.run(
                    ["gsettings", "get", "org.gnome.desktop.remote-desktop.rdp", "view-only"],
                    capture_output=True,
                    text=True,
                    check=False
                )
                if "false" in gset_vo.stdout:
                    view_only = False
                elif "true" in gset_vo.stdout:
                    view_only = True
            except Exception:
                pass

            user_m = re.search(r"Username:\s*(.+)", rdp_block)
            pass_m = re.search(r"Password:\s*(.+)", rdp_block)
            port_m = re.search(r"Port:\s*(\d+)", rdp_block)

            username = user_m.group(1).strip() if user_m else ""
            password = pass_m.group(1).strip() if pass_m else ""
            port = port_m.group(1).strip() if port_m else "3389"

            if username.lower() in ("(null)", "null"):
                username = ""
            if password.lower() in ("(null)", "null"):
                password = ""

            return {
                "enabled": enabled,
                "remote_control": not view_only,
                "username": username,
                "password": password,
                "port": port,
                "hostname": socket.gethostname(),
                "ip": cls.get_local_ip(),
            }
        except Exception as e:
            logger.warning("Failed to query grdctl status: %s", e)
            return {
                "enabled": False,
                "remote_control": False,
                "username": "",
                "password": "",
                "port": "3389",
                "hostname": socket.gethostname(),
                "ip": cls.get_local_ip(),
            }

    @classmethod
    def set_desktop_sharing(cls, enabled: bool) -> tuple[bool, str]:
        try:
            if enabled:
                subprocess.run(["grdctl", "rdp", "enable"], check=False)
                # Ensure credentials exist so connections don't fail immediately
                status = cls.get_remote_desktop_status()
                if not status.get("username") or not status.get("password"):
                    subprocess.run(["grdctl", "rdp", "set-credentials", "autodarts", "autodarts"], check=False)

                subprocess.run(["systemctl", "--user", "enable", "--now", "gnome-remote-desktop"], check=False)
                cls.ensure_secret_service_order()
                logger.info("Desktop sharing enabled successfully")
                return True, "Desktop sharing enabled."
            else:
                subprocess.run(["grdctl", "rdp", "disable"], check=False)
                subprocess.run(["systemctl", "--user", "stop", "gnome-remote-desktop"], check=False)
                logger.info("Desktop sharing disabled successfully")
                return True, "Desktop sharing disabled."
        except Exception as e:
            logger.error("Failed setting desktop sharing to %s: %s", enabled, e)
            return False, tr("Failed updating desktop sharing: {error}").format(error=e)

    @classmethod
    def set_remote_control(cls, enabled: bool) -> tuple[bool, str]:
        try:
            if enabled:
                subprocess.run(["grdctl", "rdp", "disable-view-only"], check=False)
                logger.info("Remote control enabled (view-only disabled)")
                return True, "Remote control enabled."
            else:
                subprocess.run(["grdctl", "rdp", "enable-view-only"], check=False)
                logger.info("Remote control disabled (view-only enabled)")
                return True, "Remote control disabled (view-only)."
        except Exception as e:
            logger.error("Failed setting remote control to %s: %s", enabled, e)
            return False, tr("Failed updating remote control: {error}").format(error=e)

    @classmethod
    def set_remote_credentials(cls, username: str, password: str) -> tuple[bool, str]:
        try:
            subprocess.run(["grdctl", "rdp", "set-credentials", username, password], check=False)
            logger.info("RDP credentials updated for user '%s'", username)
            return True, tr("RDP credentials set for '{username}'.").format(username=username)
        except Exception as e:
            logger.error("Failed setting credentials: %s", e)
            return False, tr("Failed setting credentials: {error}").format(error=e)

    @classmethod
    def open_gnome_sharing_settings(cls) -> bool:
        try:
            subprocess.Popen(["gnome-control-center", "system", "remote-desktop"])
            return True
        except Exception:
            try:
                subprocess.Popen(["gnome-control-center", "sharing"])
                return True
            except Exception as e:
                logger.error("Failed to open gnome-control-center: %s", e)
                return False
