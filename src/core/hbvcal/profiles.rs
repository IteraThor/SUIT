//! Per-camera profile store (~/.config/hbvcal/profiles.json), keyed by USB port path.
//!
//! All HBV cameras of a model report the same serial (hard-coded in firmware), so a camera is identified by
//! the port it is plugged into (`/dev/v4l/by-path/...`). A profile also records the camera's USB ID (older ones
//! only the serial), and is never used on a different camera that is now plugged into that port.
use super::models;
use super::settings::Settings;
use crate::core::util::home;
use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};

pub type Profiles = Map<String, Value>;

pub fn store() -> PathBuf {
    std::env::var_os("HBVCAL_PROFILES")
        .map(PathBuf::from)
        .unwrap_or_else(|| home().join(".config/hbvcal/profiles.json"))
}
fn store_dir() -> PathBuf {
    store().parent().map(|p| p.to_path_buf()).unwrap_or_else(|| home().join(".config/hbvcal"))
}
pub fn consent_file() -> PathBuf {
    store_dir().join("consent.json")
}
pub fn presets_file() -> PathBuf {
    store_dir().join("presets.json")
}
pub fn default_preset_file() -> PathBuf {
    store_dir().join("default_preset.json")
}
fn boot_mark() -> PathBuf {
    let run = std::env::var("XDG_RUNTIME_DIR")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| format!("/run/user/{}", unsafe { libc::getuid() }));
    PathBuf::from(run).join("hbvcal-default-preset-loaded")
}

fn now() -> String {
    chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string()
}

/// '/dev/v4l/by-path/pci-0000:00:14.0-usb-0:1:1.0-video-index0' -> 'pci-0000:00:14.0-usb-0:1:1.0'.
pub fn port_key(device: &str) -> String {
    let name = Path::new(device).file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
    name.split("-video-index").next().unwrap_or("").to_string()
}

fn sysfs_attr(device: &str, attr: &str) -> Option<PathBuf> {
    let real = std::fs::canonicalize(device).ok()?;
    let name = real.file_name()?.to_string_lossy().to_string();
    let mut p = std::fs::canonicalize(format!("/sys/class/video4linux/{}/device", name)).ok()?;
    for _ in 0..4 {
        if p.join(attr).exists() {
            return Some(p);
        }
        p = p.parent()?.to_path_buf();
    }
    None
}

/// USB serial of the camera behind a V4L2 node (from sysfs), or None.
pub fn serial_of(device: &str) -> Option<String> {
    let p = sysfs_attr(device, "serial")?;
    crate::core::util::read_trim(p.join("serial"))
}

/// 'vvvv:pppp' USB vendor:product of the camera behind a V4L2 node (sysfs), or None.
pub fn usb_id(device: &str) -> Option<String> {
    let p = sysfs_attr(device, "idVendor")?;
    Some(format!(
        "{}:{}",
        crate::core::util::read_trim(p.join("idVendor"))?,
        crate::core::util::read_trim(p.join("idProduct"))?
    ))
}

fn read_obj(p: &Path) -> Option<Map<String, Value>> {
    match crate::core::util::read_json(p)? {
        Value::Object(m) => Some(m),
        _ => None,
    }
}

fn write_store(p: &Path, key: &str, v: Value) {
    let doc = json!({"version": 1, key: v});
    if let Err(e) = crate::core::util::write_atomic(p, serde_json::to_string_pretty(&doc).unwrap().as_bytes()) {
        log::error!("Failed writing {}: {}", p.display(), e);
    }
}

pub fn load_all() -> Profiles {
    read_obj(&store())
        .and_then(|m| m.get("cameras").and_then(|c| c.as_object().cloned()))
        .unwrap_or_default()
}

pub fn save_all(cams: &Profiles) {
    write_store(&store(), "cameras", Value::Object(cams.clone()));
}

/// The saved profile of the camera on this port, also when it was made for another camera.
pub fn get_raw(device: &str) -> Option<Value> {
    load_all().get(&port_key(device)).cloned()
}

/// Was a saved profile (or preset entry) made for a different camera than the one connected now? Compares the
/// USB ID; for older profiles without one, the serial (shared by all cameras of an HBV model).
pub fn made_for_other(saved: &Value, device: &str) -> bool {
    saved_for_other(saved, usb_id(device).as_deref(), serial_of(device).as_deref())
}

