//! Minimal V4L2 capture and control access (x86-64 Linux, raw ioctl).
//!
//! Gives raw MJPEG/YUYV buffers together with the driver's sequence number (gaps = dropped frames) and
//! timestamp, which OpenCV's VideoCapture hides.
use std::ffi::CString;
use std::io;
use std::time::Instant;

pub const V4L2_BUF_TYPE_VIDEO_CAPTURE: u32 = 1;
pub const V4L2_MEMORY_MMAP: u32 = 1;
pub const V4L2_FIELD_NONE: u32 = 1;

pub const VIDIOC_S_FMT: u64 = 0xC0D05605;
pub const VIDIOC_G_FMT: u64 = 0xC0D05604;
pub const VIDIOC_REQBUFS: u64 = 0xC0145608;
pub const VIDIOC_QUERYBUF: u64 = 0xC0585609;
pub const VIDIOC_QBUF: u64 = 0xC058560F;
pub const VIDIOC_DQBUF: u64 = 0xC0585611;
pub const VIDIOC_STREAMON: u64 = 0x40045612;
pub const VIDIOC_STREAMOFF: u64 = 0x40045613;
pub const VIDIOC_S_PARM: u64 = 0xC0CC5616;
pub const VIDIOC_G_CTRL: u64 = 0xC008561B;
pub const VIDIOC_S_CTRL: u64 = 0xC008561C;
pub const VIDIOC_QUERYCTRL: u64 = 0xC0445624;

const BUF_SIZE: usize = 88; // struct v4l2_buffer on 64-bit

pub fn fourcc(s: &str) -> u32 {
    let b = s.as_bytes();
    u32::from_le_bytes([b[0], b[1], b[2], b[3]])
}

/// Control IDs (linux/v4l2-controls.h)
pub const CID: &[(&str, u32)] = &[
    ("brightness", 0x00980900),
    ("contrast", 0x00980901),
    ("saturation", 0x00980902),
    ("hue", 0x00980903),
    ("white_balance_automatic", 0x0098090C),
    ("gamma", 0x00980910),
    ("gain", 0x00980913),
    ("power_line_frequency", 0x00980918),
    ("white_balance_temperature", 0x0098091A),
    ("sharpness", 0x0098091B),
    ("backlight_compensation", 0x0098091C),
    ("auto_exposure", 0x009A0901),
    ("exposure_time_absolute", 0x009A0902),
    ("exposure_dynamic_framerate", 0x009A0903),
];

pub fn cid(name: &str) -> io::Result<u32> {
    CID.iter()
        .find(|(n, _)| *n == name)
        .map(|(_, v)| *v)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, format!("unknown control {}", name)))
}

#[derive(Debug, Clone)]
pub struct Frame {
    pub data: Vec<u8>,
    pub sequence: u32,
    /// driver timestamp, seconds (CLOCK_MONOTONIC)
    pub timestamp: f64,
    /// host monotonic seconds at dequeue
    pub t_host: f64,
}

#[derive(Debug, Clone, Copy, Default)]
pub struct CtrlInfo {
    pub min: i32,
    pub max: i32,
    pub step: i32,
    pub default: i32,
    pub flags: u32,
}

pub(crate) fn ioctl(fd: i32, req: u64, buf: *mut u8) -> io::Result<()> {
    loop {
        let r = unsafe { libc::ioctl(fd, req as _, buf) };
        if r == 0 {
            return Ok(());
        }
        let e = io::Error::last_os_error();
        if e.raw_os_error() == Some(libc::EINTR) {
            continue;
        }
        return Err(e);
    }
}

fn put_u32(b: &mut [u8], off: usize, v: u32) {
    b[off..off + 4].copy_from_slice(&v.to_le_bytes());
}
fn get_u32(b: &[u8], off: usize) -> u32 {
    u32::from_le_bytes(b[off..off + 4].try_into().unwrap())
}
fn get_i32(b: &[u8], off: usize) -> i32 {
    i32::from_le_bytes(b[off..off + 4].try_into().unwrap())
}
fn get_i64(b: &[u8], off: usize) -> i64 {
    i64::from_le_bytes(b[off..off + 8].try_into().unwrap())
}

