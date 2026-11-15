//! Dartboard light control: WLED, Tasmota smart plugs and Shelly relays over their local HTTP APIs.
use super::http;
use super::i18n::trf;
use super::util::{home, read_json, write_file};
use serde_json::{json, Value};
use std::path::PathBuf;

pub fn default_config_path() -> PathBuf {
    home().join(".config/suit/kiosk_config.json")
}

#[derive(Debug, Clone)]
pub struct LightConfig {
    pub light_enabled: bool,
    /// "wled" | "smart_plug" | "tasmota" | "shelly"
    pub light_device_type: String,
    pub light_ip: String,
    pub light_port: u16,
}

impl Default for LightConfig {
    fn default() -> Self {
        LightConfig { light_enabled: false, light_device_type: "wled".into(), light_ip: String::new(), light_port: 80 }
    }
}

pub fn read_config() -> LightConfig {
    let mut c = LightConfig::default();
    if let Some(d) = read_json(default_config_path()).filter(|v| v.is_object()) {
        if let Some(b) = d["light_enabled"].as_bool() {
            c.light_enabled = b;
        }
        if let Some(s) = d["light_device_type"].as_str() {
            c.light_device_type = s.into();
        }
        if let Some(s) = d["light_ip"].as_str() {
            c.light_ip = s.into();
        }
        if let Some(p) = d["light_port"].as_u64() {
            c.light_port = p as u16;
        }
    }
    c
}

pub fn save_config(enabled: bool, device_type: &str, ip: &str, port: u16) -> bool {
    let p = default_config_path();
    let mut existing = read_json(&p).filter(|v| v.is_object()).unwrap_or_else(|| json!({}));
    existing["light_enabled"] = json!(enabled);
    existing["light_device_type"] = json!(device_type.trim().to_lowercase());
    existing["light_ip"] = json!(ip.trim());
    existing["light_port"] = json!(if port == 0 { 80 } else { port });
    match write_file(&p, &serde_json::to_string_pretty(&existing).unwrap()) {
        Ok(_) => {
            log::info!("Saved light config: enabled={}, type={}, ip={}:{}", enabled, device_type, ip, port);
            true
        }
        Err(e) => {
            log::error!("Failed saving light config to {}: {}", p.display(), e);
            false
        }
    }
}

fn base(ip: &str, port: u16) -> String {
    format!("http://{}:{}", ip, if port == 0 { 80 } else { port })
}

fn get(url: &str, timeout: f64) -> Result<Value, String> {
    match http::request("GET", url, None, None, timeout)? {
        (s, b) if (200..300).contains(&s) => serde_json::from_slice(&b).map_err(|e| e.to_string()),
        (s, _) => Err(format!("HTTP {}", s)),
    }
}

fn post_json(url: &str, v: &Value, timeout: f64) -> Result<Value, String> {
    match http::send_json("POST", url, v, timeout)? {
        (s, b) if (200..300).contains(&s) => serde_json::from_slice(&b).map_err(|e| e.to_string()),
        (s, _) => Err(format!("HTTP {}", s)),
    }
}

fn is_tasmota_on(d: &Value) -> bool {
    let Some(o) = d.as_object() else { return false };
    for (k, v) in o {
        if k.to_uppercase().starts_with("POWER") {
            let s = v.as_str().map(String::from).unwrap_or_else(|| v.to_string()).to_uppercase();
            return matches!(s.as_str(), "ON" | "1" | "TRUE");
        }
    }
    if let Some(p) = d["Status"].get("Power") {
        let s = p.as_str().map(String::from).unwrap_or_else(|| p.to_string());
        return matches!(s.as_str(), "1" | "ON" | "true" | "True");
    }
    false
}

fn on_msg(on: bool, cap: bool) -> String {
    match (on, cap) {
        (true, true) => "Light turned ON",
        (false, true) => "Light turned OFF",
        (true, false) => "Light turned on",
        (false, false) => "Light turned off",
    }
    .into()
}

