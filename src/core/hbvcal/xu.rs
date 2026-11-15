//! Realtek RTS58xx extension-unit (XU) mailbox access through the uvcvideo driver.
//!
//! Uses UVCIOC_CTRL_QUERY on /dev/videoN, so it works while another program streams and needs no root.
//! Protocol verified on 0bdc:2710 (firmware bcd 31.00) and against the sample firmware:
//!
//!   * ioctl number: _IOWR('u', 0x21, struct uvc_xu_control_query) = 0xC0107521
//!   * XU unit 4. Selector 0x0A = 8-byte command (SET) / status (GET). Selector 0x0B = 8-byte data window.
//!   * command packet: [group, opcode, a0, a1, len_lo, len_hi, a2, a3]
//!   * status packet: [state, max_data_bytes(=8), 0, 0xBB...]
//!       state 0 = idle, 1 = read data pending, 2 = waiting for write data, >2 = error code of the last command
//!   * command group 0xFF resets the mailbox state to idle (also clears an error state).
//!   * tuning lock: while the flag is 0, memory and code reads come back masked. Unlock = I2CWrite (00 8A)
//!     with address 0xF7F7 and data 15 08 (all known builds). The flag lives in RAM.
//!   * the mailbox is ONE state machine per camera: every transaction holds a per-camera flock.
//!   * sensor registers (OV2710, 16-bit address, needs a running stream):
//!       read  = 00 CA, address = register, length 2 -> [value, 0]
//!       write = 00 8A, address = register, data [value, 0]
use std::cell::Cell;
use std::ffi::CString;
use std::io;

pub const UVCIOC_CTRL_QUERY: u64 = 0xC0107521;

pub const UVC_SET_CUR: u8 = 0x01;
pub const UVC_GET_CUR: u8 = 0x81;

pub const XU_UNIT: u8 = 4;
const CS_CMD: u8 = 0x0A;
const CS_DATA: u8 = 0x0B;
const PKT: usize = 8;

pub const STATE_IDLE: u8 = 0;

pub const OP_RESET: (u8, u8) = (0xFF, 0x00);
pub const OP_MEM_READ: (u8, u8) = (0x00, 0xC2);
pub const OP_MEM_WRITE: (u8, u8) = (0x00, 0x82);
pub const OP_CODE_READ: (u8, u8) = (0x00, 0xC3);
pub const OP_SENSOR_READ: (u8, u8) = (0x00, 0xCA);
pub const OP_SENSOR_WRITE: (u8, u8) = (0x00, 0x8A);

const UNLOCK_ADDR: u32 = 0xF7F7;
const UNLOCK_DATA: [u8; 2] = [0x15, 0x08];
/// build -> tuning-lock flag (XDATA)
pub const BUILDS: &[(&str, u32)] = &[("07/11/26", 0xC888), ("07/28/26", 0xC888), ("02/15/25", 0xC879)];
const BUILD_STRING_ADDRS: [u32; 2] = [0x1D10, 0x0FE4];
const LOCKED_CODE_MASKS: [u8; 3] = [0x00, 0x21, 0x0D];

pub fn xu_err(errno: i32, msg: String) -> io::Error {
    if errno != 0 {
        io::Error::new(io::Error::from_raw_os_error(errno).kind(), msg)
    } else {
        io::Error::other(msg)
    }
}

/// Command/data mailbox of one camera's Realtek extension unit.
pub struct Xu {
    pub device: String,
    unit: u8,
    delay: f64,
    fd: i32,
    lock_fd: Cell<i32>,
    lock_depth: Cell<u32>,
}

/// Guard of [`Xu::transaction`].
pub struct TxGuard<'a>(&'a Xu);

impl Drop for TxGuard<'_> {
    fn drop(&mut self) {
        let d = self.0.lock_depth.get() - 1;
        self.0.lock_depth.set(d);
        if d == 0 {
            unsafe { libc::flock(self.0.lock_fd.get(), libc::LOCK_UN) };
        }
    }
}

impl Xu {
    pub fn open(device: &str) -> io::Result<Xu> {
        let c = CString::new(device).unwrap();
        let fd = unsafe { libc::open(c.as_ptr(), libc::O_RDWR) };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(Xu { device: device.into(), unit: XU_UNIT, delay: 0.002, fd, lock_fd: Cell::new(-1), lock_depth: Cell::new(0) })
    }

