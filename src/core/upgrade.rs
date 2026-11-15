//! Repairs after a Fedora upgrade (new GNOME Shell, new Python for AutoGlow, gnome-keyring -> oo7, new systemd).
//!
//! SUIT remembers the system versions it last saw working in ~/.config/suit/system_versions.json. At start-up
//! `find_problems()` looks for what an upgrade typically leaves broken; each Problem carries its own fix, which
//! the "Fix All" page runs one after the other.
//!
//! The Python port also checked SUIT's own venv; the Rust binary has none (a binary that no longer starts after
//! an upgrade is rebuilt by the launcher script instead).
use super::i18n::{tr, trf};
use super::util::{home, read_json, run, run_cmd, run_with_input, write_atomic};
use gtk::gio;
use gtk::gio::prelude::*;
use gtk::glib::{prelude::ToVariant, VariantTy};
use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Arc;

const SYSTEM_PYTHON: &str = "/usr/bin/python3";
const VENV_BASE_PACKAGES: [&str; 3] = ["pip", "setuptools", "wheel"];

fn state_path() -> PathBuf {
    home().join(".config/suit/system_versions.json")
}

pub type Fix = Arc<dyn Fn() -> (bool, String) + Send + Sync>;

#[derive(Clone)]
pub struct Problem {
    pub id: String,
    pub title: String,
    pub detail: String,
    pub fix: Fix,
    /// the fix takes effect at the next login
    pub relogin: bool,
    /// the fix opens a system prompt the user has to answer
    pub interactive: bool,
    /// already fixed, only a login/restart is missing
    pub waiting_for_login: bool,
}

impl std::fmt::Debug for Problem {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Problem({})", self.id)
    }
}

// --- versions ---

fn os_release() -> Map<String, Value> {
    std::fs::read_to_string("/etc/os-release")
        .unwrap_or_default()
        .lines()
        .filter_map(|l| l.split_once('=').map(|(k, v)| (k.trim().to_string(), json!(v.trim().trim_matches('"')))))
        .collect()
}

/// Version of the running GNOME Shell ("51.1"), else of the installed one.
pub fn gnome_shell_version() -> Option<String> {
    let dbus = (|| {
        let b = gio::bus_get_sync(gio::BusType::Session, gio::Cancellable::NONE).ok()?;
        let v = b
            .call_sync(
                Some("org.gnome.Shell"),
                "/org/gnome/Shell",
                "org.freedesktop.DBus.Properties",
                "Get",
                Some(&("org.gnome.Shell", "ShellVersion").to_variant()),
                Some(VariantTy::new("(v)").unwrap()),
                gio::DBusCallFlags::NONE,
                2000,
                gio::Cancellable::NONE,
            )
            .ok()?;
        v.child_value(0).as_variant()?.get::<String>()
    })();
    if dbus.is_some() {
        return dbus;
    }
    let out = run(&["gnome-shell", "--version"], Some(10)).stdout;
    regex::Regex::new(r"(\d+(?:\.\w+)*)").unwrap().captures(&out).map(|c| c[1].to_string())
}

fn major(v: Option<&str>) -> Option<String> {
    regex::Regex::new(r"^(\d+)").unwrap().captures(v.unwrap_or("")).map(|c| c[1].to_string())
}

/// '3.15' for a python binary (symlinks resolved), None if it is missing.
fn interpreter_version(p: &Path) -> Option<String> {
    let real = std::fs::canonicalize(p).ok()?;
    let name = real.file_name()?.to_string_lossy().to_string();
    if let Some(c) = regex::Regex::new(r"python(\d+\.\d+)$").unwrap().captures(&name) {
        return Some(c[1].to_string());
    }
    let o = run(&[&real.to_string_lossy(), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], Some(10));
    Some(o.stdout.trim().to_string()).filter(|s| !s.is_empty())
}

pub fn current_versions() -> Map<String, Value> {
    let o = os_release();
    let mut m = Map::new();
    m.insert("os_name".into(), o.get("NAME").cloned().unwrap_or(json!("Linux")));
    m.insert("os".into(), o.get("VERSION_ID").cloned().unwrap_or(json!("")));
    m.insert("gnome".into(), json!(major(gnome_shell_version().as_deref()).unwrap_or_default()));
    m.insert("python".into(), json!(interpreter_version(Path::new(SYSTEM_PYTHON)).unwrap_or_default()));
    m
}

