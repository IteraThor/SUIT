//! Live view, readouts, manual adjustments and presets behind the Camera Controls page.
//!
//! Works for every UVC camera. The hidden layers (MJPEG quality, sensor gain) are only used on HBV cameras after
//! the user confirmed them (profiles::hidden_ok). A camera's changes are kept (re-applied by the keeper service)
//! once it has a profile: from the optimizer, from "Keep these settings", or from a loaded preset.
//!
//! Live view: the page streams the camera itself when it is free. While Autodarts holds the camera it shows
//! Autodarts' own current frame (/api/img/cams/<i>); controls still work. Autodarts in camera standby only
//! sends black frames, so it is woken up.
use super::autocal::{percentile, WHITE_TARGET};
use super::autodarts::{autodarts_index, get_bytes, get_json, AUTODARTS_API};
use super::capture::{luma, valid_jpeg, START_DISCARD};
use super::daemon::{cameras, is_streaming};
use super::settings::{apply_prestream, apply_streaming, apply_uvc, q_addr, q_addr_table, q_default, set_uvc, Settings};
use super::v4l2::Camera;
use super::xu::Xu;
use super::{board, oneclick, profiles, service};
use anyhow::{anyhow, bail, Result};
use opencv::core::{Mat, Vector};
use opencv::prelude::*;
use opencv::{imgcodecs, imgproc};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, HashMap};
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// Q factor committed at the last stream start (same address on both firmware builds)
pub const Q_COMMITTED: u32 = 0xC102;
pub const UVC_CONTROLS: [&str; 13] = [
    "exposure_time_absolute",
    "gain",
    "gamma",
    "sharpness",
    "contrast",
    "brightness",
    "saturation",
    "hue",
    "white_balance_temperature",
    "white_balance_automatic",
    "auto_exposure",
    "power_line_frequency",
    "backlight_compensation",
];
/// a live view nobody asked for frames in this long hands its camera back
pub const IDLE_S: f64 = 15.0;
/// retry interval for waking Autodarts from camera standby
pub const WAKE_EVERY_S: f64 = 10.0;

static LAST_WAKE: Mutex<f64> = Mutex::new(0.0);

fn now() -> f64 {
    crate::core::util::now_s()
}

fn stamp() -> String {
    chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string()
}

/// Bring Autodarts back from camera standby (it then sends all-black frames). True while in standby.
pub fn wake_autodarts() -> bool {
    let Some(state) = get_json("/api/state", 2.0) else { return false };
    if state["running"].as_bool().unwrap_or(true) {
        return false;
    }
    let mut last = LAST_WAKE.lock().unwrap();
    if now() - *last >= WAKE_EVERY_S {
        *last = now();
        let a: ureq::Agent =
            ureq::Agent::config_builder().timeout_global(Some(Duration::from_secs(3))).build().into();
        let _ = a.put(&format!("{}/api/start", AUTODARTS_API)).send_empty();
    }
    true
}

pub fn hbv(dev: &str) -> bool {
    profiles::hbv(dev)
}
pub fn hidden_ok(dev: &str) -> bool {
    profiles::hidden_ok(dev)
}

// --- image readouts ---

/// Grey image as f32 values with its size.
#[derive(Clone)]
pub struct Gray {
    pub w: usize,
    pub h: usize,
    pub v: Vec<f32>,
}

pub fn gray_of(m: &Mat) -> opencv::Result<Gray> {
    let y = luma(m)?;
    Ok(Gray { w: y.cols() as usize, h: y.rows() as usize, v: y.data_typed::<f32>()?.to_vec() })
}

