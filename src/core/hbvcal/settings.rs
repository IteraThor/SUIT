//! Camera settings across all control layers, applied in the order the firmware requires.
//!
//! Layers (verified on the cameras):
//!   1. `mjpeg_q`  XDATA Q byte for the stream resolution: must be written BEFORE the stream starts.
//!   2. `uvc`      standard V4L2/UVC controls (exposure writes re-assert sensor gain 0x80).
//!   3. `sensor`   sensor registers (models.rs), written last, while streaming.
//!   4. `isp`      raw XDATA ISP registers, written last, while streaming (experimental).
use super::v4l2::{Camera, CID};
use super::xu::Xu;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::io;
use std::path::Path;

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

/// XDATA address of the Q byte for this camera's firmware build and stream width; None where the build has no
/// mapped Q (the camera keeps its own quality). An error for an unknown model or build.
pub fn q_addr(xu: &Xu, width: u32) -> io::Result<Option<u32>> {
    let c = q_class(width);
    Ok(xu.build_info()?.q_slots.iter().find(|(k, _, _)| *k == c).map(|(_, a, _)| *a))
}

/// Put every Q byte of this camera back to the firmware's stock value (no-op where Q is not mapped).
pub fn write_stock_q(xu: &Xu) -> io::Result<()> {
    let b = xu.build_info()?;
    if b.q_slots.is_empty() {
        return Ok(());
    }
    xu.unlock()?;
    for (_, addr, stock) in b.q_slots {
        xu.mem_write(*addr, &[*stock as u8])?;
    }
    Ok(())
}

/// The Q steps of this camera's firmware build (least compression first, stock last); empty where Q is not mapped.
pub fn q_levels(xu: &Xu) -> &'static [i64] {
    xu.build_info().map(|b| if b.q_slots.is_empty() { &[][..] } else { b.q_levels }).unwrap_or(&[])
}

/// The Q level `steps` coarser than q (more compression, smaller frames), at most stock.
pub fn q_above(levels: &[i64], q: i64, steps: usize) -> i64 {
    let higher: Vec<i64> = levels.iter().copied().filter(|&lv| lv > q).collect();
    if higher.is_empty() {
        q.max(levels.last().copied().unwrap_or(q))
    } else {
        higher[steps.min(higher.len()).max(1) - 1]
    }
}

/// Layer 1: must run while no one is streaming (or before the next stream start).
pub fn apply_prestream(xu: &Xu, s: &Settings, width: u32) -> io::Result<()> {
    if let Some(q) = s.mjpeg_q {
        if let Some(addr) = q_addr(xu, width)? {
            xu.unlock()?;
            xu.mem_write(addr, &[q as u8])?;
        }
    }
    Ok(())
}

/// Largest frame (KB) a Q level may produce on this build at this frame height; inf where no limit is known.
pub fn frame_budget_kb(xu: &Xu, height: u32) -> f64 {
    match xu.build_info().ok().and_then(|b| b.frame_limit(height)) {
        None => f64::INFINITY,
        Some(v) => v * FRAME_HEADROOM,
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
            if let Some(a) = q_addr(xu, width)? {
                s.mjpeg_q = Some(xu.mem_read(a, 1)?[0] as i64);
            }
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
