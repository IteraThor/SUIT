//! Composite system operations of the System page (the Python SystemService facade's own logic; the rest of
//! that facade is the individual service modules).
use super::i18n::{tr, trf};
use super::{debloat, desktop, keyring, power};

#[derive(Debug, Clone, Default)]
pub struct RecommendedStatus {
    pub profile_ok: bool,
    pub power_ok: bool,
    pub autologin_ok: bool,
    pub keyring_ok: bool,
    pub visuals_ok: bool,
    pub chromium_ok: bool,
    pub all_ok: bool,
}

pub fn get_recommended_status(user: &str) -> RecommendedStatus {
    let s1 = power::get_step1_status(user);
    let keyring_ok = keyring::is_keyring_blank_or_unlocked(None).0;
    let visuals_ok = desktop::get_visuals_status().all_ok;
    let chromium_ok = debloat::is_app_installed("chromium");
    RecommendedStatus {
        profile_ok: s1.profile_ok,
        power_ok: s1.power_ok,
        autologin_ok: s1.autologin_ok,
        keyring_ok,
        visuals_ok,
        chromium_ok,
        all_ok: s1.profile_ok && s1.power_ok && s1.autologin_ok && keyring_ok && visuals_ok && chromium_ok,
    }
}

pub fn apply_all_recommended(user: &str) -> (bool, String) {
    let mut res = vec![power::apply_all_system_energy_and_login(user)];
    if !keyring::is_keyring_blank_or_unlocked(None).0 {
        res.push(keyring::reset_keyring_to_blank(None).0);
    }
    res.push(desktop::apply_desktop_visuals().0);
    if !debloat::is_app_installed("chromium") {
        res.push(debloat::install_chromium().0);
    }
    if res.iter().all(|r| *r) {
        (true, "All recommended optimizations applied.".into())
    } else {
        (false, "Some optimizations had warnings.".into())
    }
}

#[derive(Debug, Clone)]
pub struct StepResult {
    pub step: String,
    pub success: bool,
    pub message: String,
}

/// All system optimizations, theme, optional touch tweaks, bloatware removal and Chromium, one after the
/// other; a failing step does not stop the next ones. step(idx, total, title, detail) reports progress.
pub fn apply_all_system_and_apps(user: &str, include_touch: bool, step: &(dyn Fn(usize, usize, &str, &str) + Sync)) -> (bool, String, Vec<StepResult>) {
    let mut defs: Vec<(&str, &str)> = vec![
        ("Power Profile", "Locking CPU to maximum performance"),
        ("Display Always On", "Disabling screen blanking and sleep"),
        ("Automatic Login", "Configuring unattended auto-login"),
        ("Keyring Password", "Unlocking GNOME Keyring for passwordless boot"),
        ("Appearance & Theme", "Applying dark theme and organizing app grid"),
    ];
    if include_touch {
        defs.push(("Touchscreen Tweaks", "Enlarging UI elements and touchscreen controls"));
    }
    defs.push(("Remove Unused Apps", "Removing default bloatware packages"));
    defs.push(("Chromium Browser", "Installing Chromium web browser"));
    let total = defs.len();
    let mut results = Vec::new();
    for (i, (title, desc)) in defs.iter().enumerate() {
        let idx = i + 1;
        step(idx, total, title, &tr(desc));
        let (success, message): (bool, String) = match *title {
            "Power Profile" => {
                let ok = power::set_performance_profile("throughput-performance");
                (ok, if ok { "Power profile set to performance." } else { "Failed to set power profile." }.into())
            }
            "Display Always On" => {
                let ok = power::set_screen_blanking(true);
                (ok, if ok { "Display always-on configured." } else { "Failed configuring display sleep." }.into())
            }
            "Automatic Login" => {
                let ok = power::set_auto_login(true, user, None);
                (ok, if ok { "Automatic login enabled." } else { "Failed setting automatic login." }.into())
            }
            "Keyring Password" => {
                if keyring::is_keyring_blank_or_unlocked(None).0 {
                    (true, "Keyring is already unlocked.".into())
                } else {
                    keyring::reset_keyring_to_blank(None)
                }
            }
            "Appearance & Theme" => desktop::apply_desktop_visuals(),
            "Touchscreen Tweaks" => desktop::set_touch_scaling(true, 1.2),
            "Remove Unused Apps" => {
                let b = debloat::get_installed_bloatware(None);
                if b.is_empty() {
                    (true, "No default bloatware detected.".into())
                } else {
                    debloat::debloat_packages(&b, &|_c, _t, d| step(idx, total, title, &d))
                }
            }
            _ => {
                if debloat::is_app_installed("chromium") {
                    (true, "Chromium is already installed.".into())
                } else {
                    step(idx, total, title, &tr("Downloading and installing Chromium via DNF..."));
                    debloat::install_chromium()
                }
            }
        };
        results.push(StepResult { step: title.to_string(), success, message });
    }
    let all = results.iter().all(|r| r.success);
    let summary = if all {
        "All system configurations applied successfully.".to_string()
    } else {
        let failed: Vec<String> = results.iter().filter(|r| !r.success).map(|r| tr(&r.step)).collect();
        trf("Completed with warnings on: {steps}.", &[("steps", &failed.join(", "))])
    };
    (all, summary, results)
}