pub fn laplacian_var(g: &Gray, mask: &[bool]) -> f64 {
    // 3x3 Laplacian (ksize=3 in OpenCV is [2 0 2; 0 -8 0; 2 0 2])
    let (w, h) = (g.w, g.h);
    let mut vals = Vec::new();
    for y in 1..h.saturating_sub(1) {
        for x in 1..w.saturating_sub(1) {
            let i = y * w + x;
            if !mask[i] {
                continue;
            }
            let l = 2.0 * (g.v[i - w - 1] + g.v[i - w + 1] + g.v[i + w - 1] + g.v[i + w + 1]) - 8.0 * g.v[i];
            vals.push(l as f64);
        }
    }
    if vals.is_empty() {
        return 0.0;
    }
    let m = vals.iter().sum::<f64>() / vals.len() as f64;
    vals.iter().map(|v| (v - m).powi(2)).sum::<f64>() / vals.len() as f64
}

/// Mean |step| across 8x8 block boundaries divided by mean |step| inside blocks (1.0 = none).
pub fn blockiness(g: &Gray, mask: &[bool]) -> f64 {
    let (w, h) = (g.w, g.h);
    let (mut bs, mut bn, mut is, mut inn) = (0f64, 0usize, 0f64, 0usize);
    for y in 0..h {
        for x in 0..w.saturating_sub(1) {
            let i = y * w + x;
            if mask[i] && mask[i + 1] {
                let d = (g.v[i + 1] - g.v[i]).abs() as f64;
                if x % 8 == 7 {
                    bs += d;
                    bn += 1;
                } else {
                    is += d;
                    inn += 1;
                }
            }
        }
    }
    for y in 0..h.saturating_sub(1) {
        for x in 0..w {
            let i = y * w + x;
            if mask[i] && mask[i + w] {
                let d = (g.v[i + w] - g.v[i]).abs() as f64;
                if y % 8 == 7 {
                    bs += d;
                    bn += 1;
                } else {
                    is += d;
                    inn += 1;
                }
            }
        }
    }
    let b = if bn > 0 { bs / bn as f64 } else { f64::NAN };
    let i = if inn > 0 { is / inn as f64 } else { 0.0 };
    b / i.max(1e-6)
}

fn decode(data: &[u8], flags: i32) -> Option<Mat> {
    let m = imgcodecs::imdecode(&Vector::<u8>::from_slice(data), flags).ok()?;
    if m.empty() { None } else { Some(m) }
}

#[derive(Default)]
struct LiveState {
    frame: Option<Arc<Vec<u8>>>,
    frames: Vec<Gray>,
    sizes: Vec<usize>,
}

/// Latest frame of one camera: own stream if free, else Autodarts' image endpoint.
pub struct Live {
    pub dev: String,
    state: Mutex<LiveState>,
    source: Mutex<String>,
    last_request: Mutex<f64>,
    running: AtomicBool,
    pub pause: AtomicBool,
    frame_count: AtomicU64,
    pushed: AtomicU64,
    black: AtomicBool,
    pub seq: AtomicU64,
}

impl Live {
    fn new(dev: &str) -> Live {
        Live {
            dev: dev.into(),
            state: Mutex::new(LiveState::default()),
            source: Mutex::new("none".into()),
            last_request: Mutex::new(0.0),
            running: AtomicBool::new(false),
            pause: AtomicBool::new(false),
            frame_count: AtomicU64::new(0),
            pushed: AtomicU64::new(0),
            black: AtomicBool::new(false),
            seq: AtomicU64::new(0),
        }
    }

    pub fn source(&self) -> String {
        self.source.lock().unwrap().clone()
    }
    fn set_source(&self, s: &str) {
        *self.source.lock().unwrap() = s.into();
    }

    /// Latest raw JPEG.
    pub fn frame(&self) -> Option<Arc<Vec<u8>>> {
        self.state.lock().unwrap().frame.clone()
    }

    pub fn frame_count(&self) -> u64 {
        self.frame_count.load(Ordering::SeqCst)
    }

    pub fn frames_len(&self) -> usize {
        self.state.lock().unwrap().frames.len()
    }

    pub fn touch(self: &Arc<Self>) {
        *self.last_request.lock().unwrap() = now();
        if !self.running.swap(true, Ordering::SeqCst) {
            let me = self.clone();
            std::thread::spawn(move || {
                me.run();
                me.running.store(false, Ordering::SeqCst);
            });
        }
    }

