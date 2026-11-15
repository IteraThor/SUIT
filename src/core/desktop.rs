//! Desktop tweaks: touch scaling, SUIT GNOME Shell extensions (touch scaling, hardware overlay, minimal
//! desktop), wallpaper, dark mode and the app grid.
use super::i18n::trf;
use super::util::{fmt_strv, gsettings_get, gsettings_set, home, parse_strv, project_dir, run, run_cmd};
use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};
use std::process::Command;

pub const EXTENSION_UUID: &str = "touch-scaling@suit";
pub const HW_OVERLAY_UUID: &str = "hw-overlay@suit";
pub const MINIMAL_DESKTOP_UUID: &str = "minimal-desktop@suit";

pub const HW_OVERLAY_METRICS: [&str; 14] = [
    "cpu", "temp", "ram", "gpu", "disk", "net", "ip_local", "ip_tailscale", "load", "uptime", "ad_state", "ad_fps", "ad_cams", "ad_proc",
];
/// Clickable Autodarts controls: start/stop, reset, debug snapshot (opt-in) and the update button (on by default)
pub const HW_OVERLAY_BUTTONS: [&str; 4] = ["ad_toggle", "ad_reset", "ad_dump", "ad_update"];

pub fn hw_overlay_choices(key: &str) -> &'static [&'static str] {
    match key {
        "style" => &["card", "minimal"],
        "position" => &["top-right", "top-left", "bottom-right", "bottom-left"],
        "size" => &["small", "medium", "large"],
        "accent" => &["blue", "teal", "green", "purple", "white"],
        _ => &[],
    }
}
pub const HW_OVERLAY_INTERVALS: [i64; 3] = [1, 2, 5];

fn ext_dir(uuid: &str) -> PathBuf {
    home().join(".local/share/gnome-shell/extensions").join(uuid)
}
fn asset(name: &str) -> PathBuf {
    project_dir().join("assets").join(name)
}
pub fn extension_source_dir() -> PathBuf {
    asset("touch_scaling_extension")
}
pub fn user_extension_dir() -> PathBuf {
    ext_dir(EXTENSION_UUID)
}
pub fn hw_overlay_source_dir() -> PathBuf {
    asset("hw_overlay_extension")
}
pub fn hw_overlay_user_dir() -> PathBuf {
    ext_dir(HW_OVERLAY_UUID)
}
/// Watched by the extension, which redraws live on every save
pub fn hw_overlay_config_path() -> PathBuf {
    home().join(".config/suit/hw_overlay.json")
}
pub fn minimal_desktop_source_dir() -> PathBuf {
    asset("minimal_desktop_extension")
}
pub fn minimal_desktop_user_dir() -> PathBuf {
    ext_dir(MINIMAL_DESKTOP_UUID)
}
pub fn gtk4_css_path() -> PathBuf {
    home().join(".config/gtk-4.0/gtk.css")
}
pub fn gtk3_css_path() -> PathBuf {
    home().join(".config/gtk-3.0/gtk.css")
}
const WALLPAPER: &str = "wallpaper_stealth_grey_pattern_ultrasubtle.png";
fn wallpaper_installed() -> PathBuf {
    home().join(".local/share/suit").join(WALLPAPER)
}

pub const SUIT_TOUCH_MARKER_START: &str = "/* --- SUIT TOUCH SCALING START --- */";
pub const SUIT_TOUCH_MARKER_END: &str = "/* --- SUIT TOUCH SCALING END --- */";

pub fn touch_css_block() -> String {
    format!(
        "{}\nwindowcontrols button,\nheaderbar button.titlebutton,\n.titlebutton {{\n  min-width: 26px;\n  min-height: 26px;\n  padding: 2px;\n}}\n\nwindowcontrols button image,\nheaderbar button.titlebutton image,\n.titlebutton image {{\n  -gtk-icon-size: 14px;\n}}\n{}\n",
        SUIT_TOUCH_MARKER_START, SUIT_TOUCH_MARKER_END
    )
}

pub fn hw_overlay_defaults() -> Value {
    let metrics: Map<String, Value> = HW_OVERLAY_METRICS
        .iter()
        .map(|m| (m.to_string(), json!(!["net", "ip_local", "ip_tailscale", "load", "ad_proc"].contains(m))))
        .collect();
    let buttons: Map<String, Value> = HW_OVERLAY_BUTTONS.iter().map(|b| (b.to_string(), json!(*b == "ad_update"))).collect();
    json!({
        "style": "card", "position": "top-right", "size": "medium", "accent": "blue", "opacity": 70,
        "meters": true, "headings": true, "button_colors": false, "interval": 2,
        "metrics": metrics, "buttons": buttons,
    })
}

