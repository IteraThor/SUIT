//! Remove preinstalled desktop apps a dartboard machine does not need; install Chromium / Discord.
use super::i18n::{tr, trf};
use super::util::{fmt_strv, gsettings_get, gsettings_set, has_cmd, parse_strv, run};
use std::io::{BufRead, BufReader};
use std::process::{Command, Stdio};

pub const DISCORD_FLATPAK: &str = "com.discordapp.Discord";

pub const BLOATWARE_PACKAGES: [&str; 23] = [
    "gnome-contacts", "gnome-weather", "gnome-clocks", "mediawriter", "gnome-maps", "simple-scan", "gnome-boxes",
    "libreoffice-core", "showtime", "snapshot", "gnome-characters", "gnome-tour", "yelp", "gnome-font-viewer",
    "papers", "gnome-connections", "malcontent-control", "firefox", "gnome-text-editor", "gnome-calculator",
    "gnome-calendar", "loupe", "decibels",
];

pub fn friendly_name(pkg: &str) -> Option<(&'static str, &'static str, &'static str)> {
    Some(match pkg {
        "gnome-contacts" => ("Contacts", "Address book and contact management", "contact-new-symbolic"),
        "gnome-weather" => ("Weather", "Weather forecasting app", "weather-clear-symbolic"),
        "gnome-clocks" => ("Clocks", "Alarm clock, timer, and stopwatch", "preferences-system-time-symbolic"),
        "mediawriter" => ("Fedora Media Writer", "USB bootable media creator", "drive-removable-media-symbolic"),
        "gnome-maps" => ("Maps", "Desktop mapping application", "mark-location-symbolic"),
        "simple-scan" => ("Document Scanner", "Scanning utility", "scanner-symbolic"),
        "gnome-boxes" => ("Boxes", "Virtual machine manager", "computer-symbolic"),
        "libreoffice-core" => ("LibreOffice", "Office suite (Writer, Calc, Impress)", "x-office-document-symbolic"),
        "showtime" => ("Showtime", "Video player", "video-x-generic-symbolic"),
        "snapshot" => ("Snapshot", "Camera app", "camera-photo-symbolic"),
        "gnome-characters" => ("Characters", "Special characters and emoji picker", "accessories-character-map-symbolic"),
        "gnome-tour" => ("GNOME Tour", "Desktop onboarding tour", "help-about-symbolic"),
        "yelp" => ("Help", "GNOME documentation viewer", "help-browser-symbolic"),
        "gnome-font-viewer" => ("Fonts", "Font viewing utility", "preferences-desktop-font-symbolic"),
        "papers" => ("Document Viewer (Papers)", "PDF and document reader", "x-office-document-symbolic"),
        "gnome-connections" => ("Connections", "Remote desktop client", "network-wired-symbolic"),
        "malcontent-control" => ("Parental Controls", "Content restrictions", "system-users-symbolic"),
        "firefox" => ("Firefox", "Default web browser", "web-browser-symbolic"),
        "gnome-text-editor" => ("Text Editor", "Simple text editor", "accessories-text-editor-symbolic"),
        "gnome-calculator" => ("Calculator", "Desktop calculator", "accessories-calculator-symbolic"),
        "gnome-calendar" => ("Calendar", "Desktop calendar", "x-office-calendar-symbolic"),
        "loupe" => ("Image Viewer (Loupe)", "Default photo viewer", "image-x-generic-symbolic"),
        "decibels" => ("Audio Player (Decibels)", "Simple sound player", "audio-x-generic-symbolic"),
        _ => return None,
    })
}

#[derive(Debug, Clone)]
pub struct BloatApp {
    pub package: String,
    pub base: String,
    pub title: String,
    pub description: String,
    pub icon: String,
    pub is_installed: bool,
}

fn matches(full: &str, base: &str) -> bool {
    full == base || full.starts_with(&format!("{}-", base))
}

pub fn get_installed_bloatware_details(include_all: bool) -> Vec<BloatApp> {
    let full = get_installed_bloatware(None);
    let mut bases = std::collections::HashSet::new();
    for f in &full {
        match BLOATWARE_PACKAGES.iter().find(|p| matches(f, p)) {
            Some(p) => bases.insert(p.to_string()),
            None => bases.insert(f.split('-').next().unwrap_or("").to_string()),
        };
    }
    let keys: Vec<&str> = if include_all || full.is_empty() {
        BLOATWARE_PACKAGES.to_vec()
    } else {
        BLOATWARE_PACKAGES.iter().copied().filter(|k| bases.contains(*k)).collect()
    };
    keys.into_iter()
        .map(|k| {
            let (title, desc, icon) = match friendly_name(k) {
                Some((t, d, i)) => (t.to_string(), d.to_string(), i.to_string()),
                None => {
                    let t = k.replace("gnome-", "").replace('-', " ");
                    let mut c = t.chars();
                    let t = c.next().map(|f| f.to_uppercase().collect::<String>() + c.as_str()).unwrap_or_default();
                    (t, trf("Package: {name}", &[("name", &k)]), "application-x-executable-symbolic".into())
                }
            };
            BloatApp {
                package: full.iter().find(|f| matches(f, k)).cloned().unwrap_or_else(|| k.to_string()),
                base: k.into(),
                title,
                description: desc,
                icon,
                is_installed: bases.contains(k),
            }
        })
        .collect()
}

