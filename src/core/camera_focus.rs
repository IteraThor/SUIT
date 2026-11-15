//! Camera focus analysis for Autodarts: locates the board geometry and tracks relative peak sharpness
//! ("oscilloscope method") on the furthest double, T20 or a tapped point.
use super::board_detector::BoardDetector;
use super::cvx::{self, bgr};
use opencv::core::{self as cvc, Mat};
use opencv::prelude::*;
use opencv::{imgproc, videoio};
use std::sync::Mutex;
use std::time::Instant;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LightingState {
    TooDark,
    Optimal,
    Overexposed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FocusTrend {
    Sharpening,
    Stable,
    Overshot,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TargetMode {
    None,
    FurthestDouble,
    T20,
    Custom,
}

#[derive(Debug, Clone)]
pub struct FocusAnalysis {
    pub score: f64,
    pub raw_score: f64,
    pub peak_score: f64,
    pub peak_raw: f64,
    pub trend: FocusTrend,
    pub target_roi: (i32, i32, i32, i32),
    pub target_name: String,
    pub target_mode: TargetMode,
    pub bullseye: (i32, i32),
    pub focal_line_y: i32,
    pub lighting_state: LightingState,
    pub history: Vec<f64>,
    pub peak_history_idx: i64,
    pub fps: f64,
}

fn r1(v: f64) -> f64 {
    (v * 10.0).round() / 10.0
}

pub struct CameraFocusService {
    pub detector: BoardDetector,
    cap: Mutex<Option<videoio::VideoCapture>>,
    pub current_device: Option<String>,
    pub current_cam_uri: Option<String>,
    pub target_mode: TargetMode,
    pub target_name: String,
    pub custom_roi: Option<(i32, i32, i32, i32)>,
    pub locked_target_roi: Option<(i32, i32, i32, i32)>,
    pub last_bullseye: Option<(i32, i32)>,
    raw_history: Vec<f64>,
    norm_history: Vec<f64>,
    smoothed_raw: f64,
    baseline_min: f64,
    baseline_max: f64,
    peak_raw: f64,
    peak_norm: f64,
    peak_history_idx: i64,
    last_sample: Option<Instant>,
    sample_interval: f64,
    fps_tracker: Vec<Instant>,
    accum_zoom: Option<Mat>,
}

impl Default for CameraFocusService {
    fn default() -> Self {
        Self::new()
    }
}

impl CameraFocusService {
    pub fn new() -> Self {
        CameraFocusService {
            detector: BoardDetector::new(),
            cap: Mutex::new(None),
            current_device: None,
            current_cam_uri: None,
            target_mode: TargetMode::None,
            target_name: "Unselected".into(),
            custom_roi: None,
            locked_target_roi: None,
            last_bullseye: None,
            raw_history: Vec::new(),
            norm_history: Vec::new(),
            smoothed_raw: 0.0,
            baseline_min: f64::INFINITY,
            baseline_max: 0.0,
            peak_raw: 0.0,
            peak_norm: 0.0,
            peak_history_idx: -1,
            last_sample: None,
            sample_interval: 0.10,
            fps_tracker: Vec::new(),
            accum_zoom: None,
        }
    }

    /// Set active focus target (Furthest Double, T20, or custom tap at (x, y) of a frame (h, w)).
    pub fn set_target_mode(&mut self, mode: TargetMode, custom_point: Option<(i32, i32)>, frame_shape: (i32, i32)) {
        self.target_mode = mode;
        self.reset_peak();
        match mode {
            TargetMode::FurthestDouble | TargetMode::T20 => {
                self.target_name = if mode == TargetMode::T20 { "Triple 20 (T20)" } else { "Furthest Double" }.into();
                self.custom_roi = None;
                self.locked_target_roi = None;
                let d = self.current_device.clone();
                self.detector.unlock_target(d.as_deref());
            }
            TargetMode::Custom => {
                if let Some((cx, cy)) = custom_point {
                    let (h, w) = frame_shape;
                    let s = 90;
                    let x = (cx - s / 2).min(w - s).max(0);
                    let y = (cy - s / 2).min(h - s).max(0);
                    self.custom_roi = Some((x, y, s, s));
                    self.locked_target_roi = self.custom_roi;
                    self.target_name = "Custom Point".into();
                }
            }
            TargetMode::None => {}
        }
    }

    /// Open the V4L2 camera for capture with retry backoff.
    pub fn start_camera(&mut self, dev_path: &str, width: i32, height: i32, retries: u32, uri: Option<&str>) -> bool {
        self.stop_camera();
        self.reset_peak();
        self.target_mode = TargetMode::None;
        self.locked_target_roi = None;
        self.custom_roi = None;
        self.last_bullseye = None;
        let target = super::autodarts::parse_cam_device(dev_path);
        if target.is_empty() || !std::path::Path::new(&target).exists() {
            log::warn!("Camera device path does not exist: {} (resolved from {})", target, dev_path);
            return false;
        }
        self.current_cam_uri = uri
            .map(String::from)
            .or_else(|| if dev_path.contains("location=") || dev_path.contains("serial=") { Some(dev_path.into()) } else { None });
        for attempt in 0..retries {
            log::info!("Opening camera device {} ({}x{}) [attempt {}/{}]", target, width, height, attempt + 1, retries);
            let open = || -> opencv::Result<Option<videoio::VideoCapture>> {
                let mut cap = videoio::VideoCapture::from_file(&target, videoio::CAP_V4L2)?;
                if !cap.is_opened()? {
                    cap = videoio::VideoCapture::from_file_def(&target)?;
                }
                if !cap.is_opened()? {
                    if let Some(idx) = target.strip_prefix("/dev/video").and_then(|s| s.parse::<i32>().ok()) {
                        cap = videoio::VideoCapture::new(idx, videoio::CAP_V4L2)?;
                    }
                }
                if cap.is_opened()? {
                    cap.set(videoio::CAP_PROP_FOURCC, videoio::VideoWriter::fourcc('M', 'J', 'P', 'G')? as f64)?;
                    cap.set(videoio::CAP_PROP_FRAME_WIDTH, width as f64)?;
                    cap.set(videoio::CAP_PROP_FRAME_HEIGHT, height as f64)?;
                    return Ok(Some(cap));
                }
                Ok(None)
            };
            match open() {
                Ok(Some(cap)) => {
                    *self.cap.lock().unwrap() = Some(cap);
                    self.current_device = Some(target);
                    return true;
                }
                Ok(None) => {}
                Err(e) => log::error!("Exception while opening camera {}: {}", target, e),
            }
            if attempt + 1 < retries {
                std::thread::sleep(std::time::Duration::from_millis(300));
            }
        }
        log::error!("Failed to open camera device {} after {} attempts", target, retries);
        false
    }

    /// Read a single raw frame from the active camera.
    pub fn read_frame(&self) -> Option<Mat> {
        let mut g = self.cap.lock().unwrap();
        let cap = g.as_mut()?;
        let mut f = Mat::default();
        match cap.read(&mut f) {
            Ok(true) if !f.empty() => Some(f),
            Ok(_) => None,
            Err(e) => {
                log::error!("Error reading frame from camera {:?}: {}", self.current_device, e);
                None
            }
        }
    }

    pub fn is_open(&self) -> bool {
        self.cap.lock().unwrap().is_some()
    }

    /// Release hardware video capture locks.
    pub fn stop_camera(&mut self) {
        if let Some(mut c) = self.cap.lock().unwrap().take() {
            let _ = c.release();
        }
        self.current_device = None;
        self.current_cam_uri = None;
    }

    /// Reset the latched peak score and oscilloscope tracking state.
    pub fn reset_peak(&mut self) {
        self.raw_history.clear();
        self.norm_history.clear();
        self.smoothed_raw = 0.0;
        self.baseline_min = f64::INFINITY;
        self.baseline_max = 0.0;
        self.peak_raw = 0.0;
        self.peak_norm = 0.0;
        self.peak_history_idx = -1;
        self.last_sample = None;
        self.accum_zoom = None;
    }

    /// Reset locked target ROI so the target can be re-selected.
    pub fn unlock_target(&mut self) {
        self.locked_target_roi = None;
        self.custom_roi = None;
        self.accum_zoom = None;
        let d = self.current_device.clone();
        self.detector.unlock_target(d.as_deref());
    }

    /// 2.5x magnified crop on the target ROI with temporal smoothing and optional focus peaking (BGR).
    pub fn process_zoom_crop(&mut self, frame: &Mat, roi: (i32, i32, i32, i32), zoom: f64, out: (i32, i32), peaking: bool) -> Option<Mat> {
        (|| -> cvx::R<Option<Mat>> {
            let (fh, fw) = (frame.rows(), frame.cols());
            let (tx, ty, tw, th) = roi;
            let (cx, cy) = if tw > 0 && th > 0 { (tx + tw / 2, ty + th / 2) } else { (fw / 2, fh / 2) };
            let cw = ((out.0 as f64 / zoom) as i32).max(20);
            let ch = ((out.1 as f64 / zoom) as i32).max(20);
            let (x1, y1) = (cx - cw / 2, cy - ch / 2);
            let (pt, pb, pl, pr) = ((-y1).max(0), (y1 + ch - fh).max(0), (-x1).max(0), (x1 + cw - fw).max(0));
            let crop = if pt + pb + pl + pr > 0 {
                let mut p = Mat::default();
                cvc::copy_make_border(frame, &mut p, pt, pb, pl, pr, cvc::BORDER_CONSTANT, bgr(10., 12., 15.))?;
                cvx::crop(&p, x1 + pl, y1 + pt, cw, ch)?
            } else {
                cvx::crop(frame, x1, y1, cw, ch)?
            };
            if crop.empty() {
                return Ok(None);
            }
            let mut cf = Mat::default();
            crop.convert_to_def(&mut cf, cvc::CV_32F)?;
            let acc = match &self.accum_zoom {
                Some(a) if a.rows() == cf.rows() && a.cols() == cf.cols() && a.typ() == cf.typ() => cvx::add_weighted(&cf, 0.40, a, 0.60)?,
                _ => cf,
            };
            let clean = cvx::to_u8(&acc, 1.0, 0.0)?;
            self.accum_zoom = Some(acc);
            let mut render = clean.try_clone()?;
            if peaking {
                let mag = cvx::sobel_mag(&clean)?;
                let mut pk = Mat::default();
                imgproc::threshold(&mag, &mut pk, 200.0, 255.0, imgproc::THRESH_BINARY)?;
                let pk = cvx::to_u8(&pk, 1.0, 0.0)?;
                if cvx::count(&pk) > 0 {
                    let g = Mat::new_rows_cols_with_default(clean.rows(), clean.cols(), clean.typ(), bgr(40., 255., 60.))?;
                    cvx::put_masked(&cvx::add_weighted(&clean, 0.35, &g, 0.65)?, &mut render, &pk)?;
                }
            }
            Ok(Some(cvx::resize(&render, out.0, out.1, imgproc::INTER_LINEAR)?))
        })()
        .ok()
        .flatten()
    }

    pub fn locate_bullseye(&mut self, image: &Mat) -> (i32, i32) {
        let b = self.detector.locate_bullseye(Some(image), self.current_device.as_deref(), self.current_cam_uri.as_deref(), None, None);
        self.last_bullseye = Some(b);
        b
    }

    /// Target bounding box for the active target mode.
    pub fn get_target_roi(&mut self, image: &Mat, bull: (i32, i32)) -> (i32, i32, i32, i32) {
        if let Some(r) = self.locked_target_roi {
            return r;
        }
        if let (TargetMode::Custom, Some(r)) = (self.target_mode, self.custom_roi) {
            self.locked_target_roi = Some(r);
            return r;
        }
        let (dev, uri) = (self.current_device.clone(), self.current_cam_uri.clone());
        let shape = (image.rows(), image.cols());
        let roi = if self.target_mode == TargetMode::T20 {
            self.detector.get_t20_roi(shape, Some(image), dev.as_deref(), uri.as_deref(), None, Some(bull), 90)
        } else {
            self.detector.get_furthest_double_roi(Some(image), shape, dev.as_deref(), uri.as_deref(), None, Some(bull))
        };
        self.locked_target_roi = Some(roi);
        roi
    }

    /// Combined Laplacian variance + Tenengrad focus metric on a patch.
    pub fn compute_patch_sharpness(patch: &Mat) -> f64 {
        (|| -> cvx::R<f64> {
            if patch.empty() {
                return Ok(0.0);
            }
            let g = cvx::gray(patch)?;
            let mut lap = Mat::default();
            imgproc::laplacian_def(&g, &mut lap, cvc::CV_64F)?;
            let (mut mean, mut sd) = (cvc::Scalar::default(), cvc::Scalar::default());
            cvc::mean_std_dev(&lap, &mut mean, &mut sd, &cvc::no_array())?;
            let lap_var = sd[0] * sd[0];
            let (mut gx, mut gy) = (Mat::default(), Mat::default());
            imgproc::sobel(&g, &mut gx, cvc::CV_64F, 1, 0, 3, 1.0, 0.0, cvc::BORDER_DEFAULT)?;
            imgproc::sobel(&g, &mut gy, cvc::CV_64F, 0, 1, 3, 1.0, 0.0, cvc::BORDER_DEFAULT)?;
            let (mut gx2, mut gy2, mut sum) = (Mat::default(), Mat::default(), Mat::default());
            cvc::multiply(&gx, &gx, &mut gx2, 1.0, -1)?;
            cvc::multiply(&gy, &gy, &mut gy2, 1.0, -1)?;
            cvc::add(&gx2, &gy2, &mut sum, &cvc::no_array(), -1)?;
            let ten = cvc::mean(&sum, &cvc::no_array())?[0];
            Ok(lap_var.max(0.0).sqrt() + ten.max(0.0).sqrt())
        })()
        .unwrap_or(0.0)
    }

    /// Is the target patch exposed acceptably?
    pub fn check_lighting_quality(roi: &Mat) -> LightingState {
        if roi.empty() {
            return LightingState::TooDark;
        }
        let m = cvx::gray(roi).and_then(|g| cvc::mean(&g, &cvc::no_array())).map(|s| s[0]).unwrap_or(0.0);
        if m < 40.0 {
            LightingState::TooDark
        } else if m > 185.0 {
            LightingState::Overexposed
        } else {
            LightingState::Optimal
        }
    }

    fn norm(&self, v: f64, span: f64) -> f64 {
        ((v - self.baseline_min) / span * 100.0).clamp(0.0, 100.0)
    }

    /// Real-time oscilloscope focus analysis on one camera frame.
    pub fn analyze_frame(&mut self, image: &Mat) -> FocusAnalysis {
        let now = Instant::now();
        self.fps_tracker.push(now);
        self.fps_tracker.retain(|t| now.duration_since(*t).as_secs_f64() <= 1.0);
        let fps = self.fps_tracker.len() as f64;
        let bull = self.locate_bullseye(image);
        let roi = self.get_target_roi(image, bull);
        let (rx, ry, rw, rh) = roi;
        let (rw2, rh2) = (rw.min(image.cols() - rx).max(0), rh.min(image.rows() - ry).max(0));
        let patch = cvx::crop(image, rx.max(0), ry.max(0), rw2, rh2).unwrap_or_default();
        let lighting = Self::check_lighting_quality(&patch);
        let raw = if lighting != LightingState::Optimal { 0.0 } else { Self::compute_patch_sharpness(&patch) };
        self.smoothed_raw = if self.smoothed_raw == 0.0 { raw } else { self.smoothed_raw * 0.65 + raw * 0.35 };
        let s = self.smoothed_raw;
        if s > 0.0 {
            self.baseline_min = self.baseline_min.min(s);
            self.baseline_max = self.baseline_max.max(s);
        }
        self.peak_raw = self.peak_raw.max(s);
        let due = self.last_sample.map(|t| now.duration_since(t).as_secs_f64() >= self.sample_interval).unwrap_or(true);
        if due || self.raw_history.is_empty() {
            self.last_sample = Some(now);
            self.raw_history.push(s);
            if self.raw_history.len() > 100 {
                self.raw_history.remove(0);
            }
            let span = (self.baseline_max - self.baseline_min).max(10.0);
            self.norm_history = self.raw_history.iter().map(|v| self.norm(*v, span)).collect();
            self.peak_norm = if self.baseline_max > self.baseline_min { self.norm(self.peak_raw, span) } else { 0.0 };
            self.peak_history_idx = if !self.raw_history.is_empty() && self.peak_raw > 0.0 {
                self.raw_history
                    .iter()
                    .position(|v| *v == self.peak_raw)
                    .map(|i| i as i64)
                    .unwrap_or(self.raw_history.len() as i64 - 1)
            } else {
                -1
            };
        }
        let span = (self.baseline_max - self.baseline_min).max(10.0);
        let cur = if self.smoothed_raw > 0.0 { self.norm(self.smoothed_raw, span) } else { 0.0 };
        let trend = if self.peak_raw > 0.0 && self.smoothed_raw >= self.peak_raw * 0.98 {
            FocusTrend::Sharpening
        } else if self.peak_raw > 0.0 && (self.peak_raw - self.smoothed_raw) / self.peak_raw.max(1.0) > 0.06 {
            FocusTrend::Overshot
        } else {
            FocusTrend::Stable
        };
        FocusAnalysis {
            score: r1(cur),
            raw_score: r1(self.smoothed_raw),
            peak_score: r1(self.peak_norm),
            peak_raw: r1(self.peak_raw),
            trend,
            target_roi: roi,
            target_name: self.target_name.clone(),
            target_mode: self.target_mode,
            bullseye: bull,
            focal_line_y: ry + rh / 2,
            lighting_state: lighting,
            history: self.norm_history.clone(),
            peak_history_idx: self.peak_history_idx,
            fps: r1(fps),
        }
    }
}
