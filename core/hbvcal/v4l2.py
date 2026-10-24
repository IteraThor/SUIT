"""Minimal V4L2 capture and control access (x86-64 Linux, ctypes/ioctl, no extra dependencies).

Gives raw MJPEG/YUYV buffers together with the driver's sequence number (gaps = dropped frames) and
timestamp, which OpenCV's VideoCapture hides.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import mmap
import os
import select
import struct
import time
from dataclasses import dataclass
from typing import Iterator, List, Optional

V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_MEMORY_MMAP = 1
V4L2_FIELD_NONE = 1

VIDIOC_S_FMT = 0xC0D05605
VIDIOC_G_FMT = 0xC0D05604
VIDIOC_REQBUFS = 0xC0145608
VIDIOC_QUERYBUF = 0xC0585609
VIDIOC_QBUF = 0xC058560F
VIDIOC_DQBUF = 0xC0585611
VIDIOC_STREAMON = 0x40045612
VIDIOC_STREAMOFF = 0x40045613
VIDIOC_S_PARM = 0xC0CC5616
VIDIOC_G_CTRL = 0xC008561B
VIDIOC_S_CTRL = 0xC008561C
VIDIOC_QUERYCTRL = 0xC0445624

BUF_SIZE = 88  # struct v4l2_buffer on 64-bit


def fourcc(s: str) -> int:
    return struct.unpack("<I", s.encode())[0]


# Control IDs (linux/v4l2-controls.h)
CID = {
    "brightness": 0x00980900,
    "contrast": 0x00980901,
    "saturation": 0x00980902,
    "hue": 0x00980903,
    "white_balance_automatic": 0x0098090C,
    "gamma": 0x00980910,
    "gain": 0x00980913,
    "power_line_frequency": 0x00980918,
    "white_balance_temperature": 0x0098091A,
    "sharpness": 0x0098091B,
    "backlight_compensation": 0x0098091C,
    "auto_exposure": 0x009A0901,
    "exposure_time_absolute": 0x009A0902,
    "exposure_dynamic_framerate": 0x009A0903,
}


@dataclass
class Frame:
    data: bytes
    sequence: int
    timestamp: float  # driver timestamp, seconds (CLOCK_MONOTONIC)
    t_host: float  # time.monotonic() at dequeue


class Camera:
    """One V4L2 capture device. Use as a context manager; stream with start()/frames()/stop()."""

    def __init__(self, device: str):
        self.device = device
        self.fd: Optional[int] = None
        self.bufs: List[mmap.mmap] = []
        self.streaming = False
        self.width = self.height = 0
        self.pixfmt = ""

    def __enter__(self) -> "Camera":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        if self.fd is None:
            self.fd = os.open(self.device, os.O_RDWR | os.O_NONBLOCK)

    def close(self) -> None:
        if self.streaming:
            self.stop()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    # --- controls ----------------------------------------------------------------------------------

    def get(self, name: str) -> int:
        buf = bytearray(struct.pack("<Ii", CID[name], 0))
        fcntl.ioctl(self.fd, VIDIOC_G_CTRL, buf)
        return struct.unpack("<Ii", buf)[1]

    def set(self, name: str, value: int) -> None:
        buf = bytearray(struct.pack("<Ii", CID[name], int(value)))
        fcntl.ioctl(self.fd, VIDIOC_S_CTRL, buf)

    def query(self, name: str) -> dict:
        """min/max/step/default of a control."""
        buf = bytearray(0x44)
        struct.pack_into("<I", buf, 0, CID[name])
        fcntl.ioctl(self.fd, VIDIOC_QUERYCTRL, buf)
        _id, _type = struct.unpack_from("<II", buf, 0)
        mn, mx, step, default, flags = struct.unpack_from("<iiiiI", buf, 40)
        return {"min": mn, "max": mx, "step": step, "default": default, "flags": flags}

    # --- streaming ---------------------------------------------------------------------------------

    def configure(self, width: int, height: int, pixfmt: str = "MJPG", fps: int = 30) -> None:
        fmt = bytearray(208)
        struct.pack_into("<I", fmt, 0, V4L2_BUF_TYPE_VIDEO_CAPTURE)
        struct.pack_into("<IIII", fmt, 8, width, height, fourcc(pixfmt), V4L2_FIELD_NONE)
        fcntl.ioctl(self.fd, VIDIOC_S_FMT, fmt)
        self.width, self.height = struct.unpack_from("<II", fmt, 8)
        self.pixfmt = struct.pack("<I", struct.unpack_from("<I", fmt, 16)[0]).decode()
        parm = bytearray(204)
        struct.pack_into("<I", parm, 0, V4L2_BUF_TYPE_VIDEO_CAPTURE)
        struct.pack_into("<II", parm, 12, 1, fps)  # timeperframe = 1/fps
        fcntl.ioctl(self.fd, VIDIOC_S_PARM, parm)
        num, den = struct.unpack_from("<II", parm, 12)
        self.fps = den / num if num else 0.0

    def start(self, nbufs: int = 4) -> None:
        req = bytearray(struct.pack("<III8x", nbufs, V4L2_BUF_TYPE_VIDEO_CAPTURE, V4L2_MEMORY_MMAP))
        fcntl.ioctl(self.fd, VIDIOC_REQBUFS, req)
        n = struct.unpack_from("<I", req, 0)[0]
        self.bufs = []
        for i in range(n):
            b = self._buf(i)
            fcntl.ioctl(self.fd, VIDIOC_QUERYBUF, b)
            offset = struct.unpack_from("<I", b, 64)[0]
            length = struct.unpack_from("<I", b, 72)[0]
            self.bufs.append(mmap.mmap(self.fd, length, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=offset))
            fcntl.ioctl(self.fd, VIDIOC_QBUF, b)
        fcntl.ioctl(self.fd, VIDIOC_STREAMON, struct.pack("<I", V4L2_BUF_TYPE_VIDEO_CAPTURE))
        self.streaming = True

    def stop(self) -> None:
        try:
            fcntl.ioctl(self.fd, VIDIOC_STREAMOFF, struct.pack("<I", V4L2_BUF_TYPE_VIDEO_CAPTURE))
        finally:
            self.streaming = False
            for m in self.bufs:
                m.close()
            self.bufs = []
            req = bytearray(struct.pack("<III8x", 0, V4L2_BUF_TYPE_VIDEO_CAPTURE, V4L2_MEMORY_MMAP))
            try:
                fcntl.ioctl(self.fd, VIDIOC_REQBUFS, req)
            except OSError:
                pass

    @staticmethod
    def _buf(index: int = 0) -> bytearray:
        b = bytearray(BUF_SIZE)
        struct.pack_into("<II", b, 0, index, V4L2_BUF_TYPE_VIDEO_CAPTURE)
        struct.pack_into("<I", b, 60, V4L2_MEMORY_MMAP)
        return b

    def read(self, timeout: float = 2.0) -> Frame:
        """Dequeue one frame (copy), re-queue the buffer."""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            raise TimeoutError(f"{self.device}: no frame within {timeout}s")
        b = self._buf()
        fcntl.ioctl(self.fd, VIDIOC_DQBUF, b)
        t_host = time.monotonic()
        idx, _type, used = struct.unpack_from("<III", b, 0)
        sec, usec = struct.unpack_from("<qq", b, 24)
        seq = struct.unpack_from("<I", b, 56)[0]
        data = self.bufs[idx][:used]
        fcntl.ioctl(self.fd, VIDIOC_QBUF, b)
        return Frame(bytes(data), seq, sec + usec * 1e-6, t_host)

    def frames(self, n: int, timeout: float = 2.0) -> Iterator[Frame]:
        for _ in range(n):
            yield self.read(timeout)
