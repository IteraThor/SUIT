//! Installs, updates and controls AutoGlow 2 (github.com/IteraThor/Autoglow-2) as its systemd service.
use super::i18n::{tr, trf};
use super::systemd::{self, Scope};
use super::util::{home, run, run_in, shquote};
use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

pub const REPO_URL: &str = "https://github.com/IteraThor/Autoglow-2.git";
pub const SERVICE_NAME: &str = "autoglow.service";
pub const UNIT_PATH: &str = "/etc/systemd/system/autoglow.service";
pub const WEB_URL: &str = "http://localhost:8080";
/// Browser policy files AutoGlow's setup.sh writes to force-install the AutoGlow Bridge extension
const POLICY_FILES: [&str; 5] = [
    "/etc/opt/chrome/policies/managed/autoglow.json",
    "/etc/chromium/policies/managed/autoglow.json",
    "/etc/chromium-browser/policies/managed/autoglow.json",
    "/etc/brave/policies/managed/autoglow.json",
    "/etc/opt/edge/policies/managed/autoglow.json",
];
/// Files 2.1 tracked in git that are user data since 2.2; local edits to them block `git pull`
const LEGACY_TRACKED_FILES: [&str; 2] = ["game_flows.json", "presets/Default.json"];
const SETUP_TIMEOUT: u64 = 900;
/// Everything AutoGlow 2 writes for the user lives in its own folder
const USER_DATA: [&str; 8] = ["config.json", "config.json.bak", "game_flows.json", "game_flows.json.bak", "presets", "studio", "backups", "chrome_extension.key"];
/// Untracked files AutoGlow itself creates; anything else in a git checkout is someone's own work
const RUNTIME_FILES: [&str; 20] = [
    "venv", ".venv", "__pycache__", "*.pyc", "*.log", "*.log.*", "config.json", "config.json.*", "game_flows.json", "game_flows.json.*",
    "presets", "studio", "backups", "scratch", "chrome_extension.key", "online_signals.jsonl", ".sync_status.json", "build", "dist", "target",
];
const SKIP_DIRS: [&str; 5] = ["venv", "node_modules", "__pycache__", "site-packages", "snap"];

pub fn default_dir() -> PathBuf {
    home().join("Autoglow-2")
}
/// The new version is cloned here first, so a failed download leaves an older install untouched
fn staging_dir() -> PathBuf {
    home().join(".Autoglow-2.suit-new")
}
fn backup_root() -> PathBuf {
    home().join(".local/share/suit/autoglow-backups")
}

fn canon(p: &Path) -> PathBuf {
    std::fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf())
}

/// AutoGlow 1 used the same service name and also had a server.py, but no core/version.py
pub fn is_autoglow2_dir(p: &Path) -> bool {
    p.join("server.py").is_file() && p.join("core/version.py").is_file()
}

/// Where AutoGlow's one-line installer (install.sh from a GitHub release) puts AutoGlow
pub fn release_default_dir() -> PathBuf {
    home().join("autoglow")
}
const RELEASES_API: &str = "https://api.github.com/repos/IteraThor/Autoglow-2/releases/latest";

/// A folder from AutoGlow's one-line installer: the ready-built program and its installer, no source code.
pub fn is_release_dir(p: &Path) -> bool {
    p.join("autoglow").is_file()
        && p.join("install.sh").is_file()
        && p.join("web").is_dir()
        && !p.join(".git").exists()
        && !p.join("Cargo.toml").exists()
        && !p.join("server.py").exists()
}

/// WorkingDirectory of autoglow.service, whatever it belongs to.
pub fn unit_dir() -> Option<PathBuf> {
    let t = std::fs::read_to_string(UNIT_PATH).ok()?;
    regex::Regex::new(r"(?m)^WorkingDirectory=(.+)$").unwrap().captures(&t).map(|c| PathBuf::from(c[1].trim()))
}

/// The AutoGlow 2 install SUIT manages: a git clone in the default folder, or a folder from the one-line
/// installer (the one the service runs, else ~/autoglow). With both, the one the service runs wins.
pub fn get_install_dir() -> Option<PathBuf> {
    let git = default_dir();
    let git_ok = is_autoglow2_dir(&git) && git.join(".git").exists();
    if let Some(u) = unit_dir() {
        if git_ok && canon(&u) == canon(&git) {
            return Some(git);
        }
        if is_release_dir(&u) {
            return Some(u);
        }
    }
    if git_ok {
        return Some(git);
    }
    Some(release_default_dir()).filter(|r| is_release_dir(r))
}

