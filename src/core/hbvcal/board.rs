//! Board geometry: Autodarts' homography (image -> 1000x1000 board view) and board-area masks.
use crate::core::util::home;
use opencv::core::{Mat, Point, Scalar, Size, CV_64F, CV_8U};
use opencv::prelude::*;
use opencv::imgproc;
use std::path::{Path, PathBuf};

pub const BOARD_SIZE: i32 = 1000;
pub const BOARD_CENTER: (f64, f64) = (500.0, 500.0);
/// Outer edge of the double ring in the board view, measured on the warped image (px).
pub const DOUBLE_OUTER_R: f64 = 335.0;
/// Board area that matters for scoring: playing field plus a margin for darts hitting the double.
pub const BOARD_R: f64 = 345.0;

pub fn autodarts_calibration() -> PathBuf {
    home().join(".config/autodarts/calibration.json")
}

pub fn port_of(device: &str) -> String {
    super::profiles::port_key(device)
}

/// Autodarts' image->board homography for the camera on `device` (by-path) at this resolution.
pub fn load_homography(device: &str, width: u32, height: u32, path: Option<&Path>) -> Option<Mat> {
    let p = path.map(|p| p.to_path_buf()).unwrap_or_else(autodarts_calibration);
    let cal = crate::core::util::read_json(p)?;
    let port = port_of(device);
    for (key, cam) in cal["cameras"].as_object()? {
        if key.ends_with(&format!("location={}", port)) {
            let h = &cam[format!("{}x{}", width, height)]["homography"];
            let flat: Vec<f64> = match h.as_array() {
                Some(a) if a.first().map(|v| v.is_array()).unwrap_or(false) => {
                    a.iter().flat_map(|r| r.as_array().cloned().unwrap_or_default()).filter_map(|v| v.as_f64()).collect()
                }
                Some(a) => a.iter().filter_map(|v| v.as_f64()).collect(),
                None => continue,
            };
            if flat.len() == 9 {
                let rows = [[flat[0], flat[1], flat[2]], [flat[3], flat[4], flat[5]], [flat[6], flat[7], flat[8]]];
                let m = Mat::from_slice_2d(&rows).ok()?;
                return Some(m);
            }
        }
    }
    None
}

pub fn warp(img: &Mat, h: &Mat, interp: i32) -> opencv::Result<Mat> {
    let mut out = Mat::default();
    imgproc::warp_perspective(
        img,
        &mut out,
        h,
        Size::new(BOARD_SIZE, BOARD_SIZE),
        interp,
        opencv::core::BORDER_CONSTANT,
        Scalar::all(0.0),
    )?;
    Ok(out)
}

/// Mask (CV_8U, 255 = board) of the board area in the 1000x1000 board view.
pub fn board_mask_view(r: f64) -> opencv::Result<Mat> {
    let mut m = Mat::zeros(BOARD_SIZE, BOARD_SIZE, CV_8U)?.to_mat()?;
    imgproc::circle(
        &mut m,
        Point::new(BOARD_CENTER.0 as i32, BOARD_CENTER.1 as i32),
        r as i32,
        Scalar::all(255.0),
        -1,
        imgproc::LINE_8,
        0,
    )?;
    Ok(m)
}

/// Mask (CV_8U) of the board area in camera-image coordinates (for native-resolution metrics).
pub fn board_mask_image(h: &Mat, rows: i32, cols: i32, r: f64) -> opencv::Result<Mat> {
    let m = board_mask_view(r)?;
    let mut inv = Mat::default();
    let mut h64 = Mat::default();
    h.convert_to_def(&mut h64, CV_64F)?;
    opencv::core::invert(&h64, &mut inv, opencv::core::DECOMP_LU)?;
    let mut back = Mat::default();
    imgproc::warp_perspective(
        &m,
        &mut back,
        &inv,
        Size::new(cols, rows),
        imgproc::INTER_NEAREST,
        opencv::core::BORDER_CONSTANT,
        Scalar::all(0.0),
    )?;
    Ok(back)
}
