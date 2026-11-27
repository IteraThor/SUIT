//! Autodarts v2: config.toml, local engine API, cameras, systemd user service, install/uninstall.
use super::http;
use super::i18n::{tr, trf};
use super::util::{has_cmd, home, run, run_shell, username, which};
use serde_json::{json, Value};
use std::net::{TcpStream, ToSocketAddrs};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

pub const DEFAULT_V1_PORT: u16 = 3180;
pub const DEFAULT_V2_PORT: u16 = 3182;

pub fn default_host() -> String {
    std::env::var("AUTODARTS_HOST").unwrap_or_else(|_| "127.0.0.1".into())
}
pub fn default_port() -> u16 {
    std::env::var("AUTODARTS_PORT").ok().and_then(|p| p.parse().ok()).unwrap_or(3182)
}
pub fn default_config_path() -> PathBuf {
    home().join(".config/autodarts/config.toml")
}
pub fn user_unit_path() -> PathBuf {
    home().join(".config/systemd/user/autodarts.service")
}
pub const SYSTEM_UNIT_PATH: &str = "/etc/systemd/system/autodarts.service";

/// Ensure ~/.config/autodarts and ~/.local/state/autodarts exist and are valid directories (a broken symlink at
/// the state dir makes systemd's StateDirectory setup fail with 238/STATE_DIRECTORY).
pub fn ensure_autodarts_dirs() {
    let cfg = home().join(".config/autodarts");
    if let Err(e) = std::fs::create_dir_all(&cfg) {
        log::warn!("Could not create config directory {}: {}", cfg.display(), e);
    }
    let st = home().join(".local/state/autodarts");
    let meta = std::fs::symlink_metadata(&st);
    if let Ok(m) = &meta {
        if m.file_type().is_symlink() || !m.is_dir() {
            let _ = std::fs::remove_file(&st);
        }
    }
    match std::fs::create_dir_all(&st) {
        Ok(_) => {
            let _ = std::fs::set_permissions(&st, std::fs::Permissions::from_mode(0o700));
        }
        Err(e) => log::warn!("Could not setup state directory {}: {}", st.display(), e),
    }
}

fn read_toml(path: &Path) -> Option<toml::Table> {
    std::fs::read_to_string(path).ok()?.parse().ok()
}

/// Resolve the Autodarts API port: env, config.toml [api]/[host] port, a listening socket (3182, then 3180).
pub fn get_autodarts_port(config_path: Option<&Path>) -> u16 {
    if let Some(p) = std::env::var("AUTODARTS_PORT").ok().and_then(|p| p.parse().ok()) {
        return p;
    }
    let path = config_path.map(|p| p.to_path_buf()).unwrap_or_else(default_config_path);
    if let Some(t) = read_toml(&path) {
        for sec in ["api", "host"] {
            if let Some(p) = t.get(sec).and_then(|s| s.get("port")).and_then(|p| p.as_integer()) {
                return p as u16;
            }
        }
    }
    let host = default_host();
    if is_port_open(&host, DEFAULT_V2_PORT, 0.05) {
        return DEFAULT_V2_PORT;
    }
    if is_port_open(&host, DEFAULT_V1_PORT, 0.05) {
        return DEFAULT_V1_PORT;
    }
    default_port()
}

pub fn port() -> u16 {
    get_autodarts_port(None)
}

pub fn base_url() -> String {
    format!("http://{}:{}", default_host(), port())
}

pub fn is_port_open(host: &str, port: u16, timeout: f64) -> bool {
    let Ok(mut addrs) = (host, port).to_socket_addrs() else { return false };
    addrs.any(|a| TcpStream::connect_timeout(&a, Duration::from_secs_f64(timeout)).is_ok())
}

pub fn engine_online() -> bool {
    is_port_open(&default_host(), port(), 0.2)
}