/// (pid, folder) of python processes running a server.py or start.py, e.g. a hand-started copy.
fn autoglow_processes() -> Vec<(i32, PathBuf)> {
    let mut out = Vec::new();
    let Ok(rd) = std::fs::read_dir("/proc") else { return out };
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        let Ok(pid) = name.parse::<i32>() else { continue };
        let Ok(raw) = std::fs::read(format!("/proc/{}/cmdline", pid)) else { continue };
        let args: Vec<String> = String::from_utf8_lossy(&raw).split('\0').map(String::from).collect();
        if !args.first().map(|a| Path::new(a).file_name().map(|n| n.to_string_lossy().contains("python")).unwrap_or(false)).unwrap_or(false) {
            continue;
        }
        let Some(script) = args[1..].iter().find(|a| matches!(Path::new(a).file_name().and_then(|n| n.to_str()), Some("server.py" | "start.py"))) else {
            continue;
        };
        let p = if Path::new(script).is_absolute() {
            PathBuf::from(script)
        } else {
            match std::fs::read_link(format!("/proc/{}/cwd", pid)) {
                Ok(c) => c.join(script),
                Err(_) => continue,
            }
        };
        if let Some(parent) = p.parent() {
            out.push((pid, parent.to_path_buf()));
        }
    }
    out
}

fn walk(root: &Path, depth: usize, max: usize, out: &mut Vec<PathBuf>) {
    if root.join("server.py").is_file() && is_autoglow2_dir(root) {
        out.push(root.to_path_buf());
        return;
    }
    if depth >= max {
        return;
    }
    let Ok(rd) = std::fs::read_dir(root) else { return };
    for e in rd.flatten() {
        let n = e.file_name().to_string_lossy().to_string();
        if n.starts_with('.') || SKIP_DIRS.contains(&n.as_str()) {
            continue;
        }
        if e.file_type().map(|t| t.is_dir()).unwrap_or(false) {
            walk(&e.path(), depth + 1, max, out);
        }
    }
}

/// Every AutoGlow 2 copy on this computer except the one SUIT manages.
pub fn find_old_installs() -> Vec<PathBuf> {
    let managed = get_install_dir();
    let mut cands = Vec::new();
    if let Some(u) = unit_dir() {
        cands.push(u);
    }
    cands.extend(autoglow_processes().into_iter().map(|(_, d)| d));
    if managed.is_none() {
        cands.push(default_dir());
    }
    for (root, max) in [(home(), 4), (PathBuf::from("/opt"), 2)] {
        if root.is_dir() {
            walk(&root, 0, max, &mut cands);
        }
    }
    let mut seen = HashSet::new();
    let staging = canon(&staging_dir());
    let managed_c = managed.as_ref().map(|m| canon(m));
    cands
        .into_iter()
        .filter(|c| {
            let k = canon(c);
            is_autoglow2_dir(c) && seen.insert(k.clone()) && Some(&k) != managed_c.as_ref() && k != staging
        })
        .collect()
}

/// True for a git checkout someone works in: edits or commits not on its remote, or files that are neither
/// AutoGlow's nor written by it at runtime. Such a folder is never deleted.
fn has_local_work(p: &Path) -> bool {
    if !p.join(".git").exists() {
        return false;
    }
    let st = run_in(p, &["git", "status", "--porcelain", "--ignored", "--untracked-files=normal"], Some(15));
    let un = run_in(p, &["git", "rev-list", "--count", "HEAD", "--not", "--remotes"], Some(15));
    if !st.ok() || !un.ok() {
        return true;
    }
    let pats: Vec<glob::Pattern> = RUNTIME_FILES.iter().filter_map(|p| glob::Pattern::new(p).ok()).collect();
    for line in st.stdout.lines() {
        if line.len() < 4 {
            continue;
        }
        let (code, rel) = (&line[..2], line[3..].trim().trim_matches('"').trim_end_matches('/'));
        if code == "??" || code == "!!" {
            if !rel.split('/').any(|part| pats.iter().any(|p| p.matches(part))) {
                return true;
            }
        } else if !LEGACY_TRACKED_FILES.contains(&rel) {
            return true;
        }
    }
    un.stdout.trim().parse::<i64>().unwrap_or(0) > 0
}

