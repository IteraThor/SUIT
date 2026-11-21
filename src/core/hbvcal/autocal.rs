//! Automatic calibration of one camera for a new setup (needs exclusive access: stop Autodarts first).
//!
//! Strategy for the HBV OV2710 (all measured on a 3-camera HBV rig):
//!   * Exposure is sensor-referred: the OV2710 output saturates at a fixed level (~145 DN at digital gain 0).
//!     The best picture uses the longest exposure the frame rate allows with analog gain 1x, as long as the
//!     white sisal stays at ~92 % of that ceiling. Brightness is then raised with the ISP's digital gain
//!     (UVC `gain`) until the white segments hit a common target -> all cameras end up matched.
//!   * Only if the scene is too dark at the longest exposure is sensor analog gain added.
//!   * Flicker: if a short exposure shows frame-to-frame brightness changes, exposure is quantised to the mains
//!     half-period (10 ms for 50 Hz, 8.33 ms for 60 Hz; the better one is detected).
//!   * MJPEG Q is chosen by a bandwidth check over all cameras (calibrate_all).
//!   * Measuring runs at MEASURE_Q, never at the Q the camera happens to hold.
//!
//! Other models (calibrate_generic, e.g. HBV OV9732, which has no UVC gain): the longest exposure the frame rate
//! allows (quantised against flicker) at gain 1x, shortened while the white sisal is above the target; then the
//! model's gain (sensor analog gain, models.rs) raises the white level to the common target. Without the
//! extension unit (unconfirmed model or unknown firmware) only the exposure is set. The MJPEG Q is only chosen
//! where the firmware build has it mapped; elsewhere the camera keeps its own.
use super::capture::{burst, luma, START_DISCARD};
use super::models::{self, Gain, Model};
use super::settings::{apply_prestream, apply_streaming, frame_budget_kb, q_above, q_addr, q_levels, snapshot, Settings};
use super::v4l2::Camera;
use super::xu::Xu;
use super::{board, profiles};
use opencv::prelude::*;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::io;
use std::sync::{Arc, Mutex};

/// UVC exposure_time_absolute unit of the OV2710 (measured, not the 100 µs of the spec); see models.rs
pub const EXPOSURE_UNIT_S: f64 = 50e-6;
pub const WHITE_FRACTION: f64 = 0.92;
pub const WHITE_TARGET: f64 = 232.0;
pub const TONE: [(&str, i64); 9] = [
    ("sharpness", 10),
    ("gamma", 250),
    ("contrast", 34),
    ("brightness", 0),
    ("saturation", 68),
    ("hue", 0),
    ("white_balance_automatic", 1),
    ("backlight_compensation", 0),
    ("exposure_dynamic_framerate", 0),
];
pub const SHARPEN: i64 = 30;
pub const DEFAULT_Q: i64 = 5;
pub const MEASURE_Q: i64 = 8;
pub const FLICKER_CV: f64 = 0.03;
pub const LINEAR_GAMMA: i64 = 100;

pub type Log<'a> = &'a (dyn Fn(&str) + Sync);

#[derive(Debug)]
pub enum CalError {
    Io(io::Error),
    /// The cameras only stream cleanly with more compression than calibrate_all's q_max allows.
    CompressionLimit(String),
    /// The USB controller refused to reserve bandwidth for a camera stream (ENOSPC).
    UsbBandwidth(String),
    Other(String),
}

impl std::fmt::Display for CalError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CalError::Io(e) => write!(f, "{}", e),
            CalError::CompressionLimit(s) | CalError::UsbBandwidth(s) | CalError::Other(s) => write!(f, "{}", s),
        }
    }
}
impl From<io::Error> for CalError {
    fn from(e: io::Error) -> Self {
        CalError::Io(e)
    }
}
impl From<opencv::Error> for CalError {
    fn from(e: opencv::Error) -> Self {
        CalError::Other(e.to_string())
    }
}

pub fn usb_bandwidth_error(devices: &[String], refused: &[String], width: u32, height: u32) -> CalError {
    let ports: Vec<String> = devices.iter().map(|d| port_no(d)).collect();
    let hubs: std::collections::HashSet<String> =
        ports.iter().filter(|p| p.contains('.')).map(|p| p.rsplit_once('.').unwrap().0.to_string()).collect();
    let shared = if hubs.len() == 1 && ports.iter().all(|p| p.contains('.')) && ports.len() > 1 {
        format!(" They share one USB hub (ports {}).", ports.join(", "))
    } else {
        String::new()
    };
    let n = devices.len();
    CalError::UsbBandwidth(format!(
        "Not enough USB bandwidth to stream {} camera{} at once at {}x{}: port {} could not start.{} Plug the cameras into separate USB ports, ideally on different USB controllers, or choose a lower resolution, then optimize again.",
        n,
        if n > 1 { "s" } else { "" },
        width,
        height,
        refused.iter().map(|d| port_no(d)).collect::<Vec<_>>().join(", "),
        shared
    ))
}

/// OV2710 0x350B code for an analog gain >= 1 (coarse x2 steps in bits 4–6, fine n/16 in bits 0–3).
pub fn analog_code(gain: f64) -> i64 {
    let mut g = gain.clamp(1.0, 15.5);
    let mut coarse = 0;
    while g >= 2.0 && coarse < 3 {
        g /= 2.0;
        coarse += 1;
    }
    let fine = (((g - 1.0) * 16.0).round() as i64).min(15);
    (((1i64 << coarse) - 1) << 4) | fine
}

