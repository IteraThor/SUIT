//! What Autodarts reports about its cameras: local API (http://127.0.0.1:3180) and camera order in its config.
use crate::core::util::home;
use serde_json::Value;
use std::path::PathBuf;
use std::time::Duration;

pub const AUTODARTS_API: &str = "http://127.0.0.1:3180";
pub const DEFAULT_SIZE: (u32, u32) = (1280, 960);

pub fn autodarts_config() -> PathBuf {
    home().join(".config/autodarts/config.toml")
}

fn cam_table() -> Option<toml::Table> {
    let s = std::fs::read_to_string(autodarts_config()).ok()?;
    let t: toml::Table = s.parse().ok()?;
    t.get("cam").and_then(|c| c.as_table()).cloned()
}

/// (width, height) Autodarts streams its cameras at; DEFAULT_SIZE where the config does not say.
pub fn stream_size() -> (u32, u32) {
    let Some(cam) = cam_table() else { return DEFAULT_SIZE };
    let g = |k: &str, d: u32| cam.get(k).and_then(|v| v.as_integer()).map(|v| v as i64).unwrap_or(d as i64);
    let (w, h) = (g("width", DEFAULT_SIZE.0), g("height", DEFAULT_SIZE.1));
    if w > 0 && h > 0 { (w as u32, h as u32) } else { DEFAULT_SIZE }
}

/// Write the [cam] width/height Autodarts streams at, leaving every other line of its config as it is.
/// Only while Autodarts is stopped: it rewrites config.toml from its own values when it exits.
pub fn set_stream_size(width: u32, height: u32) -> anyhow::Result<()> {
    let path = autodarts_config();
    let text = std::fs::read_to_string(&path)?;
    // up to the first sub-table or next table
    let start = regex::Regex::new(r"(?m)^\[cam\]\n").unwrap();
    let Some(m) = start.find(&text) else { anyhow::bail!("no [cam] section in {}", path.display()) };
    let rest = &text[m.end()..];
    let next = regex::Regex::new(r"(?m)^\s*\[").unwrap();
    let end = m.end() + next.find(rest).map(|n| n.start()).unwrap_or(rest.len());
    let mut sec = text[m.start()..end].to_string();
    for (key, v) in [("width", width), ("height", height)] {
        let re = regex::Regex::new(&format!(r"(?m)^{}\s*=.*$", key)).unwrap();
        if re.is_match(&sec) {
            sec = re.replace_all(&sec, format!("{} = {}", key, v).as_str()).to_string();
        } else {
            sec = sec.replacen("[cam]\n", &format!("[cam]\n{} = {}\n", key, v), 1);
        }
    }
    std::fs::write(&path, format!("{}{}{}", &text[..m.start()], sec, &text[end..]))?;
    Ok(())
}

/// Position of this camera in Autodarts' camera list (its image endpoint index).
pub fn autodarts_index(dev: &str) -> Option<usize> {
    let cam = cam_table()?;
    let devs = cam.get("devices")?.as_array()?;
    let port = super::profiles::port_key(dev);
    devs.iter().position(|u| u.as_str().map(|s| s.contains(&format!("location={}", port))).unwrap_or(false))
}

fn agent(timeout: f64) -> ureq::Agent {
    ureq::Agent::config_builder()
        .timeout_global(Some(Duration::from_secs_f64(timeout)))
        .http_status_as_error(false)
        .build()
        .into()
}

pub fn get_json(path: &str, timeout: f64) -> Option<Value> {
    let mut r = agent(timeout).get(&format!("{}{}", AUTODARTS_API, path)).call().ok()?;
    if !r.status().is_success() {
        return None;
    }
    r.body_mut().read_json::<Value>().ok()
}

pub fn get_bytes(path: &str, timeout: f64) -> Option<Vec<u8>> {
    let mut r = agent(timeout).get(&format!("{}{}", AUTODARTS_API, path)).call().ok()?;
    if !r.status().is_success() {
        return None;
    }
    r.body_mut().with_config().limit(32 * 1024 * 1024).read_to_vec().ok()
}

/// POST without body; true when Autodarts answered with a 2xx status.
pub fn post(path: &str, timeout: f64) -> bool {
    agent(timeout)
        .post(&format!("{}{}", AUTODARTS_API, path))
        .send_empty()
        .map(|r| r.status().is_success())
        .unwrap_or(false)
}

/// Start Autodarts' automatic board calibration (it needs one per resolution). True if Autodarts took it.
pub fn calibrate_board() -> bool {
    agent(4.0).post(&format!("{}/api/config/calibration/auto", AUTODARTS_API)).send_empty().is_ok()
}

/// Frames per second Autodarts gets from each camera (its config order), or None when Autodarts is not
/// running detection.
pub fn camera_fps() -> Option<Vec<f64>> {
    let state = get_json("/api/state", 1.0)?;
    if !state["running"].as_bool().unwrap_or(false) {
        return None;
    }
    let stats = get_json("/api/cams/stats", 1.0)?;
    let fps = stats["fps"].as_array()?;
    Some(fps.iter().map(|f| f.as_f64().unwrap_or(0.0)).collect())
}
