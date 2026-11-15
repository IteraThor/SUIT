//! Thin wrappers over the OpenCV bindings used across SUIT (keeps call sites close to the cv2 originals).
use opencv::core::{self as cvc, Mat, Point, Point2f, Rect, Scalar, Size, Vector};
use opencv::imgproc;
use opencv::prelude::*;

pub type R<T> = opencv::Result<T>;
pub type Contours = Vector<Vector<Point>>;

/// 3x3 matrix in row-major order.
pub type M3 = [[f64; 3]; 3];

pub fn inv3(m: &M3) -> Option<M3> {
    let a = m;
    let det = a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1]) - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
        + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]);
    if det.abs() < 1e-15 {
        return None;
    }
    let d = 1.0 / det;
    Some([
        [
            (a[1][1] * a[2][2] - a[1][2] * a[2][1]) * d,
            (a[0][2] * a[2][1] - a[0][1] * a[2][2]) * d,
            (a[0][1] * a[1][2] - a[0][2] * a[1][1]) * d,
        ],
        [
            (a[1][2] * a[2][0] - a[1][0] * a[2][2]) * d,
            (a[0][0] * a[2][2] - a[0][2] * a[2][0]) * d,
            (a[0][2] * a[1][0] - a[0][0] * a[1][2]) * d,
        ],
        [
            (a[1][0] * a[2][1] - a[1][1] * a[2][0]) * d,
            (a[0][1] * a[2][0] - a[0][0] * a[2][1]) * d,
            (a[0][0] * a[1][1] - a[0][1] * a[1][0]) * d,
        ],
    ])
}

pub fn mul3(a: &M3, b: &M3) -> M3 {
    let mut r = [[0.0; 3]; 3];
    for i in 0..3 {
        for j in 0..3 {
            r[i][j] = (0..3).map(|k| a[i][k] * b[k][j]).sum();
        }
    }
    r
}

/// Project (x, y) through m.
pub fn project(m: &M3, x: f64, y: f64) -> (f64, f64) {
    let v = [m[0][0] * x + m[0][1] * y + m[0][2], m[1][0] * x + m[1][1] * y + m[1][2], m[2][0] * x + m[2][1] * y + m[2][2]];
    (v[0] / v[2], v[1] / v[2])
}

pub fn m3_to_mat(m: &M3) -> R<Mat> {
    Mat::from_slice_2d(m)
}

pub fn mat_to_m3(m: &Mat) -> Option<M3> {
    let mut d = Mat::default();
    m.convert_to_def(&mut d, cvc::CV_64F).ok()?;
    let mut r = [[0.0; 3]; 3];
    for (i, row) in r.iter_mut().enumerate() {
        for (j, v) in row.iter_mut().enumerate() {
            *v = *d.at_2d::<f64>(i as i32, j as i32).ok()?;
        }
    }
    Some(r)
}

pub fn gray(m: &Mat) -> R<Mat> {
    if m.channels() == 3 {
        let mut g = Mat::default();
        imgproc::cvt_color_def(m, &mut g, imgproc::COLOR_BGR2GRAY)?;
        Ok(g)
    } else {
        m.try_clone()
    }
}

pub fn bgr_of_gray(m: &Mat) -> R<Mat> {
    let mut o = Mat::default();
    imgproc::cvt_color_def(m, &mut o, imgproc::COLOR_GRAY2BGR)?;
    Ok(o)
}

pub fn hsv(m: &Mat) -> R<Mat> {
    let mut o = Mat::default();
    imgproc::cvt_color_def(m, &mut o, imgproc::COLOR_BGR2HSV)?;
    Ok(o)
}

pub fn in_range(m: &Mat, lo: (f64, f64, f64), hi: (f64, f64, f64)) -> R<Mat> {
    let mut o = Mat::default();
    cvc::in_range(m, &Scalar::new(lo.0, lo.1, lo.2, 0.0), &Scalar::new(hi.0, hi.1, hi.2, 255.0), &mut o)?;
    Ok(o)
}

pub fn or(a: &Mat, b: &Mat) -> R<Mat> {
    let mut o = Mat::default();
    cvc::bitwise_or(a, b, &mut o, &cvc::no_array())?;
    Ok(o)
}

pub fn and(a: &Mat, b: &Mat) -> R<Mat> {
    let mut o = Mat::default();
    cvc::bitwise_and(a, b, &mut o, &cvc::no_array())?;
    Ok(o)
}

pub fn not(a: &Mat) -> R<Mat> {
    let mut o = Mat::default();
    cvc::bitwise_not(a, &mut o, &cvc::no_array())?;
    Ok(o)
}

pub fn zeros_u8(h: i32, w: i32) -> R<Mat> {
    Mat::zeros(h, w, cvc::CV_8U)?.to_mat()
}

pub fn contours(bin: &Mat, mode: i32) -> R<Contours> {
    let mut c = Contours::new();
    imgproc::find_contours(bin, &mut c, mode, imgproc::CHAIN_APPROX_SIMPLE, Point::new(0, 0))?;
    Ok(c)
}