pub fn analog_gain(code: i64) -> f64 {
    let coarse = ((code >> 4) & 0x7).count_ones() as i32;
    2f64.powi(coarse) * (1.0 + (code & 0xF) as f64 / 16.0)
}

/// Start the stream at MJPEG Q `q`, stepping to coarser levels while the camera only sends broken frames.
/// Leaves the stream running; returns the Q in use. q None: the camera's own Q (no extension-unit command).
pub fn start_clean(cam: &mut Camera, xu: &Xu, q: Option<i64>, width: u32, height: u32, fps: u32, log: Log) -> io::Result<Option<i64>> {
    let Some(mut q) = q else {
        cam.configure(width, height, "MJPG", fps)?;
        cam.start(4)?;
        return match burst(cam, 3, START_DISCARD, 2.0, 30) {
            Ok(_) => Ok(None),
            Err(e) if e.kind() == io::ErrorKind::Other => {
                let _ = cam.stop();
                Err(io::Error::other(format!("{}: only broken frames at {}x{} ({})", cam.device, width, height, e)))
            }
            Err(e) => Err(e),
        };
    };
    loop {
        apply_prestream(xu, &Settings { mjpeg_q: Some(q), ..Default::default() }, width)?;
        cam.configure(width, height, "MJPG", fps)?;
        cam.start(4)?;
        match burst(cam, 3, START_DISCARD, 2.0, 30) {
            Ok(_) => return Ok(Some(q)),
            Err(e) if e.kind() == io::ErrorKind::Other => {
                let _ = cam.stop();
                let levels = q_levels(xu);
                if q >= levels.last().copied().unwrap_or(q) {
                    return Err(e);
                }
                log(&format!(
                    "  {}: only broken frames at Q{} (frames too big for the camera), trying Q{}",
                    cam.device,
                    q,
                    q_above(levels, q, 1)
                ));
                q = q_above(levels, q, 1);
            }
            Err(e) => return Err(e),
        }
    }
}

/// What the calibration sees right now, for a live view next to the progress (dev, phase, jpeg, seq).
#[derive(Default)]
pub struct Preview {
    state: Mutex<(String, String, Option<Arc<Vec<u8>>>, u64)>,
}

impl Preview {
    pub fn set(&self, dev: Option<&str>, phase: Option<&str>, jpeg: Option<&[u8]>) {
        let mut s = self.state.lock().unwrap();
        if let Some(d) = dev {
            s.0 = d.to_string();
        }
        if let Some(p) = phase {
            s.1 = p.to_string();
        }
        if let Some(j) = jpeg {
            s.2 = Some(Arc::new(j.to_vec()));
            s.3 += 1;
        }
    }
    pub fn clear(&self) {
        let mut s = self.state.lock().unwrap();
        s.0.clear();
        s.1.clear();
        s.2 = None;
    }
    pub fn get(&self) -> (String, String, Option<Arc<Vec<u8>>>, u64) {
        self.state.lock().unwrap().clone()
    }
}

pub static PREVIEW: once_cell::sync::Lazy<Preview> = once_cell::sync::Lazy::new(Preview::default);

#[derive(Debug, Clone)]
pub struct CalResult {
    pub device: String,
    pub settings: Settings,
    pub meta: Value,
}

/// Image-sized f32 buffer.
struct Img {
    data: Vec<f32>,
}

fn mat_to_vec(m: &Mat) -> opencv::Result<Vec<f32>> {
    let c = if m.is_continuous() { m.clone() } else { m.try_clone()? };
    Ok(c.data_typed::<f32>()?.to_vec())
}

pub fn percentile(v: &mut [f32], p: f64) -> f64 {
    if v.is_empty() {
        return 0.0;
    }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let pos = p / 100.0 * (v.len() - 1) as f64;
    let lo = pos.floor() as usize;
    let hi = pos.ceil() as usize;
    let frac = pos - lo as f64;
    v[lo] as f64 * (1.0 - frac) + v[hi] as f64 * frac
}

/// One camera, stream kept open, quick measurements of the board area.
pub struct Session<'a> {
    pub device: String,
    w: u32,
    h: u32,
    log: Log<'a>,
    pub cam: Camera,
    pub xu: Xu,
    pub homography: Option<Mat>,
    mask: Vec<bool>,
    pub q_before: Option<i64>,
    pub q: Option<i64>,
}

