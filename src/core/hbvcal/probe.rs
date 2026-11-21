//! Hardware probe for HBV cameras (hidden subcommand), to add a camera model to models.rs:
//!
//!   suit hbv-probe [--dev PATH] [--stop-autodarts] [--out DIR]   read-only: USB IDs, controls, MJPEG sizes, XU
//!       mailbox, firmware build string (scanned for at unknown addresses), sensor registers while streaming
//!   suit hbv-probe --unlock ...      tuning unlock (RAM flag), unmasked sensor registers and full XDATA dumps
//!       while streaming at several sizes, plus one JPEG per size (to look for the MJPEG Q bytes)
//!   suit hbv-probe --gain-test ...   exposure unit (UVC value vs sensor lines) and sensor gain code vs brightness;
//!       sensor registers and UVC controls only, put back afterwards
//!   suit hbv-probe --q-test ...      MJPEG Q sweep (QT_SIZES=1280x720,640x480 QT_VALS=14,0A hex): writes the Q register
//!       before each stream start, records frame size / DQT / broken frames, puts the stock value back
//!   suit hbv-probe --code-dump DIR   code memory and XDATA as raw binaries (reads only; not while streaming)
//!   suit hbv-probe --peek DEV ADDR LEN ... / --poke DEV ADDR BYTE   single unlocked XDATA reads / writes
//!   suit hbv-probe --optimize WxH    the Optimize button's job from the command line (result stays pending);
//!       with HBVCAL_PROFILES pointing elsewhere it leaves the real profiles alone
//!
//! Nothing here writes flash or code memory; everything changed lives in camera RAM until the next power cycle.
use super::capture::burst;
use super::v4l2::{Camera, CID};
use super::xu::{Xu, OP_CODE_READ, OP_SENSOR_WRITE, UNLOCK_ADDR, UNLOCK_DATA};
use super::{daemon, oneclick, profiles, service};
use opencv::prelude::*;
use serde_json::{json, Map, Value};
use std::path::PathBuf;

const MAGIC: &[u8; 8] = b"REALSIL\0";
const SENSOR_REGS: [u32; 28] = [
    // OmniVision: chip ID, exposure, gain, timing
    0x300A, 0x300B, 0x300C, 0x302A, 0x3500, 0x3501, 0x3502, 0x3503, 0x350A, 0x350B, 0x380C, 0x380D, 0x380E, 0x380F,
    0x3820, 0x3821,
    // Sony / SMIA (IMX179): model ID, coarse integration, analog gain, digital gain, frame / line length
    0x0016, 0x0017, 0x0202, 0x0203, 0x0204, 0x0205, 0x020E, 0x020F, 0x0340, 0x0341, 0x0342, 0x0343,
];

/// USB vendors of HBV cameras with the Realtek extension unit (IMX179 modules enumerate as 1e45:8025)
fn hbv_vendor(dev: &str) -> bool {
    profiles::usb_id(dev).map(|u| u.starts_with("0bdc:") || u.starts_with("1e45:")).unwrap_or(false)
}

fn sysfs(dev: &str) -> Value {
    let real = std::fs::canonicalize(dev).ok();
    let name = real.as_ref().and_then(|p| p.file_name()).map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
    let mut p = std::fs::canonicalize(format!("/sys/class/video4linux/{}/device", name)).ok();
    let mut out = Map::new();
    for _ in 0..4 {
        let Some(d) = p.clone() else { break };
        if d.join("idVendor").exists() {
            for a in ["idVendor", "idProduct", "bcdDevice", "manufacturer", "product", "serial", "speed", "version"] {
                out.insert(a.into(), json!(crate::core::util::read_trim(d.join(a))));
            }
            break;
        }
        p = d.parent().map(|x| x.to_path_buf());
    }
    Value::Object(out)
}