pub fn read_stored_auth(config_path: Option<&Path>) -> (String, String) {
    let path = config_path.map(|p| p.to_path_buf()).unwrap_or_else(default_config_path);
    let Some(t) = read_toml(&path) else { return (String::new(), String::new()) };
    let up = t.get("upstream").or_else(|| t.get("auth"));
    let g = |k: &str| {
        up.and_then(|u| u.get(k))
            .map(|v| v.as_str().map(String::from).unwrap_or_else(|| v.to_string()))
            .unwrap_or_default()
            .trim()
            .to_string()
    };
    (g("board_id"), g("api_key"))
}

/// Send PATCH /api/config with partial JSON to live update engine settings.
pub fn patch_engine_config(patch: &Value) -> bool {
    let p = port();
    if !is_port_open(&default_host(), p, 0.2) {
        return false;
    }
    match http::send_json("PATCH", &format!("http://{}:{}/api/config", default_host(), p), patch, 1.5) {
        Ok((s, _)) if (200..300).contains(&s) => true,
        r => {
            log::debug!("PATCH /api/config failed: {:?}", r.map(|x| x.0));
            false
        }
    }
}

/// Replace a whole `[section]` (up to the next table) or append it.
pub fn replace_section(content: &str, name: &str, block: &str, prepend: bool) -> String {
    let re = regex::Regex::new(&format!(r"(?ms)^\[{}\].*?(?:^\[|\z)", regex::escape(name))).unwrap();
    if content.contains(&format!("[{}]", name)) {
        if let Some(m) = re.find(content) {
            // keep the next table's '[' that the match consumed
            let consumed_bracket = m.as_str().ends_with('[') && m.end() <= content.len() && m.end() > m.start();
            let end = if consumed_bracket { m.end() - 1 } else { m.end() };
            return format!("{}{}\n\n{}", &content[..m.start()], block, &content[end..]);
        }
    }
    if prepend {
        format!("{}\n\n{}", block, content)
    } else {
        format!("{}\n\n{}\n", content.trim_end(), block)
    }
}

pub fn save_stored_auth(board_id: &str, api_key: &str, refresh_token: &str) -> bool {
    let path = default_config_path();
    let (b, k, r) = (board_id.trim(), api_key.trim(), refresh_token.trim());
    if let Some(p) = path.parent() {
        let _ = std::fs::create_dir_all(p);
    }
    let mut content = std::fs::read_to_string(&path).unwrap_or_default();
    // [upstream] (v2 native) — refresh_token lets the binary keep the session alive.
    let up = if !r.is_empty() {
        format!("[upstream]\napi_key = '{}'\nrefresh_token = '{}'\nboard_id = '{}'", k, r, b)
    } else {
        format!("[upstream]\napi_key = '{}'\nboard_id = '{}'", k, b)
    };
    content = replace_section(&content, "upstream", &up, false);
    // [auth] (v1 compatibility)
    let auth = format!("[auth]\napi_key = '{}'\nboard_id = '{}'", k, b);
    content = replace_section(&content, "auth", &auth, true);
    if let Err(e) = std::fs::write(&path, content.trim().to_string() + "\n") {
        log::error!("Failed saving auth to config.toml: {}", e);
        return false;
    }
    log::info!("Saved auth credentials to config.toml ([upstream] & [auth])");
    patch_engine_config(&json!({"auth": {"board_id": b, "api_key": k}}));
    true
}

/// Find the path to the autodarts CLI binary ('ad' or 'autodarts').
pub fn get_autodarts_cli_binary() -> Option<String> {
    for n in ["ad", "autodarts"] {
        if let Some(p) = which(n) {
            return Some(p.to_string_lossy().into());
        }
    }
    let h = home();
    let cands = [
        h.join(".local/bin/ad"),
        h.join(".local/bin/autodarts"),
        h.join(".local/share/autodarts/autodarts"),
        PathBuf::from("/usr/local/bin/ad"),
        PathBuf::from("/usr/local/bin/autodarts"),
    ];
    cands.into_iter().find(|p| is_executable(p)).map(|p| p.to_string_lossy().into())
}

