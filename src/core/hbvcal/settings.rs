//! Camera settings across all control layers, applied in the order the firmware requires.
//!
//! Layers (verified on the cameras):
//!   1. `mjpeg_q`  XDATA Q byte for the stream resolution: must be written BEFORE the stream starts.
//!   2. `uvc`      standard V4L2/UVC controls (exposure writes re-assert sensor gain 0x80).
//!   3. `sensor`   OV2710 registers, written last, while streaming.
//!   4. `isp`      raw XDATA ISP registers, written last, while streaming (experimental).
use super::v4l2::{Camera, CID};
use super::xu::Xu;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::io;
use std::path::Path;

/// Per-resolution MJPEG Q bytes in XDATA, per firmware build.
pub fn q_addr_table(build: &str) -> Option<[(&'static str, u32); 3]> {
    match build {
        "07/11/26" => Some([("large", 0xC06F), ("mid", 0xC094), ("small", 0xC079)]),
        "07/28/26" => Some([("large", 0xC070), ("mid", 0xC095), ("small", 0xC07A)]),
        "02/15/25" => Some([("large", 0xC072), ("mid", 0xC097), ("small", 0xC07C)]),
        _ => None,
    }
}
pub fn q_default(class: &str) -> i64 {
    match class {
        "large" => 24,
        "mid" => 22,
        _ => 18,
    }
}
/// the Q steps the optimizer uses, least compression first; 22 = stock
pub const Q_LEVELS: [i64; 11] = [1, 2, 3, 4, 5, 6, 7, 8, 11, 16, 22];
/// Largest MJPEG frame a build streams reliably (KB), by frame height. Other heights use the lowest.
pub fn frame_limit_kb(build: &str) -> Option<&'static [(u32, f64)]> {
    match build {
        "02/15/25" => Some(&[(960, 300.0), (720, 215.0)]),
        _ => None,
    }
}
pub const FRAME_HEADROOM: f64 = 0.8;

/// Order matters: mode switches before the values they enable.
pub const UVC_ORDER: [&str; 14] = [
    "auto_exposure",
    "exposure_dynamic_framerate",
    "exposure_time_absolute",
    "gain",
    "white_balance_automatic",
    "white_balance_temperature",
    "power_line_frequency",
    "backlight_compensation",
    "brightness",
    "contrast",
    "saturation",
    "hue",
    "gamma",
    "sharpness",
];

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Settings {
    pub uvc: BTreeMap<String, i64>,
    pub mjpeg_q: Option<i64>,
    pub sensor: BTreeMap<u32, i64>,
    pub isp: BTreeMap<u32, i64>,
}

impl Settings {
    pub fn to_json(&self) -> Value {
        let hexmap = |m: &BTreeMap<u32, i64>| {
            Value::Object(m.iter().map(|(k, v)| (format!("0x{:04X}", k), json!(v))).collect())
        };
        json!({
            "uvc": self.uvc,
            "mjpeg_q": self.mjpeg_q,
            "sensor": hexmap(&self.sensor),
            "isp": hexmap(&self.isp),
        })
    }

    pub fn from_json(d: &Value) -> Settings {
        let hexmap = |v: &Value| -> BTreeMap<u32, i64> {
            v.as_object()
                .map(|m| {
                    m.iter()
                        .filter_map(|(k, v)| {
                            let k = u32::from_str_radix(k.trim_start_matches("0x").trim_start_matches("0X"), 16).ok()?;
                            Some((k, v.as_i64().or_else(|| v.as_f64().map(|f| f as i64))?))
                        })
                        .collect()
                })
                .unwrap_or_default()
        };
        Settings {
            uvc: d["uvc"]
                .as_object()
                .map(|m| {
                    m.iter()
                        .filter_map(|(k, v)| Some((k.clone(), v.as_i64().or_else(|| v.as_f64().map(|f| f as i64))?)))
                        .collect()
                })
                .unwrap_or_default(),
            mjpeg_q: d["mjpeg_q"].as_i64(),
            sensor: hexmap(&d["sensor"]),
            isp: hexmap(&d["isp"]),
        }
    }

    /// self overridden by other.
    pub fn merged(&self, other: &Settings) -> Settings {
        let mut s = self.clone();
        s.uvc.extend(other.uvc.clone());
        if other.mjpeg_q.is_some() {
            s.mjpeg_q = other.mjpeg_q;
        }
        s.sensor.extend(other.sensor.clone());
        s.isp.extend(other.isp.clone());
        s
    }

    pub fn label(&self) -> String {
        let mut parts: Vec<String> = self.uvc.iter().map(|(k, v)| format!("{}={}", k, v)).collect();
        if let Some(q) = self.mjpeg_q {
            parts.push(format!("Q={}", q));
        }
        parts.extend(self.sensor.iter().map(|(k, v)| format!("s{:04X}={:#04x}", k, v)));
        parts.extend(self.isp.iter().map(|(k, v)| format!("x{:04X}={:#04x}", k, v)));
        if parts.is_empty() { "current".into() } else { parts.join(" ") }
    }
}

pub fn q_class(width: u32) -> &'static str {
    if width >= 1920 {
        "large"
    } else if width > 640 {
        "mid"
    } else {
        "small"
    }
}

/// XDATA address of the Q byte for this camera's firmware build and stream width. Not cached.
pub fn q_addr(xu: &Xu, width: u32) -> io::Result<u32> {
    let build = xu.firmware_build()?;
    let t = q_addr_table(&build)
        .ok_or_else(|| io::Error::other(format!("{}: unknown firmware build {:?}; Q address not mapped", xu.device, build)))?;
    let c = q_class(width);
    Ok(t.iter().find(|(k, _)| *k == c).unwrap().1)
}