fn controls(cam: &Camera) -> Value {
    let mut m = Map::new();
    for (name, _) in CID {
        if let Ok(q) = cam.query(name) {
            m.insert(
                name.to_string(),
                json!({"min": q.min, "max": q.max, "step": q.step, "default": q.default, "value": cam.get(name).ok()}),
            );
        }
    }
    Value::Object(m)
}

/// Find "REALSIL\0<date>" in code memory under any constant byte offset (locked reads are masked).
fn scan_build(xu: &Xu, end: u32) -> Value {
    let chunk = 256u32;
    let mut prev: Vec<u8> = Vec::new();
    let mut addr = 0u32;
    while addr < end {
        let raw = match xu.read(OP_CODE_READ, addr, chunk as usize) {
            Ok(r) => r,
            Err(e) => return json!({"error": format!("code read at 0x{:04X}: {}", addr, e)}),
        };
        let mut buf = prev.clone();
        buf.extend_from_slice(&raw);
        let base = addr - prev.len() as u32;
        for i in 0..buf.len().saturating_sub(16) {
            let m = buf[i].wrapping_sub(MAGIC[0]);
            if (0..8).all(|k| buf[i + k].wrapping_sub(m) == MAGIC[k]) {
                let date: Vec<u8> = buf[i + 8..i + 16].iter().map(|b| b.wrapping_sub(m)).collect();
                let e = date.iter().position(|&c| c == 0).unwrap_or(8);
                return json!({"addr": format!("0x{:04X}", base + i as u32), "mask": format!("0x{:02X}", m),
                              "build": String::from_utf8_lossy(&date[..e])});
            }
        }
        prev = raw[raw.len() - 16..].to_vec();
        addr += chunk;
    }
    json!({"error": "not found"})
}

/// XDATA 0x0000-0xFFFF in 1 KB reads; ranges the firmware rejects read as 0xEE.
fn dump_all(xu: &Xu) -> Vec<u8> {
    let mut v = Vec::with_capacity(0x10000);
    for a in (0..0x10000u32).step_by(0x400) {
        match xu.mem_read(a, 0x400) {
            Ok(d) => v.extend_from_slice(&d),
            Err(_) => v.extend(std::iter::repeat(0xEE).take(0x400)),
        }
    }
    v
}