impl<'a> Session<'a> {
    pub fn open(device: &str, width: u32, height: u32, fps: u32, log: Log<'a>, hidden: bool) -> Result<Session<'a>, CalError> {
        let xu = Xu::open(device)?;
        let mut cam = Camera::open(device)?;
        let hm = board::load_homography(device, width, height, None);
        let mut q_before = None;
        let mut measure_q = None;
        if hidden {
            xu.unlock()?;
            if let Some(a) = q_addr(&xu, width)? {
                q_before = Some(xu.mem_read(a, 1)?[0] as i64);
                measure_q = Some(xu.build_info().map(|b| b.q_measure).unwrap_or(MEASURE_Q));
            }
        }
        let q = start_clean(&mut cam, &xu, measure_q, width, height, fps, log)?;
        let mask = match &hm {
            Some(h) => {
                let m = board::board_mask_image(h, height as i32, width as i32, board::BOARD_R)?;
                m.data_bytes()?.iter().map(|&b| b > 0).collect()
            }
            None => {
                // no Autodarts calibration yet: central ellipse
                let (w, h) = (width as f64, height as f64);
                (0..height)
                    .flat_map(|y| {
                        (0..width).map(move |x| {
                            ((x as f64 - w / 2.0) / (0.3 * w)).powi(2) + ((y as f64 - h / 2.0) / (0.4 * h)).powi(2) <= 1.0
                        })
                    })
                    .collect()
            }
        };
        Ok(Session { device: device.into(), w: width, h: height, log, cam, xu, homography: hm, mask, q_before, q })
    }

    pub fn apply(&mut self, s: &Settings, settle: usize) -> io::Result<()> {
        apply_streaming(&mut self.cam, &self.xu, s, 0)?;
        burst(&mut self.cam, 0, settle, 2.0, 0)?;
        Ok(())
    }

    fn frames(&mut self, n: usize) -> Result<Vec<Img>, CalError> {
        let b = burst(&mut self.cam, n, 0, 2.0, 0)?;
        PREVIEW.set(Some(&self.device), None, Some(&b.frames.last().unwrap().data));
        let mut out = Vec::new();
        for f in b.decode()? {
            out.push(Img { data: mat_to_vec(&luma(&f)?)? });
        }
        Ok(out)
    }

    fn mean_frame(fr: &[Img]) -> Vec<f32> {
        let n = fr.len() as f32;
        let mut m = vec![0f32; fr[0].data.len()];
        for f in fr {
            for (a, b) in m.iter_mut().zip(&f.data) {
                *a += *b;
            }
        }
        m.iter_mut().for_each(|v| *v /= n);
        m
    }

    fn masked(&self, v: &[f32]) -> Vec<f32> {
        v.iter().zip(&self.mask).filter(|(_, &m)| m).map(|(v, _)| *v).collect()
    }

    /// Level of the white segments: 97th percentile of board-area luma.
    pub fn white_level(&mut self, n: usize) -> Result<f64, CalError> {
        let fr = self.frames(n)?;
        let m = Self::mean_frame(&fr);
        Ok(percentile(&mut self.masked(&m), 97.0))
    }

    pub fn stats(&mut self, n: usize) -> Result<BTreeMap<String, f64>, CalError> {
        let fr = self.frames(n)?;
        let mean = Self::mean_frame(&fr);
        let mut m = self.masked(&mean);
        let avg = m.iter().map(|&v| v as f64).sum::<f64>() / m.len().max(1) as f64;
        // temporal noise: per-pixel std over frames (ddof=1), median over the board area
        let k = fr.len() as f32;
        let mut tn: Vec<f32> = Vec::with_capacity(m.len());
        for (i, &mk) in self.mask.iter().enumerate() {
            if !mk {
                continue;
            }
            let mu = mean[i];
            let var: f32 = fr.iter().map(|f| (f.data[i] - mu).powi(2)).sum::<f32>() / (k - 1.0).max(1.0);
            tn.push(var.sqrt());
        }
        let mut out = BTreeMap::new();
        out.insert("white".into(), percentile(&mut m, 97.0));
        out.insert("black".into(), percentile(&mut m, 3.0));
        out.insert("mean".into(), avg);
        out.insert("tnoise".into(), percentile(&mut tn, 50.0));
        Ok(out)
    }

    /// Mean brightness of each of `n` frames.
    fn frame_means(&mut self, n: usize) -> Result<Vec<f64>, CalError> {
        Ok(self
            .frames(n)?
            .iter()
            .map(|f| f.data.iter().map(|&v| v as f64).sum::<f64>() / f.data.len() as f64)
            .collect())
    }
}

impl Drop for Session<'_> {
    fn drop(&mut self) {
        let _ = self.cam.stop();
        let _ = (self.w, self.h, self.log);
    }
}

pub fn exposure_max(fps: u32) -> i64 {
    (0.98 / fps as f64 / EXPOSURE_UNIT_S) as i64
}

/// May the calibration use this camera's extension unit (confirmed model, verified firmware build)?
pub fn hidden_for(device: &str) -> bool {
    profiles::hidden_ok(device) && Xu::open(device).map(|x| x.build_info().is_ok()).unwrap_or(false)
}

/// Does the calibration pick an MJPEG Q for this camera at this width?
pub fn q_capable(device: &str, width: u32) -> bool {
    hidden_for(device) && Xu::open(device).ok().and_then(|x| q_addr(&x, width).ok().flatten()).is_some()
}

fn uvc(pairs: &[(&str, i64)]) -> Settings {
    Settings { uvc: pairs.iter().map(|(k, v)| (k.to_string(), *v)).collect(), ..Default::default() }
}

fn uvc_sensor(pairs: &[(&str, i64)], sensor: &[(u32, i64)]) -> Settings {
    let mut s = uvc(pairs);
    s.sensor = sensor.iter().cloned().collect();
    s
}

/// Frame-mean variation at 1 ms exposure; if flickering, pick the mains period that cancels it.
fn detect_flicker(ses: &mut Session, base: &Settings) -> Result<Value, CalError> {
    let mut cv_at = |ses: &mut Session, exp: i64, gain: i64| -> Result<f64, CalError> {
        ses.apply(&base.merged(&uvc(&[("exposure_time_absolute", exp), ("gain", gain)])), 4)?;
        let fm = ses.frame_means(45)?;
        let mu = fm.iter().sum::<f64>() / fm.len() as f64;
        let sd = (fm.iter().map(|v| (v - mu).powi(2)).sum::<f64>() / fm.len() as f64).sqrt();
        Ok(sd / mu.max(1e-3))
    };
    let cv = cv_at(ses, 20, 128)?;
    let round4 = |v: f64| (v * 10000.0).round() / 10000.0;
    let mut res = json!({"flicker_cv_1ms": round4(cv), "flicker": cv > FLICKER_CV, "step": 1});
    if cv > FLICKER_CV {
        let cv50 = cv_at(ses, 200, 64)?;
        let cv60 = cv_at(ses, 167, 64)?;
        res["cv_10ms"] = json!(round4(cv50));
        res["cv_8ms"] = json!(round4(cv60));
        let hz = if cv50 <= cv60 { 50 } else { 60 };
        res["mains_hz"] = json!(hz);
        res["step"] = json!(if hz == 50 { 200 } else { 167 });
    }
    Ok(res)
}

