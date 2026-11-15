//! Per-camera profile store (~/.config/hbvcal/profiles.json), keyed by USB port path.
//!
//! All HBV cameras report the same serial (HB2026070001, hard-coded in firmware), so a camera is identified by
//! the port it is plugged into (`/dev/v4l/by-path/...`).
use super::settings::Settings;
use crate::core::util::home;
use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};

pub const SHARED_SERIAL: &str = "HB2026070001";
pub const HBV_USB_ID: &str = "0bdc:2710";

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

pub fn get(device: &str) -> Option<Value> {
    load_all().get(&port_key(device)).cloned()
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

pub fn set_consent(hbv_confirmed: bool) {
    let _ = crate::core::util::write_file(
        consent_file(),
        &json!({"hbv_confirmed": hbv_confirmed, "time": now()}).to_string(),
    );
}

pub fn hbv(device: &str) -> bool {
    usb_id(device).as_deref() == Some(HBV_USB_ID)
}

/// May the tuning back door (MJPEG quality, sensor registers) be used on this camera? Only on HBV cameras,
/// and only after the user confirmed once that they use HBV OV2710 cameras.
pub fn hidden_ok(device: &str) -> bool {
    consent().map(|c| c["hbv_confirmed"].as_bool().unwrap_or(false)).unwrap_or(false) && hbv(device)
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

/// Make a preset's settings the kept profiles (the keeper applies them). Returns the number of cameras.
pub fn preset_to_profiles(name: &str) -> usize {
    let presets = load_presets();
    let entry = presets.get(name).and_then(|e| e.as_object()).cloned().unwrap_or_default();
    let mut cams = load_all();
    for (port, js) in &entry {
        let p = cams.get(port).cloned();
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
        cams.insert(
            port.clone(),
            json!({"settings": js, "serial": p.as_ref().map(|p| p["serial"].clone()).unwrap_or(Value::Null),
                   "updated": now(), "meta": meta}),
        );
    }
    save_all(&cams);
    entry.len()
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