fn hex(data: &[u8], base: u32) -> String {
    data.chunks(16)
        .enumerate()
        .map(|(i, c)| {
            format!("{:04X}: {}", base as usize + i * 16, c.iter().map(|b| format!("{:02X}", b)).collect::<Vec<_>>().join(" "))
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn probe(dev: &str, out: &std::path::Path) -> Value {
    let mut r = Map::new();
    r.insert("device".into(), json!(dev));
    r.insert("port".into(), json!(profiles::port_key(dev)));
    r.insert("usb".into(), sysfs(dev));
    let mut cam = match Camera::open(dev) {
        Ok(c) => c,
        Err(e) => {
            r.insert("error".into(), json!(format!("open: {}", e)));
            return Value::Object(r);
        }
    };
    r.insert("controls".into(), controls(&cam));
    r.insert("mjpg_sizes".into(), json!(crate::core::autodarts::get_camera_supported_resolutions(dev)));
    let xu = match Xu::open(dev) {
        Ok(x) => x,
        Err(e) => {
            r.insert("xu".into(), json!({"error": e.to_string()}));
            return Value::Object(r);
        }
    };
    let st = xu.status();
    r.insert("xu_status".into(), json!(st.as_ref().map(|s| hex(s, 0)).map_err(|e| e.to_string())));
    if st.is_err() {
        return Value::Object(r);
    }
    r.insert("firmware_build_known_addrs".into(), json!(xu.firmware_build().map_err(|e| e.to_string())));
    r.insert("build_scan".into(), scan_build(&xu, 0x10000));
    match xu.mem_read(0xC000, 0x1000) {
        Ok(d) => {
            let p = out.join(format!("{}-xdata-c000-locked.txt", profiles::port_key(dev)));
            let _ = std::fs::write(&p, hex(&d, 0xC000));
            r.insert("xdata_dump".into(), json!(p));
        }
        Err(e) => {
            r.insert("xdata_dump".into(), json!({"error": e.to_string()}));
        }
    }
    let mut sensor = Map::new();
    let s = (|| -> std::io::Result<()> {
        cam.configure(1280, 720, "MJPG", 30)?;
        cam.start(4)?;
        let b = burst(&mut cam, 10, 30, 2.0, 0)?;
        let st = b.stream_stats();
        sensor.insert("stream".into(), json!({"fps": st.fps, "kb_mean": st.kb_mean, "kb_max": st.kb_max}));
        for reg in SENSOR_REGS {
            let v = xu.read(super::xu::OP_SENSOR_READ, reg, 2);
            sensor.insert(format!("0x{:04X}", reg), json!(v.map(|v| hex(&v, 0)).map_err(|e| e.to_string())));
        }
        sensor.insert("is_streaming_ov2710".into(), json!(daemon::is_streaming(&xu)));
        Ok(())
    })();
    let _ = cam.stop();
    if let Err(e) = s {
        sensor.insert("error".into(), json!(e.to_string()));
    }
    r.insert("sensor_while_streaming".into(), Value::Object(sensor));
    Value::Object(r)
}

/// --unlock: send the tuning unlock (RAM flag, gone after a power cycle), check that reads are no longer masked,
/// and dump XDATA while streaming at each size (to find the per-resolution MJPEG Q bytes and the lock flag).
fn unlocked(dev: &str, out: &std::path::Path) -> Value {
    let mut r = Map::new();
    r.insert("device".into(), json!(dev));
    let res = (|| -> std::io::Result<()> {
        let xu = Xu::open(dev)?;
        xu.write(OP_SENSOR_WRITE, &UNLOCK_DATA, UNLOCK_ADDR)?;
        r.insert("build_after_unlock".into(), scan_build(&xu, 0x2000));
        let key = profiles::port_key(dev);
        let d = dump_all(&xu);
        let _ = std::fs::write(out.join(format!("{}-xdata-idle.txt", key)), hex(&d, 0));
        let mut cam = Camera::open(dev)?;
        for (w, h) in [(1280u32, 720u32), (800, 600), (640, 480)] {
            cam.configure(w, h, "MJPG", 30)?;
            cam.start(4)?;
            let b = burst(&mut cam, 20, 30, 2.0, 0)?;
            let st = b.stream_stats();
            let _ = std::fs::write(out.join(format!("{}-{}x{}.jpg", key, w, h)), &b.frames[0].data);
            let d = dump_all(&xu);
            let mut regs = Map::new();
            for reg in SENSOR_REGS {
                regs.insert(format!("0x{:04X}", reg), json!(xu.sensor_read(reg).map(|v| format!("0x{:02X}", v)).map_err(|e| e.to_string())));
            }
            let _ = cam.stop();
            let _ = std::fs::write(out.join(format!("{}-xdata-{}x{}.txt", key, w, h)), hex(&d, 0));
            r.insert(format!("{}x{}", w, h), json!({"kb_mean": st.kb_mean, "kb_max": st.kb_max, "fps": st.fps, "sensor": regs}));
        }
        Ok(())
    })();
    if let Err(e) = res {
        r.insert("error".into(), json!(e.to_string()));
    }
    Value::Object(r)
}

fn mean_luma(cam: &mut Camera) -> std::io::Result<f64> {
    let b = burst(cam, 3, 6, 2.0, 0)?;
    let mut s = 0.0;
    for m in b.decode().map_err(std::io::Error::other)? {
        let y = super::capture::luma(&m).map_err(std::io::Error::other)?;
        s += opencv::core::mean_def(&y).map_err(std::io::Error::other)?[0];
    }
    Ok(s / 3.0)
}

fn exposure_lines(xu: &Xu) -> std::io::Result<f64> {
    let v = ((xu.sensor_read(0x3500)? as u32 & 0xF) << 16) | ((xu.sensor_read(0x3501)? as u32) << 8) | xu.sensor_read(0x3502)? as u32;
    Ok(v as f64 / 16.0)
}

/// --gain-test: exposure unit (UVC value vs sensor exposure lines) and the sensor gain code vs brightness.
/// Sensor registers and UVC controls only; everything is put back at the end.
fn gain_test(dev: &str) -> Value {
    let mut r = Map::new();
    r.insert("device".into(), json!(dev));
    let res = (|| -> std::io::Result<()> {
        let xu = Xu::open(dev)?;
        xu.write(OP_SENSOR_WRITE, &UNLOCK_DATA, UNLOCK_ADDR)?;
        let mut cam = Camera::open(dev)?;
        let keep: Vec<(&str, i32)> = ["auto_exposure", "exposure_time_absolute", "gamma"]
            .iter()
            .filter_map(|n| cam.get(n).ok().map(|v| (*n, v)))
            .collect();
        cam.configure(1280, 720, "MJPG", 30)?;
        cam.start(4)?;
        burst(&mut cam, 1, 20, 2.0, 0)?;
        cam.set("auto_exposure", 1)?;
        cam.set("gamma", 100)?;
        let vts = ((xu.sensor_read(0x380E)? as f64) * 256.0 + xu.sensor_read(0x380F)? as f64).max(1.0);
        let line_s = 1.0 / (30.0 * vts);
        let mut exp = Vec::new();
        for v in [50, 100, 200, 400, 800, 1600, 3200, 5000, 10000] {
            cam.set("exposure_time_absolute", v)?;
            burst(&mut cam, 1, 6, 2.0, 0)?;
            let lines = exposure_lines(&xu)?;
            exp.push(json!({"uvc": v, "lines": lines, "ms": lines * line_s * 1000.0, "unit_us": lines * line_s * 1e6 / v as f64,
                            "gain_350B": xu.sensor_read(0x350B)?, "luma": mean_luma(&mut cam)?}));
        }
        r.insert("vts".into(), json!(vts));
        r.insert("exposure".into(), json!(exp));
        cam.set("exposure_time_absolute", 200)?;
        burst(&mut cam, 1, 6, 2.0, 0)?;
        let g0 = (xu.sensor_read(0x350A)?, xu.sensor_read(0x350B)?);
        let mut gains = Vec::new();
        for (a, b) in [(0u8, 0x10u8), (0, 0x20), (0, 0x40), (0, 0x80), (0, 0xC0), (0, 0xFF), (1, 0x00), (3, 0xFF), (0, 0x80)] {
            xu.sensor_write(0x350A, a)?;
            xu.sensor_write(0x350B, b)?;
            burst(&mut cam, 1, 6, 2.0, 0)?;
            gains.push(json!({"code": format!("0x{:01X}{:02X}", a, b), "readback": format!("0x{:02X}{:02X}", xu.sensor_read(0x350A)?, xu.sensor_read(0x350B)?), "luma": mean_luma(&mut cam)?}));
        }
        r.insert("gain".into(), json!(gains));
        // does a UVC exposure write reset the sensor gain?
        xu.sensor_write(0x350B, 0x20)?;
        cam.set("exposure_time_absolute", 210)?;
        burst(&mut cam, 1, 6, 2.0, 0)?;
        r.insert("gain_after_uvc_exposure_write".into(), json!(format!("0x{:02X}", xu.sensor_read(0x350B)?)));
        xu.sensor_write(0x350A, g0.0)?;
        xu.sensor_write(0x350B, g0.1)?;
        let _ = cam.stop();
        for (n, v) in keep {
            let _ = cam.set(n, v);
        }
        Ok(())
    })();
    if let Err(e) = res {
        r.insert("error".into(), json!(e.to_string()));
    }
    Value::Object(r)
}

fn dqt_head(jpg: &[u8]) -> Vec<u8> {
    jpg.windows(2).position(|w| w == [0xFF, 0xDB]).map(|i| jpg[i + 5..(i + 13).min(jpg.len())].to_vec()).unwrap_or_default()
}

/// --q-test: the OV9732 JPEG scale register (XDATA 0xD104; table = base * scale >> 5, rebuilt by the firmware at
/// stream start when D104 differs from D102). Writes it before a stream start and while streaming, measures each,
/// and puts the stock value back. Register writes only, gone after a power cycle.
fn q_test(dev: &str, out: &std::path::Path) -> Value {
    let mut r = Map::new();
    r.insert("device".into(), json!(dev));
    let key = profiles::port_key(dev);
    let res = (|| -> std::io::Result<()> {
        let xu = Xu::open(dev)?;
        xu.write(OP_SENSOR_WRITE, &UNLOCK_DATA, UNLOCK_ADDR)?;
        let stock = xu.mem_read(0xD104, 1)?[0];
        r.insert("stock".into(), json!(stock));
        let mut cam = Camera::open(dev)?;
        let mut rows = Vec::new();
        let sizes: Vec<(u32, u32)> = std::env::var("QT_SIZES").ok().map(|s| s.split(',').filter_map(|p| p.split_once('x').and_then(|(w, h)| Some((w.parse().ok()?, h.parse().ok()?)))).collect()).unwrap_or(vec![(1280, 720)]);
        let vals: Vec<u8> = std::env::var("QT_VALS").ok().map(|s| s.split(',').filter_map(|v| u8::from_str_radix(v, 16).ok()).collect()).unwrap_or(vec![0x14, 0x0A, 0x08]);
        for (w, h) in sizes {
            for &v in &vals {
                xu.mem_write(0xD104, &[v])?;
                cam.configure(w, h, "MJPG", 30)?;
                cam.start(4)?;
                let b = burst(&mut cam, 30, 15, 3.0, 60);
                let _ = cam.stop();
                let d = xu.mem_read(0xD100, 8)?;
                match b {
                    Ok(b) => {
                        let st = b.stream_stats();
                        let _ = std::fs::write(out.join(format!("{}-{}x{}-d104-{:02X}.jpg", key, w, h, v)), &b.frames[0].data);
                        rows.push(json!({"size": format!("{}x{}", w, h), "d104": format!("{:02X}", d[4]), "d102": format!("{:02X}", d[2]), "dqt": dqt_head(&b.frames[0].data),
                                         "kb_mean": (st.kb_mean * 10.0).round() / 10.0, "kb_max": (st.kb_max * 10.0).round() / 10.0, "fps": (st.fps * 10.0).round() / 10.0, "dropped": st.dropped, "invalid": st.invalid}));
                    }
                    Err(e) => {
                        rows.push(json!({"size": format!("{}x{}", w, h), "d104": format!("{:02X}", d[4]), "error": e.to_string()}));
                        // a broken stream: back to stock before the next try
                        xu.mem_write(0xD104, &[stock])?;
                        cam.start(4)?;
                        let _ = burst(&mut cam, 3, 5, 3.0, 0);
                        let _ = cam.stop();
                        break;
                    }
                }
            }
        }
        r.insert("runs".into(), json!(rows));
        xu.mem_write(0xD104, &[stock])?;
        cam.configure(1280, 720, "MJPG", 30)?;
        cam.start(4)?;
        let b = burst(&mut cam, 5, 10, 3.0, 0);
        let d = xu.mem_read(0xD100, 8)?;
        let _ = cam.stop();
        r.insert("restored".into(), json!({"d104": format!("{:02X}", d[4]), "d102": format!("{:02X}", d[2]), "ok": b.is_ok()}));
        Ok(())
    })();
    if let Err(e) = res {
        r.insert("error".into(), json!(e.to_string()));
        if let Ok(xu) = Xu::open(dev) {
            let _ = xu.write(OP_SENSOR_WRITE, &UNLOCK_DATA, UNLOCK_ADDR);
            let _ = xu.mem_write(0xD104, &[0x14]);
        }
    }
    Value::Object(r)
}

fn sony_reg16(xu: &Xu, hi: u32) -> std::io::Result<u32> {
    Ok(((xu.sensor_read(hi)? as u32) << 8) | xu.sensor_read(hi + 1)? as u32)
}

/// --uvc-test: Sony/SMIA sensors (IMX179) with a UVC gain control: what UVC exposure and gain do to the sensor's
/// coarse integration (0x0202), analog gain (0x0204) and digital gain (0x020E), and to brightness. UVC controls
/// only (sensor registers are read); everything is put back.
fn uvc_test(dev: &str) -> Value {
    let mut r = Map::new();
    r.insert("device".into(), json!(dev));
    let saved: Vec<(&str, i32)> = Camera::open(dev)
        .map(|c| ["auto_exposure", "exposure_time_absolute", "gain", "gamma"].iter().filter_map(|n| c.get(n).ok().map(|v| (*n, v))).collect())
        .unwrap_or_default();
    let res = (|| -> std::io::Result<()> {
        let xu = Xu::open(dev)?;
        xu.write(OP_SENSOR_WRITE, &UNLOCK_DATA, UNLOCK_ADDR)?;
        let mut cam = Camera::open(dev)?;
        let keep: Vec<(&str, i32)> = ["auto_exposure", "exposure_time_absolute", "gain", "gamma"].iter().filter_map(|n| cam.get(n).ok().map(|v| (*n, v))).collect();
        r.insert("before".into(), json!(keep.iter().map(|(n, v)| format!("{}={}", n, v)).collect::<Vec<_>>()));
        let (w, h) = std::env::var("UT_SIZE").ok().and_then(|s| s.split_once('x').and_then(|(w, h)| Some((w.parse().ok()?, h.parse().ok()?)))).unwrap_or((1280u32, 720u32));
        cam.configure(w, h, "MJPG", 30)?;
        cam.start(4)?;
        burst(&mut cam, 1, 20, 2.0, 0)?;
        let regs = |xu: &Xu| -> std::io::Result<Value> {
            Ok(json!({"lines": sony_reg16(xu, 0x0202)?, "frame_len": sony_reg16(xu, 0x0340)?, "line_len": sony_reg16(xu, 0x0342)?,
                      "again": sony_reg16(xu, 0x0204)?, "dgain": sony_reg16(xu, 0x020E)?, "id0000": sony_reg16(xu, 0x0000)?, "id0002": sony_reg16(xu, 0x0002)?}))
        };
        r.insert("auto".into(), regs(&xu)?);
        cam.set("auto_exposure", 1)?;
        cam.set("gain", 0)?;
        let mut exp = Vec::new();
        for v in [5, 10, 50, 100, 200, 300, 333, 500] {
            cam.set("exposure_time_absolute", v)?;
            burst(&mut cam, 1, 8, 2.0, 0)?;
            exp.push(json!({"uvc": v, "sensor": regs(&xu)?, "luma": mean_luma(&mut cam)?}));
        }
        r.insert("exposure".into(), json!(exp));
        let ge: i32 = std::env::var("UT_GAIN_EXP").ok().and_then(|v| v.parse().ok()).unwrap_or(50);
        if let Ok(gm) = std::env::var("UT_GAMMA") {
            cam.set("gamma", gm.parse().unwrap_or(100))?;
        }
        cam.set("exposure_time_absolute", ge)?;
        let mut gains = Vec::new();
        for g in [0, 16, 32, 48, 64, 80, 96, 128] {
            cam.set("gain", g)?;
            if let Some(a) = std::env::var("UT_AGAIN").ok().and_then(|v| u8::from_str_radix(&v, 16).ok()) {
                burst(&mut cam, 1, 2, 2.0, 0)?;
                xu.sensor_write(0x0204, 0)?;
                xu.sensor_write(0x0205, a)?;
            }
            let b = burst(&mut cam, 3, 8, 2.0, 0)?;
            let mut mx = 0.0;
            let mut p999 = 0.0;
            for m in b.decode().map_err(std::io::Error::other)? {
                let y = super::capture::luma(&m).map_err(std::io::Error::other)?;
                let mut v: Vec<f32> = Vec::new();
                for row in 0..y.rows() {
                    for col in (0..y.cols()).step_by(4) {
                        v.push(*y.at_2d::<f32>(row, col).map_err(std::io::Error::other)?);
                    }
                }
                v.sort_by(|a, b| a.partial_cmp(b).unwrap());
                mx = v[v.len() - 1] as f64;
                p999 = v[v.len() * 999 / 1000] as f64;
            }
            gains.push(json!({"uvc": g, "sensor": regs(&xu)?, "luma": mean_luma(&mut cam)?, "max": mx, "p999": p999}));
        }
        r.insert("gain".into(), json!(gains));
        let _ = cam.stop();
        for (n, v) in keep.iter().rev() {
            let _ = cam.set(n, *v);
        }
        Ok(())
    })();
    if let Err(e) = res {
        r.insert("error".into(), json!(e.to_string()));
        // put the controls back also after a failure (exposure before the auto mode that makes it inactive)
        if let Ok(c) = Camera::open(dev) {
            for (n, v) in saved.iter().rev() {
                let _ = c.set(n, *v);
            }
        }
    }
    Value::Object(r)
}

/// --optimize WxH: the Optimize button's job without the page (result stays pending, as on the page).
fn optimize_cli(size: &str) -> i32 {
    let Some((w, h)) = size.split_once('x').and_then(|(w, h)| Some((w.parse().ok()?, h.parse().ok()?))) else {
        eprintln!("--optimize WxH");
        return 1;
    };
    let job = oneclick::Job::default();
    let r = oneclick::optimize(&job, 30, (w, h));
    let v = job.view();
    for s in &v.steps {
        println!("[{}] {}", s.state, s.title);
    }
    for l in &v.log {
        println!("  {}", l);
    }
    println!("{}", serde_json::to_string_pretty(&v.result).unwrap());
    match r {
        Ok(()) => 0,
        Err(e) => {
            println!("ERROR: {}", e);
            2
        }
    }
}

/// --code-dump DIR: unlock, then code memory (up to 128 KB) and XDATA as raw binaries. Reads only; works while
/// Autodarts streams (the XU query does not need the stream).
fn code_dump(out: &std::path::Path) -> i32 {
    let _ = std::fs::create_dir_all(out);
    for dev in daemon::cameras() {
        if !hbv_vendor(&dev) {
            continue;
        }
        let key = profiles::port_key(&dev);
        let r = (|| -> std::io::Result<()> {
            let xu = Xu::open(&dev)?;
            xu.write(OP_SENSOR_WRITE, &UNLOCK_DATA, UNLOCK_ADDR)?;
            let mut code = Vec::new();
            for a in (0..0x20000u32).step_by(0x400) {
                match xu.read(OP_CODE_READ, a, 0x400) {
                    Ok(d) => code.extend_from_slice(&d),
                    Err(e) => {
                        eprintln!("{}: code read stops at 0x{:05X}: {}", key, a, e);
                        break;
                    }
                }
            }
            std::fs::write(out.join(format!("{}-code.bin", key)), &code)?;
            std::fs::write(out.join(format!("{}-xdata.bin", key)), dump_all(&xu))?;
            eprintln!("{}: {} bytes of code", key, code.len());
            Ok(())
        })();
        if let Err(e) = r {
            eprintln!("{}: {}", key, e);
        }
        break; // one camera is enough: all run the same build
    }
    0
}

pub fn main(args: &[String]) -> i32 {
    if let Some(i) = args.iter().position(|a| a == "--poke") {
        // --poke DEV ADDR BYTE: one unlocked XDATA write (RAM/registers, gone after a power cycle)
        let p = |s: &String| u32::from_str_radix(s.trim_start_matches("0x"), 16).unwrap_or(0);
        let r = Xu::open(&args[i + 1]).and_then(|xu| {
            xu.write(OP_SENSOR_WRITE, &UNLOCK_DATA, UNLOCK_ADDR)?;
            xu.mem_write(p(&args[i + 2]), &[p(&args[i + 3]) as u8])?;
            xu.mem_read(p(&args[i + 2]), 1)
        });
        println!("{:?}", r);
        return 0;
    }
    if let Some(i) = args.iter().position(|a| a == "--peek") {
        // --peek DEV ADDR LEN [ADDR LEN...]: unlocked XDATA reads (read-only)
        let dev = &args[i + 1];
        let xu = match Xu::open(dev) { Ok(x) => x, Err(e) => { eprintln!("{}", e); return 1; } };
        let _ = xu.write(OP_SENSOR_WRITE, &UNLOCK_DATA, UNLOCK_ADDR);
        println!("state {:?}", xu.state());
        for c in args[i + 2..].chunks(2) {
            let a = u32::from_str_radix(c[0].trim_start_matches("0x"), 16).unwrap_or(0);
            let n = c.get(1).and_then(|s| s.parse().ok()).unwrap_or(16);
            match xu.mem_read(a, n) { Ok(d) => print!("{}", hex(&d, a)), Err(e) => println!("{:04X}: {}", a, e) }
        }
        return 0;
    }
    if let Some(i) = args.iter().position(|a| a == "--code-dump") {
        return code_dump(&args.get(i + 1).map(PathBuf::from).unwrap_or_else(|| std::env::temp_dir().join("hbv-probe")));
    }
    if let Some(i) = args.iter().position(|a| a == "--optimize") {
        return optimize_cli(args.get(i + 1).map(|s| s.as_str()).unwrap_or(""));
    }
    let mut devs = Vec::new();
    let mut stop_ad = false;
    let mut unlock = false;
    let mut gain = false;
    let mut qt = false;
    let mut ut = false;
    let mut out = std::env::temp_dir().join("hbv-probe");
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--dev" => devs.extend(it.next().cloned()),
            "--stop-autodarts" => stop_ad = true,
            "--unlock" => unlock = true,
            "--gain-test" => gain = true,
            "--q-test" => qt = true,
            "--uvc-test" => ut = true,
            "--out" => out = it.next().map(PathBuf::from).unwrap_or(out),
            _ => {}
        }
    }
    if devs.is_empty() {
        devs = daemon::cameras();
    }
    devs.retain(|d| hbv_vendor(d));
    if devs.is_empty() {
        eprintln!("no HBV (USB vendor 0bdc / 1e45) camera found");
        return 1;
    }
    let ad = oneclick::autodarts_active();
    if ad && !stop_ad {
        eprintln!("Autodarts is running and holds the cameras: pass --stop-autodarts (it is started again afterwards)");
        return 1;
    }
    let _ = std::fs::create_dir_all(&out);
    let keeper = service::is_enabled();
    service::stop();
    if ad {
        oneclick::systemctl("stop", "autodarts");
        std::thread::sleep(std::time::Duration::from_secs(2));
    }
    let report: Vec<Value> = devs
        .iter()
        .map(|d| if ut { uvc_test(d) } else if qt { q_test(d, &out) } else if gain { gain_test(d) } else if unlock { unlocked(d, &out) } else { probe(d, &out) })
        .collect();
    if keeper {
        service::start();
    }
    if ad {
        oneclick::systemctl("start", "autodarts");
    }
    println!("{}", serde_json::to_string_pretty(&report).unwrap());
    0
}