    /// Hand the camera back now instead of after IDLE_S.
    pub fn stop(&self) {
        *self.last_request.lock().unwrap() = 0.0;
    }

    fn wanted(&self) -> bool {
        now() - *self.last_request.lock().unwrap() < IDLE_S
    }

    fn push(&self, data: &[u8]) {
        let Some(bgr) = decode(data, imgcodecs::IMREAD_COLOR) else { return };
        let n = self.pushed.fetch_add(1, Ordering::SeqCst) + 1;
        // Autodarts' picture is only judged on a few frames a second: skip the luma on the others
        let y = if self.source() == "own stream" || n % 3 == 0 { gray_of(&bgr).ok() } else { None };
        let mut st = self.state.lock().unwrap();
        st.frame = Some(Arc::new(data.to_vec()));
        if let Some(y) = y {
            st.frames.push(y);
            let l = st.frames.len();
            if l > 10 {
                st.frames.drain(..l - 10);
            }
        }
        st.sizes.push(data.len());
        let l = st.sizes.len();
        if l > 30 {
            st.sizes.drain(..l - 30);
        }
        self.seq.fetch_add(1, Ordering::SeqCst);
    }

    fn run(&self) {
        while self.wanted() {
            if self.pause.load(Ordering::SeqCst) {
                std::thread::sleep(Duration::from_millis(500));
                continue;
            }
            // Never take a camera away from Autodarts: while its service is active only show Autodarts' picture.
            if oneclick::autodarts_active() {
                self.autodarts_poll();
                continue;
            }
            if self.own_stream().is_err() {
                self.autodarts_poll();
            }
        }
        self.set_source("none");
    }

    fn own_stream(&self) -> std::io::Result<()> {
        let mut s = profiles::get_settings(&self.dev);
        let width = 1280;
        let hidden = s.is_some() && hidden_ok(&self.dev);
        if let (Some(p), false) = (&s, hidden) {
            s = Some(profiles::uvc_only(p));
        }
        let xu = Xu::open(&self.dev)?;
        let mut cam = Camera::open(&self.dev)?;
        if hidden {
            apply_prestream(&xu, s.as_ref().unwrap(), width)?;
        }
        cam.configure(width, 960, "MJPG", 30)?;
        cam.start(4)?; // EBUSY when Autodarts streams
        let r = (|| -> std::io::Result<()> {
            self.set_source("own stream");
            for _ in 0..START_DISCARD {
                cam.read(2.0)?;
            }
            if hidden {
                // a stream start resets sensor gain: re-apply the whole profile
                apply_streaming(&mut cam, &xu, s.as_ref().unwrap(), 0)?;
            } else if let Some(s) = &s {
                apply_uvc(&cam, &s.uvc, false)?;
            }
            while self.wanted() && !self.pause.load(Ordering::SeqCst) {
                let n = self.frame_count.fetch_add(1, Ordering::SeqCst);
                if n % 30 == 0 && oneclick::autodarts_active() {
                    break; // Autodarts is starting: hand the camera back immediately
                }
                let f = cam.read(2.0)?;
                if valid_jpeg(&f.data) {
                    self.push(&f.data);
                }
            }
            Ok(())
        })();
        let _ = cam.stop();
        self.set_source("none");
        r
    }

    fn autodarts_poll(&self) {
        let Some(idx) = autodarts_index(&self.dev) else {
            self.set_source("busy (not an Autodarts camera)");
            std::thread::sleep(Duration::from_secs(1));
            return;
        };
        match get_bytes(&format!("/api/img/cams/{}", idx), 3.0) {
            Some(data) => {
                let same = self.frame().map(|f| *f == data).unwrap_or(false);
                let black = self.black.load(Ordering::SeqCst);
                if same && !black {
                    // same picture as before: the endpoint refreshes ~30x/s
                    std::thread::sleep(Duration::from_millis(30));
                    return;
                }
                let is_black = decode(&data, imgcodecs::IMREAD_REDUCED_GRAYSCALE_8)
                    .map(|g| {
                        let mut mx = 0.0;
                        let _ = opencv::core::min_max_loc(&g, None, Some(&mut mx), None, None, &opencv::core::no_array());
                        mx == 0.0
                    })
                    .unwrap_or(false);
                self.black.store(is_black, Ordering::SeqCst);
                if is_black && wake_autodarts() {
                    self.set_source("waking Autodarts from standby");
                } else if !same {
                    self.push(&data);
                    self.set_source("Autodarts");
                    return;
                }
            }
            None => self.set_source("busy (no Autodarts image)"),
        }
        std::thread::sleep(Duration::from_millis(500));
    }