pub fn is_dark_mode_enabled() -> bool {
    gsettings_get("org.gnome.desktop.interface", "color-scheme").contains("prefer-dark")
}

pub fn is_suit_wallpaper_set() -> bool {
    gsettings_get("org.gnome.desktop.background", "picture-uri-dark").contains("wallpaper_stealth_grey_pattern_ultrasubtle")
}

/// Legacy check kept for backwards compatibility.
pub fn is_map_wallpaper_set() -> bool {
    gsettings_get("org.gnome.desktop.background", "picture-uri-dark").contains("map-d.svg")
}

pub fn is_system_app_folder_configured() -> bool {
    gsettings_get("org.gnome.desktop.app-folders", "folder-children").contains("System")
}

pub fn is_touch_scaling_enabled() -> bool {
    let css = std::fs::read_to_string(gtk4_css_path()).map(|c| c.contains(SUIT_TOUCH_MARKER_START)).unwrap_or(false);
    let factor: f64 = gsettings_get("org.gnome.desktop.interface", "text-scaling-factor")
        .lines()
        .next()
        .and_then(|l| l.trim().parse().ok())
        .unwrap_or(1.0);
    css && factor > 1.05
}

pub fn is_osk_enabled() -> bool {
    gsettings_get("org.gnome.desktop.a11y.applications", "screen-keyboard-enabled").to_lowercase().contains("true")
}

pub fn set_osk(enabled: bool) {
    gsettings_set("org.gnome.desktop.a11y.applications", "screen-keyboard-enabled", if enabled { "true" } else { "false" });
}

pub fn get_button_layout() -> String {
    let s = gsettings_get("org.gnome.desktop.wm.preferences", "button-layout");
    let s = s.trim_matches(|c| c == '\'' || c == '"').to_string();
    if s.is_empty() { "appmenu:close".into() } else { s }
}

pub fn set_button_layout(layout: &str) {
    gsettings_set("org.gnome.desktop.wm.preferences", "button-layout", &format!("'{}'", layout));
}

/// Remove the SUIT block between the markers (keeping the rest of the file).
fn strip_block(content: &str) -> String {
    match content.find(SUIT_TOUCH_MARKER_START) {
        None => content.to_string(),
        Some(s) => match content.find(SUIT_TOUCH_MARKER_END) {
            Some(e) => format!("{}{}", &content[..s], &content[e + SUIT_TOUCH_MARKER_END.len()..]),
            None => content[..s].to_string(),
        },
    }
}

pub fn set_touch_scaling(enabled: bool, scale_factor: f64) -> (bool, String) {
    let r = (|| -> std::io::Result<String> {
        if enabled {
            set_osk(true);
            set_button_layout("appmenu:minimize,maximize,close");
            gsettings_set("org.gnome.desktop.interface", "text-scaling-factor", &scale_factor.to_string());
            enable_extension(EXTENSION_UUID, &extension_source_dir(), &user_extension_dir());
            for p in [gtk4_css_path(), gtk3_css_path()] {
                let existing = std::fs::read_to_string(&p).unwrap_or_default();
                let new = if existing.contains(SUIT_TOUCH_MARKER_START) {
                    let s = existing.find(SUIT_TOUCH_MARKER_START).unwrap();
                    match existing.find(SUIT_TOUCH_MARKER_END) {
                        Some(e) => format!("{}{}{}", &existing[..s], touch_css_block().trim(), &existing[e + SUIT_TOUCH_MARKER_END.len()..]),
                        None => format!("{}{}", &existing[..s], touch_css_block().trim()),
                    }
                } else {
                    format!("{}\n", format!("{}\n\n{}", existing.trim_end(), touch_css_block()).trim())
                };
                super::util::write_file(&p, &new)?;
            }
            log::info!("Touchscreen tweaks enabled successfully");
            Ok("Touchscreen tweaks, window controls, and on-screen keyboard applied.".into())
        } else {
            set_osk(false);
            set_button_layout("appmenu:close");
            gsettings_set("org.gnome.desktop.interface", "text-scaling-factor", "1.0");
            disable_extension(EXTENSION_UUID);
            for p in [gtk4_css_path(), gtk3_css_path()] {
                if let Ok(c) = std::fs::read_to_string(&p) {
                    if c.contains(SUIT_TOUCH_MARKER_START) {
                        let cleaned = strip_block(&c).trim().to_string();
                        if cleaned.is_empty() {
                            let _ = std::fs::remove_file(&p);
                        } else {
                            std::fs::write(&p, cleaned + "\n")?;
                        }
                    }
                }
            }
            log::info!("Touchscreen tweaks disabled successfully");
            Ok("Touchscreen tweaks and on-screen keyboard restored to default.".into())
        }
    })();
    match r {
        Ok(m) => (true, m),
        Err(e) => {
            log::error!("Failed configuring touchscreen scaling: {}", e);
            (false, trf("Failed updating touch scaling: {error}", &[("error", &e)]))
        }
    }
}

