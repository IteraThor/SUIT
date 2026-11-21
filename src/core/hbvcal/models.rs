//! The HBV camera models the optimizer knows: everything sensor- and firmware-specific lives here.
//!
//! All models use a Realtek RTS58xx bridge with the same extension-unit mailbox (xu.rs). What differs: USB ID,
//! sensor (chip ID, exposure unit, how brightness is raised), where the firmware build string sits in code
//! memory, and per firmware build the tuning-lock flag and the MJPEG quality (Q) bytes in XDATA.
//!
//! A model with no verified build runs in standard mode: UVC controls only, no extension-unit command.
/// How the optimizer raises brightness after the exposure is set.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Gain {
    /// ISP digital gain through the UVC `gain` control (0..128); sensor analog gain 0x350B only for dark scenes
    Uvc,
    /// sensor analog gain register, linear: gain = code / unity
    Sensor { reg: u32, unity: i64, max: i64 },
    /// UVC `gain` as a bridge digital gain whose 1x is `unity` (below it a saturated pixel no longer reaches full
    /// white), `per_x` control steps per 1x
    UvcScaled { unity: i64, per_x: f64 },
}

const OV2710_Q_LEVELS: &[i64] = &[1, 2, 3, 4, 5, 6, 7, 8, 11, 16, 22];

#[derive(Debug)]
pub struct Build {
    /// date string after "REALSIL\0" in code memory
    pub date: &'static str,
    /// XDATA tuning-lock flag (0x01 = unlocked). None: unlock state seen from an unmasked code read at `build_addr`
    pub lock_flag: Option<u32>,
    pub build_addr: u32,
    /// MJPEG Q byte per stream-width class (see settings::q_class) and its stock value; empty: Q not mapped,
    /// camera keeps its own. Written before a stream starts; lower = less compression.
    pub q_slots: &'static [(&'static str, u32, i64)],
    /// the Q steps the optimizer tries, least compression first; the last one is stock
    pub q_levels: &'static [i64],
    /// Q the per-camera calibration measures at (its 8x-gain flicker probe makes the noisiest, largest frames)
    pub q_measure: i64,
    /// Q values a user may set (outside it the camera sends only broken frames or useless pictures)
    pub q_range: (i64, i64),
    /// Q of the running stream
    pub q_committed: Option<u32>,
    /// largest MJPEG frame (KB) the build streams reliably, by frame height
    pub frame_limit_kb: &'static [(u32, f64)],
}

#[derive(Debug)]
pub struct Model {
    /// stored in profiles
    pub key: &'static str,
    pub name: &'static str,
    pub usb_ids: &'static [&'static str],
    /// sensor ID registers (only readable while some process streams)
    pub chip_id: [(u32, u8); 2],
    /// UVC exposure_time_absolute unit (measured)
    pub exposure_unit_s: f64,
    /// sensor modes with another line time than exposure_unit_s, by frame height
    pub exposure_unit_by_height: &'static [(u32, f64)],
    pub gain: Gain,
    /// code addresses of the build string
    pub build_string_addrs: &'static [u32],
    pub builds: &'static [Build],
}

