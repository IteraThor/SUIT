import subprocess
import re
import socket
from core.logger import get_logger

logger = get_logger("remote_desktop")


class RemoteDesktopService:
    """Service to configure GNOME Remote Desktop (RDP sharing and remote control)."""

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
                logger.info("Desktop sharing enabled successfully")
                return True, "Desktop sharing enabled."
            else:
                subprocess.run(["grdctl", "rdp", "disable"], check=False)
                subprocess.run(["systemctl", "--user", "stop", "gnome-remote-desktop"], check=False)
                logger.info("Desktop sharing disabled successfully")
                return True, "Desktop sharing disabled."
        except Exception as e:
            logger.error("Failed setting desktop sharing to %s: %s", enabled, e)
            return False, f"Failed updating desktop sharing: {e}"

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
            return False, f"Failed updating remote control: {e}"

    @classmethod
    def set_remote_credentials(cls, username: str, password: str) -> tuple[bool, str]:
        try:
            subprocess.run(["grdctl", "rdp", "set-credentials", username, password], check=False)
            logger.info("RDP credentials updated for user '%s'", username)
            return True, f"RDP credentials set for '{username}'."
        except Exception as e:
            logger.error("Failed setting credentials: %s", e)
            return False, f"Failed setting credentials: {e}"

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
