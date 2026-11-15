//! Tiny blocking HTTP helpers (the stand-in for urllib.request).
use serde_json::Value;
use std::time::Duration;

pub const USER_AGENT: &str = "SUIT/2.0";

fn agent(timeout: f64) -> ureq::Agent {
    ureq::Agent::config_builder()
        .timeout_global(Some(Duration::from_secs_f64(timeout)))
        .http_status_as_error(false)
        .user_agent(USER_AGENT)
        .build()
        .into()
}

/// (status, body) of a request; Err on connection failure or timeout.
pub fn request(method: &str, url: &str, body: Option<&[u8]>, content_type: Option<&str>, timeout: f64) -> Result<(u16, Vec<u8>), String> {
    let a = agent(timeout);
    let resp = match method {
        "GET" => a.get(url).call(),
        "DELETE" => a.delete(url).call(),
        m => {
            let rb = match m {
                "POST" => a.post(url),
                "PUT" => a.put(url),
                "PATCH" => a.patch(url),
                _ => return Err(format!("unsupported method {}", m)),
            };
            let rb = match content_type {
                Some(ct) => rb.header("Content-Type", ct),
                None => rb,
            };
            match body {
                Some(b) => rb.send(b),
                None => rb.send_empty(),
            }
        }
    };
    match resp {
        Ok(mut r) => {
            let status = r.status().as_u16();
            let b = r.body_mut().with_config().limit(64 * 1024 * 1024).read_to_vec().unwrap_or_default();
            Ok((status, b))
        }
        Err(e) => Err(e.to_string()),
    }
}

/// 2xx response body, else None.
pub fn get(url: &str, timeout: f64) -> Option<Vec<u8>> {
    match request("GET", url, None, None, timeout) {
        Ok((s, b)) if (200..300).contains(&s) => Some(b),
        _ => None,
    }
}

pub fn get_json(url: &str, timeout: f64) -> Option<Value> {
    serde_json::from_slice(&get(url, timeout)?).ok()
}

pub fn get_text(url: &str, timeout: f64) -> Option<String> {
    String::from_utf8(get(url, timeout)?).ok()
}

/// True on a 2xx answer.
pub fn ok(method: &str, url: &str, body: Option<&[u8]>, content_type: Option<&str>, timeout: f64) -> bool {
    matches!(request(method, url, body, content_type, timeout), Ok((s, _)) if (200..300).contains(&s))
}

pub fn send_json(method: &str, url: &str, v: &Value, timeout: f64) -> Result<(u16, Vec<u8>), String> {
    request(method, url, Some(v.to_string().as_bytes()), Some("application/json"), timeout)
}

/// Download to a file.
pub fn download(url: &str, dest: &std::path::Path, timeout: f64) -> Result<(), String> {
    match request("GET", url, None, None, timeout)? {
        (s, b) if (200..300).contains(&s) => std::fs::write(dest, b).map_err(|e| e.to_string()),
        (s, _) => Err(format!("HTTP {}", s)),
    }
}