pub static OV2710: Model = Model {
    key: "ov2710",
    name: "HBV OV2710",
    usb_ids: &["0bdc:2710"],
    chip_id: [(0x300A, 0x27), (0x300B, 0x10)],
    exposure_unit_s: 50e-6,
    exposure_unit_by_height: &[],
    gain: Gain::Uvc,
    build_string_addrs: &[0x1D10, 0x0FE4],
    builds: &[
        Build {
            date: "07/11/26",
            lock_flag: Some(0xC888),
            build_addr: 0x1D10,
            q_slots: &[("large", 0xC06F, 24), ("mid", 0xC094, 22), ("small", 0xC079, 18)],
            q_levels: OV2710_Q_LEVELS,
            q_measure: 8,
            q_range: (1, 30),
            q_committed: Some(0xC102),
            frame_limit_kb: &[],
        },
        Build {
            date: "07/28/26",
            lock_flag: Some(0xC888),
            build_addr: 0x1D10,
            q_slots: &[("large", 0xC070, 24), ("mid", 0xC095, 22), ("small", 0xC07A, 18)],
            q_levels: OV2710_Q_LEVELS,
            q_measure: 8,
            q_range: (1, 30),
            q_committed: Some(0xC102),
            frame_limit_kb: &[],
        },
        Build {
            date: "02/15/25",
            lock_flag: Some(0xC879),
            build_addr: 0x0FE4,
            q_slots: &[("large", 0xC072, 24), ("mid", 0xC097, 22), ("small", 0xC07C, 18)],
            q_levels: OV2710_Q_LEVELS,
            q_measure: 8,
            q_range: (1, 30),
            q_committed: Some(0xC102),
            frame_limit_kb: &[(960, 300.0), (720, 215.0)],
        },
    ],
};

/// Probed on 3 cameras (hbv-probe, 2026-11-21): build 02/26/25 at code 0x1868 (mask 0x0D while locked), the
/// OV2710 unlock works, chip ID 0x9732. Exposure unit 6.25 µs (sensor lines vs UVC value). Sensor gain 0x350B is
/// linear (0x10 = 1x .. 0xFF), firmware stock 0x80 = 8x; a UVC exposure write resets it to 0x80. XDATA RAM is at
/// 0x8000-0x87FF on this bridge; the MJPEG Q byte is not found yet, so Q stays the camera's own.
pub static OV9732: Model = Model {
    key: "ov9732",
    name: "HBV OV9732",
    usb_ids: &["0bdc:8088"],
    chip_id: [(0x300A, 0x97), (0x300B, 0x32)],
    exposure_unit_s: 6.25e-6,
    exposure_unit_by_height: &[],
    gain: Gain::Sensor { reg: 0x350B, unity: 0x10, max: 0xFF },
    build_string_addrs: &[0x1868],
    builds: &[Build {
        date: "02/26/25",
        lock_flag: None,
        build_addr: 0x1868,
        // XDATA 0xD104 scales the firmware's base quant tables (code 0x164C luma, 0x168C chroma): entry = base * Q >> 5.
        // At stream start the firmware rebuilds the tables when D104 differs from D102 (the running stream's Q).
        // Q 3 and below: only broken frames at any size; 1280x720 breaks above ~175 KB frames (Q 8 on a bright board).
        q_slots: &[("large", 0xD104, 0x14), ("mid", 0xD104, 0x14), ("small", 0xD104, 0x14)],
        q_levels: &[4, 5, 6, 7, 8, 9, 10, 12, 14, 16, 20],
        q_measure: 0x14,
        q_range: (4, 40),
        q_committed: Some(0xD102),
        frame_limit_kb: &[(720, 175.0)],
    }],
};

/// Probed 2026-11-21 on three modules (USB 1e45:8025 "Generic USB Camera", firmware "07/10/25" at code 0x4874).
/// Same Realtek extension unit, but standard mode only:
///   * MJPEG Q is not settable: at every stream start the firmware writes a constant per stream width into the
///     quantiser scale (XDATA 0xD104: 0x10 at 640, 0x16 up to 1280, 0x18 up to 1920, ...) and rebuilds its tables
///     from it in a code bank the mailbox can't read.
///   * the UVC exposure is the sensor's coarse integration in lines (0x0202): 1546 lines/frame at 30 fps (21.6 us),
///     1860 at 1920x1080 (17.9 us).
///   * the UVC gain is a bridge digital gain: at 0 a saturated pixel comes out at ~45 % of full white; full white
///     from ~40 (1x), 128 is ~2.2x. Sensor analog gain (0x0204/0x0205, Sony 256/(256-code)) stays at 1x in manual.
///   * Sony/SMIA registers: model ID 0x0002/0x0003 = 0x81 0x79.
pub static IMX179: Model = Model {
    key: "imx179",
    name: "HBV IMX179",
    usb_ids: &["1e45:8025"],
    chip_id: [(0x0002, 0x81), (0x0003, 0x79)],
    exposure_unit_s: 1.0 / (30.0 * 1546.0),
    exposure_unit_by_height: &[(1080, 1.0 / (30.0 * 1860.0))],
    gain: Gain::UvcScaled { unity: 40, per_x: 71.0 },
    build_string_addrs: &[0x4874],
    builds: &[],
};

