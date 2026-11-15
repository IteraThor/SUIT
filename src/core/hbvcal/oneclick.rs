//! One-click workflow behind the Camera Optimizer page: support check, optimize and restore.
//!
//! Safety: the tuning back door (XU unlock + memory writes) is only ever used on cameras that pass `support()`:
//! USB ID 0bdc:2710 (HBV OV2710 / Realtek bridge) AND a firmware build whose memory layout we verified. Other
//! cameras are never touched. The user must also confirm once that they use HBV OV2710 cameras.
use super::autocal::{calibrate_all, start_clean, CalError, CalResult, CalibrateAllOpts, PREVIEW};
use super::autodarts::{calibrate_board, set_stream_size, stream_size};
use super::capture::burst;
use super::daemon::{cameras, is_streaming, tick};
use super::settings::{apply_streaming, q_addr, q_addr_table, q_default, Settings, Q_LEVELS};
use super::v4l2::Camera;
use super::xu::Xu;
use super::{board, live, profiles, service};
use crate::core::i18n::{tr, trf};
use crate::core::util::{run, write_atomic};
use anyhow::{anyhow, bail, Result};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

pub const AUTODARTS_START_TIMEOUT: f64 = 60.0;
/// Resolutions the optimizer tries, best picture first.
pub const RESOLUTIONS: [(u32, u32); 2] = [(1280, 960), (1280, 720)];
pub const STEP_DOWN_Q: i64 = 11;

pub fn before_after_dir() -> PathBuf {
    profiles::store().parent().unwrap().join("beforeafter")
}
pub fn pending_file() -> PathBuf {
    profiles::store().parent().unwrap().join("pending.json")
}
pub fn pending_pictures() -> PathBuf {
    profiles::store().parent().unwrap().join("beforeafter-pending")
}

#[derive(Debug, Clone, Default)]
pub struct Support {
    pub device: String,
    pub port: String,
    pub usb_id: Option<String>,
    pub supported: bool,
    pub reason: String,
    pub firmware: String,
    pub optimized: bool,
}

/// Is this camera one we can safely optimize? Never sends XU commands to non-HBV cameras.
pub fn support(dev: &str) -> Support {
    let uid = profiles::usb_id(dev);
    let mut out = Support { device: dev.into(), port: profiles::port_key(dev), usb_id: uid.clone(), ..Default::default() };
    if uid.as_deref() != Some(profiles::HBV_USB_ID) {
        out.reason = trf(
            "not an HBV OV2710 camera (USB ID {uid}, expected {expected})",
            &[("uid", &uid.unwrap_or_else(|| "None".into())), ("expected", &profiles::HBV_USB_ID)],
        );
        return out;
    }
    let build = (|| -> std::io::Result<String> {
        let xu = Xu::open(dev)?;
        xu.unlock()?;
        xu.firmware_build()
    })();
    let build = match build {
        Ok(b) => b,
        Err(e) => {
            out.reason = trf("camera did not answer the tuning interface ({error})", &[("error", &e)]);
            return out;
        }
    };
    out.firmware = build.clone();
    if q_addr_table(&build).is_none() {
        out.reason = trf("unknown firmware build {build}: memory layout not verified", &[("build", &build)]);
        return out;
    }
    out.supported = true;
    out.optimized = profiles::is_calibrated(profiles::get(dev).as_ref());
    out
}

pub fn systemctl(action: &str, unit: &str) -> String {
    let r = run(&["systemctl", "--user", action, unit], Some(90));
    format!("{}{}", r.stdout, r.stderr).trim().to_string()
}

pub fn autodarts_active() -> bool {
    systemctl("is-active", "autodarts") == "active"
}

#[derive(Debug, Clone, PartialEq)]
pub struct Step {
    pub title: String,
    /// running | done | skipped | failed
    pub state: String,
}

#[derive(Default)]
struct JobState {
    name: String,
    steps: Vec<Step>,
    log: Vec<String>,
    result: Value,
    error: String,
}

/// A background task with named steps the page can show as a checklist.
#[derive(Default)]
pub struct Job {
    running: AtomicBool,
    st: Mutex<JobState>,
}

