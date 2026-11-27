//! Fullscreen Chromium kiosk autostart and the SUIT In-Page Controls extension with its native messaging host.
use super::i18n::trf;
use super::util::{has_cmd, home, project_dir, run, run_with_input, self_exe, sudo_write, write_file};
use serde_json::json;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

pub const EXTENSION_ID: &str = "ifmcoidaalmjijjhnpafmjngeceapboj";
pub const NATIVE_HOST_NAME: &str = "com.suit.kiosk";
pub const CHROMIUM_CONF_PATH: &str = "/etc/chromium/chromium.conf";
pub const SYSTEM_NATIVE_MESSAGING_DIR: &str = "/etc/chromium/native-messaging-hosts";
pub const SUIT_MARKER_START: &str = "# >>> SUIT Autodarts In-Page Controls >>>";
pub const SUIT_MARKER_END: &str = "# <<< SUIT Autodarts In-Page Controls <<<";
/// The native host launcher inside the extension folder (manifests need a plain executable, no arguments).
pub const HOST_LAUNCHER: &str = "suit_kiosk_host";

pub fn autostart_file() -> PathBuf {
    home().join(".config/autostart/suit-kiosk.desktop")
}
pub fn extension_dir() -> PathBuf {
    project_dir().join("kiosk_extension")
}
pub fn installed_extension_dir() -> PathBuf {
    home().join(".local/share/suit/kiosk_extension")
}
pub fn user_desktop_path() -> PathBuf {
    home().join(".local/share/applications/chromium-browser.desktop")
}
fn native_messaging_dirs() -> [PathBuf; 3] {
    let h = home();
    [
        h.join(".config/chromium/NativeMessagingHosts"),
        h.join(".config/google-chrome/NativeMessagingHosts"),
        h.join(".config/BraveSoftware/Brave-Browser/NativeMessagingHosts"),
    ]
}

pub fn get_extension_path() -> PathBuf {
    if installed_extension_dir().exists() { installed_extension_dir() } else { extension_dir() }
}

fn copy_tree(src: &Path, dst: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    for e in std::fs::read_dir(src)?.flatten() {
        let p = e.path();
        let t = dst.join(e.file_name());
        if p.is_dir() {
            copy_tree(&p, &t)?;
        } else {
            std::fs::copy(&p, &t)?;
        }
    }
    Ok(())
}

/// Write the host launcher that runs `suit kiosk-host` (in `dir`/host/).
fn write_host_launcher(dir: &Path) -> PathBuf {
    let p = dir.join("host").join(HOST_LAUNCHER);
    let content = format!("#!/bin/sh\nexec \"{}\" kiosk-host \"$@\"\n", self_exe().display());
    if std::fs::read_to_string(&p).ok().as_deref() != Some(content.as_str()) {
        let _ = write_file(&p, &content);
    }
    let _ = std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755));
    p
}

/// Syncs the latest extension files to the user data directory if already installed.
pub fn sync_extension_files() {
    if installed_extension_dir().exists() && extension_dir().exists() {
        let _ = copy_tree(&extension_dir(), &installed_extension_dir());
        let host = write_host_launcher(&installed_extension_dir());
        // Manifests of older installs point at another host (the Python SUIT's suit_kiosk_host.py): re-point them
        let m = manifest(&host);
        for d in native_messaging_dirs() {
            let p = d.join(format!("{}.json", NATIVE_HOST_NAME));
            if p.exists() && std::fs::read_to_string(&p).ok().as_deref() != Some(m.as_str()) {
                let _ = write_file(&p, &m);
            }
        }
        let sys = PathBuf::from(format!("{}/{}.json", SYSTEM_NATIVE_MESSAGING_DIR, NATIVE_HOST_NAME));
        if sys.exists() && std::fs::read_to_string(&sys).ok().as_deref() != Some(m.as_str()) {
            sudo_write(&sys.to_string_lossy(), &m);
        }
        // Without passwordless sudo the system manifest keeps naming the Python host: make that file start ours
        let legacy = installed_extension_dir().join("host").join("suit_kiosk_host.py");
        if legacy.exists() && std::fs::read(&legacy).ok() != std::fs::read(&host).ok() {
            let _ = std::fs::copy(&host, &legacy);
        }
    }
}

/// Is the In-Page Controls extension permanently configured in Chromium?
pub fn is_permanent_extension_installed() -> bool {
    std::fs::read_to_string(CHROMIUM_CONF_PATH).map(|c| c.contains(SUIT_MARKER_START)).unwrap_or(false)
        || std::fs::read_to_string(user_desktop_path()).map(|c| c.contains("--load-extension")).unwrap_or(false)
}