    pub fn scores(&self) -> Value {
        let (fr, sizes) = {
            let st = self.state.lock().unwrap();
            (st.frames.clone(), st.sizes.clone())
        };
        let source = self.source();
        if fr.is_empty() {
            return json!({"source": source});
        }
        let (w, h) = (fr[0].w, fr[0].h);
        let hm = if (w, h) == (1280, 960) { board::load_homography(&self.dev, w as u32, h as u32, None) } else { None };
        let mask: Vec<bool> = hm
            .as_ref()
            .and_then(|hm| board::board_mask_image(hm, h as i32, w as i32, board::BOARD_R).ok())
            .and_then(|m| m.data_bytes().ok().map(|b| b.iter().map(|&v| v > 0).collect()))
            .unwrap_or_else(|| vec![true; w * h]);
        let n = fr.len() as f32;
        let mut mean = vec![0f32; w * h];
        for f in &fr {
            for (a, b) in mean.iter_mut().zip(&f.v) {
                *a += *b / n;
            }
        }
        let mut m: Vec<f32> = mean.iter().zip(&mask).filter(|(_, &k)| k).map(|(v, _)| *v).collect();
        let avg = m.iter().map(|&v| v as f64).sum::<f64>() / m.len().max(1) as f64;
        let last = fr.last().unwrap();
        let kb = sizes.iter().sum::<usize>() as f64 / sizes.len().max(1) as f64 / 1024.0;
        let mut out = json!({
            "source": source,
            "white": percentile(&mut m.clone(), 97.0),
            "black": percentile(&mut m, 3.0),
            "mean": avg,
            "sharpness": laplacian_var(last, &mask),
            "blockiness": blockiness(last, &mask),
            "kb": kb,
            "white_target": WHITE_TARGET,
            "board_mask": hm.is_some(),
        });
        if fr.len() >= 4 && source == "own stream" {
            let mut tn = Vec::new();
            for (i, &k) in mask.iter().enumerate() {
                if k {
                    let mu = mean[i];
                    let var = fr.iter().map(|f| (f.v[i] - mu).powi(2)).sum::<f32>() / (n - 1.0);
                    tn.push(var.sqrt());
                }
            }
            out["tnoise"] = json!(percentile(&mut tn, 50.0));
        }
        let cnt = mask.iter().filter(|&&k| k).count().max(1);
        let clip = last.v.iter().zip(&mask).filter(|(v, &k)| k && **v >= 250.0).count();
        out["clip_pct"] = json!(100.0 * clip as f64 / cnt as f64);
        out
    }
}

static LIVE: once_cell::sync::Lazy<Mutex<HashMap<String, Arc<Live>>>> =
    once_cell::sync::Lazy::new(|| Mutex::new(HashMap::new()));

pub fn live(dev: &str) -> Arc<Live> {
    LIVE.lock().unwrap().entry(dev.to_string()).or_insert_with(|| Arc::new(Live::new(dev))).clone()
}

fn live_get(dev: &str) -> Option<Arc<Live>> {
    LIVE.lock().unwrap().get(dev).cloned()
}

pub fn pause_live(on: bool) {
    for lv in LIVE.lock().unwrap().values() {
        lv.pause.store(on, Ordering::SeqCst);
    }
}

pub fn stop_all() {
    for lv in LIVE.lock().unwrap().values() {
        lv.stop();
    }
}

// --- controls ---

