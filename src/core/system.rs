//! Composite system operations of the System page (the Python SystemService facade's own logic; the rest of
//! that facade is the individual service modules).
use super::i18n::{tr, trf};
use super::{debloat, desktop, keyring, power};

/// All system optimizations, theme, optional touch tweaks, bloatware removal and Chromium, one after the
/// other; a failing step does not stop the next ones. step(idx, total, title, detail) reports progress.
pub fn apply_all_system_and_apps(user: &str, include_touch: bool, step: &(dyn Fn(usize, usize, &str, &str) + Sync)) -> (bool, String) {
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
    let mut failed: Vec<String> = Vec::new();
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
        log::info!("Apply All, {}: ok={} {}", title, success, message);
        if !success {
            failed.push(tr(title));
        }
    }
    if failed.is_empty() {
        (true, "All system configurations applied successfully.".to_string())
    } else {
        (false, trf("Completed with warnings on: {steps}.", &[("steps", &failed.join(", "))]))
    }
}