fn enabled_extensions() -> Vec<String> {
    parse_strv(&gsettings_get("org.gnome.shell", "enabled-extensions")).unwrap_or_default()
}

fn set_enabled_extensions(v: &[String]) {
    gsettings_set("org.gnome.shell", "enabled-extensions", &fmt_strv(v));
}

/// Copy a bundled shell extension into the user extension folder without enabling it. GNOME Shell on Wayland
/// only discovers extension folders at login, so SUIT installs the files ahead of time. True when files changed.
pub fn install_extension_files(src: &Path, dest: &Path) -> bool {
    let Ok(rd) = std::fs::read_dir(src) else { return false };
    let _ = std::fs::create_dir_all(dest);
    let mut changed = false;
    for f in rd.flatten().map(|e| e.path()).filter(|p| p.is_file()) {
        let name = f.file_name().unwrap();
        let target = dest.join(name);
        if name == "metadata.json" && target.exists() {
            if let Some(data) = merged_metadata(&f, &target) {
                if std::fs::read(&target).ok().as_deref() != Some(&data[..]) {
                    let _ = std::fs::write(&target, &data);
                    changed = true;
                }
                continue;
            }
        }
        if std::fs::read(&target).ok() != std::fs::read(&f).ok() {
            let _ = std::fs::copy(&f, &target);
            changed = true;
        }
    }
    if changed {
        log::info!("Shell extension files installed to {}", dest.display());
    }
    changed
}

/// The bundled metadata.json plus shell versions only the installed copy lists (kept after an OS upgrade
/// repair). None when the installed copy adds nothing.
fn merged_metadata(src: &Path, target: &Path) -> Option<Vec<u8>> {
    let mut new = super::util::read_json(src)?;
    let old = super::util::read_json(target)?;
    let s = |v: &Value| v.as_str().map(String::from).unwrap_or_else(|| v.to_string());
    let bundled: Vec<String> = new["shell-version"].as_array().map(|a| a.iter().map(s).collect()).unwrap_or_default();
    let extra: Vec<String> = old["shell-version"]
        .as_array()
        .map(|a| a.iter().map(s).filter(|v| !bundled.contains(v)).collect())
        .unwrap_or_default();
    if extra.is_empty() {
        return None;
    }
    new["shell-version"] = json!(bundled.into_iter().chain(extra).collect::<Vec<_>>());
    Some((serde_json::to_string_pretty(&new).ok()? + "\n").into_bytes())
}

/// Pre-install every switchable SUIT shell extension (disabled) for discovery at next login.
pub fn install_suit_extensions() -> anyhow::Result<()> {
    install_hw_overlay_files();
    install_minimal_desktop_files();
    Ok(())
}

/// True when the running GNOME Shell knows the extension (discovered at login).
pub fn is_extension_loaded(uuid: &str) -> bool {
    run(&["gnome-extensions", "info", uuid], Some(10)).code == 0
}

fn ext_info_c(uuid: &str) -> String {
    let mut c = Command::new("gnome-extensions");
    c.args(["info", uuid]).env("LC_ALL", "C");
    run_cmd(c, Some(std::time::Duration::from_secs(10))).stdout
}

/// True when newer extension files are installed than the running GNOME Shell has loaded.
pub fn extension_needs_relogin(uuid: &str, dest: &Path) -> bool {
    let Some(on_disk) = super::util::read_json(dest.join("metadata.json")).and_then(|m| m["version"].as_i64()) else { return false };
    for line in ext_info_c(uuid).lines() {
        if let Some((k, v)) = line.trim().split_once(':') {
            if k == "Version" {
                if let Ok(n) = v.trim().parse::<i64>() {
                    return n < on_disk;
                }
            }
        }
    }
    false
}

pub fn is_extension_enabled(uuid: &str, dest: &Path) -> bool {
    dest.join("extension.js").exists() && enabled_extensions().iter().any(|e| e == uuid)
}