/// `previous`: the camera's state before the optimization, if it already holds an earlier attempt's settings.
pub fn calibrate(
    device: &str,
    fps: u32,
    width: u32,
    height: u32,
    q: Option<i64>,
    log: Log,
    previous: Option<&Settings>,
) -> Result<CalResult, CalError> {
    let t0 = std::time::Instant::now();
    let model = models::of_device(device);
    let hidden = hidden_for(device);
    if !(hidden && model.map(|m| m.gain == Gain::Uvc).unwrap_or(false)) {
        return calibrate_generic(device, fps, width, height, q, log, previous, model, hidden);
    }
    let q = q.unwrap_or(DEFAULT_Q);
    let mut base_pairs = vec![("auto_exposure", 1), ("power_line_frequency", 1), ("gain", 0)];
    base_pairs.extend_from_slice(&TONE);
    let base = uvc_sensor(&base_pairs, &[(0x350B, 0)]);
    let mut ses = Session::open(device, width, height, fps, log, true)?;
    let before = match previous {
        None => {
            let mut b = snapshot(&ses.cam, Some(&ses.xu), width);
            b.mjpeg_q = ses.q_before;
            b
        }
        Some(p) => p.clone(),
    };
    PREVIEW.set(Some(device), Some("Checking the light for flicker (short exposure, grainy on purpose)"), None);
    ses.apply(&base.merged(&uvc(&[("exposure_time_absolute", 300)])), 8)?;
    let fl = detect_flicker(&mut ses, &base)?;
    log(&format!("  flicker: {}", fl));
    let emax = exposure_max(fps);
    let step = fl["step"].as_i64().unwrap_or(1);
    let quant = |e: i64| -> i64 {
        let e = e.min(emax);
        if step > 1 { step.max((e / step) * step) } else { e.max(2) }
    };
    // Exposure search in the (near-)linear domain: gamma at its minimum, no digital gain.
    let lin = base.merged(&uvc(&[("gamma", LINEAR_GAMMA)]));
    // sensor ceiling: deliberately saturate (analog 2x, longest exposure)
    PREVIEW.set(None, Some("Measuring the sensor limit (overexposed on purpose)"), None);
    ses.apply(&lin.merged(&uvc_sensor(&[("exposure_time_absolute", emax)], &[(0x350B, 0x10)])), 8)?;
    let ceiling = ses.white_level(6)?;
    let target = WHITE_FRACTION * ceiling;
    PREVIEW.set(None, Some("Searching the exposure (flat and dark on purpose)"), None);
    ses.apply(&lin.merged(&uvc(&[("exposure_time_absolute", quant(emax))])), 8)?;
    let w_max = ses.white_level(6)?;
    let (exp, acode) = if w_max <= target {
        // dark scene: longest exposure + analog gain
        (quant(emax), analog_code(target / w_max.max(1e-3)))
    } else {
        // bisection: largest exposure with white <= target
        let unit = if step > 1 { step } else { 10 };
        let (mut lo, mut hi) = (1i64, quant(emax) / unit);
        while hi - lo > 1 {
            let mid = (lo + hi) / 2;
            ses.apply(&lin.merged(&uvc(&[("exposure_time_absolute", mid * unit)])), 5)?;
            if ses.white_level(6)? <= target {
                lo = mid;
            } else {
                hi = mid;
            }
        }
        (lo * unit, 0)
    };
    let sharp = TONE[0].1 + SHARPEN;
    let mut s = base.merged(&uvc_sensor(&[("exposure_time_absolute", exp), ("sharpness", sharp)], &[(0x350B, acode)]));
    ses.apply(&lin.merged(&uvc_sensor(&[("exposure_time_absolute", exp)], &[(0x350B, acode)])), 8)?;
    let w_sensor = ses.white_level(6)?;
    log(&format!(
        "  ceiling={:.1} DN (linear)  white@max exposure={:.1}  -> exposure {} ({:.1} ms), analog {:.2}x -> white {:.1} DN ({:.0} % of ceiling)",
        ceiling,
        w_max,
        exp,
        exp as f64 * EXPOSURE_UNIT_S * 1000.0,
        analog_gain(acode),
        w_sensor,
        100.0 * w_sensor / ceiling
    ));
    // digital gain: bisection to the common white target
    PREVIEW.set(None, Some("Matching the brightness"), None);
    let (mut lo, mut hi) = (0i64, 128i64);
    for _ in 0..7 {
        let mid = (lo + hi) / 2;
        ses.apply(&s.merged(&uvc(&[("gain", mid)])), 6)?;
        if ses.white_level(6)? < WHITE_TARGET {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    let gain = lo;
    s = s.merged(&Settings { mjpeg_q: Some(q), ..uvc(&[("gain", gain)]) });
    ses.apply(&s, 8)?;
    PREVIEW.set(None, Some("Calibrated"), None);
    let st = ses.stats(10)?;
    log(&format!(
        "  digital gain {} -> white {:.1} black {:.1} mean {:.1} tnoise {:.2}",
        gain, st["white"], st["black"], st["mean"], st["tnoise"]
    ));
    let has_h = ses.homography.is_some();
    drop(ses);
    let r1 = |v: f64| (v * 10.0).round() / 10.0;
    let r2 = |v: f64| (v * 100.0).round() / 100.0;
    let meta = json!({
        "fps": fps,
        "resolution": format!("{}x{}", width, height),
        "ceiling": r1(ceiling),
        "white_sensor": r1(w_sensor),
        "reference": st.iter().map(|(k, v)| (k.clone(), json!(r2(*v)))).collect::<serde_json::Map<_, _>>(),
        "flicker": fl,
        "calibrated": chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string(),
        "seconds": r1(t0.elapsed().as_secs_f64()),
        "previous": before.to_json(),
        "board_homography": has_h,
        "model": models::OV2710.key,
        "strategy": "exposure, digital gain",
    });
    Ok(CalResult { device: device.into(), settings: s, meta })
}

/// Settings for this camera: only controls it has, each clamped to its range.
fn fit_controls(cam: &Camera, pairs: &[(&str, i64)]) -> Settings {
    let mut s = Settings::default();
    for (k, v) in pairs {
        if let Ok(q) = cam.query(k) {
            s.uvc.insert(k.to_string(), (*v).clamp(q.min as i64, q.max as i64));
        }
    }
    s
}

/// Exposure first, then the model's gain (see the module doc). `model` None: standard controls only.
#[allow(clippy::too_many_arguments)]
fn calibrate_generic(
    device: &str,
    fps: u32,
    width: u32,
    height: u32,
    q: Option<i64>,
    log: Log,
    previous: Option<&Settings>,
    model: Option<&'static Model>,
    hidden: bool,
) -> Result<CalResult, CalError> {
    let t0 = std::time::Instant::now();
    let unit_s = model.map(|m| m.exposure_unit(height)).unwrap_or(100e-6);
    let units = |s: f64| (s / unit_s).round() as i64;
    let mut ses = Session::open(device, width, height, fps, log, hidden)?;
    let q = if ses.q.is_some() { q } else { None };
    let before = match previous {
        None => {
            let mut b = snapshot(&ses.cam, if hidden { Some(&ses.xu) } else { None }, width);
            if ses.q_before.is_some() {
                b.mjpeg_q = ses.q_before;
            }
            b
        }
        Some(p) => p.clone(),
    };
    // the brightness knob after the exposure: sensor gain (extension unit) or the UVC gain control
    let sensor_gain = match model.map(|m| m.gain) {
        Some(Gain::Sensor { reg, unity, max }) if hidden => Some((reg, unity, max)),
        _ => None,
    };
    let uvc_gain = if sensor_gain.is_none() { ses.cam.query("gain").ok() } else { None };
    let gain_at = |rel: f64| -> Settings {
        if let Some((reg, unity, max)) = sensor_gain {
            let mut s = Settings::default();
            s.sensor.insert(reg, ((unity as f64 * rel).round() as i64).clamp(unity, max));
            s
        } else if let (Some(g), Some(Gain::UvcScaled { unity, per_x })) = (uvc_gain, model.map(|m| m.gain)) {
            uvc(&[("gain", ((unity as f64 + per_x * (rel - 1.0)).round() as i64).clamp(unity.max(g.min as i64), g.max as i64))])
        } else if let Some(g) = uvc_gain {
            uvc(&[("gain", (g.min as f64 + (g.max - g.min) as f64 * (rel - 1.0) / 15.0).round() as i64)])
        } else {
            Settings::default()
        }
    };
    let mut pairs = vec![("auto_exposure", 1), ("power_line_frequency", 1)];
    pairs.extend_from_slice(&TONE);
    let sharp = TONE[0].1 + SHARPEN;
    pairs.push(("sharpness", sharp));
    let base = fit_controls(&ses.cam, &pairs).merged(&gain_at(1.0));
    let exp_ctrl = ses.cam.query("exposure_time_absolute")?;
    let (emin, emax) = (exp_ctrl.min.max(1) as i64, (units(0.98 / fps as f64)).min(exp_ctrl.max as i64));
    let exp = |e: i64| uvc(&[("exposure_time_absolute", e.clamp(emin, emax))]);

    PREVIEW.set(Some(device), Some("Checking the light for flicker (short exposure, grainy on purpose)"), None);
    ses.apply(&base.merged(&exp(units(0.015))), 8)?;
    let cv_at = |ses: &mut Session, e: i64, g: &Settings| -> Result<f64, CalError> {
        ses.apply(&base.merged(&exp(e)).merged(g), 4)?;
        let fm = ses.frame_means(45)?;
        let mu = fm.iter().sum::<f64>() / fm.len() as f64;
        let sd = (fm.iter().map(|v| (v - mu).powi(2)).sum::<f64>() / fm.len() as f64).sqrt();
        Ok(sd / mu.max(1e-3))
    };
    let round4 = |v: f64| (v * 10000.0).round() / 10000.0;
    let cv = cv_at(&mut ses, units(0.001), &gain_at(8.0))?;
    let mut fl = json!({"flicker_cv_1ms": round4(cv), "flicker": cv > FLICKER_CV, "step": 1});
    if cv > FLICKER_CV {
        let cv50 = cv_at(&mut ses, units(0.01), &gain_at(4.0))?;
        let cv60 = cv_at(&mut ses, units(1.0 / 120.0), &gain_at(4.0))?;
        fl["cv_10ms"] = json!(round4(cv50));
        fl["cv_8ms"] = json!(round4(cv60));
        let hz = if cv50 <= cv60 { 50 } else { 60 };
        fl["mains_hz"] = json!(hz);
        fl["step"] = json!(units(1.0 / (2.0 * hz as f64)));
    }
    log(&format!("  flicker: {}", fl));
    let step = fl["step"].as_i64().unwrap_or(1).max(1);
    let quant = |e: i64| -> i64 {
        let e = e.min(emax);
        if step > 1 { step.max((e / step) * step).min(emax.max(step)) } else { e.max(emin) }
    };
    // exposure: the longest (quantised) one that keeps the white at or below the target at gain 1x
    PREVIEW.set(None, Some("Searching the exposure"), None);
    ses.apply(&base.merged(&exp(quant(emax))), 8)?;
    let w_max = ses.white_level(6)?;
    let e = if w_max <= WHITE_TARGET {
        quant(emax)
    } else {
        let unit = if step > 1 { step } else { units(0.0002).max(1) };
        let (mut lo, mut hi) = (1i64, (quant(emax) / unit).max(1));
        while hi - lo > 1 {
            let mid = (lo + hi) / 2;
            ses.apply(&base.merged(&exp(mid * unit)), 5)?;
            if ses.white_level(6)? <= WHITE_TARGET {
                lo = mid;
            } else {
                hi = mid;
            }
        }
        (lo * unit).clamp(emin, emax)
    };
    let mut s = base.merged(&exp(e));
    ses.apply(&s, 8)?;
    let w_exp = ses.white_level(6)?;
    // gain: bisection to the common white target
    let mut rel = 1.0;
    let knob = if let Some((_, unity, max)) = sensor_gain {
        Some((1.0, max as f64 / unity as f64))
    } else {
        uvc_gain.map(|g| match model.map(|m| m.gain) {
            Some(Gain::UvcScaled { unity, per_x }) => (1.0, 1.0 + (g.max as f64 - unity as f64).max(0.0) / per_x),
            _ => (1.0, 16.0),
        })
    };
    if let Some((mut lo, mut hi)) = knob {
        PREVIEW.set(None, Some("Matching the brightness"), None);
        let g_hi = gain_at(hi);
        ses.apply(&s.merged(&g_hi), 6)?;
        if ses.white_level(6)? < WHITE_TARGET {
            lo = hi; // too dark even at the highest gain: take it
        } else {
            for _ in 0..8 {
                let mid = (lo + hi) / 2.0;
                ses.apply(&s.merged(&gain_at(mid)), 6)?;
                if ses.white_level(6)? < WHITE_TARGET {
                    lo = mid;
                } else {
                    hi = mid;
                }
            }
        }
        rel = lo;
        s = s.merged(&gain_at(rel));
    }
    if let Some(q) = q {
        s.mjpeg_q = Some(q);
    }
    ses.apply(&s, 8)?;
    PREVIEW.set(None, Some("Calibrated"), None);
    let st = ses.stats(10)?;
    let gain_label = match sensor_gain {
        Some((reg, _, _)) => format!("sensor gain {:.2}x (0x{:04X}={:#04x})", rel, reg, s.sensor.get(&reg).copied().unwrap_or(0)),
        None if uvc_gain.is_some() => format!("gain {}", s.uvc.get("gain").copied().unwrap_or(0)),
        None => "no gain control".into(),
    };
    log(&format!(
        "  exposure {} ({:.1} ms), white {:.1} at gain 1x -> {} -> white {:.1} black {:.1} mean {:.1} tnoise {:.2}",
        e,
        e as f64 * unit_s * 1000.0,
        w_exp,
        gain_label,
        st["white"],
        st["black"],
        st["mean"],
        st["tnoise"]
    ));
    let has_h = ses.homography.is_some();
    drop(ses);
    let r1 = |v: f64| (v * 10.0).round() / 10.0;
    let r2 = |v: f64| (v * 100.0).round() / 100.0;
    let mut meta = json!({
        "fps": fps,
        "resolution": format!("{}x{}", width, height),
        "ceiling": null,
        "white_sensor": r1(w_exp),
        "reference": st.iter().map(|(k, v)| (k.clone(), json!(r2(*v)))).collect::<serde_json::Map<_, _>>(),
        "flicker": fl,
        "calibrated": chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string(),
        "seconds": r1(t0.elapsed().as_secs_f64()),
        "previous": before.to_json(),
        "board_homography": has_h,
        "model": model.map(|m| m.key),
        "strategy": if sensor_gain.is_some() { "exposure, sensor gain" } else if uvc_gain.is_some() { "exposure, gain" } else { "exposure only" },
        "gain": r2(rel),
    });
    if st["white"] < WHITE_TARGET - 15.0 {
        meta["white_short"] = json!({"white": r1(st["white"]), "target": WHITE_TARGET});
        log(&format!("  the board stays darker than the target ({:.0} of {:.0}): more light would help", st["white"], WHITE_TARGET));
    }
    Ok(CalResult { device: device.into(), settings: s, meta })
}

pub struct CalibrateAllOpts<'a> {
    pub fps: u32,
    pub width: u32,
    pub height: u32,
    pub q_candidates: Vec<i64>,
    pub check_s: f64,
    pub confirm_s: f64,
    pub q_margin: usize,
    pub q_max: Option<i64>,
    pub save: bool,
    pub log: Log<'a>,
    pub step: &'a (dyn Fn(&str) + Sync),
    pub skipped: &'a (dyn Fn(&str) + Sync),
}

/// Calibrate every camera, then pick the lowest MJPEG Q at which all cameras stream together cleanly
/// (short check per level, lowest first, then a longer confirmation run). The saved level is q_margin levels
/// above the first clean one. `previous` is filled in on the first run and handed to the next one.
/// Q steps of a camera's firmware build, None where Q is not mapped or the camera can't be asked.
fn levels_of(dev: &str) -> Option<&'static [i64]> {
    let l = q_levels(&Xu::open(dev).ok()?);
    (!l.is_empty()).then_some(l)
}