pub fn load_seen() -> Map<String, Value> {
    read_json(state_path()).and_then(|v| v.as_object().cloned()).unwrap_or_default()
}

pub fn save_seen(v: &Map<String, Value>) {
    if let Err(e) = write_atomic(state_path(), serde_json::to_string_pretty(v).unwrap().as_bytes()) {
        log::error!("Failed saving {}: {}", state_path().display(), e);
    }
}

fn s(m: &Map<String, Value>, k: &str) -> String {
    m.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string()
}

/// Versions before an upgrade SUIT did not see coming: older Fedora kernels in /lib/modules and the
/// Python version a stale AutoGlow venv was built for.
pub fn guess_previous(current: &Map<String, Value>) -> Map<String, Value> {
    let mut prev = Map::new();
    let re = regex::Regex::new(r"\.fc(\d+)\.").unwrap();
    let cur_os: Option<i64> = s(current, "os").parse().ok();
    let rels: Vec<i64> = std::fs::read_dir("/lib/modules")
        .map(|rd| rd.flatten().filter_map(|e| re.captures(&e.file_name().to_string_lossy()).and_then(|c| c[1].parse().ok())).collect())
        .unwrap_or_default();
    if let Some(c) = cur_os {
        if let Some(m) = rels.into_iter().filter(|r| *r < c).max() {
            prev.insert("os".into(), json!(m.to_string()));
        }
    }
    if let Some(d) = autoglow_dir() {
        if let Some(b) = venv_built_for(&d.join("venv")) {
            if b != s(current, "python") {
                prev.insert("python".into(), json!(b));
            }
        }
    }
    prev
}

/// ['Fedora Linux 44 → 45', 'GNOME 50 → 51', 'Python 3.14 → 3.15'] for what changed since SUIT last looked.
pub fn describe_changes(seen: &Map<String, Value>, current: &Map<String, Value>) -> Vec<String> {
    let os_name = s(current, "os_name");
    [("os", if os_name.is_empty() { "Linux".to_string() } else { os_name }), ("gnome", "GNOME".into()), ("python", "Python".into())]
        .into_iter()
        .filter_map(|(k, label)| {
            let (o, n) = (s(seen, k), s(current, k));
            if !o.is_empty() && !n.is_empty() && o != n { Some(format!("{} {} → {}", label, o, n)) } else { None }
        })
        .collect()
}

fn last_line(o: &super::util::Output) -> String {
    let t = if o.stderr.trim().is_empty() { o.stdout.trim() } else { o.stderr.trim() };
    t.lines().last().map(String::from).unwrap_or_else(|| format!("exit code {}", o.code))
}

// --- Python environments (AutoGlow 2's venv) ---

/// Python version a venv was created with, from its pyvenv.cfg ('3.14').
pub fn venv_built_for(venv: &Path) -> Option<String> {
    let cfg = std::fs::read_to_string(venv.join("pyvenv.cfg")).ok()?;
    regex::Regex::new(r"(?m)^version(?:_info)?\s*=\s*(\d+\.\d+)").unwrap().captures(&cfg).map(|c| c[1].to_string())
}

/// True when a venv was built for another Python than the one it now runs: its packages are not found.
pub fn venv_is_stale(venv: &Path) -> bool {
    let Some(b) = venv_built_for(venv) else { return false };
    interpreter_version(&venv.join("bin/python3")).map(|r| r != b).unwrap_or(true)
}