#[derive(Debug, Clone, Default)]
pub struct JobView {
    pub name: String,
    pub running: bool,
    pub steps: Vec<Step>,
    pub log: Vec<String>,
    pub result: Value,
    pub error: String,
}

impl Job {
    pub fn start<F>(self: &Arc<Self>, name: &str, f: F) -> bool
    where
        F: FnOnce(&Job) -> Result<()> + Send + 'static,
    {
        if self.running.swap(true, Ordering::SeqCst) {
            return false;
        }
        {
            let mut s = self.st.lock().unwrap();
            *s = JobState { name: name.into(), result: json!({}), ..Default::default() };
        }
        let me = self.clone();
        let name = name.to_string();
        std::thread::spawn(move || {
            log::info!("{} started", name);
            let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| f(&me)));
            let err = match r {
                Ok(Ok(())) => None,
                Ok(Err(e)) => Some(e.to_string()),
                Err(_) => Some("internal error (panic)".to_string()),
            };
            match err {
                None => log::info!("{} finished", name),
                Some(e) => {
                    log::error!("{} failed: {}", name, e);
                    let mut s = me.st.lock().unwrap();
                    s.error = e;
                    if let Some(last) = s.steps.last_mut() {
                        if last.state == "running" {
                            last.state = "failed".into();
                        }
                    }
                }
            }
            me.running.store(false, Ordering::SeqCst);
        });
        true
    }

    pub fn running(&self) -> bool {
        self.running.load(Ordering::SeqCst)
    }

    /// A detail line for the page's log (and suit.log).
    pub fn say(&self, msg: &str) {
        let mut s = self.st.lock().unwrap();
        s.log.push(msg.into());
        log::info!("{}: {}", s.name, msg);
    }

    pub fn step(&self, title: &str) {
        let mut s = self.st.lock().unwrap();
        if let Some(last) = s.steps.last_mut() {
            if last.state == "running" {
                last.state = "done".into();
            }
        }
        s.steps.push(Step { title: tr(title), state: "running".into() });
        log::info!("{} step: {}", s.name, title);
    }

    /// The current step did not work out and the job goes on: shown as a yellow note, not a failure.
    pub fn skip(&self, note: &str) {
        let mut s = self.st.lock().unwrap();
        if let Some(last) = s.steps.last_mut() {
            if last.state == "running" {
                last.state = "skipped".into();
                last.title += &format!(": {}", tr(note));
                log::info!("step skipped: {}", last.title);
            }
        }
    }

    pub fn finish(&self) {
        let mut s = self.st.lock().unwrap();
        if let Some(last) = s.steps.last_mut() {
            if last.state == "running" {
                last.state = "done".into();
            }
        }
    }

    pub fn set_result(&self, v: Value) {
        self.st.lock().unwrap().result = v;
    }

    pub fn result_set(&self, k: &str, v: Value) {
        let mut s = self.st.lock().unwrap();
        if !s.result.is_object() {
            s.result = json!({});
        }
        s.result[k] = v;
    }

    pub fn view(&self) -> JobView {
        let s = self.st.lock().unwrap();
        let n = s.log.len();
        JobView {
            name: s.name.clone(),
            running: self.running(),
            steps: s.steps.clone(),
            log: s.log[n.saturating_sub(40)..].to_vec(),
            result: s.result.clone(),
            error: s.error.clone(),
        }
    }
}

/// the one optimize/apply/restore job of this process; pages poll it, it outlives them
pub static JOB: once_cell::sync::Lazy<Arc<Job>> = once_cell::sync::Lazy::new(|| Arc::new(Job::default()));

fn wait(cond: impl Fn() -> bool, timeout: f64, poll: f64) -> bool {
    let end = std::time::Instant::now() + std::time::Duration::from_secs_f64(timeout);
    while std::time::Instant::now() < end {
        if cond() {
            return true;
        }
        std::thread::sleep(std::time::Duration::from_secs_f64(poll));
    }
    cond()
}