/// Q a calibration result starts with (replaced by the bandwidth check): DEFAULT_Q or the next step of the
/// camera's scale at or above it.
fn first_q(dev: &str) -> i64 {
    levels_of(dev).and_then(|l| l.iter().copied().find(|&q| q >= DEFAULT_Q).or(l.last().copied())).unwrap_or(DEFAULT_Q)
}

pub fn calibrate_all(
    devices: &[String],
    o: &CalibrateAllOpts,
    previous: &mut BTreeMap<String, Settings>,
) -> Result<BTreeMap<String, CalResult>, CalError> {
    let log = o.log;
    let mut results: BTreeMap<String, CalResult> = BTreeMap::new();
    for d in devices {
        (o.step)(&format!("Calibrating exposure and gain: port {}", port_no(d)));
        log(&format!("calibrating {}", d));
        let q0 = if q_capable(d, o.width) { Some(first_q(d)) } else { None };
        match calibrate(d, o.fps, o.width, o.height, q0, log, previous.get(d)) {
            Ok(r) => {
                previous.entry(d.clone()).or_insert_with(|| Settings::from_json(&r.meta["previous"]));
                results.insert(d.clone(), r);
            }
            Err(CalError::Io(e)) if e.raw_os_error() == Some(libc::ENOSPC) => {
                return Err(usb_bandwidth_error(devices, &[d.clone()], o.width, o.height));
            }
            Err(e) => return Err(e),
        }
    }
    // one Q for all cameras: only cameras sharing the first Q-capable camera's scale take part
    let levels: Vec<i64> = if o.q_candidates.is_empty() {
        results.keys().filter(|d| q_capable(d, o.width)).find_map(|d| levels_of(d)).map(|l| l.to_vec()).unwrap_or_default()
    } else {
        o.q_candidates.clone()
    };
    let with_q: Vec<String> = results
        .keys()
        .filter(|d| q_capable(d, o.width) && levels_of(d).map(|l| l == levels.as_slice()).unwrap_or(false))
        .cloned()
        .collect();
    if with_q.is_empty() {
        // no camera has a mapped Q: one check that they stream together cleanly with their own quality
        PREVIEW.set(None, Some("Testing all cameras together · preview paused so the test is not disturbed"), None);
        (o.step)(&format!("Testing all cameras together ({:.0} s)", o.check_s));
        let (ok, why) = check_bandwidth(&results, None, o.width, o.height, o.fps, o.check_s, log)?;
        if !ok {
            return Err(CalError::Other(format!("the cameras do not stream cleanly together at {}x{}: {}", o.width, o.height, why)));
        }
        if o.save {
            for (d, r) in &results {
                profiles::put(d, &r.settings, Some(r.meta.clone()));
            }
        }
        log("done (cameras keep their own JPEG quality)");
        return Ok(results);
    }
    let candidates: Vec<i64> = match o.q_max {
        Some(qm) => levels
            .iter()
            .enumerate()
            .filter(|(i, _)| levels[(*i + o.q_margin).min(levels.len() - 1)] <= qm)
            .map(|(_, q)| *q)
            .collect(),
        None => levels.clone(),
    };
    let mut chosen = None;
    PREVIEW.set(None, Some("Testing all cameras together · preview paused so the test is not disturbed"), None);
    for (i, &q) in candidates.iter().enumerate() {
        let which = if i == 0 { "lowest" } else if Some(&q) == levels.last() { "stock" } else { "next" };
        (o.step)(&format!("Testing compression stability: level {} ({}, {:.0} s)", q, which, o.check_s));
        log(&format!(
            "bandwidth check: {} cameras at Q{}, {}x{}, {} fps, {:.0} s",
            devices.len(),
            q,
            o.width,
            o.height,
            o.fps,
            o.check_s
        ));
        let (mut ok, mut why) = check_bandwidth(&results, Some(q), o.width, o.height, o.fps, o.check_s, log)?;
        if ok {
            (o.step)(&format!("Confirming level {} ({:.0} s)", q, o.confirm_s));
            log(&format!("  Q{} clean; confirming for {:.0} s", q, o.confirm_s));
            let r = check_bandwidth(&results, Some(q), o.width, o.height, o.fps, o.confirm_s, log)?;
            ok = r.0;
            why = r.1;
        }
        if ok {
            chosen = Some(q);
            break;
        }
        (o.skipped)(&format!("skipped, trying more compression ({})", why));
        log(&format!("  Q{} not stable: {}", q, why));
    }
    let clean = match (chosen, o.q_max) {
        (Some(c), _) => c,
        (None, Some(qm)) => {
            return Err(CalError::CompressionLimit(format!(
                "no MJPEG quality level up to {} streamed cleanly on all cameras at {}x{}",
                qm, o.width, o.height
            )))
        }
        (None, None) => return Err(CalError::Other("no MJPEG quality level streamed cleanly on all cameras".into())),
    };
    let idx = levels.iter().position(|&l| l == clean).unwrap_or(0);
    let chosen = levels[(idx + o.q_margin).min(levels.len() - 1)];
    if chosen != clean {
        (o.step)(&format!("Safety margin: level {}", chosen));
        log(&format!("  Q{} was the lowest clean level; using Q{} as margin for busier scenes", clean, chosen));
    }
    for (d, r) in results.iter_mut() {
        if !with_q.contains(d) {
            // a Q-capable camera on another Q scale than the others: keeps its own quality
            r.settings.mjpeg_q = None;
            if o.save {
                profiles::put(d, &r.settings, Some(r.meta.clone()));
            }
            continue;
        }
        r.settings = r.settings.merged(&Settings { mjpeg_q: Some(chosen), ..Default::default() });
        r.meta["mjpeg_q"] = json!(chosen);
        if o.save {
            profiles::put(d, &r.settings, Some(r.meta.clone()));
        }
    }
    if o.save {
        log(&format!("done: Q{}; profiles saved to {}", chosen, profiles::store().display()));
    } else {
        log(&format!("done: Q{}", chosen));
    }
    Ok(results)
}

