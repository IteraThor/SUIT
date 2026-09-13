import os
import time
import json
import urllib.request
import threading
from pathlib import Path
from typing import Callable, Optional, Dict, List, Any

import psutil

from core.logger import get_logger
from core.autodarts_service import (
    DEFAULT_HOST, DEFAULT_PORT, is_port_open,
    fetch_cams_stats, fetch_engine_config
)

logger = get_logger("usb")


class UsbService:
    @staticmethod
    def get_cpu_info() -> dict:
        """Retrieves CPU model name and core count from /proc/cpuinfo."""
        model = "Unknown CPU"
        cores = os.cpu_count() or 1
        try:
            p = Path("/proc/cpuinfo")
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    if "model name" in line:
                        model = line.split(":", 1)[1].strip()
                        break
        except Exception:
            pass
        return {"model": model, "cores": cores}

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

    @classmethod
    def analyze_bandwidth(cls) -> dict:
        """Inspects USB host controllers and provides topology health summary."""
        cameras = cls.get_camera_devices()
        controllers = []
        try:
            usb_base = Path("/sys/bus/usb/devices")
            if usb_base.exists():
                for usb_dev in sorted(usb_base.glob("usb*")):
                    speed_file = usb_dev / "speed"
                    speed = speed_file.read_text(encoding="utf-8").strip() if speed_file.exists() else "Unknown"
                    bus_name = usb_dev.name
                    bus_num_str = bus_name.replace("usb", "")
                    bus_id_fmt = f"Bus {int(bus_num_str):03d}" if bus_num_str.isdigit() else bus_name

                    product_file = usb_dev / "product"
                    product = product_file.read_text(encoding="utf-8").strip() if product_file.exists() else "USB Host Controller"

                    cams_on_bus = [c for c in cameras if c.get("bus_id") == bus_id_fmt or bus_num_str in c.get("bus_id", "")]
                    cam_count = len(cams_on_bus)

                    controllers.append({
                        "bus": bus_id_fmt,
                        "raw_bus": bus_name,
                        "product": product,
                        "speed": speed,
                        "camera_count": cam_count,
                        "cameras": cams_on_bus,
                        "status": "Warning (Contention Risk)" if cam_count >= 3 else ("Moderate" if cam_count == 2 else "Optimal")
                    })
        except Exception:
            logger.exception("Failed inspecting USB host controllers")

        cpu_info = cls.get_cpu_info()
        rec = "Topology scanned. Tap 'Start Live Stream Test' to verify real delivered bandwidth and detect bottlenecks."
        overloaded = [c for c in controllers if c.get("camera_count", 0) >= 3]
        if overloaded:
            buses = ", ".join(c["bus"] for c in overloaded)
            rec = f"All cameras share {buses}. Use the live test to verify if streams experience frame drops."

        return {
            "controllers": controllers,
            "cameras": cameras,
            "cpu_info": cpu_info,
            "recommendation": rec
        }


class AutodartsLiveMonitorService:
    """Monitors live camera FPS and detection resolution directly from running Autodarts engine."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self._is_running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[Callable[[dict], None]] = None

    @property
    def is_running(self) -> bool:
        return self._is_running

    def start_monitoring(self, on_metrics: Callable[[dict], None]):
        if self._is_running:
            return
        self._is_running = True
        self._stop_event.clear()
        self._callback = on_metrics
        self._thread = threading.Thread(target=self._monitor_worker, daemon=True, name="autodarts-live-monitor")
        self._thread.start()

    def stop_monitoring(self):
        if not self._is_running:
            return
        self._stop_event.set()
        self._is_running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _monitor_worker(self):
        cpu_info = UsbService.get_cpu_info()
        cpu_name = cpu_info.get("model", "CPU").split("@")[0].strip()

        while not self._stop_event.is_set():
            try:
                if not is_port_open(self.host, self.port):
                    snapshot = {
                        "mode": "autodarts_live",
                        "online": False,
                        "overall_status": "Stopped",
                        "status_pill": "Engine Offline",
                        "recommendation": "Autodarts service is stopped. Start Autodarts or tap 'Benchmark USB Hardware' to test camera feeds.",
                        "cameras": {},
                        "target_fps": 25.0,
                        "cpu_percent": psutil.cpu_percent(interval=None)
                    }
                    if self._callback:
                        self._callback(snapshot)
                    time.sleep(1.5)
                    continue

                # 1. Query /api/cams/stats & /api/config via centralized Autodarts service
                stats_data = fetch_cams_stats(self.host, self.port)
                cfg_data = fetch_engine_config(self.host, self.port)

                cam_cfg = cfg_data.get("cam", {})
                configured_cams = [c for c in cam_cfg.get("cams", []) if c and c.strip()]
                if not configured_cams:
                    configured_cams = ["/dev/video0", "/dev/video2", "/dev/video4"]

                w = int(stats_data.get("resolution", {}).get("width") or cam_cfg.get("width", 1280))
                h = int(stats_data.get("resolution", {}).get("height") or cam_cfg.get("height", 960))
                target_fps = float(cam_cfg.get("fps", 25.0))
                fps_list = stats_data.get("fps", [])
                cpu_pct = psutil.cpu_percent(interval=None)

                topo_cams = {c["dev_path"]: c for c in UsbService.get_camera_devices()}
                cams_data = {}
                for idx, dev in enumerate(configured_cams):
                    cur_fps = float(fps_list[idx]) if idx < len(fps_list) else 0.0
                    c_info = topo_cams.get(dev, {})
                    cams_data[dev] = {
                        "dev": dev,
                        "delivered_fps": cur_fps,
                        "target_fps": target_fps,
                        "total_frames": 0,
                        "dropped_frames": 0,
                        "hub_name": c_info.get("hub_name", "Root Port"),
                        "devpath": c_info.get("devpath", ""),
                        "transfer_mode": c_info.get("transfer_mode", "Bulk"),
                        "status": "Optimal" if cur_fps >= (target_fps * 0.90) else ("Throttled" if cur_fps > 0 else "Starting")
                    }

                avg_fps = sum(fps_list) / max(1, len(fps_list)) if fps_list else 0.0

                # Diagnose performance
                if w >= 1920 and h >= 1080 and target_fps >= 30 and avg_fps < 28:
                    overall_status = "CPU Limit"
                    status_pill = "CPU Limit"
                    recommendation = "1080p throttled by CPU. Switch to 720p for stable 30 FPS."
                elif avg_fps >= (target_fps * 0.90):
                    overall_status = "Optimal"
                    status_pill = "Optimal"
                    recommendation = "All cameras streaming normally."
                elif avg_fps > 0:
                    overall_status = "Throttled"
                    status_pill = "Throttled"
                    recommendation = f"Cameras delivering {avg_fps:.0f} FPS. Consider lowering resolution."
                else:
                    overall_status = "Starting"
                    status_pill = "Connecting"
                    recommendation = "Connecting to camera feeds..."

                snapshot = {
                    "mode": "autodarts_live",
                    "online": True,
                    "resolution": f"{w}x{h}",
                    "width": w,
                    "height": h,
                    "target_fps": target_fps,
                    "delivered_fps": avg_fps,
                    "overall_status": overall_status,
                    "status_pill": status_pill,
                    "recommendation": recommendation,
                    "cameras": cams_data,
                    "cpu_percent": cpu_pct
                }

                if self._callback:
                    self._callback(snapshot)

            except Exception as e:
                logger.debug("Error in autodarts live monitor: %s", e)

            time.sleep(1.0)