static T0: once_cell::sync::Lazy<Instant> = once_cell::sync::Lazy::new(Instant::now);

pub fn monotonic() -> f64 {
    T0.elapsed().as_secs_f64()
}

struct Mapping {
    ptr: *mut libc::c_void,
    len: usize,
}
unsafe impl Send for Mapping {}

/// One V4L2 capture device. Opened on creation, closed on drop; stream with start()/read()/stop().
pub struct Camera {
    pub device: String,
    fd: i32,
    bufs: Vec<Mapping>,
    pub streaming: bool,
    pub width: u32,
    pub height: u32,
    pub pixfmt: String,
    pub fps: f64,
}

impl Camera {
    pub fn open(device: &str) -> io::Result<Camera> {
        let c = CString::new(device).unwrap();
        let fd = unsafe { libc::open(c.as_ptr(), libc::O_RDWR | libc::O_NONBLOCK) };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(Camera {
            device: device.to_string(),
            fd,
            bufs: Vec::new(),
            streaming: false,
            width: 0,
            height: 0,
            pixfmt: String::new(),
            fps: 0.0,
        })
    }

    // --- controls ---

    pub fn get(&self, name: &str) -> io::Result<i32> {
        let mut b = [0u8; 8];
        put_u32(&mut b, 0, cid(name)?);
        ioctl(self.fd, VIDIOC_G_CTRL, b.as_mut_ptr())?;
        Ok(get_i32(&b, 4))
    }

    pub fn set(&self, name: &str, value: i32) -> io::Result<()> {
        let mut b = [0u8; 8];
        put_u32(&mut b, 0, cid(name)?);
        b[4..8].copy_from_slice(&value.to_le_bytes());
        ioctl(self.fd, VIDIOC_S_CTRL, b.as_mut_ptr())
    }

    /// min/max/step/default of a control.
    pub fn query(&self, name: &str) -> io::Result<CtrlInfo> {
        let mut b = [0u8; 0x44];
        put_u32(&mut b, 0, cid(name)?);
        ioctl(self.fd, VIDIOC_QUERYCTRL, b.as_mut_ptr())?;
        Ok(CtrlInfo {
            min: get_i32(&b, 40),
            max: get_i32(&b, 44),
            step: get_i32(&b, 48),
            default: get_i32(&b, 52),
            flags: get_u32(&b, 56),
        })
    }

    // --- streaming ---

    pub fn configure(&mut self, width: u32, height: u32, pixfmt: &str, fps: u32) -> io::Result<()> {
        let mut f = [0u8; 208];
        put_u32(&mut f, 0, V4L2_BUF_TYPE_VIDEO_CAPTURE);
        put_u32(&mut f, 8, width);
        put_u32(&mut f, 12, height);
        put_u32(&mut f, 16, fourcc(pixfmt));
        put_u32(&mut f, 20, V4L2_FIELD_NONE);
        ioctl(self.fd, VIDIOC_S_FMT, f.as_mut_ptr())?;
        self.width = get_u32(&f, 8);
        self.height = get_u32(&f, 12);
        self.pixfmt = String::from_utf8_lossy(&f[16..20]).to_string();
        let mut p = [0u8; 204];
        put_u32(&mut p, 0, V4L2_BUF_TYPE_VIDEO_CAPTURE);
        put_u32(&mut p, 12, 1);
        put_u32(&mut p, 16, fps); // timeperframe = 1/fps
        ioctl(self.fd, VIDIOC_S_PARM, p.as_mut_ptr())?;
        let (num, den) = (get_u32(&p, 12), get_u32(&p, 16));
        self.fps = if num != 0 { den as f64 / num as f64 } else { 0.0 };
        Ok(())
    }