fn wait_cameras_free(devs: &[String], timeout: f64) -> bool {
    wait(
        || {
            devs.iter().all(|d| {
                (|| -> std::io::Result<()> {
                    let mut cam = Camera::open(d)?;
                    let (w, h) = stream_size();
                    cam.configure(w, h, "MJPG", 30)?;
                    cam.start(4)?;
                    cam.stop()
                })()
                .is_ok()
            })
        },
        timeout,
        1.0,
    )
}

fn wait_streaming(devs: &[String], timeout: f64) -> bool {
    wait(|| devs.iter().all(|d| Xu::open(d).map(|xu| is_streaming(&xu)).unwrap_or(false)), timeout, 1.0)
}

/// One complete camera frame (raw JPEG). With `s`, the settings are applied first (as the keeper would).
pub fn snapshot_frame(dev: &str, s: Option<&Settings>, say: &(dyn Fn(&str) + Sync), size: Option<(u32, u32)>) -> std::io::Result<Vec<u8>> {
    let (width, height) = size.unwrap_or_else(stream_size);
    let xu = Xu::open(dev)?;
    let mut cam = Camera::open(dev)?;
    xu.unlock()?;
    let q = match s.and_then(|s| s.mjpeg_q) {
        Some(q) => q,
        None => xu.mem_read(q_addr(&xu, width)?, 1)?[0] as i64,
    };
    start_clean(&mut cam, &xu, q, width, height, 30, say)?;
    let r = (|| -> std::io::Result<Vec<u8>> {
        if let Some(s) = s {
            apply_streaming(&mut cam, &xu, s, 0)?;
        }
        Ok(burst(&mut cam, 1, 20, 2.0, 0)?.frames.pop().unwrap().data)
    })();
    let _ = cam.stop();
    r
}

pub fn save_before_after(dev: &str, which: &str, data: &[u8], note: &str, root: &Path) -> std::io::Result<()> {
    let d = root.join(profiles::port_key(dev));
    std::fs::create_dir_all(&d)?;
    std::fs::write(d.join(format!("{}.jpg", which)), data)?;
    let mp = d.join("meta.json");
    let mut meta = crate::core::util::read_json(&mp).unwrap_or_else(|| json!({}));
    meta[which] = json!({"time": chrono::Local::now().format("%Y-%m-%d %H:%M").to_string(), "note": note});
    std::fs::write(mp, meta.to_string())
}

pub fn clear_before_after(dev: &str, root: Option<&Path>) {
    let r = root.map(|p| p.to_path_buf()).unwrap_or_else(before_after_dir);
    let _ = std::fs::remove_dir_all(r.join(profiles::port_key(dev)));
}

#[derive(Debug, Clone)]
pub struct Pictures {
    pub device: String,
    pub port: String,
    pub meta: Value,
    pub dir: PathBuf,
    pub q: (Option<i64>, Option<i64>),
    pub pending: bool,
}

fn pictures(dev: &str, root: &Path, meta_q: &Value) -> Option<Pictures> {
    let d = root.join(profiles::port_key(dev));
    if !(d.join("before.jpg").exists() && d.join("after.jpg").exists()) {
        return None;
    }
    let meta = crate::core::util::read_json(d.join("meta.json")).unwrap_or_else(|| json!({}));
    Some(Pictures {
        device: dev.into(),
        port: profiles::port_key(dev),
        meta,
        dir: d,
        q: (meta_q["previous"]["mjpeg_q"].as_i64(), meta_q["mjpeg_q"].as_i64()),
        pending: false,
    })
}

/// Pictures of a calibration waiting to be applied, else of the cameras that are optimized now.
pub fn before_after_info() -> Vec<Pictures> {
    let pend = load_pending();
    let mut out = Vec::new();
    for dev in cameras() {
        let port = profiles::port_key(&dev);
        let info = if !pend.is_empty() {
            pend.get(&port).and_then(|p| pictures(&dev, &pending_pictures(), &p["meta"]))
        } else {
            let prof = profiles::get(&dev);
            if profiles::is_calibrated(prof.as_ref()) {
                pictures(&dev, &before_after_dir(), &prof.unwrap()["meta"])
            } else {
                None
            }
        };
        if let Some(mut i) = info {
            i.pending = !pend.is_empty();
            out.push(i);
        }
    }
    out
}

// --- a finished calibration waits for the user's decision (apply or discard) ---

