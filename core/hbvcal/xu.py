"""Realtek RTS58xx extension-unit (XU) mailbox access through the uvcvideo driver.

Uses UVCIOC_CTRL_QUERY on /dev/videoN, so it works while another program streams and needs no root.
Protocol verified on 0bdc:2710 (firmware bcd 31.00) and against the sample firmware:

  * ioctl number: _IOWR('u', 0x21, struct uvc_xu_control_query) = 0xC0107521
  * XU unit 4. Selector 0x0A = 8-byte command (SET) / status (GET). Selector 0x0B = 8-byte data window.
  * command packet: [group, opcode, a0, a1, len_lo, len_hi, a2, a3]
      address/parameter = a0 | a1<<8 | a2<<16 | a3<<24, length = bytes 4..5
      opcode bit 7 = command, bit 6 = read (data flows camera -> host)
  * status packet: [state, max_data_bytes(=8), 0, 0xBB...]
      state 0 = idle, 1 = read data pending, 2 = waiting for write data, >2 = error code of the
      last command (the firmware then STALLs every request with UVC error "wrong state" -> EACCES)
  * command group 0xFF resets the mailbox state to idle (also clears an error state).
  * tuning lock (XDATA 0xC888): while 0, memory reads come back +0x39, code reads +0x21 and sensor
    commands return scrambled buffers. Unlock = I2CWrite (00 8A) with address 0xF7F7 and data 15 08.
    The flag lives in RAM: it survives stream restarts and is lost on power-off / re-enumeration.
  * the mailbox is ONE state machine per camera: concurrent users corrupt each other's transactions
    (firmware states 3/4). Every transaction therefore holds a per-camera flock (all hbvcal processes).
  * sensor registers (OV2710, 16-bit address, needs a running stream):
      read  = 00 CA, address = register, length 2 -> [value, 0]
      write = 00 8A, address = register, data [value, 0]
"""

from __future__ import annotations

import contextlib
import ctypes
import fcntl
import os
import struct
import time
from typing import Optional

UVCIOC_CTRL_QUERY = 0xC0107521

UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81
UVC_GET_MIN = 0x82
UVC_GET_MAX = 0x83
UVC_GET_RES = 0x84
UVC_GET_LEN = 0x85
UVC_GET_INFO = 0x86
UVC_GET_DEF = 0x87

XU_UNIT = 4
CS_CMD = 0x0A
CS_DATA = 0x0B
PKT = 8

STATE_IDLE, STATE_READ, STATE_WRITE = 0, 1, 2

# group, opcode
OP_RESET = (0xFF, 0x00)
OP_MEM_READ = (0x00, 0xC2)
OP_MEM_WRITE = (0x00, 0x82)
OP_CODE_READ = (0x00, 0xC3)
OP_SENSOR_READ = (0x00, 0xCA)   # vendor name I2CRead
OP_SENSOR_WRITE = (0x00, 0x8A)  # vendor name I2CWrite

LOCK_FLAG = 0xC888
UNLOCK_ADDR = 0xF7F7
UNLOCK_DATA = bytes([0x15, 0x08])
LOCKED_MEM_MASK = 0x39
BUILD_STRING_ADDR = 0x1D10


class XUError(OSError):
    pass