pub fn get_version(dir: &Path) -> String {
    if is_release_dir(dir) {
        // Prints "AutoGlow 2 v0.33.1"
        let r = run(&[&dir.join("autoglow").to_string_lossy(), "version"], Some(10));
        return r.stdout.trim().rsplit(" v").next().filter(|_| r.ok()).unwrap_or_default().to_string();
    }
    let Ok(t) = std::fs::read_to_string(dir.join("core/version.py")) else { return String::new() };
    regex::Regex::new(r#"__version__\s*=\s*["']([^"']+)["']"#).unwrap().captures(&t).map(|c| c[1].to_string()).unwrap_or_default()
}

#[derive(Debug, Clone)]
pub struct OldInstall {
    pub path: PathBuf,
    pub version: String,
    pub keep: bool,
    pub active: bool,
}

#[derive(Debug, Clone, Default)]
pub struct AutoGlowStatus {
    pub installed: bool,
    pub legacy: bool,
    pub old_installs: Vec<OldInstall>,
    pub install_dir: Option<PathBuf>,
    pub version: String,
    pub is_git: bool,
    /// Installed by AutoGlow's one-line installer: updated from the GitHub releases, not with git
    pub release: bool,
    pub service_present: bool,
    pub running: bool,
    pub active_state: String,
    pub enabled: bool,
    /// The AutoGlow Bridge extension is in a browser profile; None while AutoGlow is not running (its ID is unknown)
    pub extension: Option<bool>,
}

pub fn get_status() -> AutoGlowStatus {
    let inst = get_install_dir();
    let unit = systemd::get_status(SERVICE_NAME, Scope::System);
    let ud = unit_dir();
    let running: HashSet<PathBuf> = autoglow_processes().into_iter().map(|(_, d)| canon(&d)).collect();
    let old = find_old_installs()
        .into_iter()
        .map(|d| {
            let c = canon(&d);
            OldInstall {
                version: get_version(&d),
                keep: has_local_work(&d),
                active: running.contains(&c) || ud.as_ref().map(|u| canon(u) == c).unwrap_or(false),
                path: d,
            }
        })
        .collect();
    AutoGlowStatus {
        installed: inst.is_some(),
        // AutoGlow 1 used the same service name
        legacy: Path::new(UNIT_PATH).is_file() && !ud.as_ref().map(|u| is_autoglow2_dir(u) || is_release_dir(u)).unwrap_or(false),
        old_installs: old,
        version: inst.as_ref().map(|d| get_version(d)).unwrap_or_default(),
        is_git: inst.as_ref().map(|d| d.join(".git").exists()).unwrap_or(false),
        release: inst.as_ref().map(|d| is_release_dir(d)).unwrap_or(false),
        service_present: unit.active_state != "nofile" && Path::new(UNIT_PATH).is_file() && inst.is_some() && ud == inst,
        running: unit.active_state == "active",
        enabled: unit.unit_file_state == "enabled",
        extension: if unit.active_state == "active" { extension_id().map(|id| extension_installed(&id)) } else { None },
        active_state: unit.active_state,
        install_dir: inst,
    }
}

/// The ID of this install's AutoGlow Bridge extension, as the running AutoGlow serves it (the key is per install).
fn extension_id() -> Option<String> {
    let xml = String::from_utf8(super::http::get(&format!("{}/extension/update.xml", WEB_URL), 3.0)?).ok()?;
    let id = xml.split("appid='").nth(1)?.split('\'').next()?;
    (id.len() == 32 && id.bytes().all(|b| (b'a'..=b'p').contains(&b))).then(|| id.to_string())
}

/// Is the extension in a profile of Chromium, Chrome, Brave or Edge?
fn extension_installed(id: &str) -> bool {
    ["chromium", "google-chrome", "BraveSoftware/Brave-Browser", "microsoft-edge"].iter().any(|b| {
        let pat = format!("{}/.config/{}/*/Extensions/{}", home().display(), b, id);
        glob::glob(&pat).map(|mut g| g.next().is_some()).unwrap_or(false)
    })
}

/// Writes the browser policy that makes Chromium, Chrome, Brave and Edge install the AutoGlow Bridge extension from
/// the running AutoGlow, for the same browsers as AutoGlow's install.sh. A browser that is open adds it within seconds.
pub fn install_extension() -> (bool, String) {
    let Some(id) = extension_id() else {
        return (false, tr("Start AutoGlow first: the browser gets the extension from it."));
    };
    let policy = format!("{{\"ExtensionInstallForcelist\": [\"{};{}/extension/update.xml\"]}}", id, WEB_URL);
    let script = format!(
        "has() {{ command -v \"$1\" >/dev/null 2>&1; }}; dirs='/etc/opt/chrome /etc/chromium'; \
         if has chromium-browser || [ -d /snap/chromium ]; then dirs=\"$dirs /etc/chromium-browser\"; fi; \
         if has brave-browser || has brave; then dirs=\"$dirs /etc/brave\"; fi; \
         if has microsoft-edge || has microsoft-edge-stable; then dirs=\"$dirs /etc/opt/edge\"; fi; \
         for d in $dirs; do mkdir -p \"$d/policies/managed\" && printf '%s\\n' {} > \"$d/policies/managed/autoglow.json\" \
         && chmod 644 \"$d/policies/managed/autoglow.json\" || exit 1; done",
        shquote(&policy)
    );
    let r = run(&["sudo", "-n", "bash", "-c", &script], Some(30));
    if r.code != 0 {
        log::warn!("Writing the AutoGlow Bridge browser policy failed: {}", r.stderr.trim());
        return (false, tr("Could not set up the AutoGlow Bridge extension (administrator rights needed)."));
    }
    (true, tr("AutoGlow Bridge is set up. An open browser adds it within a few seconds, otherwise the next time it starts."))
}

/// AutoGlow's own setup.sh: system packages, venv, browser extension policy and the service.
pub fn run_setup(dir: &Path) -> (bool, String) {
    let r = run_in(dir, &["sudo", "-n", "bash", &dir.join("setup.sh").to_string_lossy()], Some(SETUP_TIMEOUT));
    if r.timed_out {
        return (false, "AutoGlow setup timed out.".into());
    }
    if r.code != 0 {
        let src = if r.stderr.trim().is_empty() { r.stdout.trim() } else { r.stderr.trim() };
        let tail = src.lines().last().unwrap_or("unknown error").to_string();
        log::warn!("AutoGlow setup.sh failed: {}", r.stderr);
        return (false, trf("AutoGlow setup failed: {error}", &[("error", &tail)]));
    }
    (true, "AutoGlow 2 is installed and running.".into())
}

fn clone(dest: &Path) -> (bool, String) {
    let _ = std::fs::remove_dir_all(dest);
    let r = run(&["git", "clone", "--depth", "1", REPO_URL, &dest.to_string_lossy()], Some(180));
    if r.code != 0 {
        log::warn!("AutoGlow clone failed: {}", r.stderr.trim());
        let _ = std::fs::remove_dir_all(dest);
        return (false, "Download failed. Check internet connection.".into());
    }
    (true, String::new())
}

fn copy_tree(src: &Path, dst: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    for e in std::fs::read_dir(src)?.flatten() {
        let t = dst.join(e.file_name());
        if e.file_type()?.is_dir() {
            copy_tree(&e.path(), &t)?;
        } else {
            std::fs::copy(e.path(), t)?;
        }
    }
    Ok(())
}

fn copy_user_data(src: &Path, dst: &Path, overwrite: bool) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    for n in USER_DATA {
        let (s, d) = (src.join(n), dst.join(n));
        if !s.exists() || (d.exists() && !overwrite) {
            continue;
        }
        if s.is_dir() {
            copy_tree(&s, &d)?;
        } else {
            std::fs::copy(&s, &d)?;
        }
    }
    Ok(())
}