pub fn camera_info(dev: &str) -> Value {
    let prof = profiles::get(dev);
    let mut info = json!({
        "device": dev, "port": profiles::port_key(dev), "autodarts_index": autodarts_index(dev),
        "profile": prof, "controls": {}, "usb_id": profiles::usb_id(dev), "hbv": hbv(dev),
        "hidden": false, "optimized": profiles::is_calibrated(prof.as_ref()),
    });
    match Camera::open(dev) {
        Ok(cam) => {
            let mut ctrls = Map::new();
            for n in UVC_CONTROLS {
                if let (Ok(q), Ok(v)) = (cam.query(n), cam.get(n)) {
                    ctrls.insert(
                        n.into(),
                        json!({"min": q.min, "max": q.max, "step": q.step, "default": q.default, "flags": q.flags, "value": v}),
                    );
                }
            }
            info["controls"] = Value::Object(ctrls);
        }
        Err(e) => {
            info["error"] = json!(e.to_string());
            return info;
        }
    }
    if let Some(p) = &prof {
        let has_cal = p["meta"].get("calibrated_settings").is_some();
        info["calibrated"] = if info["optimized"].as_bool().unwrap() || has_cal {
            p["meta"].get("calibrated_settings").cloned().unwrap_or_else(|| p["settings"].clone())
        } else {
            Value::Null
        };
    }
    if !hidden_ok(dev) {
        return info;
    }
    let r = (|| -> std::io::Result<()> {
        let xu = Xu::open(dev)?;
        xu.unlock()?;
        info["firmware"] = json!(xu.firmware_build()?);
        info["q"] = json!(xu.mem_read(q_addr(&xu, 1280)?, 1)?[0]); // applies at the next stream start
        info["q_active"] = json!(xu.mem_read(Q_COMMITTED, 1)?[0]); // what the running stream uses
        let st = is_streaming(&xu);
        info["streaming"] = json!(st);
        if st {
            info["sensor_gain"] = json!(xu.sensor_read(0x350B)?);
        }
        info["hidden"] = json!(true);
        Ok(())
    })();
    if let Err(e) = r {
        info["hidden_error"] = json!(e.to_string());
    }
    info
}

/// What the camera runs now, in every layer this camera may use.
pub fn current_settings(dev: &str) -> Settings {
    let info = camera_info(dev);
    let mut s = Settings::default();
    if let Some(c) = info["controls"].as_object() {
        for (n, v) in c {
            if let Some(val) = v["value"].as_i64() {
                s.uvc.insert(n.clone(), val);
            }
        }
    }
    if info["hidden"].as_bool().unwrap_or(false) {
        s.mjpeg_q = info["q"].as_i64();
        if s.uvc.get("auto_exposure").copied().unwrap_or(1) != 1 {
            // auto exposure drives the sensor gain itself: keeping a value would fight it
        } else if let Some(g) = info["sensor_gain"].as_i64() {
            s.sensor.insert(0x350B, g);
        } else if info["profile"].is_object() {
            s.sensor = Settings::from_json(&info["profile"]["settings"]).sensor;
        }
    }
    s
}

/// Store an edited profile; the first edit of a calibrated profile keeps the calibration for "reset".
fn save_edit(dev: &str, prof: &Value, s: &Settings, extra: &[(&str, Value)]) {
    let mut meta = prof["meta"].as_object().cloned().unwrap_or_default();
    if profiles::is_calibrated(Some(prof)) && !meta.contains_key("calibrated_settings") {
        meta.insert("calibrated_settings".into(), prof["settings"].clone());
    }
    for (k, v) in extra {
        meta.insert(k.to_string(), v.clone());
    }
    profiles::put(dev, s, Some(Value::Object(meta)));
}

