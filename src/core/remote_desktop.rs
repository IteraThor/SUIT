//! Configure GNOME Remote Desktop (RDP sharing and remote control).
use super::i18n::{tr, trf};
use super::util::{home, hostname, run, spawn};
use regex::RegexBuilder;
use std::path::{Path, PathBuf};

pub const GRD_UNIT: &str = "gnome-remote-desktop.service";
const OO7_DAEMON: &str = "/usr/libexec/oo7-daemon";
// oo7 (Fedora 45+) has no D-Bus activation file for org.freedesktop.secrets: GNOME Remote Desktop starts at
// the same moment as oo7 and fails ("The name is not activatable") unless it waits for it. oo7-daemon.service
// is Type=dbus, so After= waits until the Secret Service is on the bus.
const SECRETS_DROPIN_TEXT: &str = "# Written by SUIT: Remote Desktop reads its login from the keyring, so wait for oo7\n[Unit]\nWants=oo7-daemon.service\nAfter=oo7-daemon.service\n";

fn secrets_dropin() -> PathBuf {
    home()
        .join(".config/systemd/user")
        .join(format!("{}.d", GRD_UNIT))
        .join("50-suit-secret-service.conf")
}

#[derive(Debug, Clone, Default)]
pub struct RemoteDesktopStatus {
    pub enabled: bool,
    pub remote_control: bool,
    pub username: String,
    pub password: String,
    pub port: String,
    pub hostname: String,
    pub ip: String,
}

fn systemctl(args: &[&str]) -> super::util::Output {
    let mut a = vec!["systemctl", "--user"];
    a.extend_from_slice(args);
    run(&a, Some(60))
}

/// True when Remote Desktop is switched on under oo7 but does not wait for it yet.
pub fn needs_secret_service_order() -> bool {
    if !Path::new(OO7_DAEMON).exists() {
        return false;
    }
    if systemctl(&["is-enabled", GRD_UNIT]).stdout.trim() != "enabled" {
        return false;
    }
    std::fs::read_to_string(secrets_dropin()).map(|s| s != SECRETS_DROPIN_TEXT).unwrap_or(true)
}

/// Let Remote Desktop start after oo7, then (re)start it if it is switched on.
pub fn ensure_secret_service_order() -> (bool, String) {
    if !Path::new(OO7_DAEMON).exists() {
        return (true, String::new());
    }
    let p = secrets_dropin();
    if std::fs::read_to_string(&p).map(|s| s != SECRETS_DROPIN_TEXT).unwrap_or(true) {
        if let Err(e) = super::util::write_file(&p, SECRETS_DROPIN_TEXT) {
            return (false, trf("Could not write {path}: {error}", &[("path", &p.display()), ("error", &e)]));
        }
        log::info!("Remote Desktop now waits for the oo7 Secret Service ({})", p.display());
    }
    systemctl(&["daemon-reload"]);
    if systemctl(&["is-enabled", GRD_UNIT]).stdout.trim() != "enabled" {
        return (true, tr("Remote Desktop waits for the keyring service from now on."));
    }
    if systemctl(&["is-active", GRD_UNIT]).stdout.trim() == "active" {
        // Running fine: the order counts from the next boot. A restart would drop the RDP session
        // the user may be running this repair through.
        return (true, tr("Remote Desktop waits for the keyring service from now on."));
    }
    systemctl(&["reset-failed", GRD_UNIT]);
    systemctl(&["restart", GRD_UNIT]);
    let state = systemctl(&["is-active", GRD_UNIT]).stdout.trim().to_string();
    if state != "active" {
        return (false, trf("Remote Desktop does not start ({state}). Details are in its journal.", &[("state", &state)]));
    }
    (true, tr("Remote Desktop is running again."))
}

pub fn get_local_ip() -> String {
    if let Ok(s) = std::net::UdpSocket::bind("0.0.0.0:0") {
        if s.connect("8.8.8.8:80").is_ok() {
            if let Ok(a) = s.local_addr() {
                return a.ip().to_string();
            }
        }
    }
    let h = hostname();
    if h.is_empty() { "127.0.0.1".into() } else { h }
}