/// Names of the packages pip installed into a venv (any Python version folder).
pub fn venv_packages(venv: &Path) -> Vec<String> {
    let mut names: Vec<String> = glob::glob(&format!("{}/lib/python*/site-packages/*.dist-info", venv.display()))
        .map(|g| g.flatten().collect::<Vec<_>>())
        .unwrap_or_default()
        .into_iter()
        .filter(|i| std::fs::read_to_string(i.join("INSTALLER")).map(|s| s.trim() == "pip").unwrap_or(true))
        .map(|i| {
            let fallback = i.file_name().unwrap().to_string_lossy().split('-').next().unwrap_or("").to_string();
            std::fs::read_to_string(i.join("METADATA"))
                .ok()
                .and_then(|m| regex::Regex::new(r"(?m)^Name:\s*(\S+)").unwrap().captures(&m).map(|c| c[1].to_string()))
                .unwrap_or(fallback)
        })
        .filter(|n| !VENV_BASE_PACKAGES.contains(&n.to_lowercase().as_str()))
        .collect();
    names.sort_by_key(|n| n.to_lowercase());
    names.dedup();
    names
}

/// Recreate a venv for the current Python and reinstall the packages pip had put into it.
pub fn rebuild_venv(venv: &Path) -> (bool, String) {
    let system_site = std::fs::read_to_string(venv.join("pyvenv.cfg"))
        .map(|c| regex::RegexBuilder::new(r"^include-system-site-packages\s*=\s*true").multi_line(true).case_insensitive(true).build().unwrap().is_match(&c))
        .unwrap_or(true);
    let pk = venv_packages(venv);
    let vs = venv.to_string_lossy().to_string();
    let mut cmd = vec![SYSTEM_PYTHON, "-m", "venv", "--clear"];
    if system_site {
        cmd.push("--system-site-packages");
    }
    cmd.push(&vs);
    let r = run(&cmd, Some(300));
    if r.code != 0 {
        return (false, trf("Could not rebuild the Python environment: {error}", &[("error", &last_line(&r))]));
    }
    if !pk.is_empty() {
        let py = venv.join("bin/python3").to_string_lossy().to_string();
        let mut a = vec![py.as_str(), "-m", "pip", "install", "--quiet", "--disable-pip-version-check"];
        a.extend(pk.iter().map(|s| s.as_str()));
        if run(&a, Some(900)).code != 0 {
            return (true, trf("Python environment rebuilt, but these packages could not be reinstalled: {pkgs}", &[("pkgs", &pk.join(", "))]));
        }
    }
    (true, tr("Python environment rebuilt."))
}

// --- problem: AutoGlow 2's venv ---

fn autoglow_dir() -> Option<PathBuf> {
    let d = super::autoglow::get_install_dir().or_else(super::autoglow::unit_dir)?;
    if super::autoglow::is_autoglow2_dir(&d) { Some(d) } else { None }
}

fn check_autoglow() -> Option<Problem> {
    let dir = autoglow_dir()?;
    let venv = dir.join("venv");
    if !venv_is_stale(&venv) {
        return None;
    }
    let old = venv_built_for(&venv).unwrap_or_default();
    let d2 = dir.clone();
    Some(Problem {
        id: "autoglow_venv".into(),
        title: tr("AutoGlow 2"),
        detail: trf(
            "Its Python environment was built for Python {old} and cannot start. AutoGlow's setup runs again (takes a few minutes).",
            &[("old", &old)],
        ),
        fix: Arc::new(move || {
            // AutoGlow's setup builds a fresh venv, installs its packages and restarts the service
            let _ = std::fs::remove_dir_all(d2.join("venv"));
            let (ok, msg) = super::autoglow::run_setup(&d2);
            if !ok {
                return (false, msg);
            }
            let a = run(&["systemctl", "is-active", "autoglow.service"], Some(10)).stdout.trim().to_string();
            if a != "active" {
                return (false, trf("AutoGlow 2 was set up again but its service is {state}.", &[("state", &if a.is_empty() { "?".into() } else { a })]));
            }
            (true, tr("AutoGlow 2 is running again."))
        }),
        relogin: false,
        interactive: false,
        waiting_for_login: false,
    })
}

// --- problem: SUIT shell extensions ---

fn suit_extensions() -> Vec<(&'static str, PathBuf, PathBuf)> {
    use super::desktop as d;
    vec![
        (d::EXTENSION_UUID, d::extension_source_dir(), d::user_extension_dir()),
        (d::HW_OVERLAY_UUID, d::hw_overlay_source_dir(), d::hw_overlay_user_dir()),
        (d::MINIMAL_DESKTOP_UUID, d::minimal_desktop_source_dir(), d::minimal_desktop_user_dir()),
    ]
}

