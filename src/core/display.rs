//! Display rotation through Mutter's DisplayConfig D-Bus API, touch calibration through a udev
//! LIBINPUT_CALIBRATION_MATRIX rule, and the boot autostart that keeps both.
use super::util::{home, project_dir, read_trim, run, run_with_input, self_exe, spawn, write_file};
use gtk::gio;
use gtk::gio::prelude::*;
use gtk::glib::{self, prelude::ToVariant, Variant};
use serde_json::{json, Map, Value};
use std::collections::{BTreeSet, HashMap};
use std::path::{Path, PathBuf};

pub const UDEV_RULE_PATH: &str = "/etc/udev/rules.d/99-suit-touch.rules";

pub fn default_config_path() -> PathBuf {
    home().join(".suit_rotation_config.json")
}
pub fn default_autostart_path() -> PathBuf {
    home().join(".config/autostart/suit-rotation.desktop")
}

pub fn normalize_rotation(rot: &Value) -> i64 {
    match rot {
        Value::String(s) => match s.trim().to_lowercase().as_str() {
            "normal" => 0,
            "right" => 1,
            "inverted" => 2,
            "left" => 3,
            other => other.parse().unwrap_or(0),
        },
        Value::Number(n) => n.as_i64().unwrap_or(0),
        _ => 0,
    }
}

pub fn get_rotation_matrix(rot: i64) -> &'static str {
    match rot {
        1 => "0 -1 1 1 0 0 0 0 1",
        2 => "-1 0 1 0 -1 1 0 0 1",
        3 => "0 1 0 -1 0 1 0 0 1",
        _ => "1 0 0 0 1 0 0 0 1",
    }
}

#[derive(Debug, Clone)]
pub struct Mode {
    pub id: String,
    pub w: i32,
    pub h: i32,
    pub rate: f64,
}

#[derive(Debug, Clone)]
pub struct PhysMonitor {
    pub connector: String,
    pub vendor: String,
    pub product: String,
    pub serial: String,
    pub current: Option<Mode>,
}