pub fn is_executable(p: &Path) -> bool {
    std::fs::metadata(p).map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0).unwrap_or(false)
}

pub fn fetch_engine_config() -> Value {
    http::get_json(&format!("{}/api/config", base_url()), 1.0).unwrap_or_else(|| json!({}))
}

// --- Configuration Reading & Writing ---

fn resolve(p: &str) -> String {
    std::fs::canonicalize(p).map(|r| r.to_string_lossy().to_string()).unwrap_or_else(|_| p.to_string())
}

/// Extract and resolve a local /dev/videoX or /dev/v4l node from a camera config entry (v2 URIs like
/// 'native=/dev/video0&...&location=1-1', device paths, or numeric indices).
pub fn parse_cam_device(cam: &str) -> String {
    let c = cam.trim();
    if c.is_empty() {
        return String::new();
    }
    if c.starts_with('/') && Path::new(c).exists() {
        return if c.contains("/dev/v4l/") { resolve(c) } else { c.to_string() };
    }
    let re = regex::Regex::new(r"(?:native=)?(/dev/video\d+|/dev/v4l/[^\s&]+)").unwrap();
    let cand = re.captures(c).map(|m| m[1].to_string());
    if let Some(cd) = &cand {
        if Path::new(cd).exists() {
            return if cd.contains("/dev/v4l/") { resolve(cd) } else { cd.clone() };
        }
    }
    let loc = regex::Regex::new(r"[?&]location=([^&]+)").unwrap();
    if let Some(m) = loc.captures(c) {
        let pat = format!("/dev/v4l/by-path/*{}*video-index0", glob::Pattern::escape(&m[1]));
        if let Some(p) = glob::glob(&pat).ok().and_then(|mut g| g.find_map(|p| p.ok())) {
            return resolve(&p.to_string_lossy());
        }
    }
    if let Some(cd) = cand {
        return cd;
    }
    if c.chars().all(|ch| ch.is_ascii_digit()) {
        return format!("/dev/video{}", c);
    }
    c.to_string()
}

#[derive(Debug, Clone)]
pub struct CamConfig {
    pub cams: Vec<String>,
    pub devices: Vec<String>,
    pub width: i64,
    pub height: i64,
    pub fps: i64,
    pub fps_max: i64,
    pub auto_distortion: bool,
    pub standby_minutes: i64,
}

impl Default for CamConfig {
    fn default() -> Self {
        CamConfig {
            cams: vec![String::new(); 3],
            devices: vec![String::new(); 3],
            width: 1280,
            height: 720,
            fps: 30,
            fps_max: 30,
            auto_distortion: false,
            standby_minutes: 15,
        }
    }
}

fn apply_cam(cfg: &mut CamConfig, data: &Value) {
    let cam = &data["cam"];
    if cam.is_object() {
        let raw = cam["devices"].as_array().filter(|a| !a.is_empty()).or_else(|| cam["cams"].as_array());
        let mut list: Vec<String> = raw
            .map(|a| a.iter().map(|v| v.as_str().unwrap_or("").to_string()).collect())
            .unwrap_or_else(|| vec![String::new(); 3]);
        list.resize(3, String::new());
        list.truncate(3);
        cfg.cams = list.iter().map(|c| if c.is_empty() { String::new() } else { parse_cam_device(c) }).collect();
        cfg.devices = list;
        let i = |k: &str, d: i64| cam[k].as_i64().or_else(|| cam[k].as_f64().map(|f| f as i64)).unwrap_or(d);
        cfg.width = i("width", 1280);
        cfg.height = i("height", 720);
        cfg.fps = i("fps", 30);
        cfg.fps_max = i("fps_max", 30);
        cfg.auto_distortion = cam["auto_distortion"].as_bool().unwrap_or(false);
        if let Some(s) = cam["standby_minutes"].as_i64() {
            cfg.standby_minutes = s;
        }
    }
    if let Some(s) = data["motion"]["standby_minutes"].as_i64() {
        cfg.standby_minutes = s;
    }
}