pub fn get_remote_desktop_status() -> RemoteDesktopStatus {
    let out = run(&["grdctl", "status", "--show-credentials"], Some(10)).stdout;
    // Isolate RDP block to prevent matching VNC section's View-only setting
    let mut rdp = out.as_str();
    if let Some((_, after)) = out.split_once("RDP:") {
        rdp = after.split_once("VNC:").map(|(a, _)| a).unwrap_or(after);
    }
    let ci = |p: &str| RegexBuilder::new(p).case_insensitive(true).build().unwrap();
    let enabled = ci(r"Status:\s*enabled").is_match(rdp);
    let mut view_only = ci(r"View-only:\s*yes").is_match(rdp);
    // Cross-reference with gsettings authoritative source
    let g = run(&["gsettings", "get", "org.gnome.desktop.remote-desktop.rdp", "view-only"], Some(5)).stdout;
    if g.contains("false") {
        view_only = false;
    } else if g.contains("true") {
        view_only = true;
    }
    let cap = |p: &str| {
        regex::Regex::new(p).unwrap().captures(rdp).map(|c| c[1].trim().to_string()).unwrap_or_default()
    };
    let mut username = cap(r"Username:\s*(.+)");
    let mut password = cap(r"Password:\s*(.+)");
    let mut port = cap(r"Port:\s*(\d+)");
    if port.is_empty() {
        port = "3389".into();
    }
    for v in [&mut username, &mut password] {
        if matches!(v.to_lowercase().as_str(), "(null)" | "null") {
            v.clear();
        }
    }
    RemoteDesktopStatus {
        enabled,
        remote_control: !view_only,
        username,
        password,
        port,
        hostname: hostname(),
        ip: get_local_ip(),
    }
}

pub fn set_desktop_sharing(enabled: bool) -> (bool, String) {
    if enabled {
        run(&["grdctl", "rdp", "enable"], Some(20));
        // Ensure credentials exist so connections don't fail immediately
        let st = get_remote_desktop_status();
        if st.username.is_empty() || st.password.is_empty() {
            run(&["grdctl", "rdp", "set-credentials", "autodarts", "autodarts"], Some(20));
        }
        run(&["systemctl", "--user", "enable", "--now", "gnome-remote-desktop"], Some(30));
        ensure_secret_service_order();
        log::info!("Desktop sharing enabled successfully");
        (true, "Desktop sharing enabled.".into())
    } else {
        run(&["grdctl", "rdp", "disable"], Some(20));
        run(&["systemctl", "--user", "stop", "gnome-remote-desktop"], Some(30));
        log::info!("Desktop sharing disabled successfully");
        (true, "Desktop sharing disabled.".into())
    }
}

pub fn set_remote_control(enabled: bool) -> (bool, String) {
    if enabled {
        run(&["grdctl", "rdp", "disable-view-only"], Some(20));
        log::info!("Remote control enabled (view-only disabled)");
        (true, "Remote control enabled.".into())
    } else {
        run(&["grdctl", "rdp", "enable-view-only"], Some(20));
        log::info!("Remote control disabled (view-only enabled)");
        (true, "Remote control disabled (view-only).".into())
    }
}

pub fn set_remote_credentials(username: &str, password: &str) -> (bool, String) {
    let r = run(&["grdctl", "rdp", "set-credentials", username, password], Some(20));
    if r.code == -1 {
        return (false, trf("Failed setting credentials: {error}", &[("error", &r.stderr)]));
    }
    log::info!("RDP credentials updated for user '{}'", username);
    (true, trf("RDP credentials set for '{username}'.", &[("username", &username)]))
}

pub fn open_gnome_sharing_settings() -> bool {
    spawn(&["gnome-control-center", "system", "remote-desktop"]) || spawn(&["gnome-control-center", "sharing"])
}
