"""USB topology discovery and persistent camera identification.

Identifies UVC cameras via `/dev/v4l/by-path/` to ensure persistent mapping across system reboots.
"""

from __future__ import annotations
import dataclasses
import glob
import logging
import os
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
        try:
            cmd = ["v4l2-ctl", "-d", dev_path, "--all"]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode != 0:
                return False
            # Check for Capabilities: ... Video Capture
            return "Video Capture" in res.stdout
        except Exception:
            return False

    def scan_cameras(self) -> List[CameraDevice]:
        """Scan system for all persistent V4L2 camera devices using `/dev/v4l/by-path/`."""
        discovered: List[CameraDevice] = []
        by_path_links = sorted(glob.glob("/dev/v4l/by-path/*-video-index0"))

        # Fallback if no index0 suffix is used
        if not by_path_links:
            by_path_links = sorted(glob.glob("/dev/v4l/by-path/*"))

        cam_idx = 1
        seen_nodes = set()

        for link in by_path_links:
            if not os.path.exists(link):
                continue
            real_node = os.path.realpath(link)
            if real_node in seen_nodes:
                continue

            if self.is_capture_device(link):
                seen_nodes.add(real_node)
                # Try getting camera card name
                card_name = f"Camera {cam_idx}"
                try:
                    res = subprocess.run(
                        ["v4l2-ctl", "-d", link, "--info"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    for line in res.stdout.splitlines():
                        if "Card type" in line:
                            card_name = line.split(":", 1)[1].strip()
                            break
                except Exception:
                    pass

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

        # Fallback: if no /dev/v4l/by-path exists (e.g. simulated environment or standard dev nodes)
        if not discovered:
            for node in sorted(glob.glob("/dev/video*")):
                if node in seen_nodes:
                    continue
                if self.is_capture_device(node):
                    seen_nodes.add(node)
                    discovered.append(
                        CameraDevice(
                            index=cam_idx,
                            name=f"Camera {cam_idx} ({node})",
                            by_path=node,
                            current_dev_node=node,
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