fn manifest(host: &Path) -> String {
    let path = std::fs::canonicalize(host).unwrap_or_else(|_| host.to_path_buf());
    serde_json::to_string_pretty(&json!({
        "name": NATIVE_HOST_NAME,
        "description": "SUIT Kiosk System Control Bridge",
        "path": path.to_string_lossy(),
        "type": "stdio",
        "allowed_origins": [format!("chrome-extension://{}/", EXTENSION_ID)],
    }))
    .unwrap()
}

/// Permanently installs the extension: user data copy, native messaging hosts, chromium.conf flag and a
/// user desktop launcher override.
pub fn install_permanent_extension() -> (bool, String) {
    let r = (|| -> std::io::Result<()> {
        let inst = installed_extension_dir();
        std::fs::create_dir_all(&inst)?;
        if extension_dir().exists() {
            copy_tree(&extension_dir(), &inst)?;
        }
        let host = write_host_launcher(&inst);
        ensure_native_messaging_host(Some(&host));
        let sys_manifest = format!("{}/{}.json", SYSTEM_NATIVE_MESSAGING_DIR, NATIVE_HOST_NAME);
        run(&["sudo", "-n", "mkdir", "-p", SYSTEM_NATIVE_MESSAGING_DIR], Some(10));
        sudo_write(&sys_manifest, &manifest(&host));
        let ext = std::fs::canonicalize(&inst).unwrap_or(inst.clone()).display().to_string();
        if let Ok(content) = std::fs::read_to_string(CHROMIUM_CONF_PATH) {
            if !content.contains(SUIT_MARKER_START) {
                let snippet = format!("\n{}\nCHROMIUM_FLAGS+=\" --load-extension={}\"\n{}\n", SUIT_MARKER_START, ext, SUIT_MARKER_END);
                if !run_with_input(&["sudo", "-n", "tee", "-a", CHROMIUM_CONF_PATH], &snippet, Some(10)).ok() {
                    return Err(std::io::Error::other("could not write chromium.conf"));
                }
                log::info!("Configured {} with permanent extension", CHROMIUM_CONF_PATH);
            }
        }
        let desk = user_desktop_path();
        let src = std::fs::read_to_string("/usr/share/applications/chromium-browser.desktop")
            .or_else(|_| std::fs::read_to_string(&desk))
            .unwrap_or_default();
        if !src.is_empty() {
            let flag = format!("--load-extension={}", ext);
            let lines: Vec<String> = src
                .lines()
                .map(|l| {
                    if l.starts_with("Exec=") && !l.contains(&flag) {
                        if l.contains("%U") { l.replace("%U", &format!("{} %U", flag)) } else { format!("{} {}", l, flag) }
                    } else {
                        l.to_string()
                    }
                })
                .collect();
            write_file(&desk, &(lines.join("\n") + "\n"))?;
            log::info!("Created desktop launcher override at {}", desk.display());
        }
        Ok(())
    })();
    match r {
        Ok(_) => (true, "In-Page Controls extension installed into Chromium.".into()),
        Err(e) => {
            log::error!("Failed installing permanent extension into Chromium: {}", e);
            (false, trf("Installation failed: {error}", &[("error", &e)]))
        }
    }
}

pub fn uninstall_permanent_extension() -> (bool, String) {
    if let Ok(content) = std::fs::read_to_string(CHROMIUM_CONF_PATH) {
        if content.contains(SUIT_MARKER_START) {
            let mut out = Vec::new();
            let mut skip = false;
            for l in content.lines() {
                if l.contains(SUIT_MARKER_START) {
                    skip = true;
                    continue;
                }
                if l.contains(SUIT_MARKER_END) {
                    skip = false;
                    continue;
                }
                if !skip {
                    out.push(l);
                }
            }
            if !sudo_write(CHROMIUM_CONF_PATH, &(out.join("\n").trim().to_string() + "\n")) {
                return (false, trf("Removal failed: {error}", &[("error", &"could not write chromium.conf")]));
            }
            log::info!("Cleaned {}", CHROMIUM_CONF_PATH);
        }
    }
    let desk = user_desktop_path();
    if desk.exists() {
        let _ = std::fs::remove_file(&desk);
        log::info!("Removed {}", desk.display());
    }
    run(&["sudo", "-n", "rm", "-f", &format!("{}/{}.json", SYSTEM_NATIVE_MESSAGING_DIR, NATIVE_HOST_NAME)], Some(10));
    (true, "In-Page Controls extension removed from Chromium.".into())
}

