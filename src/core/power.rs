use super::util::run;
use std::path::Path;

pub const DEFAULT_GDM_CONFIG: &str = "/etc/gdm/custom.conf";

#[derive(Debug, Clone, Default)]
pub struct Step1Status {
    pub profile_ok: bool,
    pub power_ok: bool,
    pub autologin_ok: bool,
    pub all_ok: bool,
    pub user: String,
}

pub fn set_screen_blanking(disable_blanking: bool) -> bool {
    set_power_and_sleep(disable_blanking)
}

pub fn is_blanking_disabled() -> bool {
    is_power_and_sleep_optimized()
}

pub fn is_performance_profile_active(profile: &str) -> bool {
    run(&["tuned-adm", "active"], Some(10)).stdout.contains(profile)
}

pub fn set_performance_profile(profile: &str) -> bool {
    let r = run(&["sudo", "-n", "tuned-adm", "profile", profile], Some(60));
    if r.ok() {
        log::info!("Set tuned-adm profile to {}", profile);
        true
    } else {
        log::error!("Failed setting tuned-adm profile to {}: {}", profile, r.stderr.trim());
        false
    }
}

fn gget(schema: &str, key: &str) -> String {
    run(&["gsettings", "get", schema, key], Some(5)).stdout
}

pub fn is_power_and_sleep_optimized() -> bool {
    let idle = gget("org.gnome.desktop.session", "idle-delay");
    if !(idle.contains("uint32 0") || idle.trim() == "0") {
        return false;
    }
    if !gget("org.gnome.settings-daemon.plugins.power", "idle-dim").to_lowercase().contains("false") {
        return false;
    }
    gget("org.gnome.settings-daemon.plugins.power", "sleep-inactive-ac-type")
        .to_lowercase()
        .contains("nothing")
}

pub fn set_power_and_sleep(disable_sleep: bool) -> bool {
    let p = "org.gnome.settings-daemon.plugins.power";
    let sets: Vec<(&str, &str, &str)> = if disable_sleep {
        vec![
            ("org.gnome.desktop.session", "idle-delay", "0"),
            (p, "idle-dim", "false"),
            (p, "sleep-inactive-ac-type", "'nothing'"),
            (p, "sleep-inactive-battery-type", "'nothing'"),
            (p, "power-button-action", "'interactive'"),
        ]
    } else {
        vec![
            ("org.gnome.desktop.session", "idle-delay", "900"),
            (p, "idle-dim", "true"),
            (p, "sleep-inactive-ac-type", "'suspend'"),
            (p, "sleep-inactive-battery-type", "'suspend'"),
        ]
    };
    for (s, k, v) in sets {
        let r = run(&["gsettings", "set", s, k, v], Some(10));
        if !r.ok() {
            log::error!("Failed configuring power and sleep settings: {} {} -> {}", s, k, r.stderr.trim());
            return false;
        }
    }
    if disable_sleep {
        run(&["sudo", "-n", "rm", "-f", "/etc/systemd/logind.conf.d/99-suit-power.conf"], Some(10));
    }
    log::info!("Configured power and sleep settings: disable_sleep={}", disable_sleep);
    true
}

pub fn generate_gdm_config(config_path: &Path, enabled: bool, user: &str) -> String {
    let content = std::fs::read_to_string(config_path).unwrap_or_else(|_| "[daemon]\n".into());
    let mut lines: Vec<String> = content
        .lines()
        .filter(|l| {
            let t = l.trim();
            !t.starts_with("AutomaticLoginEnable=") && !t.starts_with("AutomaticLogin=")
        })
        .map(String::from)
        .collect();
    let insert: Vec<String> = if enabled {
        vec!["AutomaticLoginEnable=True".into(), format!("AutomaticLogin={}", user)]
    } else {
        vec!["AutomaticLoginEnable=False".into()]
    };
    if let Some(idx) = lines.iter().position(|l| l.trim() == "[daemon]") {
        for (i, l) in insert.into_iter().enumerate() {
            lines.insert(idx + 1 + i, l);
        }
    } else {
        let mut new = vec!["[daemon]".to_string()];
        new.extend(insert);
        new.push(String::new());
        new.extend(lines);
        lines = new;
    }
    lines.join("\n") + "\n"
}

pub fn get_step1_status(user: &str) -> Step1Status {
    let profile_ok = is_performance_profile_active("throughput-performance");
    let power_ok = is_power_and_sleep_optimized();
    let (enabled, active_user) = is_auto_login_enabled(None);
    let autologin_ok = enabled && (user.is_empty() || active_user == user);
    Step1Status {
        profile_ok,
        power_ok,
        autologin_ok,
        all_ok: profile_ok && power_ok && autologin_ok,
        user: if active_user.is_empty() { user.to_string() } else { active_user },
    }
}

pub fn apply_all_system_energy_and_login(user: &str) -> bool {
    let ok1 = set_performance_profile("throughput-performance");
    let ok2 = set_power_and_sleep(true);
    let ok3 = set_auto_login(true, user, None);
    ok1 && ok2 && ok3
}

pub fn set_auto_login(enabled: bool, user: &str, config_path: Option<&Path>) -> bool {
    let target = config_path.unwrap_or(Path::new(DEFAULT_GDM_CONFIG));
    let content = generate_gdm_config(target, enabled, user);
    if super::util::sudo_write(&target.to_string_lossy(), &content) {
        log::info!("Updated GDM auto-login to enabled={} for user {}", enabled, user);
        true
    } else {
        log::error!("Failed configuring GDM auto-login");
        false
    }
}

pub fn is_auto_login_enabled(config_path: Option<&Path>) -> (bool, String) {
    let target = config_path.unwrap_or(Path::new(DEFAULT_GDM_CONFIG));
    let Ok(content) = std::fs::read_to_string(target) else { return (false, String::new()) };
    let en = regex::Regex::new(r"AutomaticLoginEnable\s*=\s*(True|true|1)").unwrap();
    let us = regex::Regex::new(r"AutomaticLogin\s*=\s*(\S+)").unwrap();
    let user = us.captures(&content).map(|c| c[1].trim().to_string()).unwrap_or_default();
    (en.is_match(&content), user)
}