pub fn toml_to_json(t: &toml::Table) -> Value {
    serde_json::to_value(t).unwrap_or(Value::Null)
}

/// Read camera and video settings from running engine or config.toml.
pub fn read_cam_config() -> CamConfig {
    let mut cfg = CamConfig::default();
    if engine_online() {
        let d = fetch_engine_config();
        if d.as_object().map(|o| !o.is_empty()).unwrap_or(false) {
            apply_cam(&mut cfg, &d);
            return cfg;
        }
    }
    if let Some(t) = read_toml(&default_config_path()) {
        apply_cam(&mut cfg, &toml_to_json(&t));
    }
    cfg
}

/// Save camera, video, lens correction, and standby settings to config.toml and live engine.
pub fn save_cam_config(cams: &[String], width: i64, height: i64, fps: i64, auto_distortion: bool, standby_minutes: i64) -> bool {
    let path = default_config_path();
    if let Some(p) = path.parent() {
        let _ = std::fs::create_dir_all(p);
    }
    let mut content = std::fs::read_to_string(&path).unwrap_or_default();
    let mut formatted: Vec<String> = cams.to_vec();
    formatted.resize(3, String::new());
    formatted.truncate(3);
    // Preserve rich v2 URIs if the underlying camera node matches existing config
    if let Ok(t) = content.parse::<toml::Table>() {
        let ex = toml_to_json(&t);
        let entries: Vec<String> = ex["cam"]["devices"]
            .as_array()
            .filter(|a| !a.is_empty())
            .or_else(|| ex["cam"]["cams"].as_array())
            .map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect())
            .unwrap_or_default();
        formatted = formatted
            .into_iter()
            .map(|c| {
                if c.is_empty() {
                    return c;
                }
                entries.iter().find(|o| !o.is_empty() && parse_cam_device(o) == c).cloned().unwrap_or(c)
            })
            .collect();
    }
    let cams_str = format!("[{}]", formatted.iter().map(|c| format!("'{}'", c)).collect::<Vec<_>>().join(", "));
    let block = format!(
        "[cam]\nauto_distortion = {}\ncams = {}\ndevices = {}\nfps = {}\nfps_max = {}\nheight = {}\nstandby_minutes = {}\nwidth = {}",
        auto_distortion, cams_str, cams_str, fps, fps, height, standby_minutes, width
    );
    content = replace_section(&content, "cam", &block, false);
    content = replace_section(&content, "motion", &format!("[motion]\nstandby_minutes = {}", standby_minutes), false);
    if let Err(e) = std::fs::write(&path, content.trim().to_string() + "\n") {
        log::error!("Failed saving camera configuration: {}", e);
        return false;
    }
    log::info!("Saved camera configuration to config.toml");
    if engine_online() {
        patch_engine_config(&json!({
            "cam": {"cams": formatted, "devices": formatted, "width": width, "height": height,
                    "fps": fps, "fps_max": fps, "auto_distortion": auto_distortion},
            "motion": {"standby_minutes": standby_minutes}
        }));
    }
    true
}

// --- Systemd Service State & Installation ---

pub fn is_service_enabled() -> bool {
    if home().join(".config/systemd/user/default.target.wants/autodarts.service").exists() {
        return true;
    }
    run(&["systemctl", "--user", "is-enabled", "autodarts.service"], Some(2)).stdout.trim() == "enabled"
}