    /// Exclusive use of this camera's mailbox across processes (re-entrant within one Xu object).
    pub fn transaction(&self) -> io::Result<TxGuard<'_>> {
        if self.lock_fd.get() < 0 {
            let run = std::env::var("XDG_RUNTIME_DIR").unwrap_or_else(|_| "/tmp".into());
            let key = std::fs::canonicalize(&self.device)
                .ok()
                .and_then(|p| p.file_name().map(|n| n.to_string_lossy().to_string()))
                .unwrap_or_else(|| "video".into());
            let p = CString::new(format!("{}/hbvcal-xu-{}.lock", run, key)).unwrap();
            let fd = unsafe { libc::open(p.as_ptr(), libc::O_RDWR | libc::O_CREAT, 0o600) };
            if fd < 0 {
                return Err(io::Error::last_os_error());
            }
            self.lock_fd.set(fd);
        }
        if self.lock_depth.get() == 0 {
            unsafe { libc::flock(self.lock_fd.get(), libc::LOCK_EX) };
        }
        self.lock_depth.set(self.lock_depth.get() + 1);
        Ok(TxGuard(self))
    }

    /// One UVC request on the XU. `data` is the payload (SET); for GET pass a zeroed buffer of the length.
    pub fn query(&self, selector: u8, request: u8, data: &[u8]) -> io::Result<Vec<u8>> {
        let mut buf = data.to_vec();
        // struct uvc_xu_control_query { u8 unit; u8 selector; u8 query; u16 size; u8 *data; } (16 bytes)
        let mut req = [0u8; 16];
        req[0] = self.unit;
        req[1] = selector;
        req[2] = request;
        req[4..6].copy_from_slice(&(buf.len() as u16).to_le_bytes());
        req[8..16].copy_from_slice(&(buf.as_mut_ptr() as u64).to_le_bytes());
        super::v4l2::ioctl(self.fd, UVCIOC_CTRL_QUERY, req.as_mut_ptr()).map_err(|e| {
            xu_err(e.raw_os_error().unwrap_or(0), format!("XU sel=0x{:02X} req=0x{:02X}: {}", selector, request, e))
        })?;
        Ok(buf)
    }

    pub fn status(&self) -> io::Result<Vec<u8>> {
        self.query(CS_CMD, UVC_GET_CUR, &[0u8; PKT])
    }

    pub fn state(&self) -> io::Result<u8> {
        Ok(self.status()?[0])
    }

    pub fn reset(&self) -> io::Result<()> {
        let mut p = [0u8; PKT];
        p[0] = OP_RESET.0;
        p[1] = OP_RESET.1;
        self.query(CS_CMD, UVC_SET_CUR, &p).map(|_| ())
    }

    /// Send one command; resets a stuck mailbox first.
    pub fn command(&self, group: u8, opcode: u8, addr: u32, length: u16) -> io::Result<()> {
        if self.state()? != STATE_IDLE {
            self.reset()?;
        }
        let mut p = [0u8; PKT];
        p[0] = group;
        p[1] = opcode;
        p[2..4].copy_from_slice(&((addr & 0xFFFF) as u16).to_le_bytes());
        p[4..6].copy_from_slice(&length.to_le_bytes());
        p[6..8].copy_from_slice(&(((addr >> 16) & 0xFFFF) as u16).to_le_bytes());
        if let Err(e) = self.query(CS_CMD, UVC_SET_CUR, &p) {
            let code = self.state().unwrap_or(255);
            let _ = self.reset();
            return Err(xu_err(
                e.raw_os_error().unwrap_or(0),
                format!("command {:02X}:{:02X} rejected, firmware state {}", group, opcode, code),
            ));
        }
        std::thread::sleep(std::time::Duration::from_secs_f64(self.delay));
        Ok(())
    }

    pub fn read(&self, op: (u8, u8), addr: u32, length: usize) -> io::Result<Vec<u8>> {
        let _g = self.transaction()?;
        self.command(op.0, op.1, addr, length as u16)?;
        let mut out = Vec::with_capacity(length);
        while out.len() < length {
            match self.query(CS_DATA, UVC_GET_CUR, &[0u8; PKT]) {
                Ok(chunk) => {
                    let take = (length - out.len()).min(chunk.len());
                    out.extend_from_slice(&chunk[..take]);
                }
                Err(_) => {
                    let code = self.state().unwrap_or(255);
                    let _ = self.reset();
                    return Err(xu_err(0, format!("read {:02X}:{:02X} failed, firmware state {}", op.0, op.1, code)));
                }
            }
        }
        Ok(out)
    }

    pub fn write(&self, op: (u8, u8), payload: &[u8], addr: u32) -> io::Result<()> {
        let _g = self.transaction()?;
        self.command(op.0, op.1, addr, payload.len() as u16)?;
        for chunk in payload.chunks(PKT) {
            let mut p = [0u8; PKT];
            p[..chunk.len()].copy_from_slice(chunk);
            if self.query(CS_DATA, UVC_SET_CUR, &p).is_err() {
                let code = self.state().unwrap_or(255);
                let _ = self.reset();
                return Err(xu_err(0, format!("write {:02X}:{:02X} failed, firmware state {}", op.0, op.1, code)));
            }
        }
        let st = self.state()?;
        if st != STATE_IDLE {
            let _ = self.reset();
            return Err(xu_err(0, format!("write {:02X}:{:02X} ended in firmware state {}", op.0, op.1, st)));
        }
        Ok(())
    }

    /// XDATA read. Only meaningful after unlock() (locked reads are masked).
    pub fn mem_read(&self, addr: u32, length: usize) -> io::Result<Vec<u8>> {
        self.read(OP_MEM_READ, addr, length)
    }

    pub fn mem_write(&self, addr: u32, data: &[u8]) -> io::Result<()> {
        self.write(OP_MEM_WRITE, data, addr)
    }

    pub fn is_unlocked(&self) -> io::Result<bool> {
        Ok(self.mem_read(self.lock_flag()?, 1)?[0] == 0x01)
    }

    pub fn lock_flag(&self) -> io::Result<u32> {
        let build = self.firmware_build()?;
        BUILDS
            .iter()
            .find(|(b, _)| *b == build)
            .map(|(_, f)| *f)
            .ok_or_else(|| xu_err(0, format!("unknown firmware build {:?}: tuning lock not mapped, not touching it", build)))
    }

    /// Enable the firmware's tuning access (RAM flag, harmless, idempotent). Known builds only.
    pub fn unlock(&self) -> io::Result<()> {
        if !self.is_unlocked()? {
            self.write(OP_SENSOR_WRITE, &UNLOCK_DATA, UNLOCK_ADDR)?;
        }
        if !self.is_unlocked()? {
            return Err(xu_err(0, "tuning unlock did not take effect".into()));
        }
        Ok(())
    }

    /// Build date of the running firmware, e.g. '07/28/26' ('REALSIL\0<date>' in code).
    pub fn firmware_build(&self) -> io::Result<String> {
        for addr in BUILD_STRING_ADDRS {
            let raw = self.read(OP_CODE_READ, addr, 16)?;
            for mask in LOCKED_CODE_MASKS {
                let plain: Vec<u8> = raw.iter().map(|b| b.wrapping_sub(mask)).collect();
                if plain.starts_with(b"REALSIL\0") {
                    let s = &plain[8..16];
                    let end = s.iter().position(|&c| c == 0).unwrap_or(s.len());
                    return Ok(String::from_utf8_lossy(&s[..end]).to_string());
                }
            }
        }
        Ok("unknown".into())
    }

    /// Read one OV2710 register (camera must be streaming).
    pub fn sensor_read(&self, reg: u32) -> io::Result<u8> {
        Ok(self.read(OP_SENSOR_READ, reg, 2)?[0])
    }

    /// Write one OV2710 register (camera must be streaming).
    pub fn sensor_write(&self, reg: u32, value: u8) -> io::Result<()> {
        self.write(OP_SENSOR_WRITE, &[value, 0], reg)
    }
}

impl Drop for Xu {
    fn drop(&mut self) {
        unsafe {
            libc::close(self.fd);
            if self.lock_fd.get() >= 0 {
                libc::close(self.lock_fd.get());
            }
        }
    }
}
