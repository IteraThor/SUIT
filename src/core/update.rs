//! Check GitHub for updates, pull them, rebuild the Rust binary and restart SUIT.
use super::i18n::trf;
use super::util::{has_cmd, project_dir, run, run_in, run_shell, self_exe};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Default)]
pub struct UpdateInfo {
    pub has_update: bool,
    pub current_commit: String,
    pub remote_commit: String,
    pub commits_behind: u32,
    pub latest_message: String,
    pub error: Option<String>,
}

/// Branch the checkout follows (the Rust port keeps its own remote/branch).
fn branch(dir: &Path) -> String {
    let r = run_in(dir, &["git", "rev-parse", "--abbrev-ref", "HEAD"], Some(5));
    let b = r.stdout.trim();
    if r.ok() && !b.is_empty() && b != "HEAD" { b.to_string() } else { "main".into() }
}

fn err(msg: impl Into<String>) -> UpdateInfo {
    UpdateInfo { error: Some(msg.into()), ..Default::default() }
}

pub fn check_update(project: Option<&Path>) -> UpdateInfo {
    let dir: PathBuf = project.map(|p| p.to_path_buf()).unwrap_or_else(project_dir);
    if !dir.join(".git").exists() {
        return err("Git repository not found. SUIT must be installed via git to update.");
    }
    let br = branch(&dir);
    let remote_ref = format!("origin/{}", br);
    let f = run_in(&dir, &["git", "fetch", "origin", &br], Some(8));
    if f.timed_out {
        return err("Check for updates timed out. Check internet connection.");
    }
    if !f.ok() {
        log::debug!("Git fetch failed: {}", f.stderr.trim());
        return err("Unable to connect to GitHub. Check internet connection.");
    }
    let git = |args: &[&str]| -> Result<String, String> {
        let mut a = vec!["git"];
        a.extend_from_slice(args);
        let r = run_in(&dir, &a, Some(10));
        if r.ok() { Ok(r.stdout.trim().to_string()) } else { Err(r.stderr.trim().to_string()) }
    };
    let res = (|| -> Result<UpdateInfo, String> {
        let local = git(&["rev-parse", "--short", "HEAD"])?;
        let remote = git(&["rev-parse", "--short", &remote_ref])?;
        if local == remote {
            return Ok(UpdateInfo { current_commit: local, remote_commit: remote, ..Default::default() });
        }
        let behind: u32 = git(&["rev-list", "--count", &format!("HEAD..{}", remote_ref)])?.parse().unwrap_or(1);
        let msg = git(&["log", "-1", "--pretty=format:%s", &remote_ref])?;
        Ok(UpdateInfo {
            has_update: behind > 0,
            current_commit: local,
            remote_commit: remote,
            commits_behind: behind,
            latest_message: msg,
            error: None,
        })
    })();
    res.unwrap_or_else(|e| {
        log::error!("Error checking for updates: {}", e);
        err(trf("Failed checking updates: {error}", &[("error", &e)]))
    })
}

/// Rebuild the release binary after a pull.
pub fn rebuild(dir: &Path) -> (bool, String) {
    let cargo = if has_cmd("cargo") { "cargo".to_string() } else { super::util::home().join(".cargo/bin/cargo").to_string_lossy().to_string() };
    let r = run_in(dir, &[&cargo, "build", "--release"], Some(1800));
    if r.ok() {
        (true, String::new())
    } else {
        let tail: Vec<&str> = r.stderr.lines().rev().take(8).collect();
        (false, tail.into_iter().rev().collect::<Vec<_>>().join("\n"))
    }
}