pub fn load_pending() -> Map<String, Value> {
    crate::core::util::read_json(pending_file())
        .and_then(|v| v["cameras"].as_object().cloned())
        .unwrap_or_default()
}

/// (width, height) the pending calibration was made at, for Autodarts' config on apply.
pub fn pending_size() -> Option<(u32, u32)> {
    let v = crate::core::util::read_json(pending_file())?;
    let r = v["resolution"].as_array()?;
    Some((r.first()?.as_u64()? as u32, r.get(1)?.as_u64()? as u32))
}

/// Forget a calibration that was not applied (the cameras already run their previous settings).
pub fn discard() {
    let _ = std::fs::remove_file(pending_file());
    let _ = std::fs::remove_dir_all(pending_pictures());
    log::info!("pending calibration discarded");
}

/// MJPEG Q of the previous profile (stock without one) for the next stream start.
fn put_back(dev: &str) -> std::io::Result<()> {
    let prev = profiles::get_settings(dev);
    let xu = Xu::open(dev)?;
    xu.unlock()?;
    let build = xu.firmware_build()?;
    match prev.and_then(|p| p.mjpeg_q) {
        Some(q) if profiles::hidden_ok(dev) => xu.mem_write(q_addr(&xu, stream_size().0)?, &[q as u8])?,
        _ => {
            if let Some(t) = q_addr_table(&build) {
                for (cls, addr) in t {
                    xu.mem_write(addr, &[q_default(cls) as u8])?;
                }
            }
        }
    }
    Ok(())
}

/// calibrate_all at the first of RESOLUTIONS that streams cleanly within STEP_DOWN_Q (the last one at any Q).
fn calibrate_best(job: &Job, devs: &[String], fps: u32) -> Result<(BTreeMap<String, CalResult>, (u32, u32))> {
    let mut previous = BTreeMap::new();
    let say = |m: &str| job.say(m);
    let step = |m: &str| job.step(m);
    let skip = |m: &str| job.skip(m);
    for (i, &(w, h)) in RESOLUTIONS.iter().enumerate() {
        let last = i == RESOLUTIONS.len() - 1;
        job.say(&format!(
            "calibrating at {}x{}{}",
            w,
            h,
            if last { String::new() } else { format!(", compression up to level {}", STEP_DOWN_Q) }
        ));
        let o = CalibrateAllOpts {
            fps,
            width: w,
            height: h,
            q_candidates: Q_LEVELS.to_vec(),
            check_s: 20.0,
            confirm_s: 60.0,
            q_margin: 1,
            q_max: if last { None } else { Some(STEP_DOWN_Q) },
            save: false,
            log: &say,
            step: &step,
            skipped: &skip,
        };
        match calibrate_all(devs, &o, &mut previous) {
            Ok(r) => return Ok((r, (w, h))),
            Err(e @ (CalError::UsbBandwidth(_) | CalError::CompressionLimit(_) | CalError::Other(_))) if !last => {
                let (nw, nh) = RESOLUTIONS[i + 1];
                job.say(&format!("{}x{} not usable: {}", w, h, e));
                job.skip(&trf("not possible at {size}, see the log", &[("size", &format!("{}x{}", w, h))]));
                job.step(&trf("Stepping down to {size}", &[("size", &format!("{}x{}", nw, nh))]));
                for d in devs {
                    let _ = put_back(d); // the failed attempt may have left its Q in camera memory
                }
            }
            Err(e) => bail!("{}", e),
        }
    }
    unreachable!()
}

