import subprocess
import shutil
import json
import re
from core.logger import get_logger

logger = get_logger("tailscale")


class TailscaleService:
    @staticmethod
    def install_tailscale() -> tuple[bool, str]:
        try:
            subprocess.run("curl -fsSL https://tailscale.com/install.sh | sudo -n sh", shell=True, check=True, capture_output=True)
            subprocess.run(["sudo", "-n", "systemctl", "enable", "--now", "tailscaled"], check=False)
            return True, "Tailscale installed and service started."
        except Exception as e:
            logger.exception("Failed installing Tailscale")
            return False, f"Failed installing Tailscale: {e}"

    @classmethod
    def get_tailscale_status(cls) -> dict:
        if not shutil.which("tailscale"):
            return {
                "installed": False,
                "running": False,
                "state": "NotInstalled",
                "auth_url": None,
                "ips": [],
                "hostname": "",
                "dns_name": ""
            }

        state = "Unknown"
        auth_url = None
        ips = []
        hostname = ""
        dns_name = ""

        try:
            res = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=3
            )
            if res.returncode == 0:
                data = json.loads(res.stdout)
                backend_state = data.get("BackendState", "")
                auth_url = data.get("AuthURL")
                self_node = data.get("Self", {})
                ips = self_node.get("TailscaleIPs") or []
                hostname = self_node.get("HostName", "")
                dns_name = (self_node.get("DNSName") or "").rstrip(".")

                if backend_state == "Running":
                    state = "Running"
                elif backend_state == "NeedsLogin":
                    state = "NeedsLogin"
                elif backend_state == "Stopped":
                    state = "Stopped"
                else:
                    state = backend_state or "Unknown"
            else:
                state = "Stopped"
        except Exception:
            logger.debug("Failed querying tailscale status --json")

        return {
            "installed": True,
            "running": state == "Running",
            "state": state,
            "auth_url": auth_url,
            "ips": ips,
            "hostname": hostname,
            "dns_name": dns_name
        }

    @classmethod
    def get_tailscale_auth_url(cls) -> str | None:
        try:
            res = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=2)
            if res.returncode == 0:
                data = json.loads(res.stdout)
                if data.get("AuthURL"):
                    return data["AuthURL"]
        except Exception:
            pass

        try:
            res = subprocess.run(
                ["sudo", "-n", "tailscale", "up", "--reset", "--timeout=15s"],
                capture_output=True,
                text=True,
                timeout=18
            )
            combined = (res.stdout or "") + "\n" + (res.stderr or "")
            m = re.search(r"https://login\.tailscale\.com/a/\S+", combined)
            if m:
                return m.group(0)
        except Exception:
            pass

        try:
            res = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=2)
            if res.returncode == 0:
                data = json.loads(res.stdout)
                if data.get("AuthURL"):
                    return data["AuthURL"]
        except Exception:
            pass

        return None

    @staticmethod
    def disconnect_tailscale() -> tuple[bool, str]:
        try:
            subprocess.run(["sudo", "-n", "tailscale", "down"], check=True, capture_output=True)
            return True, "Tailscale disconnected."
        except Exception as e:
            logger.exception("Failed disconnecting Tailscale")
            return False, f"Failed disconnecting Tailscale: {e}"

    @classmethod
    def connect_tailscale(cls) -> tuple[bool, str]:
        try:
            subprocess.run(["sudo", "-n", "tailscale", "up"], check=True, capture_output=True, timeout=5)
            return True, "Tailscale connected."
        except Exception as e:
            logger.exception("Failed connecting Tailscale")
            return False, f"Failed connecting Tailscale: {e}"

    @classmethod
    def launch_tailscale_up(cls) -> bool:
        url = cls.get_tailscale_auth_url()
        return url is not None