/// Install and enable a bundled extension: "active", "relogin" (picked up at next login) or "failed".
pub fn enable_extension(uuid: &str, src: &Path, dest: &Path) -> &'static str {
    let loaded = is_extension_loaded(uuid);
    install_extension_files(src, dest);
    gsettings_set("org.gnome.shell", "disable-user-extensions", "false");
    if loaded {
        // The shell updates enabled-extensions itself; writing the list here too could race with it.
        run(&["gnome-extensions", "enable", uuid], Some(10));
    } else {
        let mut l = enabled_extensions();
        if !l.iter().any(|e| e == uuid) {
            l.push(uuid.into());
            set_enabled_extensions(&l);
        }
    }
    log::info!("Extension {} enabled (loaded={})", uuid, loaded);
    if ext_info_c(uuid).contains("ACTIVE") {
        "active"
    } else if loaded {
        "failed"
    } else {
        // GNOME Shell on Wayland only discovers new extension folders at login
        "relogin"
    }
}

pub fn disable_extension(uuid: &str) {
    if is_extension_loaded(uuid) {
        run(&["gnome-extensions", "disable", uuid], Some(10));
    } else {
        let mut l = enabled_extensions();
        let n = l.len();
        l.retain(|e| e != uuid);
        if l.len() != n {
            set_enabled_extensions(&l);
        }
    }
    log::info!("Extension {} disabled", uuid);
}

pub fn install_hw_overlay_files() -> bool {
    install_extension_files(&hw_overlay_source_dir(), &hw_overlay_user_dir())
}
pub fn hw_overlay_needs_relogin() -> bool {
    extension_needs_relogin(HW_OVERLAY_UUID, &hw_overlay_user_dir())
}
pub fn is_hw_overlay_enabled() -> bool {
    is_extension_enabled(HW_OVERLAY_UUID, &hw_overlay_user_dir())
}

/// Install and enable (or disable) the extension that draws live hardware and Autodarts stats on the desktop.
pub fn set_hw_overlay(enabled: bool) -> (bool, String) {
    if !enabled {
        disable_extension(HW_OVERLAY_UUID);
        return (true, "Hardware overlay hidden.".into());
    }
    match enable_extension(HW_OVERLAY_UUID, &hw_overlay_source_dir(), &hw_overlay_user_dir()) {
        "active" => (true, "Hardware overlay shown on the desktop.".into()),
        "failed" => (false, "Hardware overlay failed to start. Check the GNOME Shell log.".into()),
        _ => (true, "Hardware overlay installed. Log out and back in once to show it.".into()),
    }
}

pub fn install_minimal_desktop_files() -> bool {
    install_extension_files(&minimal_desktop_source_dir(), &minimal_desktop_user_dir())
}

/// True when the switch is on but the running shell has not loaded the extension yet, or runs older files.
pub fn minimal_desktop_needs_relogin() -> bool {
    let d = minimal_desktop_user_dir();
    if is_extension_enabled(MINIMAL_DESKTOP_UUID, &d) && !is_extension_loaded(MINIMAL_DESKTOP_UUID) {
        return true;
    }
    extension_needs_relogin(MINIMAL_DESKTOP_UUID, &d)
}

pub fn is_minimal_desktop_enabled() -> bool {
    is_extension_enabled(MINIMAL_DESKTOP_UUID, &minimal_desktop_user_dir())
}

pub fn set_minimal_desktop(enabled: bool) -> (bool, String) {
    if !enabled {
        disable_extension(MINIMAL_DESKTOP_UUID);
        return (true, "Minimal desktop turned off.".into());
    }
    match enable_extension(MINIMAL_DESKTOP_UUID, &minimal_desktop_source_dir(), &minimal_desktop_user_dir()) {
        "active" => (true, "Minimal desktop turned on.".into()),
        "failed" => (false, "Minimal desktop failed to start. Check the GNOME Shell log.".into()),
        _ => (true, "Minimal desktop installed. Log out and back in once to apply it.".into()),
    }
}

/// Defaults with every valid value from `raw` laid over them; unknown or bad values are dropped.
pub fn normalize_hw_overlay_config(raw: &Value) -> Value {
    let mut cfg = hw_overlay_defaults();
    let Some(r) = raw.as_object() else { return cfg };
    for key in ["style", "position", "size", "accent"] {
        if let Some(v) = r.get(key).and_then(|v| v.as_str()) {
            if hw_overlay_choices(key).contains(&v) {
                cfg[key] = json!(v);
            }
        }
    }
    if let Some(i) = r.get("interval").and_then(|v| if v.is_boolean() { None } else { v.as_i64() }) {
        if HW_OVERLAY_INTERVALS.contains(&i) {
            cfg["interval"] = json!(i);
        }
    }
    for key in ["meters", "headings", "button_colors"] {
        if let Some(b) = r.get(key).and_then(|v| v.as_bool()) {
            cfg[key] = json!(b);
        }
    }
    if let Some(o) = r.get("opacity").and_then(|v| if v.is_boolean() { None } else { v.as_f64() }) {
        cfg["opacity"] = json!(o.clamp(0.0, 100.0) as i64);
    }
    for (sec, names) in [("metrics", &HW_OVERLAY_METRICS[..]), ("buttons", &HW_OVERLAY_BUTTONS[..])] {
        if let Some(m) = r.get(sec).and_then(|v| v.as_object()) {
            for n in names {
                if let Some(b) = m.get(*n).and_then(|v| v.as_bool()) {
                    cfg[sec][*n] = json!(b);
                }
            }
        }
    }
    cfg
}