/// made_for_other with the connected camera's USB ID and serial given.
pub fn saved_for_other(saved: &Value, usb: Option<&str>, serial: Option<&str>) -> bool {
    if let Some(u) = saved["usb_id"].as_str() {
        return usb.map(|c| c != u).unwrap_or(false);
    }
    match (saved["serial"].as_str(), serial) {
        (Some(a), Some(b)) => a != b,
        _ => false,
    }
}

/// Name of the camera a mismatched profile on this port was made for (None: no profile, or it fits).
pub fn mismatch(device: &str) -> Option<String> {
    let p = get_raw(device)?;
    if !made_for_other(&p, device) {
        return None;
    }
    Some(match p["usb_id"].as_str() {
        Some(u) => models::name_of(Some(u)),
        None => p["model"].as_str().and_then(models::by_key).map(|m| m.name.to_string()).unwrap_or_else(|| "another camera".into()),
    })
}

/// The profile of the camera on this port, if it was made for this camera.
pub fn get(device: &str) -> Option<Value> {
    get_raw(device).filter(|p| !made_for_other(p, device))
}

/// The stream size a profile was calibrated at (meta.resolution "WxH").
pub fn size_of(prof: &Value) -> Option<(u32, u32)> {
    let (w, h) = prof["meta"]["resolution"].as_str()?.split_once('x')?;
    Some((w.parse().ok()?, h.parse().ok()?))
}

pub fn get_settings(device: &str) -> Option<Settings> {
    get(device).map(|p| Settings::from_json(&p["settings"]))
}

pub fn put(device: &str, settings: &Settings, meta: Option<Value>) {
    let mut cams = load_all();
    cams.insert(
        port_key(device),
        json!({
            "settings": settings.to_json(),
            "serial": serial_of(device),
            "usb_id": usb_id(device),
            "model": models::of_device(device).map(|m| m.key),
            "updated": now(),
            "meta": meta.unwrap_or_else(|| json!({})),
        }),
    );
    save_all(&cams);
}

/// Change only the saved MJPEG Q of a camera's profile (the keeper's frame-overflow fix), noting why.
pub fn set_q(device: &str, q: i64, note: &str) {
    let mut cams = load_all();
    let Some(p) = cams.get_mut(&port_key(device)) else { return };
    p["settings"]["mjpeg_q"] = json!(q);
    if !p["meta"].is_object() {
        p["meta"] = json!({});
    }
    let meta = p["meta"].as_object_mut().unwrap();
    if meta.contains_key("mjpeg_q") {
        meta.insert("mjpeg_q".into(), json!(q));
    }
    meta.insert("q_raised".into(), json!({"time": now(), "to": q, "why": note}));
    p["updated"] = json!(now());
    save_all(&cams);
}

pub fn remove(device: &str) {
    let mut cams = load_all();
    cams.remove(&port_key(device));
    save_all(&cams);
}

pub fn consent() -> Option<Value> {
    crate::core::util::read_json(consent_file())
}

/// Model keys the user confirmed (consent files from before models were recorded: OV2710).
pub fn consented_models() -> Vec<String> {
    let Some(c) = consent() else { return Vec::new() };
    if !c["hbv_confirmed"].as_bool().unwrap_or(false) {
        return Vec::new();
    }
    match c["models"].as_array() {
        Some(a) => a.iter().filter_map(|v| v.as_str().map(String::from)).collect(),
        None => vec![models::OV2710.key.to_string()],
    }
}

/// yes: these models are confirmed (added to the ones confirmed before). no: the hidden settings stay off.
pub fn set_consent(hbv_confirmed: bool, model_keys: &[&str]) {
    let mut keys = if hbv_confirmed { consented_models() } else { Vec::new() };
    for k in model_keys {
        if hbv_confirmed && !keys.iter().any(|x| x == k) {
            keys.push(k.to_string());
        }
    }
    let _ = crate::core::util::write_file(
        consent_file(),
        &json!({"hbv_confirmed": hbv_confirmed, "models": keys, "time": now()}).to_string(),
    );
}

/// A known HBV camera model (models.rs)?
pub fn hbv(device: &str) -> bool {
    models::of_device(device).is_some()
}

/// May the tuning back door (MJPEG quality, sensor registers) be used on this camera? Only on known HBV models
/// with verified firmware builds, and only after the user confirmed that they use cameras of that model.
pub fn hidden_ok(device: &str) -> bool {
    match models::of_device(device) {
        Some(m) => m.hidden_capable() && consented_models().iter().any(|k| k == m.key),
        None => false,
    }
}