/// check_together, retried once if a camera got no USB bandwidth; UsbBandwidth error if it stays so.
fn check_bandwidth(
    results: &BTreeMap<String, CalResult>,
    q: Option<i64>,
    width: u32,
    height: u32,
    fps: u32,
    seconds: f64,
    log: Log,
) -> Result<(bool, String), CalError> {
    let mut refused = Vec::new();
    for attempt in 1..=2 {
        let (ok, why, r) = check_together(results, q, width, height, fps, seconds, log)?;
        if r.is_empty() {
            return Ok((ok, why));
        }
        log(&format!(
            "  no USB bandwidth for {}{}",
            r.iter().map(|d| port_no(d)).collect::<Vec<_>>().join(", "),
            if attempt == 1 { ": retrying once" } else { "" }
        ));
        refused = r;
        std::thread::sleep(std::time::Duration::from_secs(2));
    }
    let devs: Vec<String> = results.keys().cloned().collect();
    Err(usb_bandwidth_error(&devs, &refused, width, height))
}

/// '...-usb-0:2:1.0-video-index0' -> '2'.
pub fn port_no(device: &str) -> String {
    let port = profiles::port_key(device);
    match port.rsplit_once("usb-0:") {
        Some((_, rest)) => rest.split(':').next().unwrap_or("").to_string(),
        None => port,
    }
}