#[derive(Debug, Clone)]
pub struct LogicalMonitor {
    pub x: i32,
    pub y: i32,
    pub scale: f64,
    pub trans: u32,
    pub primary: bool,
    pub monitors: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct MutterState {
    pub serial: u32,
    pub monitors: Vec<PhysMonitor>,
    pub logical: Vec<LogicalMonitor>,
}

impl MutterState {
    fn current_mode(&self, name: &str) -> Option<&Mode> {
        self.monitors.iter().find(|m| m.connector == name).and_then(|m| m.current.as_ref())
    }
}

fn parse_state(v: &Variant) -> Option<MutterState> {
    let serial: u32 = v.child_value(0).get()?;
    let mons = v.child_value(1);
    let mut monitors = Vec::new();
    for i in 0..mons.n_children() {
        let m = mons.child_value(i);
        let spec: (String, String, String, String) = m.child_value(0).get()?;
        let modes = m.child_value(1);
        let mut current = None;
        for j in 0..modes.n_children() {
            let md = modes.child_value(j);
            let props = glib::VariantDict::new(Some(&md.child_value(6)));
            if props.contains("is-current") {
                current = Some(Mode {
                    id: md.child_value(0).get()?,
                    w: md.child_value(1).get()?,
                    h: md.child_value(2).get()?,
                    rate: md.child_value(3).get()?,
                });
                break;
            }
        }
        monitors.push(PhysMonitor { connector: spec.0, vendor: spec.1, product: spec.2, serial: spec.3, current });
    }
    let lms = v.child_value(2);
    let mut logical = Vec::new();
    for i in 0..lms.n_children() {
        let l = lms.child_value(i);
        let pm = l.child_value(5);
        let names = (0..pm.n_children())
            .filter_map(|j| pm.child_value(j).child_value(0).get::<String>())
            .collect();
        logical.push(LogicalMonitor {
            x: l.child_value(0).get()?,
            y: l.child_value(1).get()?,
            scale: l.child_value(2).get()?,
            trans: l.child_value(3).get()?,
            primary: l.child_value(4).get()?,
            monitors: names,
        });
    }
    Some(MutterState { serial, monitors, logical })
}

/// Libinput affine calibration matrix for the touch screen on `connector` (Mutter trans 1=90 right,
/// 2=180, 3=270 left), as "c0 c1 c2 c3 c4 c5".
pub fn calculate_affine_matrix(st: &MutterState, connector: &str, target_trans: Option<u32>) -> Option<String> {
    let (mut tw, mut th) = (0, 0);
    let mut target: Option<(i32, i32, i32, i32, u32)> = None;
    for lm in &st.logical {
        for name in &lm.monitors {
            let Some(m) = st.current_mode(name) else { continue };
            let (mut w, mut h) = (m.w, m.h);
            let mut tr = lm.trans;
            if name == connector {
                if let Some(t) = target_trans {
                    tr = t;
                }
            }
            if tr == 1 || tr == 3 {
                std::mem::swap(&mut w, &mut h);
            }
            tw = tw.max(lm.x + w);
            th = th.max(lm.y + h);
            if name == connector {
                target = Some((lm.x, lm.y, w, h, tr));
            }
        }
    }
    let Some((x, y, w, h, tr)) = target.filter(|_| tw > 0 && th > 0) else {
        log::warn!("Could not determine geometry for matrix calculation: {}", connector);
        return None;
    };
    let (wf, hf) = (w as f64 / tw as f64, h as f64 / th as f64);
    let (xf, yf) = (x as f64 / tw as f64, y as f64 / th as f64);
    let m = match tr {
        1 => [0.0, -wf, wf + xf, hf, 0.0, yf],
        2 => [-wf, 0.0, wf + xf, 0.0, -hf, hf + yf],
        3 => [0.0, wf, xf, -hf, 0.0, hf + yf],
        _ => [wf, 0.0, xf, 0.0, hf, yf],
    };
    Some(m.iter().map(|v| format!("{:.6}", v)).collect::<Vec<_>>().join(" "))
}

fn mutter_proxy(retries: u32, delay: f64) -> Option<gio::DBusProxy> {
    for attempt in 0..retries.max(1) {
        let p = gio::bus_get_sync(gio::BusType::Session, gio::Cancellable::NONE).ok().and_then(|bus| {
            gio::DBusProxy::new_sync(
                &bus,
                gio::DBusProxyFlags::NONE,
                None,
                Some("org.gnome.Mutter.DisplayConfig"),
                "/org/gnome/Mutter/DisplayConfig",
                "org.gnome.Mutter.DisplayConfig",
                gio::Cancellable::NONE,
            )
            .ok()
        });
        if p.is_some() {
            return p;
        }
        if attempt + 1 < retries {
            std::thread::sleep(std::time::Duration::from_secs_f64(delay));
        }
    }
    log::debug!("Mutter DisplayConfig DBus interface unavailable");
    None
}

pub fn get_current_mutter_state(retries: u32) -> Option<MutterState> {
    for attempt in 0..retries.max(1) {
        if let Some(p) = mutter_proxy(1, 0.5) {
            match p.call_sync("GetCurrentState", None, gio::DBusCallFlags::NONE, 2000, gio::Cancellable::NONE) {
                Ok(v) => return parse_state(&v),
                Err(e) => log::debug!("Error querying Mutter GetCurrentState: {}", e),
            }
        }
        if attempt + 1 < retries {
            std::thread::sleep(std::time::Duration::from_millis(500));
        }
    }
    None
}

#[derive(Debug, Clone)]
pub struct MonitorView {
    pub name: String,
    pub res: String,
    pub w: i32,
    pub h: i32,
    pub x: i32,
    pub y: i32,
    pub is_primary: bool,
    pub rotation: u32,
}

pub fn get_monitors() -> Vec<MonitorView> {
    let Some(st) = get_current_mutter_state(1) else { return vec![] };
    let mut out = Vec::new();
    for lm in &st.logical {
        for name in &lm.monitors {
            let (w, h, res) = match st.current_mode(name) {
                Some(m) => (m.w, m.h, format!("{}x{}", m.w, m.h)),
                None => (0, 0, "Unknown".into()),
            };
            out.push(MonitorView { name: name.clone(), res, w, h, x: lm.x, y: lm.y, is_primary: lm.primary, rotation: lm.trans });
        }
    }
    out
}

pub fn build_monitors_variant(st: &MutterState, method: u32, connector: &str, rotation: u32) -> Variant {
    let lms: Vec<(i32, i32, f64, u32, bool, Vec<(String, String, HashMap<String, Variant>)>)> = st
        .logical
        .iter()
        .map(|lm| {
            let mut tr = lm.trans;
            let phys = lm
                .monitors
                .iter()
                .map(|name| {
                    if name == connector {
                        tr = rotation;
                    }
                    (name.clone(), st.current_mode(name).map(|m| m.id.clone()).unwrap_or_default(), HashMap::new())
                })
                .collect();
            (lm.x, lm.y, lm.scale, tr, lm.primary, phys)
        })
        .collect();
    (st.serial, method, lms, HashMap::<String, Variant>::new()).to_variant()
}

pub fn apply_rotation(connector: &str, rotation: u32, method: u32) -> bool {
    let Some(p) = mutter_proxy(1, 0.5) else { return false };
    let Some(st) = get_current_mutter_state(1) else { return false };
    let v = build_monitors_variant(&st, method, connector, rotation);
    match p.call_sync("ApplyMonitorsConfig", Some(&v), gio::DBusCallFlags::NONE, 3000, gio::Cancellable::NONE) {
        Ok(_) => {
            log::info!("Applied rotation {} to {} (method {})", rotation, connector, method);
            true
        }
        Err(e) => {
            log::error!("Failed applying rotation to {}: {}", connector, e);
            false
        }
    }
}

fn input_names() -> Vec<(PathBuf, String)> {
    glob::glob("/sys/class/input/input*/name")
        .map(|g| g.flatten().filter_map(|p| read_trim(&p).filter(|s| !s.is_empty()).map(|n| (p, n))).collect())
        .unwrap_or_default()
}

fn norm(s: &str) -> String {
    s.trim().to_lowercase().replace('_', " ")
}

/// Resolve a device name (vendor prefixes, underscores) to the kernel input device name in sysfs.
fn resolve_touch_device_name(name: &str) -> String {
    if name.is_empty() || name == "None" {
        return name.into();
    }
    let t = norm(name);
    for (_, c) in input_names() {
        let cn = norm(&c);
        if t.contains(&cn) || cn.contains(&t) {
            return c;
        }
    }
    name.into()
}

pub fn get_touchscreens() -> Vec<String> {
    let mut set = BTreeSet::new();
    // Fast path: already-parsed /run/udev/data/ entries of event nodes
    for p in glob::glob("/run/udev/data/c13:*").map(|g| g.flatten().collect::<Vec<_>>()).unwrap_or_default() {
        let Ok(content) = std::fs::read_to_string(&p) else { continue };
        if !content.contains("ID_INPUT_TOUCHSCREEN=1") {
            continue;
        }
        for line in content.lines() {
            if line.starts_with("E:ID_MODEL=") || line.starts_with("E:NAME=") {
                let val = line.split_once('=').unwrap().1.trim().trim_matches('"').to_string();
                let vendor = content
                    .lines()
                    .find(|l| l.starts_with("E:ID_VENDOR="))
                    .map(|l| l.split_once('=').unwrap().1.trim().to_string())
                    .unwrap_or_default();
                let full = if vendor.is_empty() { val } else { format!("{} {}", vendor, val).trim().to_string() };
                if !full.is_empty() {
                    set.insert(full);
                }
                break;
            }
        }
    }
    // Fallback: INPUT_PROP_DIRECT (0x2) in sysfs marks physically attached touchscreens
    if set.is_empty() {
        for p in glob::glob("/sys/class/input/event*/device/name").map(|g| g.flatten().collect::<Vec<_>>()).unwrap_or_default() {
            let props = p.with_file_name("properties");
            let Some(v) = read_trim(&props).and_then(|s| u64::from_str_radix(&s, 16).ok()) else { continue };
            if v & 0x2 != 0 {
                if let Some(n) = read_trim(&p).filter(|s| !s.is_empty()) {
                    set.insert(n);
                }
            }
        }
    }
    set.into_iter().collect()
}

pub fn write_udev_rule(touch: Option<&str>, matrix: Option<&str>) -> bool {
    let ok = |a: &[&str]| run(a, Some(20)).ok();
    match (touch.filter(|t| !t.is_empty() && *t != "None"), matrix) {
        (Some(t), Some(m)) => {
            let resolved = resolve_touch_device_name(t);
            let rule = format!(
                "ACTION==\"add|change\", KERNEL==\"event*\", ATTRS{{name}}==\"{}\", ENV{{LIBINPUT_CALIBRATION_MATRIX}}=\"{}\"\n",
                resolved, m
            );
            let r = run_with_input(&["sudo", "-n", "tee", UDEV_RULE_PATH], &rule, Some(20)).ok()
                && ok(&["sudo", "-n", "udevadm", "control", "--reload-rules"])
                && ok(&["sudo", "-n", "udevadm", "trigger", "--subsystem-match=input"]);
            if r {
                log::info!("Wrote touch calibration rule for {}: {}", resolved, m);
            } else {
                log::error!("Failed writing udev touch rule");
            }
            r
        }
        _ => {
            let r = ok(&["sudo", "-n", "rm", "-f", UDEV_RULE_PATH])
                && ok(&["sudo", "-n", "udevadm", "control", "--reload-rules"])
                // re-evaluate so stale LIBINPUT_CALIBRATION_MATRIX entries are cleared for connected devices
                && ok(&["sudo", "-n", "udevadm", "trigger", "--subsystem-match=input"]);
            if r {
                log::info!("Cleared touch calibration udev rule");
            } else {
                log::error!("Failed clearing udev touch rule");
            }
            r
        }
    }
}

/// Force libinput to re-read LIBINPUT_CALIBRATION_MATRIX by a USB unbind/bind (it only reads it on open).
/// Call this AFTER the Mutter rotation so the display transition completes first.
pub fn rebind_usb_touch(name: &str) -> bool {
    if name.is_empty() || name == "None" {
        return false;
    }
    let t = norm(&resolve_touch_device_name(name));
    let mut bus_id = None;
    for (p, n) in input_names() {
        let nn = norm(&n);
        if t.contains(&nn) || nn.contains(&t) {
            let mut cur = std::fs::canonicalize(&p).ok().and_then(|r| r.parent().map(|x| x.to_path_buf()));
            while let Some(c) = cur {
                if c == Path::new("/") {
                    break;
                }
                if c.join("idVendor").exists() && c.join("busnum").exists() {
                    bus_id = c.file_name().map(|f| f.to_string_lossy().to_string());
                    break;
                }
                cur = c.parent().map(|x| x.to_path_buf());
            }
            if bus_id.is_some() {
                break;
            }
        }
    }
    let Some(b) = bus_id else {
        log::debug!("Could not locate USB bus ID for touch device: {}", name);
        return false;
    };
    log::info!("Rebinding USB touch device {} to apply calibration matrix", b);
    let line = format!("{}\n", b);
    if !run_with_input(&["sudo", "-n", "tee", "/sys/bus/usb/drivers/usb/unbind"], &line, Some(10)).ok() {
        return false;
    }
    std::thread::sleep(std::time::Duration::from_secs(1));
    run_with_input(&["sudo", "-n", "tee", "/sys/bus/usb/drivers/usb/bind"], &line, Some(10)).ok()
}

pub fn apply_touch_calibration(touch: &str, connector: &str, rotation: i64, prev_touch: Option<&str>) -> bool {
    if touch.is_empty() || touch == "None" || rotation == 0 {
        write_udev_rule(None, None);
        let dev = if !touch.is_empty() && touch != "None" { Some(touch) } else { prev_touch };
        if let Some(d) = dev.filter(|d| !d.is_empty() && *d != "None") {
            rebind_usb_touch(d);
        }
        return true;
    }
    let Some(st) = get_current_mutter_state(1) else {
        log::warn!("Cannot calculate touch matrix without active Mutter state");
        return false;
    };
    let Some(m) = calculate_affine_matrix(&st, connector, Some(rotation as u32)) else { return false };
    let ok = write_udev_rule(Some(touch), Some(&m));
    if ok {
        rebind_usb_touch(touch);
    }
    ok
}

pub fn load_config() -> Map<String, Value> {
    super::util::read_json(default_config_path()).and_then(|v| v.as_object().cloned()).unwrap_or_default()
}

pub fn save_config(cfg: &Map<String, Value>) -> bool {
    match write_file(default_config_path(), &serde_json::to_string_pretty(cfg).unwrap()) {
        Ok(_) => {
            ensure_autostart_with(cfg);
            true
        }
        Err(e) => {
            log::error!("Failed saving rotation config: {}", e);
            false
        }
    }
}

pub fn ensure_autostart() -> bool {
    ensure_autostart_with(&load_config())
}

fn ensure_autostart_with(cfg: &Map<String, Value>) -> bool {
    let target = default_autostart_path();
    if cfg.is_empty() {
        if target.exists() {
            let _ = std::fs::remove_file(&target);
            log::info!("Removed rotation autostart file: {}", target.display());
        }
        return true;
    }
    let entry = format!(
        "[Desktop Entry]\nType=Application\nName=SUIT Display & Touch Rotation\nExec=bash -c \"cd {} && {} display --apply-boot\"\nHidden=false\nNoDisplay=false\nX-GNOME-Autostart-enabled=true\nComment=Maintain screen and touch rotation on boot\n",
        project_dir().display(),
        self_exe().display()
    );
    if std::fs::read_to_string(&target).ok().as_deref() == Some(&entry) {
        return true;
    }
    match write_file(&target, &entry) {
        Ok(_) => {
            log::info!("Created rotation autostart file: {}", target.display());
            true
        }
        Err(e) => {
            log::error!("Failed configuring rotation autostart: {}", e);
            false
        }
    }
}

/// Write ~/.config/monitors.xml directly so Mutter boots natively in this rotation.
pub fn write_monitors_xml(connector: &str, rotation: i64) -> bool {
    let Some(st) = get_current_mutter_state(1) else { return false };
    let rot = |r: i64| match r {
        1 => "left",
        2 => "upside_down",
        3 => "right",
        _ => "normal",
    };
    let mut lines = vec!["<monitors version=\"2\">".to_string(), "  <configuration>".into(), "    <layoutmode>logical</layoutmode>".into()];
    for lm in &st.logical {
        for name in &lm.monitors {
            let pm = st.monitors.iter().find(|m| &m.connector == name);
            let (v, p, s) = pm
                .map(|m| (m.vendor.clone(), m.product.clone(), m.serial.clone()))
                .unwrap_or(("unknown".into(), "unknown".into(), "unknown".into()));
            let (w, h, rate) = pm.and_then(|m| m.current.as_ref()).map(|m| (m.w, m.h, m.rate)).unwrap_or((1920, 1080, 60.0));
            let er = if name == connector { rot(rotation) } else { rot(lm.trans as i64) };
            lines.extend([
                "    <logicalmonitor>".to_string(),
                format!("      <x>{}</x>", lm.x),
                format!("      <y>{}</y>", lm.y),
                format!("      <scale>{}</scale>", lm.scale),
                format!("      <primary>{}</primary>", if lm.primary { "yes" } else { "no" }),
                "      <transform>".into(),
                format!("        <rotation>{}</rotation>", er),
                "        <flipped>no</flipped>".into(),
                "      </transform>".into(),
                "      <monitor>".into(),
                "        <monitorspec>".into(),
                format!("          <connector>{}</connector>", name),
                format!("          <vendor>{}</vendor>", v),
                format!("          <product>{}</product>", p),
                format!("          <serial>{}</serial>", s),
                "        </monitorspec>".into(),
                "        <mode>".into(),
                format!("          <width>{}</width>", w),
                format!("          <height>{}</height>", h),
                format!("          <rate>{:.3}</rate>", rate),
                "        </mode>".into(),
                "      </monitor>".into(),
                "    </logicalmonitor>".into(),
            ]);
        }
    }
    lines.push("  </configuration>".into());
    lines.push("</monitors>\n".into());
    match write_file(home().join(".config/monitors.xml"), &lines.join("\n")) {
        Ok(_) => {
            log::info!("Wrote persistent monitors.xml for {} (rotation: {})", connector, rot(rotation));
            true
        }
        Err(e) => {
            log::error!("Failed writing monitors.xml: {}", e);
            false
        }
    }
}

/// Persist confirmed display orientation and touch device to configuration and Mutter.
pub fn persist_display_config(connector: &str, rotation: i64, touch: Option<&str>) -> bool {
    write_monitors_xml(connector, rotation);
    apply_rotation(connector, rotation as u32, 2);
    match touch.filter(|t| !t.is_empty() && *t != "None") {
        Some(t) => {
            apply_touch_calibration(t, connector, rotation, None);
        }
        None => {
            write_udev_rule(None, None);
        }
    }
    let mut cfg = load_config();
    cfg.insert(connector.into(), json!({"rotation": rotation, "touch_device": touch.unwrap_or("None")}));
    save_config(&cfg)
}

pub fn apply_display_and_touch(connector: &str, rotation: i64, touch: Option<&str>, method: Option<u32>, persist: bool, prev_touch: Option<&str>) -> bool {
    log::info!("Applying display rotation ({}) and touch ({:?}) to {} (persist={})", rotation, touch, connector, persist);
    let touch_dev = touch.filter(|t| *t != "None" && !t.is_empty());
    let rebind = touch_dev.or(prev_touch);
    // 1. udev rule with the new matrix (does not yet affect libinput); at 0° clear any rule
    if let (Some(t), true) = (touch_dev, rotation != 0) {
        if let Some(st) = get_current_mutter_state(1) {
            if let Some(m) = calculate_affine_matrix(&st, connector, Some(rotation as u32)) {
                write_udev_rule(Some(t), Some(&m));
            }
        }
    } else {
        write_udev_rule(None, None);
    }
    // 2. rotate via Mutter (method 1=temporary, 2=persistent)
    let ok = apply_rotation(connector, rotation as u32, method.unwrap_or(if persist { 2 } else { 1 }));
    // 3. USB rebind after the rotation animation
    if let Some(d) = rebind.filter(|d| !d.is_empty() && *d != "None") {
        std::thread::sleep(std::time::Duration::from_millis(600));
        rebind_usb_touch(d);
    }
    if persist {
        persist_display_config(connector, rotation, touch);
    }
    ok
}

/// Remove SUIT rotation configuration, autostart entries, and touch calibration rules.
pub fn clear_config(clear_udev: bool) -> bool {
    let mut ok = true;
    for p in [default_config_path(), default_autostart_path(), home().join(".config/monitors.xml")] {
        if p.exists() {
            match std::fs::remove_file(&p) {
                Ok(_) => log::info!("Removed {}", p.display()),
                Err(e) => {
                    log::error!("Failed removing {}: {}", p.display(), e);
                    ok = false;
                }
            }
        }
    }
    if clear_udev && !write_udev_rule(None, None) {
        ok = false;
    }
    ok
}

pub fn open_display_settings() -> bool {
    spawn(&["gnome-control-center", "display"])
}

/// `suit display --apply-boot` from the session autostart.
pub fn apply_boot_config() -> bool {
    log::info!("Applying saved display and touch configuration from boot autostart");
    let cfg = load_config();
    if cfg.is_empty() {
        return true;
    }
    let Some(st) = get_current_mutter_state(15) else {
        log::warn!("Mutter DisplayConfig not available during boot application");
        return false;
    };
    for (mon, s) in &cfg {
        let target = normalize_rotation(&s["rotation"]);
        let cur = st.logical.iter().find(|lm| lm.monitors.iter().any(|m| m == mon)).map(|lm| lm.trans as i64);
        if let Some(c) = cur {
            if c != target {
                log::info!("Aligning {} from {} to {}", mon, c, target);
                apply_rotation(mon, target as u32, 2);
            }
        }
    }
    // Check the udev rule without unbinding USB unless the rule was missing
    if let Some((mon, s)) = cfg.iter().next() {
        let touch = s["touch_device"].as_str().unwrap_or("None");
        let target = normalize_rotation(&s["rotation"]);
        let rule = Path::new(UDEV_RULE_PATH).exists();
        if touch != "None" && !touch.is_empty() && target != 0 {
            if !rule {
                log::info!("Udev rule missing on boot; writing and rebinding");
                apply_touch_calibration(touch, mon, target, None);
            }
        } else if rule {
            log::info!("Udev rule found when none expected; clearing");
            write_udev_rule(None, None);
        }
    }
    true
}
