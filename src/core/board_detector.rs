//! Dartboard geometry for the Camera Focus tool: the bullseye and the focus target (furthest double, T20),
//! from Autodarts' calibration when there is one, else found in the picture.
use super::cvx::{self, M3};
use super::util::home;
use opencv::core::{Mat, Point, Scalar};
use opencv::imgproc;
use opencv::prelude::*;
use serde_json::Value;
use std::collections::HashMap;
use std::f64::consts::PI;
use std::path::PathBuf;

fn config_path() -> PathBuf {
    home().join(".config/autodarts/config.toml")
}

fn read_config_json() -> Option<Value> {
    let t: toml::Table = std::fs::read_to_string(config_path()).ok()?.parse().ok()?;
    Some(super::autodarts::toml_to_json(&t))
}

#[derive(Debug, Clone)]
pub struct CalibrationEntry {
    pub homography: M3,
}

fn realpath(p: &str) -> String {
    std::fs::canonicalize(p).map(|r| r.to_string_lossy().to_string()).unwrap_or_else(|_| p.to_string())
}

#[derive(Default)]
pub struct BoardDetector {
    pub locked_rois: HashMap<String, (i32, i32, i32, i32)>,
}

impl BoardDetector {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn load_calibration_homography(&self, w: i32, h: i32, dev: Option<&str>, by_path: Option<&str>, cam_idx: Option<usize>) -> Option<M3> {
        self.load_calibration_entry(w, h, dev, by_path, cam_idx).map(|e| e.homography)
    }

    /// One camera's Autodarts calibration at the given capture resolution, or None without a usable homography.
    pub fn load_calibration_entry(&self, w: i32, h: i32, dev: Option<&str>, by_path: Option<&str>, cam_idx: Option<usize>) -> Option<CalibrationEntry> {
        let cal = super::util::read_json(home().join(".config/autodarts/calibration.json"))?;
        let cams = cal["cameras"].as_object()?;
        if cams.is_empty() {
            return None;
        }
        let keys: Vec<&String> = cams.keys().collect();
        let mut target: Option<String> = None;
        // 1. Match dev_node / by_path against devices in config.toml
        if dev.is_some() || by_path.is_some() {
            if let Some(cfg) = read_config_json() {
                let devs = cfg["cam"]["devices"]
                    .as_array()
                    .filter(|a| !a.is_empty())
                    .or_else(|| cfg["cam"]["cams"].as_array())
                    .cloned()
                    .unwrap_or_default();
                let norm = dev.map(realpath).unwrap_or_default();
                let re = regex::Regex::new(r"(?:native=)?(/dev/video\d+|/dev/v4l/[^\s&]+)").unwrap();
                let loc = regex::Regex::new(r"[?&]location=([^&]+)").unwrap();
                for d in devs.iter().filter_map(|v| v.as_str()).filter(|s| !s.is_empty()) {
                    let mut hit = false;
                    if let Some(c) = re.captures(d) {
                        let cand = &c[1];
                        if !norm.is_empty() && (realpath(cand) == norm || Some(cand) == dev) {
                            hit = true;
                        }
                    }
                    if !hit {
                        if let (Some(bp), Some(m)) = (by_path, loc.captures(d)) {
                            hit = bp.contains(&m[1]);
                        }
                    }
                    if hit {
                        if let Some(k) = keys.iter().find(|k| k.contains(d) || d.contains(k.as_str())) {
                            target = Some((*k).clone());
                            break;
                        }
                    }
                }
            }
        }
        // 2. Match directly against keys in calibration.json
        if target.is_none() {
            if let Some(d) = dev {
                let norm = realpath(d);
                target = keys
                    .iter()
                    .find(|k| k.as_str() == d || **k == &norm || by_path.map(|b| b.contains(k.as_str())).unwrap_or(false))
                    .map(|k| (*k).clone());
            }
        }
        // 3. Match by numeric index
        if target.is_none() {
            let idx = match (cam_idx, dev) {
                (Some(i), _) if i >= 1 && i <= keys.len() => Some(i - 1),
                (_, Some(d)) => regex::Regex::new(r"video(\d+)")
                    .unwrap()
                    .captures(d)
                    .and_then(|c| c[1].parse::<usize>().ok())
                    .map(|n| n / 2)
                    .filter(|&i| i < keys.len()),
                _ => None,
            };
            target = idx.map(|i| keys[i].clone());
        }
        let key = target?;
        let entry = cams.get(&key)?;
        let res_key = format!("{}x{}", w, h);
        if entry[&res_key]["homography"].is_array() {
            return Self::make_entry(&entry[&res_key], &[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]);
        }
        // A homography from another resolution is only valid if it shares the aspect ratio (pure scaling).
        let rk = regex::Regex::new(r"^(\d+)x(\d+)$").unwrap();
        for (ek, ev) in entry.as_object()? {
            if !ev["homography"].is_array() {
                continue;
            }
            let Some(c) = rk.captures(ek) else { continue };
            let (sw, sh): (f64, f64) = (c[1].parse().ok()?, c[2].parse().ok()?);
            if (sw / sh - w as f64 / h as f64).abs() > 0.01 {
                continue;
            }
            let s = [[sw / w as f64, 0.0, 0.0], [0.0, sh / h as f64, 0.0], [0.0, 0.0, 1.0]];
            return Self::make_entry(ev, &s);
        }
        None
    }

