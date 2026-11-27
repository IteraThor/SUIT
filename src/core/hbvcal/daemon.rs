//! Keep the saved camera settings applied while Autodarts runs.
//!
//! Runs as the user service suit-camera-keeper, ordered before autodarts.service (see service.rs):
//!     suit camera-keeper [--interval 2] [--once]
//!
//! At its first start of a boot it makes the default preset (if one is set) the kept profiles.
//!
//! Per camera with a saved profile (matched by USB port):
//!   * HBV camera with hidden layers (optimized), only after the user confirmed HBV cameras:
//!     - as soon as the device appears: unlock + write the MJPEG Q byte (before the stream starts)
//!     - while it is streaming: if any UVC control or sensor register differs from the profile, re-apply all
//!     - frame overflow guard: if Autodarts runs detection but gets 0 fps from this camera for STALL_S, the saved
//!       Q is raised two levels and written.
//!   * any other profile (standard controls only): whenever a UVC control differs, re-apply the standard
//!     controls. No extension-unit command is ever sent to these cameras.
use super::autodarts::{autodarts_index, camera_fps};
use super::profiles;
use super::settings::{apply_prestream, apply_streaming, apply_uvc, inactive, q_above, q_addr, Settings};
use super::v4l2::{monotonic, Camera};
use super::xu::Xu;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::os::unix::fs::MetadataExt;
use std::sync::atomic::{AtomicBool, Ordering};

/// one camera at 0 fps this long while Autodarts runs detection: taken as an MJPEG frame overflow
pub const STALL_S: f64 = 8.0;

pub fn cameras() -> Vec<String> {
    let mut v: Vec<String> = glob::glob("/dev/v4l/by-path/*-usb-*-video-index0")
        .map(|g| g.flatten().map(|p| p.to_string_lossy().to_string()).collect())
        .unwrap_or_default();
    v.sort();
    v
}

/// The sensor is only powered (and its ID readable) while some process streams.
pub fn is_streaming(xu: &Xu) -> bool {
    matches!((xu.sensor_read(0x300B), xu.sensor_read(0x300A)), (Ok(0x10), Ok(0x27)))
}

pub fn differences(
    cam: &Camera,
    xu: Option<&Xu>,
    s: &Settings,
    skip: &HashMap<String, String>,
) -> std::io::Result<BTreeMap<String, (i64, i64)>> {
    let mut diff = BTreeMap::new();
    for (name, want) in &s.uvc {
        if skip.contains_key(name) || inactive(&s.uvc, name) {
            continue;
        }
        let Ok(have) = cam.get(name) else { continue };
        if have as i64 != *want {
            diff.insert(name.clone(), (have as i64, *want));
        }
    }
    let Some(xu) = xu else { return Ok(diff) };
    if s.uvc.get("auto_exposure").copied().unwrap_or(1) != 1 {
        // auto exposure drives the sensor gain itself
        return Ok(diff);
    }
    for (reg, want) in &s.sensor {
        let have = xu.sensor_read(*reg)? as i64;
        if have != *want {
            diff.insert(format!("sensor 0x{:04X}", reg), (have, *want));
        }
    }
    Ok(diff)
}

fn fmt_diff(d: &BTreeMap<String, (i64, i64)>) -> String {
    d.iter().map(|(k, v)| format!("{} {}->{}", k, v.0, v.1)).collect::<Vec<_>>().join(", ")
}

#[derive(Default)]
pub struct Tracker {
    pub inode: Option<u64>,
    pub applies: u32,
    /// standard controls this camera refused: not retried every tick
    pub refused: HashMap<String, String>,
    pub stall_since: Option<f64>,
    pub stall_warned: bool,
}

fn inode(dev: &str) -> Option<u64> {
    std::fs::metadata(dev).ok().map(|m| m.ino())
}

impl Tracker {
    fn new(dev: &str) -> Tracker {
        Tracker { inode: inode(dev), ..Default::default() }
    }
}

/// Has Autodarts been getting no frames from this camera for STALL_S (while it runs detection)?
fn stalled(dev: &str, t: &mut Tracker, fps: &Option<Vec<f64>>) -> bool {
    let idx = fps.as_ref().and_then(|_| autodarts_index(dev));
    let (Some(fps), Some(idx)) = (fps, idx) else {
        t.stall_since = None;
        return false;
    };
    if idx >= fps.len() || fps[idx] > 0.0 {
        t.stall_since = None;
        return false;
    }
    let now = monotonic();
    let since = *t.stall_since.get_or_insert(now);
    if now - since < STALL_S {
        return false;
    }
    t.stall_since = Some(now); // give Autodarts' reconnect time to pick up a change before judging again
    true
}