pub fn apply_update(project: Option<&Path>) -> (bool, String) {
    let dir: PathBuf = project.map(|p| p.to_path_buf()).unwrap_or_else(project_dir);
    let br = branch(&dir);
    let f = run_in(&dir, &["git", "fetch", "origin", &br], Some(15));
    if f.timed_out {
        return (false, "Update timed out. Please check internet connection.".into());
    }
    if !f.ok() {
        let e = if f.stderr.trim().is_empty() { "Network error".to_string() } else { f.stderr.trim().to_string() };
        return (false, trf("Failed fetching updates: {error}", &[("error", &e)]));
    }
    // Fast-forward pull with hard reset fallback for rewritten history
    let p = run_in(&dir, &["git", "pull", "--ff-only", "origin", &br], Some(15));
    if !p.ok() {
        log::warn!("Fast-forward pull failed, attempting hard reset: {}", p.stderr.trim());
        let r = run_in(&dir, &["git", "reset", "--hard", &format!("origin/{}", br)], Some(15));
        if !r.ok() {
            log::warn!("Hard reset failed: {}", r.stderr.trim());
            return (false, trf("Unable to update: {error}", &[("error", &r.stderr.trim())]));
        }
    }
    let (built, e) = rebuild(&dir);
    if !built {
        return (false, trf("Update failed: {error}", &[("error", &e)]));
    }
    log::info!("Successfully updated SUIT");
    ensure_dependencies();
    ensure_autodarts_service();
    ensure_camera_keeper();
    ensure_display_autostart();
    (true, "SUIT updated successfully.".into())
}

/// Migrate old camera watchers; keep the camera keeper unit on this checkout.
pub fn ensure_camera_keeper() {
    super::hbvcal::service::heal();
}

pub fn ensure_display_autostart() {
    super::display::ensure_autostart();
}

/// Pre-install the switchable SUIT shell extensions (disabled) so GNOME Shell discovers them at the next login.
pub fn ensure_shell_extensions_installed() {
    if let Err(e) = super::desktop::install_suit_extensions() {
        log::warn!("Shell extension pre-install notice: {}", e);
    }
}

/// Ensure Autodarts systemd service is provisioned and enabled if Autodarts is installed.
pub fn ensure_autodarts_service() {
    use super::autodarts as ad;
    if ad::is_autodarts_installed() {
        ad::ensure_service_unit();
        if !ad::is_service_enabled() {
            log::info!("Post-update: enabling autodarts.service for boot startup");
            ad::set_service_enabled(true);
        }
    }
}

/// Essential runtime tools; return the DNF packages that are missing.
pub fn check_requirements() -> Vec<String> {
    let mut missing = Vec::new();
    if !has_cmd("v4l2-ctl") {
        missing.push("v4l-utils".to_string());
    }
    if !Path::new("/usr/lib64/libopencv_core.so").exists() && run_shell("ldconfig -p | grep -q libopencv_core", Some(5)).code != 0 {
        missing.push("opencv".to_string());
    }
    if !has_cmd("git") {
        missing.push("git".to_string());
    }
    missing
}

pub fn install_requirements(packages: Option<Vec<String>>) -> (bool, String) {
    let to_install = packages.unwrap_or_else(check_requirements);
    if to_install.is_empty() {
        return (true, "All requirements are already installed.".into());
    }
    let res = if super::privileges::check_sudo_privileges() {
        let mut a = vec!["sudo", "dnf", "install", "-y", "--setopt=install_weak_deps=False"];
        a.extend(to_install.iter().map(|s| s.as_str()));
        log::info!("Installing requirements via passwordless sudo: {:?}", a);
        run(&a, Some(180))
    } else {
        let cmd = format!("pkexec dnf install -y --setopt=install_weak_deps=False {}", to_install.join(" "));
        log::info!("Installing requirements via pkexec: {}", cmd);
        run_shell(&cmd, Some(180))
    };
    if res.timed_out {
        return (false, "Installation timed out.".into());
    }
    if res.ok() {
        log::info!("Successfully installed requirement packages: {:?}", to_install);
        (true, "Requirements installed successfully.".into())
    } else {
        let e = if res.stderr.trim().is_empty() { "Installation failed.".to_string() } else { res.stderr.trim().to_string() };
        log::error!("Failed installing requirements: {}", e);
        (false, trf("Installation failed: {error}", &[("error", &e)]))
    }
}

pub fn ensure_dependencies() {
    let m = check_requirements();
    if !m.is_empty() {
        install_requirements(Some(m));
    }
}

/// Replace this process with the (freshly built) binary.
pub fn restart_application() {
    use std::os::unix::process::CommandExt;
    let exe = self_exe();
    let args: Vec<String> = std::env::args().skip(1).collect();
    log::info!("Restarting SUIT process: {} {:?}", exe.display(), args);
    let e = std::process::Command::new(&exe).args(&args).exec();
    log::error!("Failed executing process reload via exec: {}", e);
    if std::process::Command::new(&exe).args(&args).spawn().is_ok() {
        std::process::exit(0);
    }
}