/// Stop and remove the service, the browser extension policies and the SELinux labels of old copies.
fn remove_system_parts(old: &[PathBuf]) -> bool {
    let selinux: String = old
        .iter()
        .map(|d| format!("semanage fcontext -d {} 2>/dev/null || true;", shquote(&format!("{}/venv/bin(/.*)?", d.display()))))
        .collect::<Vec<_>>()
        .join(" ");
    let script = format!(
        "systemctl disable --now {} 2>/dev/null || true; rm -f {} {}; if command -v semanage >/dev/null 2>&1; then {} fi; systemctl daemon-reload",
        SERVICE_NAME,
        UNIT_PATH,
        POLICY_FILES.join(" "),
        if selinux.is_empty() { "true;".to_string() } else { selinux }
    );
    let r = run(&["sudo", "-n", "bash", "-c", &script], Some(60));
    if r.code != 0 {
        log::warn!("Removing the old AutoGlow service failed: {}", r.stderr.trim());
    }
    r.code == 0
}

/// Hand-started copies would keep port 8080 busy for the new service.
fn stop_processes(old: &[PathBuf]) {
    let targets: HashSet<PathBuf> = old.iter().map(|d| canon(d)).collect();
    let mut pids: Vec<i32> = autoglow_processes()
        .into_iter()
        .filter(|(_, f)| targets.contains(&canon(f)))
        .map(|(p, _)| p)
        .filter(|p| unsafe { libc::kill(*p, libc::SIGTERM) } == 0)
        .collect();
    let end = Instant::now() + Duration::from_secs(5);
    while !pids.is_empty() && Instant::now() < end {
        pids.retain(|p| Path::new(&format!("/proc/{}", p)).exists());
        std::thread::sleep(Duration::from_millis(200));
    }
    for p in pids {
        unsafe { libc::kill(p, libc::SIGKILL) };
    }
}

