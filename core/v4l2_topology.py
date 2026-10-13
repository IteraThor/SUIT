"""USB topology discovery and persistent camera identification.

Identifies UVC cameras via `/dev/v4l/by-path/` to ensure persistent mapping across system reboots.
"""

from __future__ import annotations
import dataclasses
import glob
import logging
import os
from pathlib import Path
import re
import subprocess
from typing import Dict, List, Optional

logger = logging.getLogger("cam_librator.topology")


@dataclasses.dataclass
class CameraDevice:
    index: int  # 1, 2, 3
    name: str
    by_path: str
    current_dev_node: str
    is_available: bool = True
    sensor_model: str = "OmniVision OV2710"

    @property
    def dev_node(self) -> str:
        return self.current_dev_node


class V4L2Topology:
    """Discovers and resolves persistent camera hardware mappings."""

    def __init__(self):
        self._devices: List[CameraDevice] = []

    def get_autodarts_cameras(self) -> List[CameraDevice]:
        """Convenience alias for scan_cameras."""
        return self.scan_cameras()

    @staticmethod
    def is_capture_device(dev_path: str) -> bool:
        """Check if a V4L2 device node supports Video Capture (excluding metadata nodes)."""
        # 1. Direct V4L2 ioctl query (fast, robust, zero external package dependency)
        try:
            import fcntl
            import struct
            vidioc_querycap = 0x80685600
            fd = os.open(dev_path, os.O_RDONLY | os.O_NONBLOCK)
            try:
                buf = bytearray(104)
                fcntl.ioctl(fd, vidioc_querycap, buf)
                _, _, _, _, caps, device_caps, _, _, _ = struct.unpack("16s32s32sIIIIII", buf)
                effective_caps = device_caps if (caps & 0x80000000) else caps
                return bool(effective_caps & 0x00000001)
            finally:
                os.close(fd)
        except Exception:
            pass

        # 2. CLI fallback via v4l2-ctl if available
        try:
            cmd = ["v4l2-ctl", "-d", dev_path, "--all"]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode == 0:
                return "Video Capture" in res.stdout
        except Exception:
            pass

        return False

    def get_card_name(self, dev_path: str, default: str = "") -> str:
        """Get human-readable card or model name for a V4L2 device."""
        # 1. Sysfs device name
        try:
            real_node = os.path.realpath(dev_path)
            node_name = os.path.basename(real_node)
            sys_path = Path(f"/sys/class/video4linux/{node_name}/name")
            if sys_path.exists():
                name = sys_path.read_text(encoding="utf-8").strip()
                if name:
                    return name
        except Exception:
            pass

        # 2. V4L2 ioctl card name
        try:
            import fcntl
            import struct
            vidioc_querycap = 0x80685600
            fd = os.open(dev_path, os.O_RDONLY | os.O_NONBLOCK)
            try:
                buf = bytearray(104)
                fcntl.ioctl(fd, vidioc_querycap, buf)
                _, card, _, _, _, _, _, _, _ = struct.unpack("16s32s32sIIIIII", buf)
                name = card.decode("utf-8", errors="replace").rstrip("\x00").strip()
                if name:
                    return name
            finally:
                os.close(fd)
        except Exception:
            pass

        # 3. v4l2-ctl fallback
        try:
            res = subprocess.run(
                ["v4l2-ctl", "-d", dev_path, "--info"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for line in res.stdout.splitlines():
                if "Card type" in line:
                    return line.split(":", 1)[1].strip()
        except Exception:
            pass

        return default

    def scan_cameras(self) -> List[CameraDevice]:
        """Scan system for all available V4L2 video capture devices without depending on Autodarts."""
        discovered: List[CameraDevice] = []
        seen_nodes = set()
        cam_idx = 1

        # 1. Scan persistent by-path links first (prefer index0 capture nodes)
        by_path_candidates = sorted(glob.glob("/dev/v4l/by-path/*-video-index0"))
        if not by_path_candidates:
            by_path_candidates = sorted(glob.glob("/dev/v4l/by-path/*"))

        for link in by_path_candidates:
            if not os.path.exists(link):
                continue
            real_node = os.path.realpath(link)
            if real_node in seen_nodes:
                continue

            if self.is_capture_device(link):
                seen_nodes.add(real_node)
                card_name = self.get_card_name(link, default=f"Camera {cam_idx}")
                discovered.append(
                    CameraDevice(
                        index=cam_idx,
                        name=f"Camera {cam_idx} ({card_name})",
                        by_path=link,
                        current_dev_node=real_node,
                        is_available=True,
                    )
                )
                cam_idx += 1

        # 2. Scan remaining /dev/video* devices to ensure every connected camera is included
        def _video_sort_key(node_path: str) -> int:
            m = re.search(r"video(\d+)", node_path)
            return int(m.group(1)) if m else 999

        for node in sorted(glob.glob("/dev/video*"), key=_video_sort_key):
            if not os.path.exists(node):
                continue
            real_node = os.path.realpath(node)
            if real_node in seen_nodes:
                continue

            if self.is_capture_device(node):
                seen_nodes.add(real_node)
                card_name = self.get_card_name(node, default=f"Camera {cam_idx}")
                discovered.append(
                    CameraDevice(
                        index=cam_idx,
                        name=f"Camera {cam_idx} ({card_name})",
                        by_path=node,
                        current_dev_node=real_node,
                        is_available=True,
                    )
                )
                cam_idx += 1

        self._devices = discovered
        return discovered

    def get_camera_by_index(self, index: int) -> Optional[CameraDevice]:
        for cam in self._devices:
            if cam.index == index:
                return cam
        return None