pub fn ensure_service_unit_at(unit: &Path, bin: Option<&str>) -> bool {
    if unit.is_file() {
        return true;
    }
    let Some(b) = bin.map(String::from).or_else(get_autodarts_cli_binary) else {
        log::warn!("Cannot generate autodarts.service: autodarts binary not found");
        return false;
    };
    let content = format!(
        "# Generated by SUIT for Autodarts v2\n[Unit]\nDescription=Autodarts board\nAfter=network-online.target\nWants=network-online.target\nStartLimitIntervalSec=60\nStartLimitBurst=5\n\n[Service]\nType=notify\nNotifyAccess=main\nExecStart={} run\nRestart=always\nRestartSec=5\nTimeoutStopSec=15\nWatchdogSec=30\nStateDirectory=autodarts\nStateDirectoryMode=0700\nStandardOutput=journal\nStandardError=journal\n\n[Install]\nWantedBy=default.target\n",
        b
    );
    match super::util::write_file(unit, &content) {
        Ok(_) => {
            log::info!("Wrote user systemd unit: {}", unit.display());
            ensure_autodarts_dirs();
            true
        }
        Err(e) => {
            log::error!("Failed writing user systemd unit {}: {}", unit.display(), e);
            false
        }
    }
}

pub fn ensure_service_unit() -> bool {
    ensure_service_unit_at(&user_unit_path(), None)
}

/// Enable or disable systemd user unit and user lingering.
pub fn set_service_enabled(enabled: bool) -> bool {
    if enabled {
        ensure_service_unit();
        run(&["systemctl", "--user", "daemon-reload"], Some(5));
        let r = run(&["systemctl", "--user", "enable", "autodarts.service"], Some(5));
        if r.code != 0 {
            log::warn!("systemctl --user enable failed: {}", r.stderr.trim());
        }
        run(&["loginctl", "enable-linger", &username()], Some(5));
    } else {
        run(&["systemctl", "--user", "disable", "autodarts.service"], Some(5));
        run(&["systemctl", "--user", "daemon-reload"], Some(5));
    }
    true
}

pub fn is_autodarts_installed() -> bool {
    let h = home();
    [
        user_unit_path(),
        PathBuf::from(SYSTEM_UNIT_PATH),
        h.join(".local/share/autodarts/autodarts"),
        h.join(".local/bin/autodarts"),
        h.join(".local/opt/autodarts/autodarts"),
        PathBuf::from("/usr/local/bin/autodarts"),
    ]
    .iter()
    .any(|p| p.exists())
        || has_cmd("autodarts")
}