/// (reachable, is_on, message)
pub fn query_status(cfg: Option<&LightConfig>, timeout: f64) -> (bool, bool, String) {
    let c = cfg.cloned().unwrap_or_else(read_config);
    if !c.light_enabled {
        return (false, false, "Dartboard light is disabled in settings".into());
    }
    let ip = c.light_ip.trim();
    if ip.is_empty() {
        return (false, false, "No device IP address configured".into());
    }
    let b = base(ip, c.light_port);
    match c.light_device_type.as_str() {
        "wled" => match get(&format!("{}/json/state", b), timeout) {
            Ok(d) => (true, d["on"].as_bool().unwrap_or(false), "OK".into()),
            Err(e) => (false, false, format!("Unreachable ({})", e)),
        },
        "tasmota" | "smart_plug" => match get(&format!("{}/cm?cmnd=Power", b), timeout) {
            Ok(d) => (true, is_tasmota_on(&d), "OK".into()),
            Err(e) => (false, false, format!("Unreachable ({})", e)),
        },
        "shelly" => query_shelly(&b, timeout),
        t => (false, false, format!("Unsupported device type: {}", t)),
    }
}

fn query_shelly(b: &str, timeout: f64) -> (bool, bool, String) {
    if let Ok(d) = get(&format!("{}/relay/0", b), timeout) {
        return (true, d["ison"].as_bool().unwrap_or(false), "OK".into());
    }
    match get(&format!("{}/rpc/Switch.GetStatus?id=0", b), timeout) {
        Ok(d) => (true, d["output"].as_bool().unwrap_or(false), "OK".into()),
        Err(e) => (false, false, format!("Shelly query error ({})", e)),
    }
}

/// Set device power explicitly. (success, is_now_on, message)
pub fn set_power(on: bool, cfg: Option<&LightConfig>, timeout: f64) -> (bool, bool, String) {
    let c = cfg.cloned().unwrap_or_else(read_config);
    let ip = c.light_ip.trim();
    if ip.is_empty() {
        return (false, false, "No device IP address configured".into());
    }
    let b = base(ip, c.light_port);
    match c.light_device_type.as_str() {
        "wled" => match post_json(&format!("{}/json/state", b), &json!({"on": on, "v": true}), timeout) {
            Ok(d) => {
                let s = d["on"].as_bool().unwrap_or(false);
                (true, s, on_msg(s, true))
            }
            Err(e) => match http::get(&format!("{}/win&T={}", b, if on { 1 } else { 0 }), timeout) {
                Some(_) => (true, on, on_msg(on, true)),
                None => {
                    log::error!("Failed setting WLED power at {}: {}", b, e);
                    (false, false, format!("Failed setting power ({})", e))
                }
            },
        },
        "tasmota" | "smart_plug" => match get(&format!("{}/cm?cmnd=Power%20{}", b, if on { "ON" } else { "OFF" }), timeout) {
            Ok(d) => {
                let s = is_tasmota_on(&d);
                (true, s, on_msg(s, true))
            }
            Err(e) => (false, false, format!("Unreachable ({})", e)),
        },
        "shelly" => {
            if let Ok(d) = get(&format!("{}/relay/0?turn={}", b, if on { "on" } else { "off" }), timeout) {
                let s = d["ison"].as_bool().unwrap_or(false);
                return (true, s, on_msg(s, true));
            }
            match http::get(&format!("{}/rpc/Switch.Set?id=0&on={}", b, on), timeout) {
                Some(_) => (true, on, on_msg(on, true)),
                None => (false, false, "Shelly error (unreachable)".into()),
            }
        }
        t => (false, false, format!("Unsupported device type: {}", t)),
    }
}

