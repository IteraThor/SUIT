"""V4L2 device controller for UVC cameras (OmniVision OV2710).

Provides direct parameter control via v4l2-ctl and OpenCV MJPEG frame acquisition.
Supports snapshot and instant rollback.
"""

from __future__ import annotations
import dataclasses
import logging
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple
import cv2
import numpy as np

logger = logging.getLogger("cam_librator.v4l2")


@dataclasses.dataclass
class V4L2Control:
    name: str
    ctrl_type: str
    min_val: int
    max_val: int
    step: int
    default_val: int
    current_val: int
    flags: List[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class CameraSettings:
    exposure_auto: int = 1  # 1 = Manual Mode, 3 = Auto Mode
    exposure_absolute: int = 166  # in 100 µs units (e.g. 210 = 21ms)
    gain: int = 0
    wb_auto: int = 1  # 0 = Manual, 1 = Auto
    wb_temperature: int = 5000  # in Kelvin
    sharpness: int = 14
    contrast: int = 34
    brightness: int = 0
    gamma: int = 300  # 100 - 500 (default 300)
    saturation: int = 68  # 0 - 128 (default 68)
    power_line_frequency: int = 1  # 1 = 50Hz, 2 = 60Hz, 0 = Disabled

    def to_dict(self) -> Dict[str, int]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CameraSettings:
        valid_fields = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: int(v) for k, v in data.items() if k in valid_fields}
        return cls(**filtered)