fn shell_versions(dir: &Path) -> Vec<String> {
    read_json(dir.join("metadata.json"))
        .and_then(|m| m["shell-version"].as_array().cloned())
        .map(|a| a.iter().map(|v| v.as_str().map(String::from).unwrap_or_else(|| v.to_string())).collect())
        .unwrap_or_default()
}

/// GNOME Shell's state for an extension ('ACTIVE', 'OUT OF DATE', 'ERROR', ...), None if it does not know it.
pub fn extension_state(uuid: &str) -> Option<String> {
    let mut c = Command::new("gnome-extensions");
    c.args(["info", uuid]).env("LC_ALL", "C");
    let r = run_cmd(c, Some(std::time::Duration::from_secs(10)));
    if r.code != 0 {
        return None;
    }
    regex::Regex::new(r"(?m)^\s*State:\s*(.+)$").unwrap().captures(&r.stdout).map(|c| c[1].trim().to_string())
}

fn check_extensions() -> Option<Problem> {
    let shell = major(gnome_shell_version().as_deref())?;
    let enabled = super::util::parse_strv(&super::util::gsettings_get("org.gnome.shell", "enabled-extensions")).unwrap_or_default();
    let (mut outdated, mut waiting) = (Vec::new(), Vec::new());
    for (uuid, _, dest) in suit_extensions() {
        if !dest.join("metadata.json").is_file() {
            continue;
        }
        if !shell_versions(&dest).contains(&shell) {
            outdated.push(uuid.to_string());
        } else if enabled.iter().any(|e| e == uuid) && extension_state(uuid).as_deref() == Some("OUT OF DATE") {
            waiting.push(uuid.to_string()); // files already fixed, GNOME Shell reads them at the next login
        }
    }
    if outdated.is_empty() && waiting.is_empty() {
        return None;
    }
    let sh = shell.clone();
    let names = outdated.iter().chain(&waiting).cloned().collect::<Vec<_>>().join(", ");
    let detail = if !outdated.is_empty() {
        trf("GNOME {version} switched off: {names}. They get a version for this GNOME.", &[("version", &shell), ("names", &names)])
    } else {
        trf("Already updated, GNOME loads them at the next login: {names}.", &[("names", &names)])
    };
    Some(Problem {
        id: "extensions".into(),
        title: tr("Touch, overlay and desktop extensions"),
        detail,
        fix: Arc::new(move || {
            let mut untested = Vec::new();
            for (uuid, src, dest) in suit_extensions() {
                if !dest.join("metadata.json").is_file() {
                    continue;
                }
                super::desktop::install_extension_files(&src, &dest);
                let mut versions = shell_versions(&dest);
                if !versions.contains(&sh) {
                    // A GNOME Shell newer than this SUIT knows: let it load the extension anyway.
                    if let Some(mut meta) = read_json(dest.join("metadata.json")) {
                        versions.push(sh.clone());
                        meta["shell-version"] = json!(versions);
                        let _ = std::fs::write(dest.join("metadata.json"), serde_json::to_string_pretty(&meta).unwrap() + "\n");
                        untested.push(uuid);
                        log::warn!("{}: added untested GNOME Shell {} to the installed metadata", uuid, sh);
                    }
                }
            }
            if !untested.is_empty() {
                return (true, trf("Extensions updated (not yet tested with GNOME {version}). Restart or log out to load them.", &[("version", &sh)]));
            }
            (true, trf("Extensions updated for GNOME {version}. Restart or log out to load them.", &[("version", &sh)]))
        }),
        relogin: true,
        interactive: false,
        waiting_for_login: outdated.is_empty(),
    })
}

// --- problem: keyring (gnome-keyring -> oo7) ---

