//! Computer vision utilities and image filters for dartboard calibration: difference heatmaps, dart tip
//! localisation, wire edge gradient maps, sensor grain amplification, and zoom/HUD rendering.
use super::board_detector::BoardRoi;
use super::cvx::{self, bgr};
use opencv::core::{self as cvc, Mat, Scalar};
use opencv::imgproc;
use opencv::prelude::*;

fn same_size(a: &Mat, like: &Mat) -> cvx::R<Mat> {
    if a.rows() != like.rows() || a.cols() != like.cols() {
        cvx::resize(a, like.cols(), like.rows(), imgproc::INTER_LINEAR)
    } else {
        a.try_clone()
    }
}

/// Isolate the dart difference signal and colourise it with a high-contrast heatmap.
pub fn apply_difference_filter(empty: &Mat, dart: &Mat) -> cvx::R<Mat> {
    if empty.empty() || dart.empty() {
        return if dart.empty() { empty.try_clone() } else { dart.try_clone() };
    }
    let e = cvx::gray(&same_size(empty, dart)?)?;
    let d = cvx::gray(dart)?;
    let mut diff = Mat::default();
    cvc::absdiff(&e, &d, &mut diff)?;
    let amp = cvx::to_u8(&diff, 3.5, 0.0)?;
    let heat = cvx::color_map(&amp, imgproc::COLORMAP_TURBO)?;
    cvx::add_weighted(&heat, 0.65, &cvx::bgr_of_gray(&d)?, 0.35)
}

/// Pixel coordinate of the inserted dart tip using background subtraction.
pub fn detect_dart_tip(empty: &Mat, dart: &Mat, roi: Option<&BoardRoi>) -> cvx::R<Option<(i32, i32)>> {
    if empty.empty() || dart.empty() {
        return Ok(None);
    }
    let e = cvx::gray(&same_size(empty, dart)?)?;
    let d = cvx::gray(dart)?;
    let mut diff = Mat::default();
    cvc::absdiff(&e, &d, &mut diff)?;
    if let Some(r) = roi {
        diff.set_to(&Scalar::all(0.0), &cvx::not(&r.board_mask)?)?;
    }
    let mut th = Mat::default();
    imgproc::threshold(&diff, &mut th, 18.0, 255.0, imgproc::THRESH_BINARY)?;
    let k = cvx::kernel_ellipse(5)?;
    let cleaned = cvx::morph(&cvx::morph(&th, imgproc::MORPH_OPEN, &k)?, imgproc::MORPH_CLOSE, &k)?;
    let cs = cvx::contours(&cleaned, imgproc::RETR_EXTERNAL)?;
    let Some(largest) = cs.iter().filter(|c| cvx::area(c) > 60.0).max_by(|a, b| cvx::area(a).partial_cmp(&cvx::area(b)).unwrap()) else {
        return Ok(None);
    };
    if let Some(r) = roi {
        let (bx, by) = r.center;
        let p = largest
            .iter()
            .min_by_key(|p| ((p.x - bx) as i64).pow(2) + ((p.y - by) as i64).pow(2))
            .unwrap();
        return Ok(Some((p.x, p.y)));
    }
    Ok(Some(cvx::centroid(&largest).map(|(x, y)| (x as i32, y as i32)).unwrap_or_else(|| {
        let p = largest.get(0).unwrap();
        (p.x, p.y)
    })))
}

/// Colourised Sobel edge gradient map highlighting wire steepness.
pub fn apply_edge_contrast_filter(frame: &Mat) -> cvx::R<Mat> {
    if frame.empty() {
        return frame.try_clone();
    }
    let g = cvx::gray(frame)?;
    let (mut gx, mut gy, mut mag) = (Mat::default(), Mat::default(), Mat::default());
    imgproc::sobel(&g, &mut gx, cvc::CV_32F, 1, 0, 3, 1.0, 0.0, cvc::BORDER_DEFAULT)?;
    imgproc::sobel(&g, &mut gy, cvc::CV_32F, 0, 1, 3, 1.0, 0.0, cvc::BORDER_DEFAULT)?;
    cvc::magnitude(&gx, &gy, &mut mag)?;
    cvx::color_map(&cvx::to_u8(&mag, 2.2, 0.0)?, imgproc::COLORMAP_VIRIDIS)
}

/// Colourised Sobel edge maps of two crops on ONE shared brightness scale (99.5th percentile of the stronger),
/// so a pair is directly comparable. A missing crop stays None.
pub fn edge_strength_pair(before: Option<&Mat>, after: Option<&Mat>) -> cvx::R<(Option<Mat>, Option<Mat>)> {
    let mags: Vec<Option<Mat>> = [before, after]
        .iter()
        .map(|m| m.filter(|m| !m.empty()).map(cvx::sobel_mag).transpose())
        .collect::<cvx::R<_>>()?;
    let peaks: Vec<f64> = mags.iter().flatten().map(|m| cvx::percentile(&cvx::values_f32(m).unwrap_or_default(), 99.5)).collect();
    if peaks.is_empty() {
        return Ok((before.map(|m| m.clone()), after.map(|m| m.clone())));
    }
    // floor of ~25 DN step edge (Sobel gain 4): a flat crop must not stretch sensor noise into "edges"
    let scale = 235.0 / peaks.iter().cloned().fold(100.0, f64::max);
    let mut out = mags.into_iter().map(|m| m.map(|m| cvx::color_map(&cvx::to_u8(&m, scale, 0.0)?, imgproc::COLORMAP_VIRIDIS)).transpose());
    Ok((out.next().unwrap()?, out.next().unwrap()?))
}

