//! Stream several cameras at once and record stability (fps, drops, broken frames, gaps, bandwidth).
//!
//! Used by autocal::calibrate_all to pick the best MJPEG quality all cameras can stream together.
//! Kernel USB/UVC errors logged during the run are counted from `journalctl -k`.
use super::capture::{dropped, valid_jpeg, START_DISCARD};
use super::settings::{apply_prestream, apply_streaming, Settings};
use super::v4l2::{monotonic, Camera};
use super::xu::Xu;
use std::sync::{Arc, Barrier};

pub const SETTLE_S: f64 = 2.0;

#[derive(Debug, Clone, Default)]
pub struct WorkerStats {
    pub fps: f64,
    pub dropped: u64,
    pub broken: usize,
    pub gap_max_ms: f64,
    pub gaps_over_1_5x: usize,
    pub kb_mean: f64,
    pub kb_max: f64,
    pub mbps: f64,
    pub frames: usize,
}

#[derive(Debug, Clone, Default)]
pub struct WorkerResult {
    pub dev: String,
    pub stats: Option<WorkerStats>,
    pub error: Option<String>,
    /// ENOSPC: the USB controller could not reserve bandwidth for this stream
    pub errno: Option<i32>,
}

pub fn run_worker(dev: &str, s: &Settings, w: u32, h: u32, fps: u32, seconds: f64, start: &Barrier) -> WorkerResult {
    let mut res = WorkerResult { dev: dev.to_string(), ..Default::default() };
    let mut waited = false;
    let r = (|| -> std::io::Result<WorkerStats> {
        let xu = Xu::open(dev)?;
        apply_prestream(&xu, s, w)?;
        let mut cam = Camera::open(dev)?;
        cam.configure(w, h, "MJPG", fps)?;
        start.wait();
        waited = true;
        cam.start(4)?;
        let (mut seqs, mut ts, mut sizes, mut bad) = (Vec::new(), Vec::new(), Vec::new(), 0usize);
        let body = (|| -> std::io::Result<()> {
            for _ in 0..START_DISCARD {
                cam.read(5.0)?;
            }
            apply_streaming(&mut cam, &xu, s, 0)?;
            // a mode switch causes a one-time 1–2 frame hiccup right after apply: settle first
            let t_settle = monotonic() + SETTLE_S;
            while monotonic() < t_settle {
                cam.read(5.0)?;
            }
            let t_end = monotonic() + seconds;
            while monotonic() < t_end {
                let f = cam.read(5.0)?;
                if !valid_jpeg(&f.data) {
                    bad += 1;
                }
                seqs.push(f.sequence);
                ts.push(f.timestamp);
                sizes.push(f.data.len() as f64);
            }
            Ok(())
        })();
        let _ = cam.stop();
        body?;
        if ts.len() < 2 {
            return Err(std::io::Error::other("no frames"));
        }
        let span = ts[ts.len() - 1] - ts[0];
        let dt: Vec<f64> = ts.windows(2).map(|w| w[1] - w[0]).collect();
        let mean = sizes.iter().sum::<f64>() / sizes.len() as f64;
        Ok(WorkerStats {
            fps: (ts.len() - 1) as f64 / span,
            dropped: dropped(&seqs),
            broken: bad,
            gap_max_ms: dt.iter().cloned().fold(0.0, f64::max) * 1000.0,
            gaps_over_1_5x: dt.iter().filter(|&&d| d > 1.5 / fps as f64).count(),
            kb_mean: mean / 1024.0,
            kb_max: sizes.iter().cloned().fold(0.0, f64::max) / 1024.0,
            mbps: mean * 8.0 / 1e6 * ts.len() as f64 / span,
            frames: ts.len(),
        })
    })();
    if !waited {
        // keep the others from waiting forever on a worker that failed before the start line
        start.wait();
    }
    match r {
        Ok(s) => res.stats = Some(s),
        Err(e) => {
            res.errno = e.raw_os_error();
            res.error = Some(format!("{:?}: {}", e.kind(), e));
        }
    }
    res
}

/// Stream all `devs` together for `seconds` with the same settings each; one result per camera.
pub fn run_all(devs: &[(String, Settings)], w: u32, h: u32, fps: u32, seconds: f64) -> Vec<WorkerResult> {
    let barrier = Arc::new(Barrier::new(devs.len()));
    let handles: Vec<_> = devs
        .iter()
        .cloned()
        .map(|(d, s)| {
            let b = barrier.clone();
            std::thread::spawn(move || run_worker(&d, &s, w, h, fps, seconds, &b))
        })
        .collect();
    handles.into_iter().map(|h| h.join().unwrap_or_default()).collect()
}

pub const ERROR_KEYS: [&str; 6] = ["uvcvideo", "xhci", "-71", "disconnect", "reset", "URB"];

/// Number of USB/UVC problem lines in this boot's kernel log (compare before/after a run).
pub fn kernel_error_lines() -> usize {
    let out = crate::core::util::run(&["journalctl", "-k", "-b", "--no-pager", "-q"], Some(30)).stdout;
    out.lines()
        .filter(|l| ERROR_KEYS.iter().any(|k| l.contains(k)) && !l.contains("Found UVC") && !l.contains("registered"))
        .count()
}