pub fn area(c: &Vector<Point>) -> f64 {
    imgproc::contour_area_def(c).unwrap_or(0.0)
}

/// Centroid from the moments, or None for an empty contour.
pub fn centroid(c: &Vector<Point>) -> Option<(f64, f64)> {
    let m = imgproc::moments_def(c).ok()?;
    if m.m00 > 0.0 {
        Some((m.m10 / m.m00, m.m01 / m.m00))
    } else {
        None
    }
}

pub fn min_circle(c: &Vector<Point>) -> ((f64, f64), f64) {
    let mut p = Point2f::default();
    let mut r = 0f32;
    let _ = imgproc::min_enclosing_circle(c, &mut p, &mut r);
    ((p.x as f64, p.y as f64), r as f64)
}

pub fn blur(m: &Mat, k: i32, sigma: f64) -> R<Mat> {
    let mut o = Mat::default();
    imgproc::gaussian_blur_def(m, &mut o, Size::new(k, k), sigma)?;
    Ok(o)
}

pub fn canny(m: &Mat, a: f64, b: f64) -> R<Mat> {
    let mut o = Mat::default();
    imgproc::canny_def(m, &mut o, a, b)?;
    Ok(o)
}

pub fn sobel_mag(m: &Mat) -> R<Mat> {
    let g = gray(m)?;
    let mut f = Mat::default();
    g.convert_to_def(&mut f, cvc::CV_32F)?;
    let (mut gx, mut gy, mut mag) = (Mat::default(), Mat::default(), Mat::default());
    imgproc::sobel(&f, &mut gx, cvc::CV_32F, 1, 0, 3, 1.0, 0.0, cvc::BORDER_DEFAULT)?;
    imgproc::sobel(&f, &mut gy, cvc::CV_32F, 0, 1, 3, 1.0, 0.0, cvc::BORDER_DEFAULT)?;
    cvc::magnitude(&gx, &gy, &mut mag)?;
    Ok(mag)
}

pub fn kernel_ellipse(k: i32) -> R<Mat> {
    imgproc::get_structuring_element_def(imgproc::MORPH_ELLIPSE, Size::new(k, k))
}

pub fn dilate(m: &Mat, k: &Mat, it: i32) -> R<Mat> {
    let mut o = Mat::default();
    imgproc::dilate(m, &mut o, k, Point::new(-1, -1), it, cvc::BORDER_CONSTANT, imgproc::morphology_default_border_value()?)?;
    Ok(o)
}

pub fn erode(m: &Mat, k: &Mat, it: i32) -> R<Mat> {
    let mut o = Mat::default();
    imgproc::erode(m, &mut o, k, Point::new(-1, -1), it, cvc::BORDER_CONSTANT, imgproc::morphology_default_border_value()?)?;
    Ok(o)
}

pub fn morph(m: &Mat, op: i32, k: &Mat) -> R<Mat> {
    let mut o = Mat::default();
    imgproc::morphology_ex_def(m, &mut o, op, k)?;
    Ok(o)
}

pub fn circle(m: &mut Mat, c: (i32, i32), r: i32, color: Scalar, thickness: i32) -> R<()> {
    imgproc::circle(m, Point::new(c.0, c.1), r.max(0), color, thickness, imgproc::LINE_8, 0)
}

pub fn line(m: &mut Mat, a: (i32, i32), b: (i32, i32), color: Scalar, thickness: i32) -> R<()> {
    imgproc::line(m, Point::new(a.0, a.1), Point::new(b.0, b.1), color, thickness, imgproc::LINE_8, 0)
}

pub fn rect(m: &mut Mat, x: i32, y: i32, w: i32, h: i32, color: Scalar, thickness: i32, aa: bool) -> R<()> {
    imgproc::rectangle(m, Rect::new(x, y, w, h), color, thickness, if aa { imgproc::LINE_AA } else { imgproc::LINE_8 }, 0)
}

pub fn marker(m: &mut Mat, c: (i32, i32), color: Scalar, size: i32, thickness: i32, aa: bool) -> R<()> {
    imgproc::draw_marker(
        m,
        Point::new(c.0, c.1),
        color,
        imgproc::MARKER_CROSS,
        size,
        thickness,
        if aa { imgproc::LINE_AA } else { imgproc::LINE_8 },
    )
}

pub fn text(m: &mut Mat, s: &str, org: (i32, i32), scale: f64, color: Scalar, thickness: i32) -> R<()> {
    imgproc::put_text(m, s, Point::new(org.0, org.1), imgproc::FONT_HERSHEY_SIMPLEX, scale, color, thickness, imgproc::LINE_AA, false)
}

pub fn text_size(s: &str, scale: f64, thickness: i32) -> (i32, i32) {
    let mut base = 0;
    imgproc::get_text_size(s, imgproc::FONT_HERSHEY_SIMPLEX, scale, thickness, &mut base)
        .map(|z| (z.width, z.height))
        .unwrap_or((0, 0))
}