/// Isolate high-frequency sensor grain by subtracting low-pass structure and amplifying 7x.
pub fn apply_noise_inspector_filter(frame: &Mat) -> cvx::R<Mat> {
    if frame.empty() {
        return frame.try_clone();
    }
    let g = cvx::gray(frame)?;
    let b = cvx::blur(&g, 5, 1.2)?;
    let (mut gf, mut bf, mut res) = (Mat::default(), Mat::default(), Mat::default());
    g.convert_to_def(&mut gf, cvc::CV_32F)?;
    b.convert_to_def(&mut bf, cvc::CV_32F)?;
    cvc::subtract(&gf, &bf, &mut res, &cvc::no_array(), -1)?;
    cvx::bgr_of_gray(&cvx::to_u8(&res, 7.0, 128.0)?)
}

/// Digital zoom crop with temporal smoothing, neon-green wire focus peaking, and a reticle.
/// Returns (rendered crop at out_size, new accumulator).
pub fn process_zoom_crop_suit(frame: &Mat, target: (i32, i32, i32, i32), accum: Option<&Mat>, zoom: f64, out_size: (i32, i32)) -> cvx::R<(Mat, Mat)> {
    let (fh, fw) = (frame.rows(), frame.cols());
    let (tx, ty, tw, th) = target;
    let (cx, cy) = if tw > 0 && th > 0 { (tx + tw / 2, ty + th / 2) } else { (fw / 2, fh / 4) };
    let cw = ((out_size.0 as f64 / zoom) as i32).max(20);
    let ch = ((out_size.1 as f64 / zoom) as i32).max(20);
    let (x1, y1) = (cx - cw / 2, cy - ch / 2);
    let (x2, y2) = (x1 + cw, y1 + ch);
    let (pt, pb, pl, pr) = ((-y1).max(0), (y2 - fh).max(0), (-x1).max(0), (x2 - fw).max(0));
    let crop = if pt > 0 || pb > 0 || pl > 0 || pr > 0 {
        let mut padded = Mat::default();
        cvc::copy_make_border(frame, &mut padded, pt, pb, pl, pr, cvc::BORDER_CONSTANT, bgr(10., 12., 15.))?;
        cvx::crop(&padded, x1 + pl, y1 + pt, cw, ch)?
    } else {
        cvx::crop(frame, x1, y1, cw, ch)?
    };
    if crop.empty() {
        return Ok((frame.try_clone()?, crop));
    }
    let mut cf = Mat::default();
    crop.convert_to_def(&mut cf, cvc::CV_32F)?;
    let acc = match accum {
        Some(a) if a.rows() == cf.rows() && a.cols() == cf.cols() && a.typ() == cf.typ() => cvx::add_weighted(&cf, 0.35, a, 0.65)?,
        _ => cf,
    };
    let clean = cvx::to_u8(&acc, 1.0, 0.0)?;
    // focus peaking edge highlights
    let mag = cvx::sobel_mag(&clean)?;
    let mut peak = Mat::default();
    imgproc::threshold(&mag, &mut peak, 250.0, 255.0, imgproc::THRESH_BINARY)?;
    let peak = cvx::to_u8(&peak, 1.0, 0.0)?;
    let mut render = clean.try_clone()?;
    if cvx::count(&peak) > 0 {
        let green = Mat::new_rows_cols_with_default(clean.rows(), clean.cols(), clean.typ(), bgr(40., 255., 60.))?;
        let mixed = cvx::add_weighted(&clean, 0.35, &green, 0.65)?;
        cvx::put_masked(&mixed, &mut render, &peak)?;
    }
    let mut out = cvx::resize(&render, out_size.0, out_size.1, imgproc::INTER_LINEAR)?;
    let c = (out_size.0 / 2, out_size.1 / 2);
    cvx::marker(&mut out, c, bgr(18., 20., 24.), 24, 3, true)?;
    cvx::marker(&mut out, c, bgr(0., 255., 200.), 22, 1, true)?;
    Ok((out, acc))
}

/// Sweet-spot target box and reticle on the full frame.
pub fn draw_sweet_spot_overlay_suit(frame: &Mat, target: (i32, i32, i32, i32), score: f64) -> cvx::R<Mat> {
    let mut out = frame.try_clone()?;
    let (tx, ty, tw, th) = target;
    let locked = score >= 85.0;
    let (color, thick) = if locked {
        (bgr(46., 194., 78.), 3)
    } else if score >= 55.0 {
        (bgr(10., 165., 229.), 2)
    } else {
        (bgr(229., 132., 53.), 2)
    };
    cvx::rect(&mut out, tx, ty, tw, th, color, thick, false)?;
    let (cx, cy) = (tx + tw / 2, ty + th / 2);
    cvx::line(&mut out, (cx - 16, cy), (cx + 16, cy), color, 1)?;
    cvx::line(&mut out, (cx, cy - 12), (cx, cy + 12), color, 1)?;
    let lbl = if locked { "SWEET SPOT".to_string() } else { format!("TARGET ({:.0}%)", score) };
    let (tw_t, th_t) = cvx::text_size(&lbl, 0.45, 1);
    let ly1 = (ty - th_t - 8).max(0);
    cvx::rect(&mut out, tx, ly1, tw_t + 12, ty - ly1, bgr(18., 20., 24.), -1, false)?;
    cvx::text(&mut out, &lbl, (tx + 6, ty - 4), 0.45, color, 1)?;
    Ok(out)
}