fn remove_folder(p: &Path) {
    // Guard against ever deleting anything but an AutoGlow 2 folder
    let c = canon(p);
    if !is_autoglow2_dir(p) || c == canon(&home()) || c == Path::new("/") || c.components().count() < 3 {
        log::warn!("Refusing to delete {}", p.display());
        return;
    }
    if let Err(e) = std::fs::remove_dir_all(p) {
        if e.kind() == std::io::ErrorKind::PermissionDenied {
            // Files created by an older setup run as root
            run(&["sudo", "-n", "rm", "-rf", "--", &p.to_string_lossy()], Some(60));
        } else {
            log::error!("Failed deleting {}: {}", p.display(), e);
        }
    }
    if p.exists() {
        log::warn!("Could not fully delete {}", p.display());
    } else {
        log::info!("Removed older AutoGlow 2 copy {}", p.display());
    }
}

/// Back up and carry over the user data of the older copies, then remove them completely.
fn replace_old_installs(old: &[PathBuf], target: &Path, has_own_data: bool) -> (bool, String) {
    // The copy the service ran is the one in use; otherwise the most recently saved settings win.
    let ud = unit_dir().map(|u| canon(&u));
    let mut primary = old.iter().find(|d| Some(canon(d)) == ud).cloned();
    if primary.is_none() && !(has_own_data && target.join("config.json").exists()) {
        primary = old
            .iter()
            .filter(|d| d.join("config.json").is_file())
            .max_by_key(|d| std::fs::metadata(d.join("config.json")).and_then(|m| m.modified()).ok())
            .cloned();
    }
    let backup = backup_root().join(chrono::Local::now().format("%Y%m%d-%H%M%S").to_string());
    let mut sources: Vec<PathBuf> = old.to_vec();
    if has_own_data && primary.is_some() {
        sources.push(target.to_path_buf());
    }
    for (i, d) in sources.iter().enumerate() {
        let dst = backup.join(format!("{}-{}", i + 1, d.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default()));
        if let Err(e) = copy_user_data(d, &dst, true).and_then(|_| std::fs::write(dst.join("ORIGIN.txt"), format!("{}\n", d.display()))) {
            log::error!("Failed backing up AutoGlow user data: {}", e);
            return (false, trf("Could not back up the older AutoGlow profiles: {error}", &[("error", &e)]));
        }
    }
    log::info!("Backed up AutoGlow user data to {}", backup.display());
    let ok = remove_system_parts(old);
    stop_processes(old);
    if !ok {
        return (false, "Failed removing the AutoGlow service.".into());
    }
    let r = (|| -> std::io::Result<()> {
        if let Some(p) = &primary {
            copy_user_data(p, target, true)?;
        }
        // Profiles that exist only in the other copies: AutoGlow imports presets/*.json on start
        for d in old {
            if Some(d) != primary.as_ref() && d.join("presets").is_dir() {
                std::fs::create_dir_all(target.join("presets"))?;
                for f in std::fs::read_dir(d.join("presets"))?.flatten() {
                    let n = f.file_name();
                    if n.to_string_lossy().ends_with(".json") && !target.join("presets").join(&n).exists() {
                        std::fs::copy(f.path(), target.join("presets").join(&n))?;
                    }
                }
            }
        }
        Ok(())
    })();
    if let Err(e) = r {
        log::error!("Failed carrying over AutoGlow user data: {}", e);
        return (false, trf("Could not carry over the older AutoGlow profiles: {error}", &[("error", &e)]));
    }
    for d in old {
        if has_local_work(d) {
            log::warn!("Keeping {}: it has git changes not on its remote", d.display());
            continue;
        }
        remove_folder(d);
    }
    (true, String::new())
}