pub static MODELS: [&Model; 3] = [&OV2710, &OV9732, &IMX179];

impl Model {
    pub fn build(&self, date: &str) -> Option<&'static Build> {
        self.builds.iter().find(|b| b.date == date)
    }
    /// Has this model verified builds (may the extension unit be used at all)?
    pub fn hidden_capable(&self) -> bool {
        !self.builds.is_empty()
    }
    /// UVC exposure value for a time in seconds.
    pub fn exp_units(&self, seconds: f64) -> i64 {
        (seconds / self.exposure_unit_s).round() as i64
    }
    /// Seconds per UVC exposure step at this frame height (the sensor mode decides the line time).
    pub fn exposure_unit(&self, height: u32) -> f64 {
        self.exposure_unit_by_height.iter().find(|(h, _)| *h == height).map(|(_, u)| *u).unwrap_or(self.exposure_unit_s)
    }
}

impl Build {
    pub fn frame_limit(&self, height: u32) -> Option<f64> {
        if self.frame_limit_kb.is_empty() {
            return None;
        }
        Some(
            self.frame_limit_kb
                .iter()
                .find(|(h, _)| *h == height)
                .map(|(_, v)| *v)
                .unwrap_or_else(|| self.frame_limit_kb.iter().map(|(_, v)| *v).fold(f64::INFINITY, f64::min)),
        )
    }
}

pub fn by_usb_id(id: &str) -> Option<&'static Model> {
    MODELS.iter().copied().find(|m| m.usb_ids.contains(&id))
}

pub fn by_key(key: &str) -> Option<&'static Model> {
    MODELS.iter().copied().find(|m| m.key == key)
}

pub fn of_device(dev: &str) -> Option<&'static Model> {
    super::profiles::usb_id(dev).and_then(|u| by_usb_id(&u))
}

/// Display name of a USB ID: the model's name, else "USB camera vvvv:pppp".
pub fn name_of(usb_id: Option<&str>) -> String {
    match usb_id {
        Some(u) => by_usb_id(u).map(|m| m.name.to_string()).unwrap_or_else(|| format!("USB camera {}", u)),
        None => "unknown camera".into(),
    }
}

/// Every build-string address any model uses (for a camera whose model is not known).
pub fn all_build_addrs() -> Vec<u32> {
    let mut v: Vec<u32> = MODELS.iter().flat_map(|m| m.build_string_addrs.iter().copied()).collect();
    v.dedup();
    v
}

/// Is some process (Autodarts) streaming this camera? With the extension unit allowed: the sensor answers its chip
/// ID only while powered. Otherwise: the device refuses a format change while another process streams.
pub fn streaming(dev: &str) -> bool {
    if super::profiles::hidden_ok(dev) {
        if let Ok(xu) = super::xu::Xu::open(dev) {
            if xu.unlock().is_ok() {
                return super::daemon::is_streaming(&xu);
            }
        }
    }
    busy(dev)
}

pub fn busy(dev: &str) -> bool {
    let Ok(mut cam) = super::v4l2::Camera::open(dev) else { return false };
    let (w, h) = super::autodarts::stream_size();
    match cam.configure(w, h, "MJPG", 30) {
        Err(e) => e.raw_os_error() == Some(libc::EBUSY),
        Ok(()) => false,
    }
}