    fn make_entry(raw: &Value, scale_inv: &M3) -> Option<CalibrationEntry> {
        let flat: Vec<f64> = match raw["homography"].as_array()? {
            a if a.first().map(|v| v.is_array()).unwrap_or(false) => {
                a.iter().flat_map(|r| r.as_array().cloned().unwrap_or_default()).filter_map(|v| v.as_f64()).collect()
            }
            a => a.iter().filter_map(|v| v.as_f64()).collect(),
        };
        if flat.len() != 9 {
            return None;
        }
        let hm = [[flat[0], flat[1], flat[2]], [flat[3], flat[4], flat[5]], [flat[6], flat[7], flat[8]]];
        Some(CalibrationEntry { homography: cvx::mul3(&hm, scale_inv) })
    }

    /// Ground-truth dartboard geometry (bull, 20 double-outer points) from calibration.json or config.toml.
    fn load_autodarts_geometry(&self, h: i32, w: i32, dev: Option<&str>, by_path: Option<&str>, cam_idx: Option<usize>) -> Option<((i32, i32), Vec<Point>)> {
        if let Some(hm) = self.load_calibration_homography(w, h, dev, by_path, cam_idx) {
            if let Some(inv) = cvx::inv3(&hm) {
                let (bx, by) = cvx::project(&inv, 500.0, 500.0);
                let pts = (0..20)
                    .map(|i| {
                        let a = ((90 - i * 18) as f64).to_radians();
                        let (x, y) = cvx::project(&inv, 500.0 + 323.0 * a.cos(), 500.0 + 323.0 * a.sin());
                        Point::new(x as i32, y as i32)
                    })
                    .collect();
                return Some(((bx as i32, by as i32), pts));
            }
        }
        // legacy config.toml double_outers
        let data = read_config_json()?;
        let cams: Vec<String> = data["cam"]["cams"]
            .as_array()
            .map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect())
            .unwrap_or_else(|| vec!["/dev/video0".into(), "/dev/video2".into(), "/dev/video4".into()]);
        let mut c_idx = "0".to_string();
        if let Some(i) = dev.and_then(|d| cams.iter().position(|c| c == d)) {
            c_idx = i.to_string();
        } else if let Some(ci) = cam_idx {
            c_idx = (ci as i64 - 1).to_string();
        }
        let db = &data["dartboard"][&c_idx];
        let outers = db["double_outers"].as_array()?;
        if outers.len() != 20 {
            return None;
        }
        let bull = db["bull"].as_array().cloned().unwrap_or_else(|| vec![640.into(), 480.into()]);
        let (sx, sy) = (w as f64 / 1280.0, h as f64 / 960.0);
        let pts = outers
            .iter()
            .map(|p| {
                Point::new(
                    (p[0].as_f64().unwrap_or(0.0) * sx).round() as i32,
                    (p[1].as_f64().unwrap_or(0.0) * sy).round() as i32,
                )
            })
            .collect();
        let b = ((bull[0].as_f64().unwrap_or(640.0) * sx) as i32, (bull[1].as_f64().unwrap_or(480.0) * sy) as i32);
        Some((b, pts))
    }

    /// Locate bullseye: concentric red/green geometry, colour-agnostic ring clustering, or calibration.
    pub fn locate_bullseye(&self, frame: Option<&Mat>, dev: Option<&str>, by_path: Option<&str>, cam_idx: Option<usize>, shape: Option<(i32, i32)>) -> (i32, i32) {
        let (h, w) = match (frame, shape) {
            (Some(f), _) => (f.rows(), f.cols()),
            (None, Some(s)) => s,
            _ => (960, 1280),
        };
        if let Some(f) = frame.filter(|f| !f.empty()) {
            if let Ok(Some(p)) = self.bull_from_rings(f, h, w) {
                return p;
            }
        }
        if let Some(hm) = self.load_calibration_homography(w, h, dev, by_path, cam_idx) {
            if let Some(inv) = cvx::inv3(&hm) {
                let (x, y) = cvx::project(&inv, 500.0, 500.0);
                let (x, y) = (x as i32, y as i32);
                if 0 < x && x < w && 0 < y && y < h {
                    return (x, y);
                }
            }
        }
        ((0.5 * w as f64) as i32, (0.45 * h as f64) as i32)
    }

    fn bull_from_rings(&self, frame: &Mat, h: i32, w: i32) -> cvx::R<Option<(i32, i32)>> {
        let (y1, y2) = ((0.15 * h as f64) as i32, (0.85 * h as f64) as i32);
        let (x1, x2) = ((0.15 * w as f64) as i32, (0.85 * w as f64) as i32);
        let crop = cvx::crop(frame, x1, y1, x2 - x1, y2 - y1)?;
        let (ch, cw) = (crop.rows(), crop.cols());
        let gray = cvx::gray(&crop)?;
        let mut accum = vec![0f32; (ch * cw) as usize];
        let mut add = |x: i32, y: i32, v: f64| {
            if 0 <= x && x < cw && 0 <= y && y < ch {
                accum[(y * cw + x) as usize] += v as f32;
            }
        };
        if crop.channels() == 3 {
            let hsv = cvx::hsv(&crop)?;
            let red = cvx::or(&cvx::in_range(&hsv, (0., 55., 55.), (14., 255., 255.))?, &cvx::in_range(&hsv, (166., 55., 55.), (180., 255., 255.))?)?;
            let green = cvx::in_range(&hsv, (35., 40., 40.), (88., 255., 255.))?;
            let pts = |m: &Mat, lo: f64, hi: f64| -> cvx::R<Vec<(f64, f64, f64)>> {
                Ok(cvx::contours(m, imgproc::RETR_EXTERNAL)?
                    .iter()
                    .filter_map(|c| {
                        let a = cvx::area(&c);
                        if lo < a && a < hi {
                            cvx::centroid(&c).map(|(x, y)| (x, y, a))
                        } else {
                            None
                        }
                    })
                    .collect())
            };
            let rp = pts(&red, 20.0, 2500.0)?;
            let gp = pts(&green, 50.0, 4500.0)?;
            for (rx, ry, ra) in &rp {
                for (gx, gy, ga) in &gp {
                    let d = (rx - gx).hypot(ry - gy);
                    let ratio = ga / (ra + 1e-5);
                    if d < 12.0 && 1.5 < ratio && ratio < 15.0 {
                        add(((rx + gx) / 2.0) as i32, ((ry + gy) / 2.0) as i32, 5000.0 / (1.0 + d * 2.0));
                    }
                }
            }
        }
        // Colour-agnostic concentric edge / grayscale ring detector
        let blurred = cvx::blur(&gray, 5, 1.0)?;
        let mut thr = Mat::default();
        imgproc::adaptive_threshold(&blurred, &mut thr, 255.0, imgproc::ADAPTIVE_THRESH_GAUSSIAN_C, imgproc::THRESH_BINARY_INV, 25, 4.0)?;
        let edges = cvx::canny(&blurred, 30.0, 100.0)?;
        let mut valid = Vec::new();
        for bin in [&thr, &edges] {
            for c in cvx::contours(bin, imgproc::RETR_TREE)? {
                let a = cvx::area(&c);
                if !(20.0 < a && a < 4500.0) {
                    continue;
                }
                let (_, r) = cvx::min_circle(&c);
                if !(3.0 < r && r < 40.0) {
                    continue;
                }
                let circ = a / (PI * r * r + 1e-5);
                if 0.35 < circ && circ < 1.1 {
                    if let Some((x, y)) = cvx::centroid(&c) {
                        valid.push((x, y, a));
                    }
                }
            }
        }
        for i in 0..valid.len() {
            for j in i + 1..valid.len() {
                let (c1, c2) = (valid[i], valid[j]);
                let d = (c1.0 - c2.0).hypot(c1.1 - c2.1);
                let (inner, outer) = (c1.2.min(c2.2), c1.2.max(c2.2));
                let ratio = outer / (inner + 1e-5);
                if d < 4.5 && 1.8 < ratio && ratio < 9.0 && 80.0 < inner && inner < 700.0 && 450.0 < outer && outer < 3500.0 {
                    add(((c1.0 + c2.0) / 2.0) as i32, ((c1.1 + c2.1) / 2.0) as i32, 1500.0 / (1.0 + d * 2.0));
                }
            }
        }
        let am = Mat::new_rows_cols_with_data(ch, cw, &accum)?.try_clone()?;
        let mut sm = cvx::blur(&am, 21, 0.0)?;
        let (hw, hh) = (cw as f64 / 2.0, ch as f64 / 2.0);
        let den = hw * hw + hh * hh;
        for y in 0..ch {
            for x in 0..cw {
                let prior = (1.0 - 0.25 * ((x as f64 - hw).powi(2) + (y as f64 - hh).powi(2)) / den).clamp(0.5, 1.0);
                *sm.at_2d_mut::<f32>(y, x)? *= prior as f32;
            }
        }
        let (mx, loc) = cvx::max_val(&sm);
        if mx > 50.0 {
            return Ok(Some((loc.0 + x1, loc.1 + y1)));
        }
        Ok(None)
    }

    /// Bounding box (x, y, w, h) centred on the furthest double field; locked once found to keep previews static.
    pub fn get_furthest_double_roi(&mut self, frame: Option<&Mat>, shape: (i32, i32), dev: Option<&str>, by_path: Option<&str>, cam_idx: Option<usize>, center: Option<(i32, i32)>) -> (i32, i32, i32, i32) {
        let key = dev.map(String::from).or_else(|| cam_idx.map(|i| format!("cam_{}", i)));
        if let Some(r) = key.as_ref().and_then(|k| self.locked_rois.get(k)) {
            return *r;
        }
        let (h, w) = frame.map(|f| (f.rows(), f.cols())).unwrap_or(shape);
        let (bx, by) = center.unwrap_or_else(|| self.locate_bullseye(frame, dev, by_path, cam_idx, Some((h, w))));
        let mut calib: Option<(i32, i32)> = None;
        if let Some(inv) = self.load_calibration_homography(w, h, dev, by_path, cam_idx).and_then(|m| cvx::inv3(&m)) {
            calib = (0..360)
                .step_by(2)
                .map(|deg| {
                    let a = (deg as f64).to_radians();
                    cvx::project(&inv, 500.0 + 323.0 * a.cos(), 500.0 + 323.0 * a.sin())
                })
                .min_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal))
                .map(|p| (p.0 as i32, p.1 as i32));
        }
        if calib.is_none() {
            if let Some((_, outers)) = self.load_autodarts_geometry(h, w, dev, by_path, cam_idx) {
                calib = outers.iter().min_by_key(|p| p.y).map(|p| (p.x, p.y));
            }
        }
        let mut cv_target = None;
        if let Some(f) = frame.filter(|f| !f.empty() && f.channels() == 3) {
            cv_target = (|| -> cvx::R<Option<(i32, i32)>> {
                let hsv = cvx::hsv(f)?;
                let r = cvx::or(&cvx::in_range(&hsv, (0., 70., 70.), (14., 255., 255.))?, &cvx::in_range(&hsv, (166., 70., 70.), (180., 255., 255.))?)?;
                let g = cvx::in_range(&hsv, (35., 60., 60.), (88., 255., 255.))?;
                let mut upper = cvx::zeros_u8(h, w)?;
                if by > 0 {
                    cvx::rect(&mut upper, 0, 0, w, by.min(h), Scalar::all(255.0), -1, false)?;
                }
                let search = cvx::and(&cvx::or(&r, &g)?, &upper)?;
                let (mind, maxd) = (0.150 * h as f64, 0.210 * h as f64);
                let mut cands: Vec<(f64, f64)> = Vec::new();
                for c in cvx::contours(&search, imgproc::RETR_EXTERNAL)? {
                    let a = cvx::area(&c);
                    if 80.0 < a && a < 3500.0 {
                        if let Some((cx, cy)) = cvx::centroid(&c) {
                            let d = (cx - bx as f64).hypot(cy - by as f64);
                            if mind <= d && d <= maxd && cy < by as f64 - 50.0 {
                                cands.push((cy, cx));
                            }
                        }
                    }
                }
                if cands.is_empty() {
                    return Ok(None);
                }
                match calib {
                    Some((tx, ty)) => cands.sort_by(|a, b| {
                        let da = (a.1 - tx as f64).hypot(a.0 - ty as f64);
                        let db = (b.1 - tx as f64).hypot(b.0 - ty as f64);
                        da.partial_cmp(&db).unwrap()
                    }),
                    None => cands.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap()),
                }
                Ok(Some((cands[0].1 as i32, cands[0].0 as i32)))
            })()
            .ok()
            .flatten();
        }
        let (tx, ty) = cv_target.or(calib).unwrap_or_else(|| (bx, (by - (0.187 * h as f64) as i32).max(25)));
        let (rw, rh) = (160, 46);
        let x = (tx - rw / 2).min(w - rw).max(0);
        let y = (ty - rh / 2).min(h - rh).max(0);
        let roi = (x, y, rw, rh);
        if cv_target.is_some() || calib.is_some() {
            if let Some(k) = key {
                self.locked_rois.insert(k, roi);
            }
        }
        roi
    }

    /// Bounding box (x, y, w, h) centred on the Triple 20 segment.
    pub fn get_t20_roi(&mut self, shape: (i32, i32), frame: Option<&Mat>, dev: Option<&str>, by_path: Option<&str>, cam_idx: Option<usize>, center: Option<(i32, i32)>, roi_size: i32) -> (i32, i32, i32, i32) {
        let key = dev.map(String::from).or_else(|| cam_idx.map(|i| format!("cam_{}", i)));
        let tkey = key.map(|k| format!("{}_t20", k));
        if let Some(r) = tkey.as_ref().and_then(|k| self.locked_rois.get(k)) {
            return *r;
        }
        let (h, w) = frame.map(|f| (f.rows(), f.cols())).unwrap_or(shape);
        let (bx, by) = center.unwrap_or_else(|| self.locate_bullseye(frame, dev, by_path, cam_idx, Some((h, w))));
        let calib = self
            .load_calibration_homography(w, h, dev, by_path, cam_idx)
            .and_then(|m| cvx::inv3(&m))
            .map(|inv| {
                let (x, y) = cvx::project(&inv, 500.0, 500.0 - 202.0);
                (x as i32, y as i32)
            });
        let (tx, ty) = calib.unwrap_or((bx, (by - (0.116 * h as f64) as i32).max(25)));
        let x = (tx - roi_size / 2).min(w - roi_size).max(0);
        let y = (ty - roi_size / 2).min(h - roi_size).max(0);
        let roi = (x, y, roi_size, roi_size);
        if let (Some(k), true) = (tkey, calib.is_some()) {
            self.locked_rois.insert(k, roi);
        }
        roi
    }

    /// Reset locked target ROI so furthest double can be re-detected.
    pub fn unlock_target(&mut self, dev: Option<&str>) {
        match dev {
            Some(d) => {
                self.locked_rois.remove(d);
                self.locked_rois.remove(&format!("{}_t20", d));
            }
            None => self.locked_rois.clear(),
        }
    }

}