pub fn tick(trackers: &mut HashMap<String, Tracker>) {
    let present: HashSet<String> = cameras().into_iter().collect();
    let mut fps_cache: Option<Option<Vec<f64>>> = None;
    trackers.retain(|dev, _| {
        let keep = present.contains(dev);
        if !keep {
            log::info!("{} disappeared", dev);
        }
        keep
    });
    let mut devs: Vec<&String> = present.iter().collect();
    devs.sort();
    for dev in devs {
        let Some(s) = profiles::get_settings(dev) else { continue };
        let ino = inode(dev);
        let fresh = trackers.get(dev).map(|t| t.inode != ino).unwrap_or(true);
        if fresh {
            // new or re-enumerated device
            trackers.insert(dev.clone(), Tracker::new(dev));
            log::info!("{} appeared (profile {})", dev, s.label());
        }
        let t = trackers.get_mut(dev).unwrap();
        if !((s.mjpeg_q.is_some() || !s.sensor.is_empty() || !s.isp.is_empty()) && profiles::hidden_ok(dev)) {
            keep_uvc(dev, t, &s);
            continue;
        }
        let r = (|| -> std::io::Result<()> {
            let xu = Xu::open(dev)?;
            xu.unlock()?;
            let width: u32 = profiles::get(dev)
                .and_then(|p| p["meta"]["resolution"].as_str().map(String::from))
                .unwrap_or_else(|| "1280x960".into())
                .split('x')
                .next()
                .and_then(|w| w.parse().ok())
                .unwrap_or(1280);
            if let Some(q) = s.mjpeg_q {
                let have = xu.mem_read(q_addr(&xu, width)?, 1)?[0] as i64;
                if have != q {
                    let streaming = is_streaming(&xu);
                    apply_prestream(&xu, &s, width)?;
                    log::info!(
                        "{} MJPEG Q {} -> {}{}",
                        dev,
                        have,
                        q,
                        if streaming { " (camera already streaming: applies at next stream start)" } else { "" }
                    );
                }
                let fps = fps_cache.get_or_insert_with(camera_fps).clone();
                if stalled(dev, t, &fps) {
                    let new = q_above(q, 2);
                    if new != q {
                        profiles::set_q(dev, new, &format!("Autodarts got no frames at Q{} for {:.0} s", q, STALL_S));
                        apply_prestream(&xu, &Settings { mjpeg_q: Some(new), ..Default::default() }, width)?;
                        log::warn!(
                            "{}: Autodarts gets no frames at MJPEG Q {} (frames too big for the camera); saved and wrote Q {}",
                            dev,
                            q,
                            new
                        );
                    } else if !t.stall_warned {
                        t.stall_warned = true;
                        log::warn!("{}: Autodarts gets no frames even at stock MJPEG Q {}", dev, q);
                    }
                }
            }
            if !is_streaming(&xu) {
                return Ok(());
            }
            let mut cam = Camera::open(dev)?;
            let d = differences(&cam, Some(&xu), &s, &HashMap::new())?;
            if !d.is_empty() {
                apply_streaming(&mut cam, &xu, &s, 0)?;
                t.applies += 1;
                log::info!("{} re-applied settings (#{}), changed: {}", dev, t.applies, fmt_diff(&d));
            }
            Ok(())
        })();
        if let Err(e) = r {
            log::warn!("{}: {}", dev, e);
        }
    }
}

/// Standard controls only: works on any UVC camera, streaming or not.
fn keep_uvc(dev: &str, t: &mut Tracker, s: &Settings) {
    let r = (|| -> std::io::Result<()> {
        let cam = Camera::open(dev)?;
        let d = differences(&cam, None, s, &t.refused)?;
        if d.is_empty() {
            return Ok(());
        }
        let uvc: BTreeMap<String, i64> =
            s.uvc.iter().filter(|(k, _)| !t.refused.contains_key(*k)).map(|(k, v)| (k.clone(), *v)).collect();
        let refused = apply_uvc(&cam, &uvc, false)?;
        t.applies += 1;
        log::info!("{} re-applied standard controls (#{}), changed: {}", dev, t.applies, fmt_diff(&d));
        for (k, e) in &refused {
            log::warn!("{} refuses {}={} ({}): not retried until it reconnects", dev, k, s.uvc[k], e);
        }
        t.refused.extend(refused);
        Ok(())
    })();
    if let Err(e) = r {
        log::warn!("{}: {}", dev, e);
    }
}

static STOP: AtomicBool = AtomicBool::new(false);

extern "C" fn on_term(_: libc::c_int) {
    STOP.store(true, Ordering::SeqCst);
}

pub fn main(args: &[String]) -> i32 {
    let mut interval = 2.0f64;
    let mut once = false;
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--interval" => interval = it.next().and_then(|v| v.parse().ok()).unwrap_or(2.0),
            "--once" => once = true,
            _ => {}
        }
    }
    unsafe { libc::signal(libc::SIGTERM, on_term as *const () as libc::sighandler_t) };
    let mut trackers = HashMap::new();
    log::info!("camera keeper started, profiles: {}", profiles::store().display());
    if let Some(name) = profiles::load_default_preset_once() {
        log::info!("loaded the default preset {:?} for this boot", name);
    }
    while !STOP.load(Ordering::SeqCst) {
        tick(&mut trackers);
        if once {
            break;
        }
        std::thread::sleep(std::time::Duration::from_secs_f64(interval));
    }
    0
}