pub fn get_hw_overlay_config() -> Value {
    normalize_hw_overlay_config(&super::util::read_json(hw_overlay_config_path()).unwrap_or_else(|| json!({})))
}

/// Write the overlay look and metric choices (atomically); the running extension picks them up at once.
pub fn save_hw_overlay_config(cfg: &Value) -> Value {
    let clean = normalize_hw_overlay_config(cfg);
    let s = serde_json::to_string_pretty(&clean).unwrap() + "\n";
    if let Err(e) = super::util::write_atomic(hw_overlay_config_path(), s.as_bytes()) {
        log::error!("Failed saving hardware overlay config: {}", e);
    }
    clean
}

#[derive(Debug, Clone, Default)]
pub struct VisualsStatus {
    pub dark_mode: bool,
    pub wallpaper: bool,
    pub app_grid: bool,
    pub all_ok: bool,
}

pub fn get_visuals_status() -> VisualsStatus {
    let dm = is_dark_mode_enabled();
    let wp = is_suit_wallpaper_set() || is_map_wallpaper_set();
    let grid = is_system_app_folder_configured();
    VisualsStatus { dark_mode: dm, wallpaper: wp, app_grid: grid, all_ok: dm && wp && grid }
}

pub fn apply_desktop_visuals() -> (bool, String) {
    // 1. Wallpaper: install bundled asset to a stable user path, then apply
    let inst = wallpaper_installed();
    if let Some(p) = inst.parent() {
        let _ = std::fs::create_dir_all(p);
    }
    if asset(WALLPAPER).exists() {
        if let Err(e) = std::fs::copy(asset(WALLPAPER), &inst) {
            return (false, trf("Failed applying visuals: {error}", &[("error", &e)]));
        }
    }
    let uri = format!("'file://{}'", inst.display());
    gsettings_set("org.gnome.desktop.background", "picture-uri", &uri);
    gsettings_set("org.gnome.desktop.background", "picture-uri-dark", &uri);
    gsettings_set("org.gnome.desktop.background", "picture-options", "'zoom'");
    // 2. Dark mode
    gsettings_set("org.gnome.desktop.interface", "color-scheme", "'prefer-dark'");
    // 3. Organize App Grid
    let apps: Vec<String> = [
        "org.gnome.Ptyxis.desktop",
        "org.gnome.Terminal.desktop",
        "Waydroid.desktop",
        "waydroid.desktop",
        "waydroid.com.aurora.store.desktop",
        "org.gnome.seahorse.Application.desktop",
        "seahorse.desktop",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    let mut children = parse_strv(&gsettings_get("org.gnome.desktop.app-folders", "folder-children")).unwrap_or_default();
    if !children.iter().any(|c| c == "System") {
        children.push("System".into());
        gsettings_set("org.gnome.desktop.app-folders", "folder-children", &fmt_strv(&children));
    }
    let schema = "org.gnome.desktop.app-folders.folder:/org/gnome/desktop/app-folders/folders/System/";
    gsettings_set(schema, "name", "'X-GNOME-System.directory'");
    gsettings_set(schema, "translate", "true");
    let mut cur = parse_strv(&gsettings_get(schema, "apps")).unwrap_or_default();
    for a in &apps {
        if !cur.contains(a) {
            cur.push(a.clone());
        }
    }
    gsettings_set(schema, "apps", &fmt_strv(&cur));
    let util = "org.gnome.desktop.app-folders.folder:/org/gnome/desktop/app-folders/folders/Utilities/";
    let ua = parse_strv(&gsettings_get(util, "apps")).unwrap_or_default();
    let clean: Vec<String> = ua.iter().filter(|a| !apps.contains(a)).cloned().collect();
    if clean.len() != ua.len() {
        gsettings_set(util, "apps", &fmt_strv(&clean));
    }
    log::info!("Visuals and App Grid organized successfully");
    (true, "Desktop visuals and App Grid organized successfully.".into())
}