/// Install (or repair) AutoGlow 2 in the default folder; older copies are removed with their data carried over.
pub fn install() -> (bool, String) {
    let inst = get_install_dir();
    let old = find_old_installs();
    let dd = default_dir();
    if inst.is_none() && dd.exists() && std::fs::read_dir(&dd).map(|mut r| r.next().is_some()).unwrap_or(false) {
        if !old.iter().any(|o| canon(o) == canon(&dd)) {
            return (false, trf("{path} already exists and is not an AutoGlow 2 folder.", &[("path", &dd.display())]));
        }
        if has_local_work(&dd) {
            return (false, trf("{path} has changes that are not on GitHub. Move the folder and try again.", &[("path", &dd.display())]));
        }
    }
    let target = match &inst {
        Some(d) => d.clone(),
        None => {
            let (ok, msg) = clone(&staging_dir());
            if !ok {
                return (ok, msg);
            }
            staging_dir()
        }
    };
    if !old.is_empty() {
        let (ok, msg) = replace_old_installs(&old, &target, inst.is_some());
        if !ok {
            return (ok, msg);
        }
    }
    if inst.as_ref().is_some_and(|d| is_release_dir(d)) {
        let (ok, mut msg) = run_installer(&target);
        if ok && !old.is_empty() {
            msg = tr("AutoGlow 2 is installed. The older version was removed, your profiles were kept.");
        }
        return (ok, msg);
    }
    if target == staging_dir() {
        if let Err(e) = std::fs::rename(staging_dir(), &dd) {
            return (false, trf("AutoGlow setup failed: {error}", &[("error", &e)]));
        }
    }
    let (ok, mut msg) = run_setup(&dd);
    if ok && !old.is_empty() {
        msg = tr("AutoGlow 2 is installed. The older version was removed, your profiles were kept.");
    }
    (ok, msg)
}

/// AutoGlow's own installer in a one-line install: downloads the latest release into the folder and restarts
/// the service. Settings, flows and profiles are kept.
fn run_installer(dir: &Path) -> (bool, String) {
    let installer = dir.join("install.sh");
    let r = run_in(
        dir,
        &["sudo", "-n", "env", &format!("AUTOGLOW_DIR={}", dir.display()), "bash", &installer.to_string_lossy()],
        Some(SETUP_TIMEOUT),
    );
    if r.timed_out {
        return (false, "AutoGlow setup timed out.".into());
    }
    if r.code != 0 {
        let out = format!("{}\n{}", r.stdout, r.stderr);
        let tail = out.lines().map(str::trim).filter(|l| !l.is_empty()).last().unwrap_or("unknown error").to_string();
        log::warn!("AutoGlow install.sh failed: {}", out.trim());
        return (false, trf("AutoGlow setup failed: {error}", &[("error", &tail)]));
    }
    (true, "AutoGlow 2 is installed and running.".into())
}

/// "0.33.1" -> (0, 33, 1)
fn version_key(v: &str) -> Vec<u64> {
    v.trim().trim_start_matches('v').split('.').map(|p| p.parse().unwrap_or(0)).collect()
}

/// A one-line install: is there a newer GitHub release?
fn check_release_update(dir: &Path) -> super::update::UpdateInfo {
    let current = get_version(dir);
    let Some(tag) = super::http::get_json(RELEASES_API, 15.0).and_then(|j| j["tag_name"].as_str().map(String::from)) else {
        return super::update::UpdateInfo { error: Some("Could not reach GitHub to check for updates.".into()), ..Default::default() };
    };
    let latest = tag.trim_start_matches('v').to_string();
    let has = !current.is_empty() && version_key(&latest) > version_key(&current);
    super::update::UpdateInfo {
        has_update: has,
        commits_behind: has as u32,
        latest_message: if has { format!("v{}", latest) } else { String::new() },
        current_commit: current,
        remote_commit: latest,
        error: None,
    }
}