    fn buf(index: u32) -> [u8; BUF_SIZE] {
        let mut b = [0u8; BUF_SIZE];
        put_u32(&mut b, 0, index);
        put_u32(&mut b, 4, V4L2_BUF_TYPE_VIDEO_CAPTURE);
        put_u32(&mut b, 60, V4L2_MEMORY_MMAP);
        b
    }

    fn reqbufs(&self, n: u32) -> io::Result<u32> {
        let mut r = [0u8; 20];
        put_u32(&mut r, 0, n);
        put_u32(&mut r, 4, V4L2_BUF_TYPE_VIDEO_CAPTURE);
        put_u32(&mut r, 8, V4L2_MEMORY_MMAP);
        ioctl(self.fd, VIDIOC_REQBUFS, r.as_mut_ptr())?;
        Ok(get_u32(&r, 0))
    }

    pub fn start(&mut self, nbufs: u32) -> io::Result<()> {
        let n = self.reqbufs(nbufs)?;
        self.unmap();
        for i in 0..n {
            let mut b = Self::buf(i);
            ioctl(self.fd, VIDIOC_QUERYBUF, b.as_mut_ptr())?;
            let offset = get_u32(&b, 64);
            let length = get_u32(&b, 72) as usize;
            let ptr = unsafe {
                libc::mmap(
                    std::ptr::null_mut(),
                    length,
                    libc::PROT_READ | libc::PROT_WRITE,
                    libc::MAP_SHARED,
                    self.fd,
                    offset as libc::off_t,
                )
            };
            if ptr == libc::MAP_FAILED {
                return Err(io::Error::last_os_error());
            }
            self.bufs.push(Mapping { ptr, len: length });
            ioctl(self.fd, VIDIOC_QBUF, b.as_mut_ptr())?;
        }
        let mut t = V4L2_BUF_TYPE_VIDEO_CAPTURE.to_le_bytes();
        ioctl(self.fd, VIDIOC_STREAMON, t.as_mut_ptr())?;
        self.streaming = true;
        Ok(())
    }

    fn unmap(&mut self) {
        for m in self.bufs.drain(..) {
            unsafe { libc::munmap(m.ptr, m.len) };
        }
    }

    pub fn stop(&mut self) -> io::Result<()> {
        let mut t = V4L2_BUF_TYPE_VIDEO_CAPTURE.to_le_bytes();
        let r = ioctl(self.fd, VIDIOC_STREAMOFF, t.as_mut_ptr());
        self.streaming = false;
        self.unmap();
        let _ = self.reqbufs(0);
        r
    }

    /// Dequeue one frame (copy), re-queue the buffer.
    pub fn read(&mut self, timeout: f64) -> io::Result<Frame> {
        let mut pfd = libc::pollfd { fd: self.fd, events: libc::POLLIN, revents: 0 };
        let ms = (timeout * 1000.0) as i32;
        let r = unsafe { libc::poll(&mut pfd, 1, ms) };
        if r < 0 {
            return Err(io::Error::last_os_error());
        }
        if r == 0 {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                format!("{}: no frame within {}s", self.device, timeout),
            ));
        }
        let mut b = Self::buf(0);
        ioctl(self.fd, VIDIOC_DQBUF, b.as_mut_ptr())?;
        let t_host = monotonic();
        let idx = get_u32(&b, 0) as usize;
        let used = get_u32(&b, 8) as usize;
        let sec = get_i64(&b, 24);
        let usec = get_i64(&b, 32);
        let seq = get_u32(&b, 56);
        let data = match self.bufs.get(idx) {
            Some(m) => unsafe { std::slice::from_raw_parts(m.ptr as *const u8, used.min(m.len)) }.to_vec(),
            None => Vec::new(),
        };
        ioctl(self.fd, VIDIOC_QBUF, b.as_mut_ptr())?;
        Ok(Frame { data, sequence: seq, timestamp: sec as f64 + usec as f64 * 1e-6, t_host })
    }
}

impl Drop for Camera {
    fn drop(&mut self) {
        if self.streaming {
            let _ = self.stop();
        }
        self.unmap();
        if self.fd >= 0 {
            unsafe { libc::close(self.fd) };
        }
    }
}
