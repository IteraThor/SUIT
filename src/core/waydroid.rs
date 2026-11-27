//! Waydroid (Android container) with Aurora Store, ARM translation and the Darts Scorer app.
//!
//! The automated Darts Scorer installer lives in a private module that the public SUIT does not ship, so
//! here (as in the public Python build) it reports itself unavailable and Aurora Store is the way in.
use super::i18n::trf;
use super::util::{has_cmd, home, run, run_with_input, spawn, write_file};
use std::path::{Path, PathBuf};

pub const AURORA_APK_URL: &str = "https://gitlab.com/-/project/6922885/uploads/b9f5d827145461a2195699660545160a/AuroraStore-4.8.3.apk";

#[derive(Debug, Clone, Default)]
pub struct WaydroidStatus {
    pub installed: bool,
    pub darts_scorer: bool,
}

pub fn is_waydroid_installed() -> bool {
    has_cmd("waydroid")
}

pub fn is_waydroid_initialized() -> bool {
    Path::new("/var/lib/waydroid/images/system.img").exists() || Path::new("/var/lib/waydroid/system.img").exists()
}

fn apps_dir() -> PathBuf {
    home().join(".local/share/applications")
}

pub fn is_aurora_store_installed() -> bool {
    if apps_dir().join("waydroid.com.aurora.store.desktop").exists() {
        return true;
    }
    run_with_input(&["sudo", "-n", "waydroid", "shell"], "pm list packages com.aurora.store\n", Some(3)).stdout.contains("package:com.aurora.store")
}

pub fn has_private_darts_scorer() -> bool {
    false
}

pub fn is_darts_scorer_installed() -> bool {
    apps_dir().join("waydroid.de.muetzner.dartsscorer.desktop").exists()
}

pub fn get_waydroid_status() -> WaydroidStatus {
    let installed = is_waydroid_installed();
    let darts_scorer = installed && is_waydroid_initialized() && is_darts_scorer_installed();
    WaydroidStatus { installed, darts_scorer }
}

pub fn get_system_locale() -> String {
    for line in run(&["localectl", "status"], Some(5)).stdout.lines() {
        if let Some((_, rest)) = line.split_once("LANG=") {
            let lang = rest.split_whitespace().next().unwrap_or("").replace('"', "");
            let base = lang.split('.').next().unwrap_or("");
            if let Some((a, b)) = base.split_once('_') {
                return format!("{}-{}", a, b);
            }
            return lang;
        }
    }
    if std::env::var("LANG").unwrap_or_default().to_lowercase().contains("de") { "de-DE".into() } else { "en-US".into() }
}

fn session_running() -> bool {
    let s = run(&["waydroid", "status"], Some(10)).stdout;
    s.contains("Session:\tRUNNING") || s.contains("Session: RUNNING")
}

pub fn waydroid_debloat_and_seamless() -> (bool, String) {
    let loc = get_system_locale();
    run(&["sudo", "-n", "waydroid", "shell", "settings", "put", "system", "system_locales", &loc], Some(30));
    let disables = [
        "pm disable-user --user 0 org.lineageos.jelly",
        "pm disable-user --user 0 com.android.calculator2",
        "pm disable-user --user 0 com.android.providers.calendar",
        "pm disable-user --user 0 org.lineageos.etar",
        "pm disable-user --user 0 org.lineageos.aperture",
        "pm disable-user --user 0 com.android.deskclock",
        "pm disable-user --user 0 com.android.contacts",
        "pm disable-user --user 0 com.android.documentsui",
        "pm disable-user --user 0 com.android.gallery3d",
        "pm disable-user --user 0 org.lineageos.eleven",
        "pm disable-user --user 0 org.lineageos.recorder",
        "am force-stop com.android.launcher3",
        "settings put global policy_control immersive.full=*",
    ];
    run_with_input(&["sudo", "-n", "waydroid", "shell"], &(disables.join("\n") + "\n"), Some(60));
    // native fullscreen mode rather than a floating freeform window
    run(&["waydroid", "prop", "set", "persist.waydroid.multi_windows", "false"], Some(20));
    run(&["waydroid", "prop", "set", "persist.waydroid.dark_theme", "true"], Some(20));
    let (w, h) = get_primary_screen_resolution();
    run(&["waydroid", "prop", "set", "persist.waydroid.width", &w.to_string()], Some(20));
    run(&["waydroid", "prop", "set", "persist.waydroid.height", &h.to_string()], Some(20));
    run(&["gsettings", "set", "org.gnome.desktop.wm.keybindings", "toggle-fullscreen", "['F11']"], Some(5));
    log::info!("Waydroid debloat and fullscreen configured ({}x{})", w, h);
    (true, "Waydroid debloat and fullscreen configured.".into())
}