/// Toggle device power. (success, is_now_on, message)
pub fn toggle_light(cfg: Option<&LightConfig>, timeout: f64) -> (bool, bool, String) {
    let c = cfg.cloned().unwrap_or_else(read_config);
    if !c.light_enabled {
        return (false, false, "Light is disabled in settings".into());
    }
    let ip = c.light_ip.trim();
    if ip.is_empty() {
        return (false, false, "No device IP address configured".into());
    }
    let b = base(ip, c.light_port);
    match c.light_device_type.as_str() {
        // {"on": "t", "v": true} toggles power and returns the new state
        "wled" => match post_json(&format!("{}/json/state", b), &json!({"on": "t", "v": true}), timeout) {
            Ok(d) => {
                let s = d["on"].as_bool().unwrap_or(false);
                (true, s, on_msg(s, false))
            }
            Err(e) => match http::get(&format!("{}/win&T=2", b), timeout) {
                Some(_) => {
                    let (_, s, _) = query_status(Some(&c), timeout);
                    (true, s, on_msg(s, false))
                }
                None => {
                    log::error!("Failed toggling WLED at {}: {}", b, e);
                    (false, false, format!("Failed to toggle light ({})", e))
                }
            },
        },
        "tasmota" | "smart_plug" => match get(&format!("{}/cm?cmnd=Power%20TOGGLE", b), timeout) {
            Ok(d) => {
                let s = is_tasmota_on(&d);
                (true, s, on_msg(s, false))
            }
            Err(e) => (false, false, format!("Tasmota toggle error ({})", e)),
        },
        "shelly" => {
            if let Ok(d) = get(&format!("{}/relay/0?turn=toggle", b), timeout) {
                let s = d["ison"].as_bool().unwrap_or(false);
                return (true, s, on_msg(s, false));
            }
            match http::get(&format!("{}/rpc/Switch.Toggle?id=0", b), timeout) {
                Some(_) => {
                    let (_, s, _) = query_shelly(&b, timeout);
                    (true, s, on_msg(s, false))
                }
                None => (false, false, "Shelly toggle error (unreachable)".into()),
            }
        }
        t => (false, false, format!("Unsupported device type: {}", t)),
    }
}

/// Reachability and model / version info. (success, message)
pub fn test_connection(ip: &str, device_type: &str, port: u16, timeout: f64) -> (bool, String) {
    let ip = ip.trim();
    if ip.is_empty() {
        return (false, "IP address cannot be empty".into());
    }
    let b = base(ip, port);
    match device_type {
        "wled" => match get(&format!("{}/json/info", b), timeout) {
            Ok(d) => {
                let mut extra = format!(" ({}, v{}", d["name"].as_str().unwrap_or("WLED"), d["ver"].as_str().unwrap_or(""));
                if let Some(n) = d["leds"]["count"].as_i64() {
                    extra += &format!(", {} LEDs", n);
                }
                extra += ")";
                (true, trf("Connected to WLED successfully{extra}", &[("extra", &extra)]))
            }
            Err(e) => (false, trf("Connection failed: {error}", &[("error", &e)])),
        },
        "tasmota" | "smart_plug" => match get(&format!("{}/cm?cmnd=Status%202", b), timeout) {
            Ok(d) => {
                let v = d["StatusFWR"]["Version"].as_str().unwrap_or("");
                let vs = if v.is_empty() { String::new() } else { format!(" (v{})", v) };
                (true, trf("Connected to Tasmota Smart Plug{ver_str}", &[("ver_str", &vs)]))
            }
            Err(_) => match get(&format!("{}/cm?cmnd=Status", b), timeout) {
                Ok(d) => {
                    let n = d["Status"]["DeviceName"].as_str().unwrap_or("Tasmota").to_string();
                    (true, trf("Connected to {dev_name} Smart Plug", &[("dev_name", &n)]))
                }
                Err(e) => (false, trf("Connection failed: {error}", &[("error", &e)])),
            },
        },
        "shelly" => match get(&format!("{}/shelly", b), timeout) {
            Ok(d) => {
                let m = d["type"].as_str().unwrap_or("Shelly").to_string();
                (true, trf("Connected to {model} Smart Plug", &[("model", &m)]))
            }
            Err(e) => (false, trf("Connection failed: {error}", &[("error", &e)])),
        },
        t => (false, trf("Unsupported device type: {device_type}", &[("device_type", &t)])),
    }
}