/// A profile made by the optimizer (autocal stamps the calibration time), not one kept from manual changes.
pub fn is_calibrated(prof: Option<&Value>) -> bool {
    prof.map(|p| p["meta"].get("calibrated").is_some()).unwrap_or(false)
}

pub fn uvc_only(s: &Settings) -> Settings {
    Settings { uvc: s.uvc.clone(), ..Default::default() }
}

// --- named presets: a snapshot of every camera's settings, loaded all at once ---

/// {preset name: {port key: settings json}}
pub fn load_presets() -> Map<String, Value> {
    read_obj(&presets_file())
        .and_then(|m| m.get("presets").and_then(|c| c.as_object().cloned()))
        .unwrap_or_default()
}

pub fn save_presets(presets: &Map<String, Value>) {
    write_store(&presets_file(), "presets", Value::Object(presets.clone()));
}

// --- the default preset: loaded into the profiles once per boot (by the keeper) ---

pub fn default_preset() -> Option<String> {
    let name = crate::core::util::read_json(default_preset_file())?["name"].as_str()?.to_string();
    if load_presets().contains_key(&name) { Some(name) } else { None }
}

pub fn set_default_preset(name: Option<&str>) {
    match name {
        None => {
            let _ = std::fs::remove_file(default_preset_file());
        }
        Some(n) => {
            let _ = crate::core::util::write_file(default_preset_file(), &json!({"name": n, "time": now()}).to_string());
        }
    }
}

/// The camera device now connected at a port key, if any.
pub fn device_at(port: &str) -> Option<String> {
    let d = format!("/dev/v4l/by-path/{}-video-index0", port);
    if Path::new(&d).exists() { Some(d) } else { None }
}

/// May this preset entry be used for the camera on its port? Not when it was made for another camera (entries
/// record the USB ID; older ones are judged by the profile saved for that port).
pub fn preset_entry_fits(js: &Value, saved: Option<&Value>, device: Option<&str>) -> bool {
    let Some(dev) = device else { return true };
    if js["usb_id"].is_string() {
        return !made_for_other(js, dev);
    }
    saved.map(|p| !made_for_other(p, dev)).unwrap_or(true)
}

/// Make a preset's settings the kept profiles (the keeper applies them). Returns the number of cameras.
pub fn preset_to_profiles(name: &str) -> usize {
    let presets = load_presets();
    let entry = presets.get(name).and_then(|e| e.as_object()).cloned().unwrap_or_default();
    let mut cams = load_all();
    let mut n = 0;
    for (port, js) in &entry {
        let p = cams.get(port).cloned();
        let dev = device_at(port);
        if !preset_entry_fits(js, p.as_ref(), dev.as_deref()) {
            log::warn!("preset {:?}: entry for {} was made for another camera, not loaded", name, port);
            continue;
        }
        n += 1;
        let p = p.filter(|p| dev.as_deref().map(|d| !made_for_other(p, d)).unwrap_or(true));
        let mut meta = p
            .as_ref()
            .and_then(|p| p["meta"].as_object().cloned())
            .unwrap_or_else(|| {
                let mut m = Map::new();
                m.insert("source".into(), json!("preset"));
                m
            });
        if let Some(p) = &p {
            if meta.contains_key("calibrated") && !meta.contains_key("calibrated_settings") {
                meta.insert("calibrated_settings".into(), p["settings"].clone());
            }
        }
        meta.remove("manual_edit");
        meta.insert("preset".into(), json!(name));
        let usb = js["usb_id"].clone();
        let mut settings = js.clone();
        if let Some(o) = settings.as_object_mut() {
            o.remove("usb_id");
        }
        cams.insert(
            port.clone(),
            json!({"settings": settings, "serial": p.as_ref().map(|p| p["serial"].clone()).unwrap_or(Value::Null),
                   "usb_id": if usb.is_string() { usb } else { p.as_ref().map(|p| p["usb_id"].clone()).unwrap_or(Value::Null) },
                   "updated": now(), "meta": meta}),
        );
    }
    save_all(&cams);
    n
}

/// At the first keeper start of a boot: the default preset becomes the kept profiles. Its name, if loaded.
pub fn load_default_preset_once() -> Option<String> {
    let name = default_preset()?;
    if boot_mark().exists() {
        return None;
    }
    preset_to_profiles(&name);
    let _ = std::fs::write(boot_mark(), &name);
    Some(name)
}