pub fn install_aurora_store() -> (bool, String) {
    let r = (|| -> Result<(), String> {
        let bin = home().join(".local/share/suit/bin");
        std::fs::create_dir_all(&bin).map_err(|e| e.to_string())?;
        let apk = bin.join("AuroraStore-4.8.3.apk");
        if !apk.exists() {
            super::http::download(AURORA_APK_URL, &apk, 300.0)?;
        }
        run(&["sudo", "-n", "systemctl", "start", "waydroid-container"], Some(60));
        if !session_running() {
            spawn(&["waydroid", "session", "start"]);
            std::thread::sleep(std::time::Duration::from_secs(3));
        }
        let p = run(&["waydroid", "app", "install", &apk.to_string_lossy()], Some(300));
        if p.code != 0 {
            log::warn!("waydroid app install failed: {}. Trying pm install fallback...", p.stderr);
            let tmp = home().join(".local/share/waydroid/data/local/tmp");
            std::fs::create_dir_all(&tmp).map_err(|e| e.to_string())?;
            let target = tmp.join("aurora.apk");
            std::fs::copy(&apk, &target).map_err(|e| e.to_string())?;
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o666));
            run_with_input(&["sudo", "-n", "waydroid", "shell"], "pm install -r /data/local/tmp/aurora.apk\n", Some(300));
        }
        // installation and storage permissions for Aurora Store
        run_with_input(
            &["sudo", "-n", "waydroid", "shell"],
            "appops set com.aurora.store REQUEST_INSTALL_PACKAGES allow\npm grant com.aurora.store android.permission.READ_EXTERNAL_STORAGE\npm grant com.aurora.store android.permission.WRITE_EXTERNAL_STORAGE\nappops set com.aurora.store MANAGE_EXTERNAL_STORAGE allow\n",
            Some(60),
        );
        let desk = apps_dir().join("waydroid.com.aurora.store.desktop");
        if !desk.exists() {
            write_file(&desk, "[Desktop Entry]\nName=Aurora Store\nComment=Open source client for Google Play Store\nExec=waydroid app launch com.aurora.store\nIcon=com.aurora.store\nTerminal=false\nType=Application\nCategories=Utility;\n").map_err(|e| e.to_string())?;
            run(&["update-desktop-database", &apps_dir().to_string_lossy()], Some(30));
        }
        Ok(())
    })();
    match r {
        Ok(_) => {
            log::info!("Aurora Store installed into Waydroid successfully");
            (true, "Aurora Store installed into Waydroid.".into())
        }
        Err(e) => {
            log::error!("Failed installing Aurora Store: {}", e);
            (false, trf("Failed installing Aurora Store: {error}", &[("error", &e)]))
        }
    }
}

pub fn ensure_darts_scorer_launcher() -> Option<PathBuf> {
    None
}

pub fn pin_darts_scorer_to_dash() -> bool {
    false
}

pub fn get_primary_screen_resolution() -> (i32, i32) {
    let mons = super::display::get_monitors();
    if let Some(p) = mons.iter().find(|m| m.is_primary).or(mons.first()) {
        let (w, h) = if p.rotation == 1 || p.rotation == 3 { (p.h, p.w) } else { (p.w, p.h) };
        if w > 0 && h > 0 {
            return (w, h);
        }
    }
    if let Ok(x) = std::fs::read_to_string(home().join(".config/monitors.xml")) {
        let tag = |t: &str| regex::Regex::new(&format!(r"<{t}>\s*([^<]*?)\s*</{t}>")).unwrap().captures(&x).map(|c| c[1].to_string());
        let rot = tag("rotation").unwrap_or_else(|| "normal".into());
        if let (Some(w), Some(h)) = (tag("width").and_then(|v| v.parse().ok()), tag("height").and_then(|v| v.parse().ok())) {
            return if rot == "right" || rot == "left" { (h, w) } else { (w, h) };
        }
    }
    (1200, 1920)
}

pub fn is_virtual_machine() -> (bool, String) {
    let r = run(&["systemd-detect-virt"], Some(5));
    let v = r.stdout.trim().to_string();
    if r.code == 0 && !v.is_empty() && v != "none" { (true, v) } else { (false, "none".into()) }
}

pub fn launch_aurora_store() -> (bool, String) {
    let (vm, virt) = is_virtual_machine();
    if run(&["systemctl", "is-active", "waydroid-container"], Some(5)).stdout.trim() != "active" {
        run(&["sudo", "-n", "systemctl", "start", "waydroid-container"], Some(60));
    }
    if !session_running() {
        run(&["waydroid", "session", "stop"], Some(30));
        spawn(&["waydroid", "session", "start"]);
        std::thread::sleep(std::time::Duration::from_secs(2));
    }
    if !spawn(&["waydroid", "app", "launch", "com.aurora.store"]) {
        return (false, trf("Failed launching Aurora Store: {error}", &[("error", &"waydroid not found")]));
    }
    if vm {
        return (
            true,
            trf(
                "Launched Aurora Store. Note: Running in a virtual machine ({virt_type}). Waydroid requires physical GPU hardware acceleration (Intel/AMD) to render UI windows.",
                &[("virt_type", &virt)],
            ),
        );
    }
    (true, "Launched Aurora Store. Search for 'Darts Scorer' to install or open.".into())
}

pub fn launch_darts_scorer() -> (bool, String) {
    if is_darts_scorer_installed() {
        if let Some(l) = ensure_darts_scorer_launcher() {
            pin_darts_scorer_to_dash();
            if spawn(&[&l.to_string_lossy()]) {
                let (vm, virt) = is_virtual_machine();
                if vm {
                    return (true, trf("Launched Darts Scorer. Note: Running in a virtual machine ({virt_type}).", &[("virt_type", &virt)]));
                }
                return (true, "Launched Darts Scorer.".into());
            }
        }
    }
    launch_aurora_store()
}