/// Calibrate and take before/after pictures. Nothing is kept: the result waits in PENDING for apply() or
/// discard(), and the cameras go back to their previous settings meanwhile.
pub fn optimize(job: &Job, fps: u32) -> Result<()> {
    let ad_size = stream_size();
    let devs: Vec<String> = cameras().iter().map(|d| support(d)).filter(|s| s.supported).map(|s| s.device).collect();
    if devs.is_empty() {
        bail!("no supported HBV OV2710 camera found");
    }
    let was_running = autodarts_active();
    service::migrate_legacy();
    let keeper_was_on = service::is_enabled();
    service::stop(); // it would re-apply the old profile onto the calibration stream
    live::pause_live(true);
    PREVIEW.clear();
    discard();
    let say = |m: &str| job.say(m);
    let r = (|| -> Result<()> {
        job.step("Stopping Autodarts");
        if was_running {
            systemctl("stop", "autodarts");
        }
        if !wait_cameras_free(&devs, 20.0) {
            bail!("cameras are still in use by another program");
        }
        job.step("Taking 'before' pictures");
        let mut before: BTreeMap<String, Vec<u8>> = BTreeMap::new();
        for d in &devs {
            match snapshot_frame(d, None, &say, None) {
                Ok(b) => {
                    PREVIEW.set(Some(d), Some("Before: the settings Autodarts used"), Some(&b));
                    before.insert(d.clone(), b);
                }
                Err(e) => job.say(&format!("no 'before' picture of {}: {}", profiles::port_key(d), e)),
            }
        }
        if before.len() < devs.len() {
            job.skip("not every camera, see the log");
        }
        job.say(&format!("resolution in Autodarts: {}x{}", ad_size.0, ad_size.1));
        let (mut results, size) = calibrate_best(job, &devs, fps)?;
        job.say(&format!("calibrated at {}x{}", size.0, size.1));
        if size != ad_size {
            job.step(&trf("Taking 'before' pictures again at {size}", &[("size", &format!("{}x{}", size.0, size.1))]));
            for (d, r) in &results {
                match snapshot_frame(d, Some(&Settings::from_json(&r.meta["previous"])), &say, Some(size)) {
                    Ok(b) => {
                        before.insert(d.clone(), b);
                    }
                    Err(e) => {
                        before.remove(d);
                        job.say(&format!("no 'before' picture of {} at {}x{}: {}", profiles::port_key(d), size.0, size.1, e));
                    }
                }
            }
        }
        job.step("Taking 'after' pictures");
        for (d, r) in &results {
            let after = match snapshot_frame(d, Some(&r.settings), &say, Some(size)) {
                Ok(a) => a,
                Err(e) => {
                    job.say(&format!("no 'after' picture of {}: {}", profiles::port_key(d), e));
                    continue;
                }
            };
            if let Some(b) = before.get(d) {
                save_before_after(d, "before", b, "settings before optimizing", &pending_pictures())?;
            }
            save_before_after(d, "after", &after, &format!("optimized, {}x{}", size.0, size.1), &pending_pictures())?;
            PREVIEW.set(Some(d), Some("After: optimized"), Some(&after));
        }
        let cams: Map<String, Value> = results
            .iter()
            .map(|(d, r)| (profiles::port_key(d), json!({"settings": r.settings.to_json(), "meta": r.meta})))
            .collect();
        write_atomic(
            pending_file(),
            serde_json::to_string_pretty(&json!({"resolution": [size.0, size.1], "cameras": cams}))?.as_bytes(),
        )?;
        job.step("Previous settings back until you apply");
        for d in &devs {
            let _ = put_back(d);
        }
        let res: Map<String, Value> = results
            .iter_mut()
            .map(|(d, r)| {
                (
                    d.clone(),
                    json!({"port": profiles::port_key(d), "settings": r.settings.label(),
                           "white": r.meta["reference"]["white"], "noise": r.meta["reference"]["tnoise"],
                           "mjpeg_q": r.meta["mjpeg_q"], "resolution": format!("{}x{}", size.0, size.1),
                           "resolution_change": size != ad_size, "pending": true}),
                )
            })
            .collect();
        job.set_result(Value::Object(res));
        job.finish();
        Ok(())
    })();
    if keeper_was_on {
        service::start(); // the previous profiles
    }
    if was_running && !autodarts_active() {
        systemctl("start", "autodarts");
    }
    live::pause_live(false);
    r
}