/// Stream all cameras at once, each with its own calibrated settings at MJPEG Q `q` (None: each camera's own).
/// (clean?, why not, cameras that got no USB bandwidth)
fn check_together(
    results: &BTreeMap<String, CalResult>,
    q: Option<i64>,
    width: u32,
    height: u32,
    fps: u32,
    seconds: f64,
    log: Log,
) -> Result<(bool, String, Vec<String>), CalError> {
    use super::multistream::{kernel_error_lines, run_all};
    let mut budgets = BTreeMap::new();
    for d in results.keys() {
        let b = if hidden_for(d) { frame_budget_kb(&Xu::open(d)?, height) } else { f64::INFINITY };
        budgets.insert(d.clone(), b);
    }
    let k0 = kernel_error_lines();
    let jobs: Vec<(String, Settings)> = results
        .iter()
        .map(|(d, r)| (d.clone(), r.settings.merged(&Settings { mjpeg_q: q, ..Default::default() })))
        .collect();
    let ws = run_all(&jobs, width, height, fps, seconds);
    let mut problems = Vec::new();
    for w in &ws {
        let port = format!("port {}", port_no(&w.dev));
        if let Some(st) = &w.stats {
            let ql = q.map(|q| format!("Q{} ", q)).unwrap_or_default();
            log(&format!("    {}: {}frames mean {:.0} KB, max {:.0} KB", port, ql, st.kb_mean, st.kb_max));
        }
        match (&w.error, &w.stats) {
            (Some(e), _) => problems.push(format!("{}: {}", port, e)),
            (None, None) => problems.push(format!("{}: no frames", port)),
            (None, Some(st)) => {
                let fps_bad = (st.fps - fps as f64).abs() > 0.2;
                if st.dropped > 0 || st.broken > 0 || st.gaps_over_1_5x > 0 || fps_bad {
                    let mut bad = Vec::new();
                    if fps_bad {
                        bad.push(format!("{:.1} fps", st.fps));
                    }
                    if st.dropped > 0 {
                        bad.push(format!("{} dropped", st.dropped));
                    }
                    if st.broken > 0 {
                        bad.push(format!("{} broken", st.broken));
                    }
                    if st.gaps_over_1_5x > 0 {
                        bad.push(format!("{} gaps", st.gaps_over_1_5x));
                    }
                    problems.push(format!("{}: {}", port, bad.join(", ")));
                } else if st.kb_max > budgets[&w.dev] {
                    problems.push(format!(
                        "{}: frames up to {:.0} KB, firmware budget {:.0} KB",
                        port, st.kb_max, budgets[&w.dev]
                    ));
                }
            }
        }
    }
    let kerr = kernel_error_lines().saturating_sub(k0);
    if kerr > 0 {
        problems.push(format!("{} USB/UVC kernel error(s)", kerr));
    }
    let refused: Vec<String> = ws.iter().filter(|w| w.errno == Some(libc::ENOSPC)).map(|w| w.dev.clone()).collect();
    Ok((problems.is_empty(), problems.join("; "), refused))
}