pub fn check_update() -> super::update::UpdateInfo {
    if let Some(d) = get_install_dir().filter(|d| is_release_dir(d)) {
        return check_release_update(&d);
    }
    match get_install_dir().filter(|d| d.join(".git").exists()) {
        Some(d) => super::update::check_update(Some(&d)),
        None => super::update::UpdateInfo {
            error: Some("AutoGlow was not installed with git, so it cannot update here.".into()),
            ..Default::default()
        },
    }
}

pub fn update() -> (bool, String) {
    if let Some(d) = get_install_dir().filter(|d| is_release_dir(d)) {
        let (ok, msg) = run_installer(&d);
        if !ok {
            return (ok, msg);
        }
        let v = get_version(&d);
        return (true, if v.is_empty() { tr("AutoGlow 2 updated to the latest version.") } else { trf("AutoGlow 2 updated to {version}.", &[("version", &v)]) });
    }
    let Some(dir) = get_install_dir().filter(|d| d.join(".git").exists()) else {
        return (false, "AutoGlow was not installed with git, so it cannot update here.".into());
    };
    // Coming from 2.1: keep the user's flows, then drop the local edits that block the pull
    let mut flows = None;
    for rel in LEGACY_TRACKED_FILES {
        let tracked = run_in(&dir, &["git", "ls-files", "--error-unmatch", rel], Some(15)).code == 0;
        if tracked && run_in(&dir, &["git", "diff", "--quiet", "--", rel], Some(15)).code != 0 {
            if rel == "game_flows.json" {
                flows = std::fs::read(dir.join(rel)).ok();
            }
            run_in(&dir, &["git", "checkout", "--", rel], Some(15));
        }
    }
    let r = run_in(&dir, &["git", "pull", "--ff-only", "origin", "main"], Some(120));
    // presets/ is regenerated from config.json, only the flows are restored
    if let Some(f) = flows {
        let _ = std::fs::write(dir.join("game_flows.json"), f);
    }
    if r.code != 0 {
        log::warn!("AutoGlow git pull failed: {}", r.stderr.trim());
        return (false, "Update download failed. Local changes in the AutoGlow folder may block it.".into());
    }
    let (ok, msg) = run_setup(&dir);
    if !ok {
        return (ok, msg);
    }
    let v = get_version(&dir);
    if v.is_empty() {
        (true, tr("AutoGlow 2 updated to the latest version."))
    } else {
        (true, trf("AutoGlow 2 updated to {version}.", &[("version", &v)]))
    }
}

pub fn start() -> bool {
    systemd::reset_failed(SERVICE_NAME, Scope::System);
    systemd::start_unit_scoped(SERVICE_NAME, Scope::System)
}

pub fn stop() -> bool {
    systemd::stop_unit_scoped(SERVICE_NAME, Scope::System)
}

pub fn restart() -> bool {
    systemd::reset_failed(SERVICE_NAME, Scope::System);
    systemd::restart_unit_scoped(SERVICE_NAME, Scope::System)
}

pub fn set_enabled(enabled: bool) -> bool {
    let a = if enabled { "enable" } else { "disable" };
    let r = run(&["sudo", "-n", "systemctl", a, SERVICE_NAME], Some(10));
    if r.code != 0 {
        log::warn!("systemctl {} autoglow failed: {}", a, r.stderr.trim());
    }
    r.code == 0
}

/// SUIT's clone in ~/Autoglow-2, or the one-line installer's ~/autoglow: folders that only hold AutoGlow
pub fn is_own_folder(d: &Path) -> bool {
    d == default_dir() || (d == release_default_dir() && is_release_dir(d))
}

pub fn uninstall(remove_files: bool) -> (bool, String) {
    let inst = get_install_dir();
    if !remove_system_parts(&inst.iter().cloned().collect::<Vec<_>>()) {
        return (false, "Failed removing the AutoGlow service.".into());
    }
    // Only a folder SUIT cloned itself, or the one-line installer's ~/autoglow, is deleted, never a copy the user
    // set up elsewhere
    if let Some(d) = inst.filter(|d| remove_files && is_own_folder(d)) {
        if let Err(e) = std::fs::remove_dir_all(&d) {
            return (false, trf("Uninstall failed: {error}", &[("error", &e)]));
        }
        return (true, "AutoGlow 2 removed.".into());
    }
    (true, "AutoGlow 2 service removed, its folder was kept.".into())
}

