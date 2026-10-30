from pathlib import Path

from core.logger import get_logger

logger = get_logger("usb")


class UsbService:

    @staticmethod
    def get_camera_devices() -> list[dict]:
        """Scans video4linux devices and resolves physical USB hierarchy, transfer mode, and speed."""
        cams = []
        try:
            v4l = Path("/sys/class/video4linux")
            if v4l.exists():
                for p in sorted(v4l.glob("video*")):
                    name_file = p / "name"
                    index_file = p / "index"
                    idx = index_file.read_text(encoding="utf-8").strip() if index_file.exists() else "0"
                    if idx == "0" and name_file.exists():
                        name = name_file.read_text(encoding="utf-8").strip()
                        dev_path = f"/dev/{p.name}"

                        bus_id = "unknown"
                        devpath = ""
                        speed = "Unknown"
                        hub_name = "Root Controller Port"
                        hub_id = "root"
                        transfer_mode = "Bulk"

                        try:
                            device_link = (p / "device").resolve()
                            dev_dir = device_link
                            while dev_dir and not (dev_dir / "idVendor").exists() and dev_dir != dev_dir.parent:
                                dev_dir = dev_dir.parent

                            if (dev_dir / "idVendor").exists():
                                busnum = (dev_dir / "busnum").read_text(encoding="utf-8").strip() if (dev_dir / "busnum").exists() else ""
                                bus_id = f"Bus {int(busnum):03d}" if busnum.isdigit() else busnum
                                devpath = (dev_dir / "devpath").read_text(encoding="utf-8").strip() if (dev_dir / "devpath").exists() else ""
                                speed = (dev_dir / "speed").read_text(encoding="utf-8").strip() if (dev_dir / "speed").exists() else "Unknown"

                                parent_hub = dev_dir.parent
                                if parent_hub and (parent_hub / "product").exists():
                                    hub_id = parent_hub.name
                                    hub_name = (parent_hub / "product").read_text(encoding="utf-8").strip()
                                elif parent_hub and (parent_hub / "idVendor").exists():
                                    hub_id = parent_hub.name
                                    hub_name = f"Hub {parent_hub.name}"
                                else:
                                    hub_id = "root"
                                    hub_name = "Root Controller Port"

                                # Inspect video streaming interfaces for transfer mode (Bulk vs Isochronous)
                                for iface in dev_dir.glob("*:*.*"):
                                    c = (iface / "bInterfaceClass").read_text(encoding="utf-8").strip().lower() if (iface / "bInterfaceClass").exists() else ""
                                    sc = (iface / "bInterfaceSubClass").read_text(encoding="utf-8").strip().lower() if (iface / "bInterfaceSubClass").exists() else ""
                                    if c in ("0e", "14") and sc in ("02", "2"):
                                        for ep in iface.glob("ep_*"):
                                            t = (ep / "type").read_text(encoding="utf-8").strip() if (ep / "type").exists() else ""
                                            if t:
                                                transfer_mode = t
                                                break
                        except Exception:
                            logger.exception("Error parsing topology for %s", p.name)

                        cams.append({
                            "dev": p.name,
                            "dev_path": dev_path,
                            "name": name,
                            "bus_id": bus_id,
                            "devpath": devpath,
                            "speed": speed,
                            "hub_name": hub_name,
                            "hub_id": hub_id,
                            "transfer_mode": transfer_mode
                        })
        except Exception:
            logger.exception("Failed reading video4linux devices")
        return cams