/// Set one control now. If the camera has a profile, the change is saved into it, so the keeper keeps it.
pub fn set_control(dev: &str, layer: &str, name: &str, value: i64) -> Result<Value> {
    if layer != "uvc" && !hidden_ok(dev) {
        bail!("JPEG quality and sensor gain are only used on confirmed HBV OV2710 cameras");
    }
    let mut out = json!({"ok": true});
    let prof = profiles::get(dev);
    let mut s = prof.as_ref().map(|p| Settings::from_json(&p["settings"]));
    if layer == "uvc" {
        if let Some(s) = s.as_mut() {
            s.uvc.insert(name.into(), value);
        }
        let cam = Camera::open(dev)?;
        set_uvc(&cam, name, value)?;
        if (name == "exposure_time_absolute" || name == "auto_exposure")
            && s.as_ref().map(|s| !s.sensor.is_empty()).unwrap_or(false)
            && hidden_ok(dev)
        {
            let xu = Xu::open(dev)?; // UVC exposure writes reset sensor gain
            if is_streaming(&xu) {
                for (r, v) in &s.as_ref().unwrap().sensor {
                    xu.sensor_write(*r, *v as u8)?;
                }
            }
        }
    } else {
        let xu = Xu::open(dev)?;
        xu.unlock()?;
        if layer == "q" {
            if let Some(s) = s.as_mut() {
                s.mjpeg_q = Some(value);
            }
            xu.mem_write(q_addr(&xu, 1280)?, &[value as u8])?;
            match live_get(dev) {
                Some(lv) if lv.source() == "own stream" => {
                    // restart our own stream so the new Q applies
                    lv.pause.store(true, Ordering::SeqCst);
                    std::thread::sleep(Duration::from_secs(1));
                    lv.pause.store(false, Ordering::SeqCst);
                    out["note"] = json!("Applied (live view restarted)");
                }
                _ if is_streaming(&xu) => {
                    out["needs_restart"] = json!(true);
                    out["note"] = json!("Saved. Autodarts' running stream keeps its quality until Autodarts restarts");
                }
                _ => {}
            }
        } else if layer == "sensor" {
            let reg = u32::from_str_radix(name.trim_start_matches("0x"), 16)?;
            if let Some(s) = s.as_mut() {
                s.sensor.insert(reg, value);
            }
            xu.sensor_write(reg, value as u8)?;
        }
    }
    if let (Some(s), Some(p)) = (s, prof) {
        save_edit(dev, &p, &s, &[("manual_edit", json!(stamp()))]);
        out["saved_to_profile"] = json!(true);
    }
    Ok(out)
}

/// Keep this camera's current settings: the keeper re-applies them after Autodarts restarts.
pub fn keep(dev: &str) {
    profiles::put(dev, &current_settings(dev), Some(json!({"source": "kept", "kept": stamp()})));
    service::sync();
}

pub fn write_stock_q(dev: &str) -> std::io::Result<()> {
    let xu = Xu::open(dev)?;
    xu.unlock()?;
    let build = xu.firmware_build()?;
    let t = q_addr_table(&build).ok_or_else(|| std::io::Error::other(format!("unknown firmware build {}", build)))?;
    for (cls, addr) in t {
        xu.mem_write(addr, &[q_default(cls) as u8])?;
    }
    Ok(())
}

/// Stop keeping this camera's settings. An optimized camera also gets its stock JPEG quality and its control
/// values from before the optimization back.
pub fn forget(dev: &str) -> Result<Value> {
    let mut out = json!({"ok": true});
    let Some(prof) = profiles::get(dev) else { return Ok(out) };
    let s = Settings::from_json(&prof["settings"]);
    if s.mjpeg_q.is_some() && hidden_ok(dev) {
        write_stock_q(dev)?;
        out["needs_restart"] = json!(true);
    }
    if let Some(prev) = prof["meta"].get("previous").filter(|v| v.is_object()) {
        let cam = Camera::open(dev)?;
        apply_uvc(&cam, &Settings::from_json(prev).uvc, false)?;
    }
    profiles::remove(dev);
    oneclick::clear_before_after(dev, None);
    service::sync();
    Ok(out)
}

