//! User service that keeps the saved camera settings applied (runs `suit camera-keeper`).
//!
//! The keeper starts before Autodarts (the MJPEG quality must be in camera RAM before a stream starts) and
//! re-applies the controls once Autodarts has opened the cameras. It runs while any camera has a saved profile;
//! sync() switches it on and off. migrate_legacy() removes older watchers that would fight it;
//! migrate_manual_controls() moves the old Manual Controls page's presets and boot settings into presets.
use super::profiles;
use super::settings::Settings;
use super::v4l2::CID;
use crate::core::util::{home, project_dir, run, self_exe, Output};
use serde_json::{Map, Value};
use std::path::PathBuf;

pub const SERVICE_NAME: &str = "suit-camera-keeper.service";
pub const LEGACY_UNITS: [&str; 4] =
    ["suit-exposure-lock.service", "hbv-camera-optimizer.service", "hbvcal.service", "hbvcal-webui.service"];
pub const MANUAL_BOOT_UNIT: &str = "suit-camera-settings.service";
pub const MANUAL_BOOT_PRESET: &str = "Old boot settings";

pub fn unit_dir() -> PathBuf {
    home().join(".config/systemd/user")
}
pub fn user_unit_path() -> PathBuf {
    unit_dir().join(SERVICE_NAME)
}
pub fn legacy_files() -> [PathBuf; 2] {
    [home().join(".config/suit/exposure_lock.json"), home().join(".config/suit/camera_profile.json")]
}
fn manual_presets() -> PathBuf {
    home().join(".suit_camera_profiles.json")
}

fn systemctl(args: &[&str], timeout: u64) -> Output {
    let mut a = vec!["systemctl", "--user"];
    a.extend_from_slice(args);
    run(&a, Some(timeout))
}

pub fn unit_content() -> String {
    format!(
        "[Unit]\nDescription=SUIT camera keeper (keeps the optimized HBV camera settings applied)\nBefore=autodarts.service\n\n[Service]\nType=simple\nWorkingDirectory={}\nExecStart={} camera-keeper\nRestart=always\nRestartSec=3\n\n[Install]\nWantedBy=default.target\n",
        project_dir().display(),
        self_exe().display()
    )
}

pub fn is_enabled() -> bool {
    systemctl(&["is-enabled", SERVICE_NAME], 5).stdout.trim() == "enabled"
}

pub fn enable() -> bool {
    if let Err(e) = crate::core::util::write_file(user_unit_path(), &unit_content()) {
        log::error!("Failed enabling {}: {}", SERVICE_NAME, e);
        return false;
    }
    systemctl(&["daemon-reload"], 5);
    let r = systemctl(&["enable", SERVICE_NAME], 10);
    systemctl(&["restart", SERVICE_NAME], 10);
    log::info!("Enabled {}", SERVICE_NAME);
    r.code == 0
}

pub fn disable() -> bool {
    systemctl(&["disable", "--now", SERVICE_NAME], 10);
    let _ = std::fs::remove_file(user_unit_path());
    systemctl(&["daemon-reload"], 5);
    true
}

/// Keeper on while any camera has a saved profile, off (and removed) otherwise.
pub fn sync() {
    if !profiles::load_all().is_empty() {
        if !is_enabled() {
            enable();
        } else {
            start();
        }
    } else if user_unit_path().exists() || is_enabled() {
        disable();
    }
}

/// Pause the keeper (an optimization needs the cameras to itself); start() resumes it.
pub fn stop() {
    systemctl(&["stop", SERVICE_NAME], 10);
}

pub fn start() {
    systemctl(&["start", SERVICE_NAME], 10);
}

/// At start and after an update: migrate old watchers and the Manual Controls data, and point the keeper unit
/// at this checkout.
pub fn heal() {
    migrate_legacy();
    migrate_manual_controls();
    if user_unit_path().exists() {
        if std::fs::read_to_string(user_unit_path()).unwrap_or_default() != unit_content() {
            enable();
        }
    } else if !profiles::load_all().is_empty() {
        enable();
    }
}

/// Port key for a by-path link or a /dev/videoN node (resolved through the by-path links), or None.
fn port_of(device: &str) -> Option<String> {
    if device.starts_with("/dev/v4l/by-path/") {
        return Some(profiles::port_key(device));
    }
    let real = std::fs::canonicalize(device).ok()?;
    let mut links: Vec<PathBuf> =
        glob::glob("/dev/v4l/by-path/*-usb-*").ok()?.flatten().collect();
    links.sort();
    links
        .into_iter()
        .find(|l| std::fs::canonicalize(l).ok().as_ref() == Some(&real))
        .map(|l| profiles::port_key(&l.to_string_lossy()))
}

fn legacy_key(k: &str) -> &str {
    match k {
        "exposure_auto" => "auto_exposure",
        "exposure_absolute" => "exposure_time_absolute",
        "wb_auto" => "white_balance_automatic",
        "wb_temperature" => "white_balance_temperature",
        _ => k,
    }
}

