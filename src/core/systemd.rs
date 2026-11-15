//! systemd unit control over D-Bus with a systemctl fallback.
use super::util::run;
use gtk::gio;
use gtk::gio::prelude::*;
use gtk::glib;
use std::sync::atomic::{AtomicBool, Ordering};

#[derive(Debug, Clone, PartialEq)]
pub struct UnitStatus {
    pub active_state: String,
    pub sub_state: String,
    pub unit_file_state: String,
}

impl Default for UnitStatus {
    fn default() -> Self {
        Self { active_state: "nofile".into(), sub_state: "dead".into(), unit_file_state: "missing".into() }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Scope {
    Auto,
    User,
    System,
}

fn bus(scope: Scope) -> Option<gio::DBusConnection> {
    let t = if scope == Scope::User { gio::BusType::Session } else { gio::BusType::System };
    gio::bus_get_sync(t, gio::Cancellable::NONE).ok()
}

fn manager(scope: Scope) -> Option<gio::DBusProxy> {
    let b = bus(scope)?;
    match gio::DBusProxy::new_sync(
        &b,
        gio::DBusProxyFlags::NONE,
        None,
        Some("org.freedesktop.systemd1"),
        "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager",
        gio::Cancellable::NONE,
    ) {
        Ok(p) => Some(p),
        Err(e) => {
            log::error!("Failed to connect to systemd DBus Manager on {:?} bus: {}", scope, e);
            None
        }
    }
}

pub fn get_status(unit: &str, scope: Scope) -> UnitStatus {
    if scope == Scope::Auto {
        let u = status_scoped(unit, Scope::User);
        if u.active_state != "nofile" {
            return u;
        }
        return status_scoped(unit, Scope::System);
    }
    status_scoped(unit, scope)
}

fn call_path(proxy: &gio::DBusProxy, method: &str, unit: &str) -> Option<String> {
    let v = proxy
        .call_sync(method, Some(&(unit,).to_variant()), gio::DBusCallFlags::NONE, 1000, gio::Cancellable::NONE)
        .ok()?;
    let (p,): (glib::variant::ObjectPath,) = v.get()?;
    Some(p.as_str().to_string())
}

fn status_scoped(unit: &str, scope: Scope) -> UnitStatus {
    let mut result = UnitStatus::default();
    let Some(proxy) = manager(scope) else { return result };
    let Some(path) = call_path(&proxy, "GetUnit", unit).or_else(|| call_path(&proxy, "LoadUnit", unit)) else {
        return result;
    };
    let Some(b) = bus(scope) else { return result };
    let up = match gio::DBusProxy::new_sync(
        &b,
        gio::DBusProxyFlags::NONE,
        None,
        Some("org.freedesktop.systemd1"),
        &path,
        "org.freedesktop.systemd1.Unit",
        gio::Cancellable::NONE,
    ) {
        Ok(p) => p,
        Err(e) => {
            log::error!("Failed querying properties for {} on {:?} bus: {}", unit, scope, e);
            return result;
        }
    };
    let prop = |n: &str| up.cached_property(n).and_then(|v| v.get::<String>());
    let ls = prop("LoadState").unwrap_or_else(|| "not-found".into());
    if ls != "not-found" {
        result.active_state = prop("ActiveState").unwrap_or_else(|| "inactive".into());
        result.sub_state = prop("SubState").unwrap_or_else(|| "dead".into());
        result.unit_file_state = prop("UnitFileState").unwrap_or_else(|| "unknown".into());
    }
    result
}

fn resolve_scope(unit: &str, scope: Scope) -> Scope {
    if scope != Scope::Auto {
        return scope;
    }
    if status_scoped(unit, Scope::User).active_state != "nofile" {
        Scope::User
    } else {
        Scope::System
    }
}

/// Reset failed state of a unit to unblock rate-limiting or previous crashes.
pub fn reset_failed(unit: &str, scope: Scope) -> bool {
    let target = resolve_scope(unit, scope);
    if let Some(p) = manager(target) {
        if p.call_sync(
            "ResetFailedUnit",
            Some(&(unit,).to_variant()),
            gio::DBusCallFlags::ALLOW_INTERACTIVE_AUTHORIZATION,
            1500,
            gio::Cancellable::NONE,
        )
        .is_ok()
        {
            log::info!("Reset failed state for {} via {:?} DBus", unit, target);
            return true;
        }
    }
    let cmd: Vec<&str> = if target == Scope::User {
        vec!["systemctl", "--user", "reset-failed", unit]
    } else {
        vec!["sudo", "systemctl", "reset-failed", unit]
    };
    run(&cmd, Some(5));
    true
}

fn unit_action(action: &str, unit: &str, mode: &str, scope: Scope) -> bool {
    let target = resolve_scope(unit, scope);
    if action == "start" || action == "restart" {
        reset_failed(unit, target);
    }
    let method = match action {
        "start" => Some("StartUnit"),
        "stop" => Some("StopUnit"),
        "restart" => Some("RestartUnit"),
        _ => None,
    };
    if let (Some(p), Some(m)) = (manager(target), method) {
        match p.call_sync(
            m,
            Some(&(unit, mode).to_variant()),
            gio::DBusCallFlags::ALLOW_INTERACTIVE_AUTHORIZATION,
            3000,
            gio::Cancellable::NONE,
        ) {
            Ok(_) => {
                log::info!("Successfully executed {} for {} via {:?} DBus", action, unit, target);
                return true;
            }
            Err(e) => log::warn!("DBus {} failed for {} ({:?}): {}. Trying CLI fallback...", action, unit, target, e),
        }
    }
    let cmd: Vec<&str> = if target == Scope::User {
        vec!["systemctl", "--user", action, unit]
    } else {
        vec!["sudo", "systemctl", action, unit]
    };
    let r = run(&cmd, Some(10));
    if r.ok() {
        log::info!("Successfully executed {} for {} via {:?} systemctl", action, unit, target);
        true
    } else {
        log::error!("{:?} systemctl {} {} failed: {}", target, action, unit, r.stderr.trim());
        false
    }
}

pub fn start_unit(unit: &str) -> bool {
    unit_action("start", unit, "replace", Scope::Auto)
}
pub fn stop_unit(unit: &str) -> bool {
    unit_action("stop", unit, "replace", Scope::Auto)
}
pub fn restart_unit(unit: &str) -> bool {
    unit_action("restart", unit, "replace", Scope::Auto)
}
pub fn start_unit_scoped(unit: &str, scope: Scope) -> bool {
    unit_action("start", unit, "replace", scope)
}
pub fn stop_unit_scoped(unit: &str, scope: Scope) -> bool {
    unit_action("stop", unit, "replace", scope)
}
pub fn restart_unit_scoped(unit: &str, scope: Scope) -> bool {
    unit_action("restart", unit, "replace", scope)
}

pub fn is_unit_active(unit: &str, scope: Scope) -> bool {
    get_status(unit, scope).active_state == "active"
}

/// Controls autodarts.service to release camera locks (CAM-librator interface).
pub mod manager {
    use super::*;

    static PAUSED_BY_SUIT: AtomicBool = AtomicBool::new(false);

    pub fn is_autodarts_active() -> bool {
        is_unit_active("autodarts.service", Scope::Auto)
    }

    pub fn stop_autodarts() -> bool {
        log::info!("Stopping autodarts.service to release V4L2 camera locks...");
        stop_unit("autodarts.service")
    }

    pub fn start_autodarts() -> bool {
        log::info!("Restarting autodarts.service...");
        start_unit("autodarts.service")
    }

    /// Pause autodarts.service if active, tracking that SUIT paused it.
    pub fn pause_autodarts(settle: bool) -> bool {
        if is_autodarts_active() {
            PAUSED_BY_SUIT.store(true, Ordering::SeqCst);
            log::info!("Pausing autodarts.service to release camera locks (tracked by SUIT)");
            let stopped = stop_autodarts();
            if stopped && settle {
                // Allow camera drivers and kernel V4L2 buffers to be fully released
                std::thread::sleep(std::time::Duration::from_millis(500));
            }
            return stopped;
        }
        false
    }

    /// Resume autodarts.service if it was previously paused by SUIT.
    pub fn resume_autodarts() -> bool {
        if PAUSED_BY_SUIT.swap(false, Ordering::SeqCst) {
            log::info!("Resuming autodarts.service previously paused by SUIT");
            return start_autodarts();
        }
        false
    }

    pub fn paused_by_suit() -> bool {
        PAUSED_BY_SUIT.load(Ordering::SeqCst)
    }

    /// Process exit safety net: ensure autodarts is started if paused by SUIT.
    pub fn ensure_autodarts_restored_on_exit() {
        if paused_by_suit() {
            log::info!("Emergency exit cleanup: restoring autodarts.service before exit");
            resume_autodarts();
        }
    }
}