class V4L2Device:
    """Manages V4L2 controls and frame capture for a single camera node."""

    def __init__(self, dev_node: str):
        self.dev_node = dev_node
        self._initial_snapshot: Optional[Dict[str, int]] = None

    def query_controls(self) -> Dict[str, V4L2Control]:
        """Query all available V4L2 controls with their bounds and current values."""
        controls: Dict[str, V4L2Control] = {}
        try:
            cmd = ["v4l2-ctl", "-d", self.dev_node, "-l"]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            controls = self.parse_v4l2_ctl_output(result.stdout)
        except Exception as e:
            logger.warning("Failed to query controls on %s: %s", self.dev_node, e)
        return controls

    @staticmethod
    def parse_v4l2_ctl_output(output: str) -> Dict[str, V4L2Control]:
        """Parse the output of `v4l2-ctl -l`."""
        controls: Dict[str, V4L2Control] = {}
        # Example lines:
        # exposure_time_absolute 0x009a0902 (int)    : min=1 max=10000 step=1 default=166 value=166 flags=inactive
        # auto_exposure 0x009a0901 (menu)   : min=0 max=3 default=3 value=1
        pattern = re.compile(
            r"^\s*([a-zA-Z0-9_]+)\s+0x[0-9a-fA-F]+\s+\(([^)]+)\)\s*:\s*(.*)$"
        )
        for line in output.splitlines():
            match = pattern.match(line)
            if not match:
                continue
            name, c_type, rest = match.groups()
            min_m = re.search(r"min=(-?\d+)", rest)
            max_m = re.search(r"max=(-?\d+)", rest)
            step_m = re.search(r"step=(-?\d+)", rest)
            def_m = re.search(r"default=(-?\d+)", rest)
            val_m = re.search(r"value=(-?\d+)", rest)
            flags_m = re.search(r"flags=([a-zA-Z0-9_,]+)", rest)

            min_val = int(min_m.group(1)) if min_m else 0
            max_val = int(max_m.group(1)) if max_m else 255
            step_val = int(step_m.group(1)) if step_m else 1
            def_val = int(def_m.group(1)) if def_m else min_val
            curr_val = int(val_m.group(1)) if val_m else def_val
            flags = flags_m.group(1).split(",") if flags_m else []

            controls[name] = V4L2Control(
                name=name,
                ctrl_type=c_type,
                min_val=min_val,
                max_val=max_val,
                step=step_val,
                default_val=def_val,
                current_val=curr_val,
                flags=flags,
            )
        return controls

    def take_snapshot(self) -> Dict[str, int]:
        """Record the initial state of the camera to allow full rollback."""
        ctrls = self.query_controls()
        self._initial_snapshot = {name: c.current_val for name, c in ctrls.items()}
        return self._initial_snapshot

    def restore_snapshot(self) -> bool:
        """Restore camera controls to the recorded initial snapshot."""
        if not self._initial_snapshot:
            logger.warning("No snapshot available to restore on %s", self.dev_node)
            return False
        return self.set_controls_dict(self._initial_snapshot)

    def get_control(self, name: str) -> Optional[int]:
        """Get the current integer value of a V4L2 control."""
        try:
            cmd = ["v4l2-ctl", "-d", self.dev_node, f"--get-ctrl={name}"]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            match = re.search(r":\s*(-?\d+)", result.stdout)
            if match:
                return int(match.group(1))
        except Exception as e:
            logger.debug("Failed to get control %s on %s: %s", name, self.dev_node, e)
        return None

    def set_control(self, name: str, value: int) -> bool:
        """Set a single V4L2 control on the device."""
        try:
            cmd = ["v4l2-ctl", "-d", self.dev_node, f"--set-ctrl={name}={value}"]
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            return True
        except subprocess.CalledProcessError as e:
            logger.error("Failed to set control %s=%d on %s: %s", name, value, self.dev_node, e.stderr.strip())
            return False
        except Exception as e:
            logger.exception("Unexpected error setting control %s on %s: %s", name, self.dev_node, e)
            return False

    def set_controls_dict(self, ctrls: Dict[str, int]) -> bool:
        """Set multiple controls safely in proper order, respecting auto/manual dependencies."""
        clean_ctrls = dict(ctrls)
        
        # If auto exposure is active (not 1=manual), absolute exposure cannot be set
        if clean_ctrls.get("auto_exposure", 1) != 1 and "exposure_time_absolute" in clean_ctrls:
            clean_ctrls.pop("exposure_time_absolute", None)
            
        # If auto white balance is active (1=auto), manual temperature cannot be set
        if clean_ctrls.get("white_balance_automatic", 0) == 1 and "white_balance_temperature" in clean_ctrls:
            clean_ctrls.pop("white_balance_temperature", None)

        ordered_keys = []
        # If setting to manual, ensure manual mode is set before manual values
        if clean_ctrls.get("auto_exposure") == 1:
            ordered_keys.append("auto_exposure")
        if clean_ctrls.get("white_balance_automatic") == 0 and "white_balance_automatic" in clean_ctrls:
            ordered_keys.append("white_balance_automatic")

        for k in clean_ctrls:
            if k not in ordered_keys and k not in ("auto_exposure", "white_balance_automatic"):
                ordered_keys.append(k)

        # If setting to auto mode, apply auto at the end so preceding manual values don't conflict
        if clean_ctrls.get("auto_exposure", 1) != 1 and "auto_exposure" in clean_ctrls:
            ordered_keys.append("auto_exposure")
        if clean_ctrls.get("white_balance_automatic") == 1:
            ordered_keys.append("white_balance_automatic")

        success = True
        for name in ordered_keys:
            if not self.set_control(name, clean_ctrls[name]):
                success = False
        return success

    def apply_settings(self, settings: CameraSettings) -> bool:
        """Apply a full CameraSettings profile, omitting controls made inactive by auto modes."""
        ctrl_map = {
            "auto_exposure": settings.exposure_auto,
            "gain": settings.gain,
            "white_balance_automatic": settings.wb_auto,
            "sharpness": settings.sharpness,
            "contrast": settings.contrast,
            "brightness": settings.brightness,
            "gamma": settings.gamma,
            "saturation": settings.saturation,
            "power_line_frequency": settings.power_line_frequency,
        }
        if settings.exposure_auto == 1:
            ctrl_map["exposure_time_absolute"] = settings.exposure_absolute
        if settings.wb_auto == 0:
            ctrl_map["white_balance_temperature"] = settings.wb_temperature

        return self.set_controls_dict(ctrl_map)

    def reset_to_defaults(self) -> bool:
        """Reset camera controls to standard factory defaults."""
        return self.set_controls_dict({
            "white_balance_automatic": 1,
            "auto_exposure": 3,
            "sharpness": 40,
            "contrast": 34,
            "gain": 64,
            "saturation": 68,
            "gamma": 300,
            "brightness": 0,
            "power_line_frequency": 1,
        })

    def _get_device_index(self) -> Optional[int]:
        """Extract integer index from device node (e.g., 0 from /dev/video0)."""
        real_path = os.path.realpath(self.dev_node) if os.path.exists(self.dev_node) else self.dev_node
        match = re.search(r"video(\d+)", real_path)
        return int(match.group(1)) if match else None

    def _open_capture(self, width: int = 1280, height: int = 960, retries: int = 3) -> Optional[cv2.VideoCapture]:
        """Open OpenCV VideoCapture with robust backend fallbacks and index resolution."""
        int_idx = self._get_device_index()

        for attempt in range(retries):
            cap = None
            if int_idx is not None:
                cap = cv2.VideoCapture(int_idx, cv2.CAP_V4L2)
                if not cap.isOpened():
                    cap.release()
                    cap = cv2.VideoCapture(int_idx)

            if cap is None or not cap.isOpened():
                if cap is not None:
                    cap.release()
                cap = cv2.VideoCapture(self.dev_node, cv2.CAP_V4L2)
                if not cap.isOpened():
                    cap.release()
                    cap = cv2.VideoCapture(self.dev_node)

            if cap is not None and cap.isOpened():
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc(*"MJPG"))
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                return cap
            
            if attempt < retries - 1:
                time.sleep(0.2)

        logger.error("Could not open video capture for %s after %d attempts", self.dev_node, retries)
        return None

    def open_stream(self, width: int = 1280, height: int = 960) -> Optional[cv2.VideoCapture]:
        """Open persistent capture stream."""
        return self._open_capture(width=width, height=height)

    def read_stream_frame(self, cap: cv2.VideoCapture, flush_frames: int = 2) -> Optional[np.ndarray]:
        """Read a single frame from an active stream with hardware buffer flush."""
        for _ in range(flush_frames):
            cap.grab()
            time.sleep(0.025)

        ret, frame = cap.read()
        if ret and frame is not None and frame.mean() > 1.0:
            return frame

        # Fallback if frame was black/empty
        for _ in range(4):
            time.sleep(0.033)
            ret, frame = cap.read()
            if ret and frame is not None and frame.mean() > 1.0:
                return frame
        return frame if (ret and frame is not None) else None

    def read_stream_burst(self, cap: cv2.VideoCapture, count: int = 4, flush_frames: int = 2) -> List[np.ndarray]:
        """Read a burst of N consecutive frames from an active stream."""
        frames: List[np.ndarray] = []
        for _ in range(flush_frames):
            cap.grab()
            time.sleep(0.025)

        for _ in range(count):
            ret, frame = cap.read()
            if ret and frame is not None:
                frames.append(frame)
            time.sleep(0.033)
        return frames

    def capture_frame(self, width: int = 1280, height: int = 960, warmup_frames: int = 5) -> Optional[np.ndarray]:
        """Capture a single 960p frame via OpenCV using the MJPG stream."""
        cap = self._open_capture(width, height)
        if cap is None:
            return None

        try:
            # Paced warmup to allow USB UVC buffer population
            for _ in range(warmup_frames):
                cap.grab()
                time.sleep(0.025)

            ret, frame = cap.read()
            if ret and frame is not None and frame.mean() > 1.0:
                return frame

            for _ in range(5):
                time.sleep(0.033)
                ret, frame = cap.read()
                if ret and frame is not None and frame.mean() > 1.0:
                    return frame
            return frame if ret else None
        finally:
            cap.release()

    def capture_burst(self, count: int = 4, width: int = 1280, height: int = 960, warmup_frames: int = 5) -> List[np.ndarray]:
        """Capture a burst of N consecutive frames for temporal noise measurement."""
        frames: List[np.ndarray] = []
        cap = self._open_capture(width, height)
        if cap is None:
            return frames

        try:
            # Paced warmup to avoid half-decoded MJPEG frames
            for _ in range(warmup_frames):
                cap.grab()
                time.sleep(0.025)

            for _ in range(count):
                ret, frame = cap.read()
                if ret and frame is not None:
                    frames.append(frame)
                time.sleep(0.033)  # ~30 fps interval
        finally:
            cap.release()
        return frames