/// Standard controls only: old presets also carried keys no camera knows.
fn uvc_settings(ctrls: &Map<String, Value>) -> Value {
    let mut s = Settings::default();
    for (k, v) in ctrls {
        let name = legacy_key(k);
        if CID.iter().any(|(n, _)| *n == name) {
            let n = v.as_i64().or_else(|| v.as_f64().map(|f| f as i64)).or_else(|| v.as_str().and_then(|s| s.trim().parse().ok()));
            if let Some(n) = n {
                s.uvc.insert(name.to_string(), n);
            }
        }
    }
    s.to_json()
}

/// Old Manual Controls presets and its "Load on boot" values become presets of the Camera Controls page.
pub fn migrate_manual_controls() {
    let mut presets = profiles::load_presets();
    let mut changed = false;
    let mp = manual_presets();
    if mp.exists() {
        match crate::core::util::read_json(&mp) {
            Some(data) => {
                let data = data.as_object().cloned().unwrap_or_default();
                let old: Map<String, Value> = if let Some(p) = data.get("profiles") {
                    p.as_object().cloned().unwrap_or_default()
                } else {
                    // oldest format: {"/dev/videoN": {...}} written by an early optimizer
                    let cams: Map<String, Value> =
                        data.iter().filter(|(k, _)| k.starts_with("/dev/")).map(|(k, v)| (k.clone(), v.clone())).collect();
                    let mut m = Map::new();
                    m.insert("CAM-librator".into(), Value::Object(cams));
                    m
                };
                for (name, cams) in &old {
                    let mut entry = Map::new();
                    for (dev, ctrls) in cams.as_object().cloned().unwrap_or_default() {
                        if let (Some(port), Some(c)) = (port_of(&dev), ctrls.as_object()) {
                            entry.insert(port, uvc_settings(c));
                        }
                    }
                    if !entry.is_empty() && !presets.contains_key(name) {
                        presets.insert(name.clone(), Value::Object(entry));
                        changed = true;
                    }
                }
                let _ = std::fs::rename(&mp, mp.with_file_name(".suit_camera_profiles.json.migrated"));
                log::info!("Migrated {} Manual Controls preset(s)", old.len());
            }
            None => log::error!("Failed migrating {}", mp.display()),
        }
    }
    let unit = unit_dir().join(MANUAL_BOOT_UNIT);
    if unit.exists() {
        let text = std::fs::read_to_string(&unit).unwrap_or_default();
        let re = regex::Regex::new(r"(?m)^ExecStart=-?\S*v4l2-ctl -d (\S+) --set-ctrl=(\S+)").unwrap();
        let mut entry = Map::new();
        for c in re.captures_iter(&text) {
            if let Some(port) = port_of(&c[1]) {
                let ctrls: Map<String, Value> = c[2]
                    .split(',')
                    .filter_map(|kv| kv.split_once('='))
                    .map(|(k, v)| (k.to_string(), Value::String(v.to_string())))
                    .collect();
                entry.insert(port, uvc_settings(&ctrls));
            }
        }
        if !entry.is_empty() && !presets.contains_key(MANUAL_BOOT_PRESET) {
            presets.insert(MANUAL_BOOT_PRESET.into(), Value::Object(entry));
            changed = true;
        }
        systemctl(&["disable", "--now", MANUAL_BOOT_UNIT], 10);
        let _ = std::fs::remove_file(&unit);
        systemctl(&["daemon-reload"], 5);
        log::info!("Removed {} (its values are the preset '{}')", MANUAL_BOOT_UNIT, MANUAL_BOOT_PRESET);
    }
    if changed {
        profiles::save_presets(&presets);
    }
}

pub fn migrate_legacy() {
    for unit in LEGACY_UNITS {
        let active = systemctl(&["is-active", unit], 5).stdout.trim() == "active";
        let enabled = systemctl(&["is-enabled", unit], 5).stdout.trim() == "enabled";
        if active || enabled {
            systemctl(&["disable", "--now", unit], 10);
            log::info!("Disabled legacy {}", unit);
        }
        // SUIT's own old unit file can go; the standalone app's files belong to its own uninstaller
        if unit == "suit-exposure-lock.service" && unit_dir().join(unit).exists() {
            let _ = std::fs::remove_file(unit_dir().join(unit));
            systemctl(&["daemon-reload"], 5);
        }
    }
    for f in legacy_files() {
        let _ = std::fs::remove_file(f);
    }
}

/// `suit exposure-lock`: a leftover suit-exposure-lock.service removes itself.
pub fn exposure_lock_cleanup() {
    let unit = "suit-exposure-lock.service";
    for f in legacy_files() {
        let _ = std::fs::remove_file(f);
    }
    systemctl(&["disable", unit], 10);
    let _ = std::fs::remove_file(unit_dir().join(unit));
    systemctl(&["daemon-reload"], 10);
    systemctl(&["stop", "--no-block", unit], 10);
}