pub fn bgr(b: f64, g: f64, r: f64) -> Scalar {
    Scalar::new(b, g, r, 0.0)
}

pub fn add_weighted(a: &Mat, wa: f64, b: &Mat, wb: f64) -> R<Mat> {
    let mut o = Mat::default();
    cvc::add_weighted(a, wa, b, wb, 0.0, &mut o, -1)?;
    Ok(o)
}

pub fn color_map(m: &Mat, map: i32) -> R<Mat> {
    let mut o = Mat::default();
    imgproc::apply_color_map(m, &mut o, map)?;
    Ok(o)
}

/// `clip(m * scale + offset, 0, 255).astype(uint8)`
pub fn to_u8(m: &Mat, scale: f64, offset: f64) -> R<Mat> {
    let mut o = Mat::default();
    m.convert_to(&mut o, cvc::CV_8U, scale, offset)?;
    Ok(o)
}

pub fn resize(m: &Mat, w: i32, h: i32, interp: i32) -> R<Mat> {
    let mut o = Mat::default();
    imgproc::resize(m, &mut o, Size::new(w, h), 0.0, 0.0, interp)?;
    Ok(o)
}

pub fn crop(m: &Mat, x: i32, y: i32, w: i32, h: i32) -> R<Mat> {
    Mat::roi(m, Rect::new(x, y, w, h))?.try_clone()
}

pub fn count(m: &Mat) -> i32 {
    cvc::count_non_zero(m).unwrap_or(0)
}

/// Copy `src` into `dst` where `mask` is set.
pub fn put_masked(src: &Mat, dst: &mut Mat, mask: &Mat) -> R<()> {
    src.copy_to_masked(dst, mask)
}

pub fn max_val(m: &Mat) -> (f64, (i32, i32)) {
    let mut mx = 0.0;
    let mut loc = Point::default();
    let _ = cvc::min_max_loc(m, None, Some(&mut mx), None, Some(&mut loc), &cvc::no_array());
    (mx, (loc.x, loc.y))
}

/// Values of a single-channel mat as f32 (row-major).
pub fn values_f32(m: &Mat) -> R<Vec<f32>> {
    let mut f = Mat::default();
    m.convert_to_def(&mut f, cvc::CV_32F)?;
    let c = if f.is_continuous() { f } else { f.try_clone()? };
    Ok(c.data_typed::<f32>()?.to_vec())
}

/// Values selected by a mask (non-zero).
pub fn masked_values(m: &Mat, mask: &Mat) -> R<Vec<f32>> {
    let v = values_f32(m)?;
    let mk = if mask.is_continuous() { mask.clone() } else { mask.try_clone()? };
    let b = mk.data_bytes()?;
    Ok(v.into_iter().zip(b.iter()).filter(|(_, &k)| k > 0).map(|(v, _)| v).collect())
}

pub fn percentile(v: &[f32], p: f64) -> f64 {
    let mut s = v.to_vec();
    crate::core::hbvcal::autocal::percentile(&mut s, p)
}

pub fn mean(v: &[f32]) -> f64 {
    if v.is_empty() {
        return 0.0;
    }
    v.iter().map(|&x| x as f64).sum::<f64>() / v.len() as f64
}

pub fn var(v: &[f32]) -> f64 {
    let m = mean(v);
    if v.is_empty() {
        return 0.0;
    }
    v.iter().map(|&x| (x as f64 - m).powi(2)).sum::<f64>() / v.len() as f64
}

pub fn decode_jpeg(data: &[u8]) -> Option<Mat> {
    let m = opencv::imgcodecs::imdecode(&Vector::<u8>::from_slice(data), opencv::imgcodecs::IMREAD_COLOR).ok()?;
    if m.empty() { None } else { Some(m) }
}

pub fn encode_jpeg(m: &Mat, quality: i32) -> Option<Vec<u8>> {
    let mut buf = Vector::<u8>::new();
    let params = Vector::<i32>::from_slice(&[opencv::imgcodecs::IMWRITE_JPEG_QUALITY, quality]);
    opencv::imgcodecs::imencode(".jpg", m, &mut buf, &params).ok()?;
    Some(buf.to_vec())
}

/// BGR mat -> tightly packed RGB bytes (for GdkTexture).
pub fn to_rgb_bytes(m: &Mat) -> R<(i32, i32, Vec<u8>)> {
    let mut rgb = Mat::default();
    if m.channels() == 1 {
        imgproc::cvt_color_def(m, &mut rgb, imgproc::COLOR_GRAY2RGB)?;
    } else {
        imgproc::cvt_color_def(m, &mut rgb, imgproc::COLOR_BGR2RGB)?;
    }
    let c = if rgb.is_continuous() { rgb } else { rgb.try_clone()? };
    Ok((c.cols(), c.rows(), c.data_bytes()?.to_vec()))
}