/// Keep the pending calibration: save the profiles, save them as a preset that loads at every boot, set the
/// resolution it was made at in Autodarts, and restart Autodarts so the cameras open with them.
pub fn apply(job: &Job) -> Result<()> {
    let pend = load_pending();
    let size = pending_size();
    let devs: Vec<String> = cameras().into_iter().filter(|d| pend.contains_key(&profiles::port_key(d))).collect();
    if devs.is_empty() {
        bail!("no calibration waiting to be applied (or its cameras are not connected)");
    }
    let was_running = autodarts_active();
    live::pause_live(true);
    let r = (|| -> Result<()> {
        job.step("Saving the calibration");
        for d in &devs {
            let p = &pend[&profiles::port_key(d)];
            profiles::put(d, &Settings::from_json(&p["settings"]), Some(p["meta"].clone()));
            clear_before_after(d, None);
            let src = pending_pictures().join(profiles::port_key(d));
            if src.exists() {
                std::fs::create_dir_all(before_after_dir())?;
                std::fs::rename(&src, before_after_dir().join(profiles::port_key(d)))?;
            }
        }
        let name = format!("Optimized {}", chrono::Local::now().format("%Y-%m-%d %H:%M"));
        job.step(&trf("Saving as default preset “{name}”", &[("name", &name)]));
        live::save_preset(&name)?;
        profiles::set_default_preset(Some(&name));
        job.set_result(json!({"preset": name}));
        discard();
        job.step("Stopping Autodarts");
        if was_running {
            systemctl("stop", "autodarts");
        }
        service::stop();
        let new_size = size.filter(|s| *s != stream_size());
        if let Some((w, h)) = new_size {
            // after the stop: Autodarts writes its config back when it exits
            job.step(&trf("Setting Autodarts to {size}", &[("size", &format!("{}x{}", w, h))]));
            set_stream_size(w, h)?;
            job.result_set("resolution", json!(format!("{}x{}", w, h)));
        }
        wait_cameras_free(&devs, 20.0);
        job.step("Applying the settings");
        tick(&mut Default::default()); // cameras not streaming: writes the MJPEG Q for the next stream start
        service::enable();
        if was_running {
            job.step("Starting Autodarts");
            systemctl("start", "autodarts");
            if !wait_streaming(&devs, AUTODARTS_START_TIMEOUT) {
                job.say("Autodarts did not open all cameras in time; the keeper will apply later");
            }
        }
        if let Some((w, h)) = new_size {
            if devs.iter().any(|d| board::load_homography(d, w, h, None).is_none()) {
                job.step(&trf("Calibrating the board in Autodarts at {size}", &[("size", &format!("{}x{}", w, h))]));
                if was_running && calibrate_board() {
                    job.result_set("board", json!("started"));
                } else {
                    job.result_set("board", json!("needed"));
                    job.skip("start it in Autodarts (no darts in the board)");
                }
            }
        }
        job.finish();
        Ok(())
    })();
    if was_running && !autodarts_active() {
        systemctl("start", "autodarts");
    }
    live::pause_live(false);
    r
}

/// Back to the camera's stock JPEG quality and Autodarts' own control values.
pub fn restore(job: &Job) -> Result<()> {
    let devs: Vec<String> = cameras().iter().map(|d| support(d)).filter(|s| s.supported).map(|s| s.device).collect();
    service::stop();
    service::migrate_legacy();
    let was_running = autodarts_active();
    live::pause_live(true);
    let r = (|| -> Result<()> {
        job.step("Stopping Autodarts");
        if was_running {
            systemctl("stop", "autodarts");
        }
        wait_cameras_free(&devs, 20.0);
        job.step("Restoring stock JPEG quality and controls");
        for d in &devs {
            live::write_stock_q(d).map_err(|e| anyhow!("{}: {}", d, e))?;
            if let Some(prev) = profiles::get(d).and_then(|p| p["meta"].get("previous").cloned()).filter(|v| v.is_object()) {
                let cam = Camera::open(d)?;
                for (name, v) in Settings::from_json(&prev).uvc {
                    let _ = cam.set(&name, v as i32);
                }
            }
            profiles::remove(d);
            clear_before_after(d, None);
        }
        discard();
        if was_running {
            job.step("Starting Autodarts");
            systemctl("start", "autodarts");
        }
        job.finish();
        Ok(())
    })();
    service::sync(); // other cameras may still have kept settings
    if was_running && !autodarts_active() {
        systemctl("start", "autodarts");
    }
    live::pause_live(false);
    r
}