/// Install official Autodarts v2 release and configure user systemd service.
pub fn install_autodarts() -> (bool, String) {
    let user = username();
    let h = home();
    // 1. Preserve existing credentials and camera settings
    let (pb, pk) = read_stored_auth(None);
    let pcam = read_cam_config();
    // 2. Stop and remove legacy v1 system service if present
    run_shell(
        "sudo -n systemctl stop autodarts.service autodartsupdater.service 2>/dev/null || true; \
         sudo -n systemctl disable autodarts.service autodartsupdater.service 2>/dev/null || true; \
         sudo -n rm -f /etc/systemd/system/autodarts.service /etc/systemd/system/autodartsupdater.service \
         /etc/systemd/system/*.wants/autodarts.service /etc/systemd/system/*.wants/autodartsupdater.service 2>/dev/null || true; \
         sudo -n systemctl daemon-reload 2>/dev/null || true",
        Some(10),
    );
    // 3. Stop any stray autodarts processes
    run_shell("pkill -9 -x autodarts 2>/dev/null || true", Some(5));
    for _ in 0..6 {
        if !is_port_open("127.0.0.1", DEFAULT_V2_PORT, 0.05) && !is_port_open("127.0.0.1", DEFAULT_V1_PORT, 0.05) {
            break;
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    // 4. Run official installer (https: the script runs with sudo rights; pipefail: a failed download fails)
    let r = run_shell("set -o pipefail; curl -fsSL https://autodarts.sh | bash -s -- --headless", Some(600));
    run(&["sudo", "-n", "usermod", "-aG", "video", &user], Some(10));
    if r.code != 0 {
        log::warn!("Installer script returned {}: {}", r.code, r.stderr);
        let e = if r.stderr.trim().is_empty() { tr("Unknown error") } else { r.stderr.trim().to_string() };
        return (false, trf("Installation script failed: {error}", &[("error", &e)]));
    }
    // 5. Locate binary
    let cands = [h.join(".local/share/autodarts/autodarts"), h.join(".local/bin/autodarts"), PathBuf::from("/usr/local/bin/autodarts")];
    let bin = cands
        .iter()
        .find(|p| is_executable(p))
        .map(|p| resolve(&p.to_string_lossy()))
        .or_else(|| which("autodarts").map(|p| resolve(&p.to_string_lossy())));
    let Some(bin) = bin else { return (false, "Installation completed, but autodarts binary could not be found.".into()) };
    // 6. systemd user service
    if !ensure_service_unit_at(&user_unit_path(), Some(&bin)) {
        return (false, "Failed creating systemd user service".into());
    }
    ensure_autodarts_dirs();
    // 7. Restore preserved credentials and camera configuration
    if !pb.is_empty() || !pk.is_empty() {
        save_stored_auth(&pb, &pk, "");
    }
    if pcam.cams.iter().any(|c| !c.is_empty()) {
        save_cam_config(&pcam.cams, pcam.width, pcam.height, pcam.fps, pcam.auto_distortion, pcam.standby_minutes);
    }
    // 8. lingering
    run_shell(&format!("loginctl enable-linger {} 2>/dev/null || true", user), Some(5));
    // 9. start service
    let s = run_shell(
        "systemctl --user daemon-reload && systemctl --user reset-failed autodarts.service 2>/dev/null || true; \
         systemctl --user enable autodarts.service && systemctl --user restart autodarts.service",
        Some(15),
    );
    if s.code != 0 {
        log::warn!("systemctl --user failed: {}", s.stderr);
    }
    // 10. Verify API listener
    let p = port();
    let mut ready = false;
    for _ in 0..10 {
        std::thread::sleep(Duration::from_millis(500));
        if is_port_open("127.0.0.1", p, 0.1) {
            ready = true;
            break;
        }
    }
    if !ready {
        log::warn!("Autodarts API port {} not yet listening, triggering second restart", p);
        run_shell("systemctl --user restart autodarts.service 2>/dev/null || true", Some(10));
    }
    log::info!("Autodarts installation/upgrade completed successfully");
    (true, "Autodarts v2 installation completed and service started.".into())
}

/// Stop, disable service, remove systemd unit and local config/binaries.
pub fn uninstall_autodarts() -> (bool, String) {
    let h = super::util::shquote(&home().display().to_string());
    let cmd = format!(
        "systemctl --user stop autodarts.service 2>/dev/null || true; \
         systemctl --user disable autodarts.service 2>/dev/null || true; \
         rm -f {h}/.config/systemd/user/autodarts.service {h}/.config/systemd/user/default.target.wants/autodarts.service; \
         systemctl --user daemon-reload 2>/dev/null || true; \
         sudo -n systemctl stop autodarts 2>/dev/null || true; \
         sudo -n systemctl disable autodarts 2>/dev/null || true; \
         sudo -n rm -f /etc/systemd/system/autodarts.service /etc/systemd/system/autodartsupdater.service; \
         sudo -n systemctl daemon-reload 2>/dev/null || true; \
         rm -rf {h}/.local/share/autodarts {h}/.local/opt/autodarts {h}/.local/bin/autodarts {h}/.local/bin/ad {h}/.config/autodarts {h}/.local/state/autodarts; \
         sudo -n rm -f /usr/local/bin/autodarts; \
         rpm -qf /usr/bin/autodarts >/dev/null 2>&1 || sudo -n rm -f /usr/bin/autodarts; \
         pkill -9 -x autodarts 2>/dev/null || true",
        h = h
    );
    let r = run_shell(&cmd, Some(30));
    if r.timed_out {
        return (false, "timed out".into());
    }
    // the steps above ignore their own errors: check what is left
    if is_autodarts_installed() {
        log::warn!("Autodarts uninstall left files behind: {}", r.stderr.trim());
        return (false, tr("Autodarts could not be removed completely."));
    }
    log::info!("Autodarts uninstallation completed");
    (true, tr("Autodarts has been uninstalled."))
}