fn check_keyring() -> Option<Problem> {
    use super::keyring as k;
    if !k::uses_oo7() {
        return None;
    }
    let (autologin, _) = super::power::is_auto_login_enabled(None);
    // Only a problem for an automatic login: nothing types the keyring password then.
    if !autologin || k::oo7_autounlock_ok().0 {
        return None;
    }
    Some(Problem {
        id: "keyring".into(),
        title: tr("Login keyring"),
        detail: tr("The new keyring service (oo7) cannot unlock the keyring after the automatic login and asks for a password at every start. SUIT stores a keyring password protected by this computer's TPM, so oo7 unlocks it by itself. Saved entries are kept."),
        fix: Arc::new(k::setup_oo7_autounlock),
        relogin: false,
        interactive: k::oo7_login_locked() == Some(true),
        waiting_for_login: false,
    })
}

// --- problem: Remote Desktop starts before oo7 ---

fn check_remote_desktop() -> Option<Problem> {
    if !super::remote_desktop::needs_secret_service_order() {
        return None;
    }
    Some(Problem {
        id: "remote_desktop".into(),
        title: tr("Remote Desktop"),
        detail: tr("It starts at the same moment as the new keyring service (oo7) and fails when oo7 is not ready yet. It is set to wait for oo7."),
        fix: Arc::new(super::remote_desktop::ensure_secret_service_order),
        relogin: false,
        interactive: false,
        waiting_for_login: false,
    })
}

// --- problem: Fast Boot's login unit copy ---

const USER_SESSIONS_OVERRIDE: &str = "/etc/systemd/system/systemd-user-sessions.service";

fn check_login_override() -> Option<Problem> {
    let installed = std::fs::read_to_string(USER_SESSIONS_OVERRIDE).ok()?;
    if !installed.contains("Written by SUIT Fast Boot") {
        return None;
    }
    let wanted = super::boot::user_sessions_text()?;
    if wanted == installed {
        return None;
    }
    Some(Problem {
        id: "login_override".into(),
        title: tr("Fast Boot: login before network"),
        detail: tr("SUIT's copy of systemd-user-sessions.service is older than the new systemd's. It is copied again."),
        fix: Arc::new(move || {
            let r = run_with_input(&["sudo", "-n", "tee", USER_SESSIONS_OVERRIDE], &wanted, Some(30));
            if r.code != 0 {
                return (false, trf("Could not write {path}: {error}", &[("path", &USER_SESSIONS_OVERRIDE), ("error", &last_line(&r))]));
            }
            run(&["sudo", "-n", "systemctl", "daemon-reload"], Some(60));
            (true, tr("Login unit updated to the new systemd version."))
        }),
        relogin: false,
        interactive: false,
        waiting_for_login: false,
    })
}

// --- public API ---

/// Everything an upgrade left broken, in the order Fix All runs it (interactive fixes last).
pub fn find_problems() -> Vec<Problem> {
    let checks: [fn() -> Option<Problem>; 5] = [check_autoglow, check_extensions, check_login_override, check_keyring, check_remote_desktop];
    let mut p: Vec<Problem> = checks.iter().filter_map(|c| c()).collect();
    p.sort_by_key(|p| p.interactive);
    p
}

pub struct StartupCheck {
    pub problems: Vec<Problem>,
    pub changes: Vec<String>,
    pub current: Map<String, Value>,
}

/// Run at SUIT start (background thread). Records the versions when all is well.
pub fn startup_check() -> StartupCheck {
    let current = current_versions();
    let mut seen = load_seen();
    let problems = find_problems();
    if !problems.is_empty() && seen.is_empty() {
        seen = guess_previous(&current);
    }
    if seen != current && problems.iter().all(|p| p.waiting_for_login) {
        save_seen(&current);
    }
    let changes = describe_changes(&seen, &current);
    if !problems.is_empty() {
        log::info!(
            "After the system upgrade ({}) {} thing(s) need fixing: {}",
            if changes.is_empty() { "versions unknown".to_string() } else { changes.join(", ") },
            problems.len(),
            problems.iter().map(|p| p.id.as_str()).collect::<Vec<_>>().join(", ")
        );
    }
    StartupCheck { problems, changes, current }
}

pub fn mark_fixed() {
    save_seen(&current_versions());
}

pub fn reboot() -> (bool, String) {
    let r = run(&["systemctl", "reboot"], Some(30));
    (r.code == 0, if r.code != 0 { last_line(&r) } else { String::new() })
}