/// The Q level `steps` coarser than q (more compression, smaller frames), at most stock.
pub fn q_above(q: i64, steps: usize) -> i64 {
    let higher: Vec<i64> = Q_LEVELS.iter().copied().filter(|&lv| lv > q).collect();
    if higher.is_empty() {
        q.max(*Q_LEVELS.last().unwrap())
    } else {
        higher[steps.min(higher.len()).max(1) - 1]
    }
}

/// Layer 1: must run while no one is streaming (or before the next stream start).
pub fn apply_prestream(xu: &Xu, s: &Settings, width: u32) -> io::Result<()> {
    if let Some(q) = s.mjpeg_q {
        xu.unlock()?;
        xu.mem_write(q_addr(xu, width)?, &[q as u8])?;
    }
    Ok(())
}

/// Largest frame (KB) a Q level may produce on this build at this frame height; inf where no limit is known.
pub fn frame_budget_kb(xu: &Xu, height: u32) -> f64 {
    let Ok(b) = xu.firmware_build() else { return f64::INFINITY };
    match frame_limit_kb(&b) {
        None => f64::INFINITY,
        Some(l) => {
            let v = l
                .iter()
                .find(|(h, _)| *h == height)
                .map(|(_, v)| *v)
                .unwrap_or_else(|| l.iter().map(|(_, v)| *v).fold(f64::INFINITY, f64::min));
            v * FRAME_HEADROOM
        }
    }
}

/// Controls an auto mode in the same settings makes inactive (the driver refuses writes to them).
pub fn inactive(uvc: &BTreeMap<String, i64>, name: &str) -> bool {
    match name {
        "exposure_time_absolute" => uvc.get("auto_exposure").copied().unwrap_or(1) != 1,
        "white_balance_temperature" => uvc.get("white_balance_automatic").copied().unwrap_or(0) != 0,
        _ => false,
    }
}

/// One UVC write. Some cameras refuse exposure writes after a plain switch to manual until auto exposure is
/// toggled once more.
pub fn set_uvc(cam: &Camera, name: &str, value: i64) -> io::Result<()> {
    match cam.set(name, value as i32) {
        Err(e) if e.kind() == io::ErrorKind::PermissionDenied && name == "exposure_time_absolute" => {
            cam.set("auto_exposure", 3)?;
            cam.set("auto_exposure", 1)?;
            cam.set(name, value as i32)
        }
        r => r,
    }
}

pub fn uvc_rank(n: &str) -> usize {
    UVC_ORDER.iter().position(|x| *x == n).unwrap_or(99)
}

/// Layer 2 in the order the firmware needs, skipping controls an auto mode makes inactive.
/// strict: return on the first failure; otherwise collect {control: error} and carry on.
pub fn apply_uvc(cam: &Camera, uvc: &BTreeMap<String, i64>, strict: bool) -> io::Result<BTreeMap<String, String>> {
    let mut failed = BTreeMap::new();
    let mut names: Vec<&String> = uvc.keys().collect();
    names.sort_by_key(|n| uvc_rank(n));
    for name in names {
        if inactive(uvc, name) {
            continue;
        }
        if let Err(e) = set_uvc(cam, name, uvc[name]) {
            if strict {
                return Err(e);
            }
            failed.insert(name.clone(), e.to_string());
        }
    }
    Ok(failed)
}

/// Layers 2–4: while streaming.
pub fn apply_streaming(cam: &mut Camera, xu: &Xu, s: &Settings, settle_frames: usize) -> io::Result<()> {
    apply_uvc(cam, &s.uvc, true)?;
    if !s.sensor.is_empty() || !s.isp.is_empty() {
        xu.unlock()?;
        for (reg, val) in &s.sensor {
            xu.sensor_write(*reg, *val as u8)?;
        }
        for (addr, val) in &s.isp {
            xu.mem_write(*addr, &[*val as u8])?;
        }
    }
    for _ in 0..settle_frames {
        cam.read(2.0)?;
    }
    Ok(())
}

pub const SNAPSHOT_REGS: [u32; 5] = [0x3500, 0x3501, 0x3502, 0x350A, 0x350B];

/// Read back the current state of every layer (sensor regs only if streaming).
pub fn snapshot(cam: &Camera, xu: Option<&Xu>, width: u32) -> Settings {
    let mut s = Settings::default();
    for (name, _) in CID {
        if let Ok(v) = cam.get(name) {
            s.uvc.insert(name.to_string(), v as i64);
        }
    }
    if let Some(xu) = xu {
        let r = (|| -> io::Result<()> {
            xu.unlock()?;
            s.mjpeg_q = Some(xu.mem_read(q_addr(xu, width)?, 1)?[0] as i64);
            if cam.streaming {
                let mut m = BTreeMap::new();
                for r in SNAPSHOT_REGS {
                    m.insert(r, xu.sensor_read(r)? as i64);
                }
                s.sensor = m;
            }
            Ok(())
        })();
        let _ = r;
    }
    s
}

pub fn save(s: &Settings, path: &Path) -> io::Result<()> {
    std::fs::write(path, serde_json::to_string_pretty(&s.to_json()).unwrap())
}

pub fn load(path: &Path) -> io::Result<Settings> {
    let v: Value = serde_json::from_str(&std::fs::read_to_string(path)?).map_err(io::Error::other)?;
    Ok(Settings::from_json(&v))
}