/// Install the native messaging host manifest for Chromium, Google Chrome, and Brave.
pub fn ensure_native_messaging_host(host: Option<&Path>) -> bool {
    let host = host.map(|h| h.to_path_buf()).unwrap_or_else(|| write_host_launcher(&get_extension_path()));
    let m = manifest(&host);
    for d in native_messaging_dirs() {
        if let Err(e) = write_file(d.join(format!("{}.json", NATIVE_HOST_NAME)), &m) {
            log::error!("Failed installing native messaging host manifest: {}", e);
            return false;
        }
    }
    log::info!("Ensured SUIT Kiosk native messaging host manifests across browsers");
    true
}

pub fn get_kiosk_flags(enable_controls: bool) -> Vec<String> {
    let mut f: Vec<String> = [
        "--ozone-platform=wayland",
        "--kiosk",
        "--noerrdialogs",
        "--disable-session-crashed-bubble",
        "--disable-infobars",
        "--check-for-update-interval=31536000",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    if enable_controls {
        let d = get_extension_path();
        if d.exists() {
            f.push(format!("--load-extension={}", std::fs::canonicalize(&d).unwrap_or(d).display()));
        }
    }
    f
}

/// Remove stale Singleton lock files from browser config directories.
pub fn clean_stale_singleton_locks() {
    for n in ["chromium", "google-chrome", "BraveSoftware/Brave-Browser"] {
        let pat = format!("{}/Singleton*", home().join(".config").join(n).display());
        for p in glob::glob(&pat).map(|g| g.flatten().collect::<Vec<_>>()).unwrap_or_default() {
            let _ = std::fs::remove_file(p);
        }
    }
}

pub fn ensure_launcher_script() -> PathBuf {
    let p = home().join(".local/share/suit/bin/launch-kiosk.sh");
    let content = r#"#!/usr/bin/env bash
# SUIT Autodarts Kiosk Launcher
# Remove stale lock files to ensure unattended boot never hangs after crash or hostname change
rm -f "$HOME/.config/chromium/Singleton"* "$HOME/.config/google-chrome/Singleton"* 2>/dev/null

# Automatically dismiss GNOME Shell Overview on startup so kiosk displays fullscreen seamlessly
(
    for i in $(seq 1 40); do
        if [ "$(busctl --user get-property org.gnome.Shell /org/gnome/Shell org.gnome.Shell OverviewActive 2>/dev/null)" = "b true" ]; then
            gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell --method org.freedesktop.DBus.Properties.Set org.gnome.Shell OverviewActive "<false>" >/dev/null 2>&1
        fi
        sleep 0.25
    done
) &

exec "$@"
"#;
    let _ = write_file(&p, content);
    let _ = std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755));
    p
}

pub fn find_browser_binary() -> String {
    ["chromium-browser", "chromium", "google-chrome", "brave-browser"]
        .iter()
        .find(|c| has_cmd(c))
        .map(|s| s.to_string())
        .unwrap_or_else(|| "chromium".into())
}

pub fn resolve_browser(browser: Option<&str>) -> String {
    match browser {
        Some(b) if !b.is_empty() && has_cmd(b) => b.into(),
        Some(b) if !b.is_empty() => {
            let d = find_browser_binary();
            if has_cmd(&d) { d } else { b.into() }
        }
        _ => find_browser_binary(),
    }
}

pub fn generate_desktop_entry(url: &str, browser: &str, enable_controls: bool) -> String {
    let l = ensure_launcher_script();
    let cmd = format!("{} {} {} \"{}\"", l.display(), resolve_browser(Some(browser)), get_kiosk_flags(enable_controls).join(" "), url);
    format!("[Desktop Entry]\nType=Application\nName=Autodarts Kiosk\nExec={}\nHidden=false\nNoDisplay=false\nX-GNOME-Autostart-enabled=true\n", cmd)
}

pub fn set_autostart(enabled: bool, url: &str, browser: &str, enable_controls: bool) -> bool {
    let t = autostart_file();
    if !enabled {
        let _ = std::fs::remove_file(&t);
        log::info!("Removed kiosk autostart file: {}", t.display());
        return true;
    }
    if enable_controls {
        ensure_native_messaging_host(None);
    }
    match write_file(&t, &generate_desktop_entry(url, browser, enable_controls)) {
        Ok(_) => {
            let _ = std::fs::set_permissions(&t, std::fs::Permissions::from_mode(0o644));
            log::info!("Created kiosk autostart file: {}", t.display());
            true
        }
        Err(e) => {
            log::error!("Failed configuring kiosk autostart: {}", e);
            false
        }
    }
}

pub fn is_autostart_enabled() -> bool {
    autostart_file().exists()
}