/// Back to the values the last optimization picked; the keeper applies them within 2 s.
pub fn reset_profile(dev: &str) -> Result<Value> {
    let prof = profiles::get(dev).ok_or_else(|| anyhow!("this camera has no profile: run Optimize first"))?;
    let mut meta = prof["meta"].as_object().cloned().unwrap_or_default();
    let calibrated = meta.remove("calibrated_settings");
    meta.remove("manual_edit");
    meta.remove("preset");
    let Some(cal) = calibrated else { return Ok(json!({"ok": true, "note": "Already at the calibrated values"})) };
    profiles::put(dev, &Settings::from_json(&cal), Some(Value::Object(meta)));
    let mut out = json!({"ok": true});
    if cal["mjpeg_q"] != prof["settings"]["mjpeg_q"] {
        out["needs_restart"] = json!(true);
    }
    Ok(out)
}

/// Standard controls back to the camera's own defaults (saved into its profile, if it has one).
pub fn reset_defaults(dev: &str) -> Result<Value> {
    let cam = Camera::open(dev)?;
    let mut defaults = BTreeMap::new();
    for n in UVC_CONTROLS {
        if let Ok(q) = cam.query(n) {
            defaults.insert(n.to_string(), q.default as i64);
        }
    }
    let refused = apply_uvc(&cam, &defaults, false)?;
    drop(cam);
    if let Some(p) = profiles::get(dev) {
        let mut s = Settings::from_json(&p["settings"]);
        for (k, v) in &defaults {
            if !refused.contains_key(k) {
                s.uvc.insert(k.clone(), *v);
            }
        }
        save_edit(dev, &p, &s, &[("manual_edit", json!(stamp()))]);
    }
    Ok(json!({"ok": true, "refused": refused.keys().collect::<Vec<_>>()}))
}

// --- presets ---

/// built in: every camera on its own defaults, nothing kept
pub const DEFAULT_PRESET: &str = "Camera defaults";

fn check_name(name: &str) -> Result<()> {
    if name.trim().to_lowercase() == DEFAULT_PRESET.to_lowercase() {
        bail!("“{}” is built in; pick another name", DEFAULT_PRESET);
    }
    Ok(())
}

/// Every camera's kept profile (what the keeper enforces), or its current settings if nothing is kept.
pub fn save_preset(name: &str) -> Result<()> {
    check_name(name)?;
    let mut presets = profiles::load_presets();
    let mut entry = Map::new();
    for d in cameras() {
        let s = profiles::get_settings(&d).unwrap_or_else(|| current_settings(&d));
        entry.insert(profiles::port_key(&d), s.to_json());
    }
    presets.insert(name.into(), Value::Object(entry));
    profiles::save_presets(&presets);
    Ok(())
}

/// The built-in preset: stop keeping anything and put every camera's standard controls on its own defaults.
pub fn load_defaults() -> Result<Value> {
    let mut out = json!({"ok": true, "cameras": 0});
    let mut n = 0;
    for dev in cameras() {
        if forget(&dev)?["needs_restart"].as_bool().unwrap_or(false) {
            out["needs_restart"] = json!(true);
        }
        reset_defaults(&dev)?;
        n += 1;
    }
    out["cameras"] = json!(n);
    Ok(out)
}

/// Every connected camera in the preset gets its settings as its kept profile and applied now.
pub fn load_preset(name: &str) -> Result<Value> {
    if name == DEFAULT_PRESET {
        return load_defaults();
    }
    let presets = profiles::load_presets();
    let entry = presets.get(name).ok_or_else(|| anyhow!("no preset named {}", name))?.clone();
    let mut out = json!({"ok": true, "cameras": 0});
    let mut n = 0;
    for dev in cameras() {
        let Some(js) = entry.get(profiles::port_key(&dev)) else { continue };
        let mut s = Settings::from_json(js);
        let hidden = hidden_ok(&dev);
        if !hidden {
            s = profiles::uvc_only(&s);
        }
        match profiles::get(&dev) {
            Some(p) => save_edit(&dev, &p, &s, &[("preset", json!(name))]),
            None => profiles::put(&dev, &s, Some(json!({"source": "preset", "preset": name}))),
        }
        let cam = Camera::open(&dev)?;
        apply_uvc(&cam, &s.uvc, false)?;
        drop(cam);
        if hidden && s.mjpeg_q.is_some() {
            let xu = Xu::open(&dev)?;
            apply_prestream(&xu, &s, 1280)?;
            if is_streaming(&xu) && Some(xu.mem_read(Q_COMMITTED, 1)?[0] as i64) != s.mjpeg_q {
                out["needs_restart"] = json!(true);
            }
        }
        n += 1;
    }
    out["cameras"] = json!(n);
    service::sync();
    Ok(out)
}

