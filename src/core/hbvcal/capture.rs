//! Validated frame bursts and stream statistics.
use super::v4l2::{Camera, Frame};
use opencv::core::{Mat, Vector, CV_8UC2};
use opencv::prelude::*;
use opencv::{imgcodecs, imgproc};
use std::io;

/// frames dropped after stream start (startup fragments, AE/ISP settling)
pub const START_DISCARD: usize = 30;

pub fn valid_jpeg(data: &[u8]) -> bool {
    if data.len() < 4 || data[0] != 0xFF || data[1] != 0xD8 {
        return false;
    }
    let mut end = data.len();
    while end > 0 && data[end - 1] == 0 {
        end -= 1;
    }
    end >= 2 && data[end - 2] == 0xFF && data[end - 1] == 0xD9
}

#[derive(Debug, Clone, Default)]
pub struct StreamStats {
    pub frames: usize,
    pub fps: f64,
    pub dt_max_ms: f64,
    pub dropped: u64,
    pub invalid: usize,
    pub kb_mean: f64,
    pub kb_max: f64,
    pub mbps: f64,
}

pub struct Burst {
    pub frames: Vec<Frame>,
    pub invalid: usize,
    pub pixfmt: String,
    pub width: u32,
    pub height: u32,
}

pub fn dropped(seq: &[u32]) -> u64 {
    seq.windows(2).map(|w| (w[1] as i64 - w[0] as i64 - 1).max(0) as u64).sum()
}

impl Burst {
    pub fn stream_stats(&self) -> StreamStats {
        let fr = &self.frames;
        let seq: Vec<u32> = fr.iter().map(|f| f.sequence).collect();
        let ts: Vec<f64> = fr.iter().map(|f| f.timestamp).collect();
        let sizes: Vec<f64> = fr.iter().map(|f| f.data.len() as f64).collect();
        let dt: Vec<f64> = ts.windows(2).map(|w| w[1] - w[0]).collect();
        let fps = if ts.len() > 1 { 1.0 / (dt.iter().sum::<f64>() / dt.len() as f64) } else { f64::NAN };
        let mean = if sizes.is_empty() { f64::NAN } else { sizes.iter().sum::<f64>() / sizes.len() as f64 };
        StreamStats {
            frames: fr.len(),
            fps,
            dt_max_ms: if ts.len() > 1 { dt.iter().cloned().fold(f64::MIN, f64::max) * 1000.0 } else { f64::NAN },
            dropped: dropped(&seq),
            invalid: self.invalid,
            kb_mean: mean / 1024.0,
            kb_max: sizes.iter().cloned().fold(f64::NAN, f64::max) / 1024.0,
            mbps: mean * fps * 8.0 / 1e6,
        }
    }

    /// BGR images, one per frame.
    pub fn decode(&self) -> opencv::Result<Vec<Mat>> {
        self.frames.iter().map(|f| decode_frame(&f.data, &self.pixfmt, self.width, self.height)).collect()
    }
}

pub fn decode_frame(data: &[u8], pixfmt: &str, w: u32, h: u32) -> opencv::Result<Mat> {
    if pixfmt == "YUYV" {
        yuyv_to_bgr(data, w, h)
    } else {
        let buf = Vector::<u8>::from_slice(data);
        imgcodecs::imdecode(&buf, imgcodecs::IMREAD_COLOR)
    }
}

/// Read `discard` frames, then collect `n` complete frames (bad JPEG / short YUYV frames are counted and
/// skipped). Gives up after `max_reads` reads (default 4*n + 60).
pub fn burst(cam: &mut Camera, n: usize, discard: usize, timeout: f64, max_reads: usize) -> io::Result<Burst> {
    for _ in 0..discard {
        cam.read(timeout)?;
    }
    let mut out = Vec::new();
    let mut bad = 0usize;
    let limit = if max_reads > 0 { max_reads } else { 4 * n + 60 };
    while out.len() < n && out.len() + bad < limit {
        let f = cam.read(timeout)?;
        let broken = (cam.pixfmt == "MJPG" && !valid_jpeg(&f.data))
            || (cam.pixfmt == "YUYV" && f.data.len() != (cam.width * cam.height * 2) as usize);
        if broken {
            bad += 1;
            continue;
        }
        out.push(f);
    }
    if out.len() < n {
        return Err(io::Error::other(format!(
            "{}: only {} of {} complete frames ({} broken)",
            cam.device,
            out.len(),
            n,
            bad
        )));
    }
    Ok(Burst { frames: out, invalid: bad, pixfmt: cam.pixfmt.clone(), width: cam.width, height: cam.height })
}

/// BT.601 luma like the JPEG Y channel (CV_32F).
pub fn luma(bgr: &Mat) -> opencv::Result<Mat> {
    let mut out = Mat::default();
    if bgr.channels() == 3 {
        let mut ycc = Mat::default();
        imgproc::cvt_color_def(bgr, &mut ycc, imgproc::COLOR_BGR2YCrCb)?;
        let mut y = Mat::default();
        opencv::core::extract_channel(&ycc, &mut y, 0)?;
        y.convert_to_def(&mut out, opencv::core::CV_32F)?;
    } else {
        bgr.convert_to_def(&mut out, opencv::core::CV_32F)?;
    }
    Ok(out)
}

pub fn yuyv_to_bgr(data: &[u8], w: u32, h: u32) -> opencv::Result<Mat> {
    let need = (w * h * 2) as usize;
    let mut m = Mat::new_rows_cols_with_default(h as i32, w as i32, CV_8UC2, opencv::core::Scalar::all(0.0))?;
    let n = need.min(data.len());
    m.data_bytes_mut()?[..n].copy_from_slice(&data[..n]);
    let mut out = Mat::default();
    imgproc::cvt_color_def(&m, &mut out, imgproc::COLOR_YUV2BGR_YUYV)?;
    Ok(out)
}