class XU:
    """Command/data mailbox of one camera's Realtek extension unit."""

    def __init__(self, device: str, unit: int = XU_UNIT, delay: float = 0.002):
        self.device = device
        self.unit = unit
        self.delay = delay  # pause after a command before touching the data window
        self.fd: Optional[int] = None
        self._lock_fd: Optional[int] = None
        self._lock_depth = 0

    def __enter__(self) -> "XU":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        if self.fd is None:
            self.fd = os.open(self.device, os.O_RDWR)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    @contextlib.contextmanager
    def transaction(self):
        """Exclusive use of this camera's mailbox across processes (re-entrant within one XU object)."""
        if self._lock_fd is None:
            run = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
            key = os.path.basename(os.path.realpath(self.device))  # videoN
            self._lock_fd = os.open(os.path.join(run, f"hbvcal-xu-{key}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        if self._lock_depth == 0:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
        self._lock_depth += 1
        try:
            yield
        finally:
            self._lock_depth -= 1
            if self._lock_depth == 0:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    # --- ioctl layer -------------------------------------------------------------------------------

    def query(self, selector: int, request: int, data: bytes | int) -> bytes:
        """One UVC request on the XU. `data` is the payload (SET) or the length to read (GET)."""
        size = data if isinstance(data, int) else len(data)
        buf = (ctypes.c_uint8 * size)()
        if not isinstance(data, int):
            ctypes.memmove(buf, bytes(data), size)
        req = bytearray(struct.pack("<BBBxH2xQ", self.unit, selector, request, size, ctypes.addressof(buf)))
        try:
            fcntl.ioctl(self.fd, UVCIOC_CTRL_QUERY, req)
        except OSError as e:
            raise XUError(e.errno, f"XU sel=0x{selector:02X} req=0x{request:02X}: {e.strerror}") from None
        return bytes(buf)

    # --- mailbox layer -----------------------------------------------------------------------------

    def status(self) -> bytes:
        return self.query(CS_CMD, UVC_GET_CUR, PKT)

    def state(self) -> int:
        return self.status()[0]

    def reset(self) -> None:
        self.query(CS_CMD, UVC_SET_CUR, bytes([OP_RESET[0], OP_RESET[1]]) + bytes(6))

    def command(self, group: int, opcode: int, addr: int = 0, length: int = 0) -> None:
        """Send one command; resets a stuck mailbox first. Raises XUError with the firmware's code.
        Call inside transaction() (read()/write() do that)."""
        if self.state() != STATE_IDLE:
            self.reset()
        pkt = struct.pack("<BBHHH", group, opcode, addr & 0xFFFF, length & 0xFFFF, (addr >> 16) & 0xFFFF)
        try:
            self.query(CS_CMD, UVC_SET_CUR, pkt)
        except XUError as e:
            code = self.state()
            self.reset()
            raise XUError(e.errno, f"command {group:02X}:{opcode:02X} rejected, firmware state {code}") from None
        time.sleep(self.delay)

    def read(self, group: int, opcode: int, addr: int = 0, length: int = 1) -> bytes:
        with self.transaction():
            return self._read(group, opcode, addr, length)

    def _read(self, group: int, opcode: int, addr: int, length: int) -> bytes:
        self.command(group, opcode, addr, length)
        out = bytearray()
        try:
            while len(out) < length:
                out += self.query(CS_DATA, UVC_GET_CUR, PKT)[:length - len(out)]
        except XUError:
            code = self.state()
            self.reset()
            raise XUError(0, f"read {group:02X}:{opcode:02X} failed, firmware state {code}") from None
        return bytes(out)

    def write(self, group: int, opcode: int, payload: bytes, addr: int = 0) -> None:
        with self.transaction():
            self._write(group, opcode, payload, addr)

    def _write(self, group: int, opcode: int, payload: bytes, addr: int) -> None:
        self.command(group, opcode, addr, len(payload))
        try:
            for i in range(0, len(payload), PKT):
                self.query(CS_DATA, UVC_SET_CUR, payload[i:i + PKT].ljust(PKT, b"\0"))
        except XUError:
            code = self.state()
            self.reset()
            raise XUError(0, f"write {group:02X}:{opcode:02X} failed, firmware state {code}") from None
        st = self.state()
        if st != STATE_IDLE:
            self.reset()
            raise XUError(0, f"write {group:02X}:{opcode:02X} ended in firmware state {st}")

    # --- typed helpers -----------------------------------------------------------------------------

    def mem_read(self, addr: int, length: int = 1) -> bytes:
        """XDATA read. Only meaningful after unlock() (locked reads are masked)."""
        return self.read(*OP_MEM_READ, addr=addr, length=length)

    def mem_write(self, addr: int, data: bytes) -> None:
        self.write(*OP_MEM_WRITE, data, addr=addr)

    def is_unlocked(self) -> bool:
        # locked: the flag (0) reads back as 0x39; unlocked: reads back as 1
        return self.mem_read(LOCK_FLAG, 1)[0] == 0x01

    def unlock(self) -> None:
        """Enable the firmware's tuning access (RAM flag, harmless, idempotent)."""
        if not self.is_unlocked():
            self.write(*OP_SENSOR_WRITE, UNLOCK_DATA, addr=UNLOCK_ADDR)
        if not self.is_unlocked():
            raise XUError(0, "tuning unlock did not take effect")

    def firmware_build(self) -> str:
        """Build date of the running firmware, e.g. '07/28/26' (string 'REALSIL' at code 0x1D10)."""
        raw = self.read(*OP_CODE_READ, addr=BUILD_STRING_ADDR, length=16)
        if not raw.startswith(b"REALSIL"):
            raw = bytes((b - 0x21) & 0xFF for b in raw)  # still locked: code reads are masked
        return raw[8:16].split(b"\0")[0].decode(errors="replace")

    def sensor_read(self, reg: int) -> int:
        """Read one OV2710 register (camera must be streaming)."""
        return self.read(*OP_SENSOR_READ, addr=reg, length=2)[0]

    def sensor_write(self, reg: int, value: int) -> None:
        """Write one OV2710 register (camera must be streaming)."""
        self.write(*OP_SENSOR_WRITE, bytes([value & 0xFF, 0]), addr=reg)