/// The default preset's stored name, even when no preset has it (any more).
fn default_file_name() -> Option<String> {
    crate::core::util::read_json(profiles::default_preset_file())?["name"].as_str().map(String::from)
}

pub fn rename_preset(old: &str, new: &str) -> Result<()> {
    check_name(new)?;
    let mut presets = profiles::load_presets();
    if presets.contains_key(new) {
        bail!("a preset named {:?} already exists", new);
    }
    let v = presets.remove(old).ok_or_else(|| anyhow!("no preset named {}", old))?;
    presets.insert(new.into(), v);
    profiles::save_presets(&presets);
    if profiles::default_preset().is_none() && default_file_name().as_deref() == Some(old) {
        profiles::set_default_preset(Some(new));
    }
    Ok(())
}

pub fn delete_preset(name: &str) {
    let mut presets = profiles::load_presets();
    presets.remove(name);
    profiles::save_presets(&presets);
    if default_file_name().as_deref() == Some(name) {
        profiles::set_default_preset(None);
    }
}

/// Load this preset at every boot (on), or no preset at all (off).
pub fn set_default_preset(name: &str, on: bool) {
    profiles::set_default_preset(if on { Some(name) } else { None });
}

/// Compare the white level with the calibration reference; optionally trim digital gain to restore it.
pub fn recheck(dev: &str, trim: bool) -> Result<Value> {
    let lv = live(dev);
    lv.touch();
    for _ in 0..40 {
        if lv.frames_len() >= 4 {
            break;
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    let sc = lv.scores();
    let prof = profiles::get(dev);
    if !profiles::is_calibrated(prof.as_ref()) || sc.get("white").is_none() {
        let mut o = sc.clone();
        o["error"] = json!("no calibration or no frames");
        return Ok(o);
    }
    let prof = prof.unwrap();
    let white = sc["white"].as_f64().unwrap_or(0.0);
    let reference = prof["meta"]["reference"]["white"].as_f64().unwrap_or(WHITE_TARGET);
    let dev_pct = 100.0 * (white - reference) / reference;
    let verdict = if dev_pct.abs() < 6.0 { "ok" } else if dev_pct.abs() > 20.0 { "recalibrate" } else { "trim gain" };
    let mut out = json!({"white": white, "reference": reference, "deviation_pct": (dev_pct * 10.0).round() / 10.0, "verdict": verdict});
    if trim && verdict != "ok" {
        let g = profiles::get_settings(dev).and_then(|s| s.uvc.get("gain").copied()).unwrap_or(44);
        let new = (g as f64 * reference / white.max(1.0)).clamp(0.0, 128.0).round() as i64;
        set_control(dev, "uvc", "gain", new)?; // saves it into the profile
        out["gain"] = json!([g, new]);
    }
    Ok(out)
}

/// A stored camera frame (BGR), warped to Autodarts' 1000x1000 board view when its calibration is known.
pub fn board_view(dev: &str, path: &Path) -> Option<Mat> {
    let img = imgcodecs::imread(&path.to_string_lossy().to_string(), imgcodecs::IMREAD_COLOR).ok()?;
    if img.empty() {
        return None;
    }
    match board::load_homography(dev, img.cols() as u32, img.rows() as u32, None) {
        Some(h) => board::warp(&img, &h, imgproc::INTER_CUBIC).ok(),
        None => Some(img),
    }
}