pub fn get_installed_bloatware(list: Option<&[String]>) -> Vec<String> {
    let pkgs: Vec<String> = list.map(|l| l.to_vec()).unwrap_or_else(|| BLOATWARE_PACKAGES.iter().map(|s| s.to_string()).collect());
    let mut c = Command::new("rpm");
    c.arg("-q").args(&pkgs).env("LC_ALL", "C");
    let out = super::util::run_cmd(c, Some(std::time::Duration::from_secs(30))).stdout;
    out.lines()
        .map(|l| l.trim())
        .filter(|l| !l.is_empty() && !l.starts_with("package ") && !l.contains("not installed") && !l.contains("nicht installiert"))
        .filter(|l| pkgs.iter().any(|p| matches(l, p)))
        .map(String::from)
        .collect()
}

fn edit_strv(schema: &str, key: &str, f: impl FnOnce(&mut Vec<String>) -> bool) -> bool {
    let Some(mut v) = parse_strv(&gsettings_get(schema, key)) else { return false };
    if f(&mut v) {
        return gsettings_set(schema, key, &fmt_strv(&v));
    }
    true
}

pub fn clean_dash_favorites() -> bool {
    edit_strv("org.gnome.shell", "favorite-apps", |v| {
        let n = v.len();
        v.retain(|f| f != "org.gnome.Software.desktop" && f != "org.gnome.Nautilus.desktop");
        v.len() != n
    })
}

pub fn move_seahorse_to_utilities() -> bool {
    edit_strv("org.gnome.desktop.app-folders.folder:/org/gnome/desktop/app-folders/folders/Utilities/", "apps", |v| {
        let s = "org.gnome.seahorse.Application.desktop".to_string();
        if v.contains(&s) {
            return false;
        }
        v.push(s);
        true
    })
}

/// Remove packages with `dnf remove`, reporting (current, total, message) while it runs.
pub fn debloat_packages(packages: &[String], progress: &dyn Fn(usize, usize, String)) -> (bool, String) {
    if packages.is_empty() {
        return (true, "No packages selected for removal.".into());
    }
    let mut child = match Command::new("sudo")
        .args(["-n", "dnf", "remove", "-y"])
        .args(packages)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(c) => c,
        Err(e) => return (false, trf("Debloat failed: {error}", &[("error", &e)])),
    };
    let re = regex::Regex::new(r"(?:Erasing|Removing)\s*:\s*(\S+)\s+(\d+)/(\d+)").unwrap();
    let total = packages.len();
    progress(0, total, tr("Starting package removal transaction..."));
    if let Some(out) = child.stdout.take() {
        for line in BufReader::new(out).lines().map_while(Result::ok) {
            let l = line.trim();
            if l.is_empty() {
                continue;
            }
            log::debug!("[dnf-debloat] {}", l);
            if let Some(m) = re.captures(l) {
                let (name, cur, tot) = (&m[1], m[2].parse().unwrap_or(0), m[3].parse().unwrap_or(total));
                let disp = BLOATWARE_PACKAGES
                    .iter()
                    .find(|k| name.starts_with(*k))
                    .and_then(|k| friendly_name(k))
                    .map(|f| f.0.to_string())
                    .unwrap_or_else(|| name.split('-').next().unwrap_or(name).to_string());
                progress(cur, tot, trf("Removing {name} ({cur}/{tot})...", &[("name", &tr(&disp)), ("cur", &cur), ("tot", &tot)]));
            } else if l.contains("Running transaction") {
                progress(0, total, tr("Running removal transaction..."));
            } else if l.contains("Preparing") {
                progress(0, total, tr("Preparing transaction..."));
            }
        }
    }
    let _ = child.wait();
    progress(total, total, tr("Cleaning Dash favorites and app grid..."));
    clean_dash_favorites();
    move_seahorse_to_utilities();
    log::info!("Debloat and Dash cleanup completed successfully");
    (true, trf("Successfully removed {packages} application(s).", &[("packages", &packages.len())]))
}

pub fn add_favorite_app(desktop: &str) -> bool {
    edit_strv("org.gnome.shell", "favorite-apps", |v| {
        if v.iter().any(|f| f == desktop) {
            return false;
        }
        v.push(desktop.into());
        true
    })
}

pub fn is_app_installed(app: &str) -> bool {
    match app {
        "chromium" => has_cmd("chromium-browser") || has_cmd("chromium"),
        a => has_cmd(a),
    }
}

pub fn is_discord_installed() -> bool {
    run(&["flatpak", "info", DISCORD_FLATPAK], Some(5)).code == 0
}

pub fn install_discord() -> (bool, String) {
    // Not in the Fedora repos; Flathub ships the official client
    run(&["sudo", "-n", "flatpak", "remote-modify", "--enable", "flathub"], Some(15));
    let r = run(&["sudo", "-n", "flatpak", "install", "-y", "--noninteractive", "flathub", DISCORD_FLATPAK], Some(900));
    if !r.ok() {
        log::error!("Failed installing Discord: {}", r.stderr);
        return (false, trf("Failed installing Discord: {error}", &[("error", &r.stderr.trim())]));
    }
    add_favorite_app(&format!("{}.desktop", DISCORD_FLATPAK));
    (true, "Discord installed and pinned to Dash.".into())
}

pub fn install_chromium() -> (bool, String) {
    let r = run(&["sudo", "-n", "dnf", "install", "-y", "chromium"], None);
    if !r.ok() {
        log::error!("Failed installing Chromium: {}", r.stderr);
        return (false, trf("Failed installing Chromium: {error}", &[("error", &r.stderr.trim())]));
    }
    add_favorite_app("chromium-browser.desktop");
    (true, "Chromium installed and pinned to Dash.".into())
}
