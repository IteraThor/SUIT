//! Wireless Screen: a tablet as the screen of this PC. Sunshine (host) streams to the Moonlight app on the
//! tablet. With no monitor attached there is nothing to stream, so SUIT forces an unused video port on with
//! its own EDID (kernel args + firmware file, needs a restart): a virtual screen. `suit wireless-screen --watch`
//! mirrors (or extends) a real monitor onto it when one is plugged in.
use super::display::{self, LogicalConfig, Mode, PhysMonitor};
use super::i18n::{tr, trf};
use super::util::{home, project_dir, read_json, read_trim, run, self_exe, sudo_write, write_file, write_json, Output};
use gtk::gio;
use gtk::glib;
use serde::{Deserialize, Serialize};
use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::path::PathBuf;
use std::rc::Rc;

const EDID_FILE: &str = "suit-virtual.bin";
const EDID_PATH: &str = "/usr/lib/firmware/edid/suit-virtual.bin";
const DRACUT_CONF: &str = "/etc/dracut.conf.d/90-suit-wireless-screen.conf";
const COPR_REPO: &str = "/etc/yum.repos.d/_copr:copr.fedorainfracloud.org:lizardbyte:stable.repo";
const COPR_TEXT: &str = "[copr:copr.fedorainfracloud.org:lizardbyte:stable]\nname=Copr repo for stable owned by lizardbyte\nbaseurl=https://download.copr.fedorainfracloud.org/results/lizardbyte/stable/fedora-$releasever-$basearch/\ntype=rpm-md\nskip_if_unavailable=True\ngpgcheck=1\ngpgkey=https://download.copr.fedorainfracloud.org/results/lizardbyte/stable/pubkey.gpg\nrepo_gpgcheck=0\nenabled=1\nenabled_metadata=1\n";
const SUNSHINE_UNIT: &str = "app-dev.lizardbyte.app.Sunshine.service";
/// What Moonlight needs. Not 47990: Sunshine's web UI stays on this PC, where only SUIT uses it.
const PORTS: [&str; 7] = ["47984/tcp", "47989/tcp", "48010/tcp", "47998/udp", "47999/udp", "48000/udp", "48002/udp"];
/// Opened by older SUIT versions for the web UI; closed again.
const OLD_PORTS: [&str; 1] = ["47990/tcp"];
const API: &str = "https://localhost:47990";
/// Login of Sunshine's web UI. The password is random per PC (`web_pass`); older SUIT versions used "suit".
const WEB_USER: &str = "suit";
const OLD_WEB_PASS: &str = "suit";
pub const MOONLIGHT_IOS: &str = "https://apps.apple.com/app/moonlight-game-streaming/id1000551566";
pub const MOONLIGHT_ANDROID: &str = "https://play.google.com/store/apps/details?id=com.limelight";

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    pub width: u32,
    pub height: u32,
    pub refresh: u32,
    /// Port forced on as the virtual screen (empty until set up).
    pub connector: String,
    /// Ports a real monitor was seen on ("Detect my monitor port").
    pub monitor_ports: Vec<String>,
    /// When a monitor is connected: mirror it onto the tablet (true) or extend the desktop (false).
    pub mirror: bool,
    /// Blank cursor theme while Sunshine streams (Moonlight draws its own pointer).
    pub hide_cursor: bool,
    /// Picture quality: Sharp (higher bitrate) or Smooth (lower, for weak Wi-Fi).
    pub sharp: bool,
}

impl Default for Config {
    fn default() -> Self {
        Config { width: 1920, height: 1200, refresh: 60, connector: String::new(), monitor_ports: vec![], mirror: true, hide_cursor: false, sharp: true }
    }
}

/// Tablet sizes offered under "Change" (all go through `fit_size` for the port).
pub const PRESETS: [(&str, u32, u32); 9] = [
    ("Android 16:10", 1920, 1200),
    ("Android 16:10 (large)", 2560, 1600),
    ("iPad 10.2\"", 2160, 1620),
    ("iPad 9.7\" / mini", 2048, 1536),
    ("iPad Air", 2360, 1640),
    ("iPad mini 6", 2266, 1488),
    ("iPad Pro 11\"", 2388, 1668),
    ("iPad Pro 12.9\"", 2732, 2048),
    ("Full HD", 1920, 1080),
];

fn config_path() -> PathBuf {
    home().join(".config/suit/wireless_screen.json")
}

pub fn load_config() -> Config {
    read_json(config_path()).and_then(|v| serde_json::from_value(v).ok()).unwrap_or_default()
}

pub fn save_config(c: &Config) -> bool {
    write_json(config_path(), &serde_json::to_value(c).unwrap_or_default()).is_ok()
}

// ---------------------------------------------------------------- EDID

/// The name Moonlight shows for this PC: the short host name, or "SUIT" when the router gave it a long
/// machine-generated one (e.g. p200300cd...dip0.t-ipconnect.de).
pub fn pc_name() -> String {
    friendly_name(&super::util::hostname())
}

pub fn friendly_name(host: &str) -> String {
    let short = host.split('.').next().unwrap_or("");
    if (1..=16).contains(&short.len()) && !short.chars().all(|c| c.is_ascii_hexdigit()) { short.to_string() } else { "SUIT".into() }
}

/// "1920x1200", "1920 x 1200" or "1920×1200" -> (1920, 1200), within what the virtual screen supports.
pub fn parse_resolution(text: &str) -> Option<(u32, u32)> {
    let t = text.to_lowercase().replace('×', "x");
    let (w, h) = t.split_once('x')?;
    let (w, h): (u32, u32) = (w.trim().parse().ok()?, h.trim().parse().ok()?);
    ((640..=4096).contains(&w) && (480..=4096).contains(&h)).then_some((w, h))
}

/// Detailed timing descriptor in CVT reduced blanking (v1) and its pixel clock in 10 kHz units.
fn cvt_rb_dtd(w: u32, h: u32, refresh: u32) -> ([u8; 18], u32) {
    let is = |a: u32, b: u32| w * b == h * a;
    let vsync = if is(4, 3) { 4 } else if is(16, 9) { 5 } else if is(16, 10) { 6 } else if is(5, 4) || is(15, 9) { 7 } else { 10 };
    let (hblank, hfront, hsync, vfront) = (160u32, 48u32, 32u32, 3u32);
    // 460 us minimum vertical blanking, at least 6 lines back porch
    let h_period_us = (1e6 / refresh as f64 - 460.0) / h as f64;
    let vblank = ((460.0 / h_period_us).floor() as u32 + 1).max(vfront + vsync + 6);
    let mhz = refresh as f64 * ((h + vblank) * (w + hblank)) as f64 / 1e6;
    let clock = ((mhz / 0.25).floor() * 25.0) as u32;
    // physical size for ~96 dpi, so GNOME keeps 100 % scaling
    let (wmm, hmm) = (w * 254 / 960, h * 254 / 960);
    let d = [
        clock as u8,
        (clock >> 8) as u8,
        w as u8,
        hblank as u8,
        (((w >> 8) << 4) | (hblank >> 8)) as u8,
        h as u8,
        vblank as u8,
        (((h >> 8) << 4) | (vblank >> 8)) as u8,
        hfront as u8,
        hsync as u8,
        ((vfront << 4) | vsync) as u8,
        0,
        wmm as u8,
        hmm as u8,
        (((wmm >> 8) << 4) | (hmm >> 8)) as u8,
        0,
        0,
        0x1a, // digital separate sync, hsync +, vsync -
    ];
    (d, clock)
}

/// Highest pixel clock HDMI gives on the Intel GPUs SUIT runs on (HDMI 1.4, 297 MHz).
const HDMI_MAX_KHZ10: u32 = 29700;

/// Size the port can drive at `refresh`: on HDMI the largest size with the same aspect ratio (so the
/// tablet stays filled) under the pixel clock limit, width in steps of 8, height even; DP takes any size.
pub fn fit_size(w: u32, h: u32, refresh: u32, hdmi: bool) -> (u32, u32) {
    if !hdmi || cvt_rb_dtd(w, h, refresh).1 <= HDMI_MAX_KHZ10 {
        return (w, h);
    }
    (640..=w / 8 * 8)
        .rev()
        .step_by(8)
        .map(|fw| (fw, ((fw * h + w) / (2 * w)) * 2))
        .find(|(fw, fh)| cvt_rb_dtd(*fw, *fh, refresh).1 <= HDMI_MAX_KHZ10)
        .unwrap_or((640, 480))
}

/// Size the virtual screen gets on `port`.
pub fn size_on_port(w: u32, h: u32, refresh: u32, port: &str) -> (u32, u32) {
    fit_size(w, h, refresh, port.starts_with("HDMI-A-"))
}

fn checksum(block: &mut [u8]) {
    let sum = block[..127].iter().fold(0u8, |a, b| a.wrapping_add(*b));
    block[127] = 0u8.wrapping_sub(sum);
}

/// EDID 1.3 with a CTA-861 extension: the tablet size as preferred mode plus 1920x1080 and 1280x720, so a
/// normal monitor shares a mode with the virtual screen when mirroring.
pub fn build_edid(w: u32, h: u32, refresh: u32) -> [u8; 256] {
    let mut e = [0u8; 256];
    let (dtd, clock) = cvt_rb_dtd(w, h, refresh);
    e[0..8].copy_from_slice(&[0x00, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x00]);
    e[8..12].copy_from_slice(&[0x4e, 0xa9, 0x01, 0x00]); // "SUI", product 1
    e[16] = 1;
    e[17] = 36; // 2026
    e[18] = 1;
    e[19] = 3;
    e[20] = 0x80; // digital
    e[21] = (w * 254 / 9600).min(255) as u8; // size in cm, same ~96 dpi as the DTD
    e[22] = (h * 254 / 9600).min(255) as u8;
    e[23] = 0x78; // gamma 2.2
    e[24] = 0x0e; // RGB, sRGB, preferred timing in DTD 1
    e[25..35].copy_from_slice(&[0xee, 0x91, 0xa3, 0x54, 0x4c, 0x99, 0x26, 0x0f, 0x50, 0x54]); // sRGB
    e[35..38].copy_from_slice(&[0x21, 0x08, 0x00]); // 640x480, 800x600, 1024x768 @60
    e[38..54].copy_from_slice(&[0xd1, 0xc0, 0x81, 0xc0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]); // 1920x1080, 1280x720 @60
    e[54..72].copy_from_slice(&dtd);
    let total_h = w + 160;
    let hfreq_khz = (clock * 10 / total_h + 1).max(68);
    let max_clk = (clock.max(14850) + 999) / 1000 + 1;
    e[72..90].copy_from_slice(&[
        0, 0, 0, 0xfd, 0, 48, (refresh + 2).max(62).min(255) as u8, 30, hfreq_khz.min(255) as u8, max_clk.min(255) as u8, 0, 0x0a, 0x20, 0x20, 0x20, 0x20, 0x20, 0x20,
    ]);
    e[90..95].copy_from_slice(&[0, 0, 0, 0xfc, 0]);
    e[95..108].copy_from_slice(b"SUIT Screen\n ");
    e[108..113].copy_from_slice(&[0, 0, 0, 0x10, 0]);
    e[126] = 1;
    checksum(&mut e[..128]);
    // CTA-861 rev 3, underscanned, DTDs from byte 18; video data block VIC 16 (1080p60) + VIC 4 (720p60);
    // HDMI VSDB (OUI 00-0C-03, address 1.0.0.0, max TMDS 300 MHz): without it i915 treats the port as DVI
    // and drops every mode above 165 MHz; VCDB (RGB range selectable, underscan)
    e[128..146].copy_from_slice(&[0x02, 0x03, 0x12, 0x80, 0x42, 16, 4, 0x67, 0x03, 0x0c, 0x00, 0x10, 0x00, 0x00, 60, 0xe2, 0x00, 0x4a]);
    checksum(&mut e[128..]);
    e
}

// ---------------------------------------------------------------- ports and boot entries

#[derive(Debug, Clone, PartialEq)]
pub struct Connector {
    pub name: String,
    pub connected: bool,
}

pub fn list_connectors() -> Vec<Connector> {
    let mut v: Vec<Connector> = glob::glob("/sys/class/drm/card*-*/status")
        .map(|g| g.flatten().collect::<Vec<_>>())
        .unwrap_or_default()
        .into_iter()
        .filter_map(|p| {
            let dir = p.parent()?.file_name()?.to_string_lossy().to_string();
            let name = dir.split_once('-')?.1.to_string();
            (!name.starts_with("Writeback")).then(|| Connector { name, connected: read_trim(&p).as_deref() == Some("connected") })
        })
        .collect();
    v.sort_by(|a, b| a.name.cmp(&b.name));
    v
}

/// Port for the virtual screen: not connected now, never seen with a monitor; HDMI before DP (no link
/// training), the highest number first (the last ports are the least likely to get a monitor).
pub fn suggest_connector(conns: &[Connector], monitor_ports: &[String]) -> Option<String> {
    let rank = |n: &str| ["HDMI-A-", "DP-", "DVI-"].iter().position(|p| n.starts_with(p));
    let index = |n: &str| n.rsplit('-').next().and_then(|i| i.parse::<u32>().ok()).unwrap_or(0);
    conns
        .iter()
        .filter(|c| !c.connected && !monitor_ports.contains(&c.name))
        .filter_map(|c| rank(&c.name).map(|r| (r, std::cmp::Reverse(index(&c.name)), c)))
        .min_by_key(|(r, i, _)| (*r, *i))
        .map(|(_, _, c)| c.name.clone())
}

/// `drm.edid_firmware=CONN:edid/FILE` from a kernel command line: (connector, firmware path).
pub fn parse_edid_arg(cmdline: &str) -> Option<(String, String)> {
    let v = cmdline.split_whitespace().find_map(|a| a.strip_prefix("drm.edid_firmware="))?;
    let (c, f) = v.split(',').next()?.split_once(':')?;
    Some((c.to_string(), f.to_string()))
}

/// Kernel args of the default boot entry (the next boot), falling back to the running ones.
fn boot_args() -> String {
    let r = run(&["sudo", "-n", "grubby", "--info=DEFAULT"], Some(20));
    r.stdout
        .lines()
        .find_map(|l| l.strip_prefix("args="))
        .map(|a| a.trim_matches('"').to_string())
        .unwrap_or_else(|| read_trim("/proc/cmdline").unwrap_or_default())
}

/// Port the kernel command line already forces on (`video=CONN:e`, or a `drm.edid_firmware=` one), e.g. a
/// virtual screen made by hand. It shows as connected, so the suggestion would skip it and take a real port.
pub fn forced_connector(cmdline: &str) -> Option<String> {
    let mode_chars = |m: &str| m.chars().all(|c| c.is_ascii_digit() || "x@.-RMim".contains(c));
    cmdline
        .split_whitespace()
        .filter_map(|a| a.strip_prefix("video="))
        .find_map(|v| v.split_once(':').filter(|(_, m)| m.strip_suffix('e').is_some_and(mode_chars)).map(|(c, _)| c.to_string()))
        .or_else(|| parse_edid_arg(cmdline).map(|(c, _)| c))
}

/// The forced port when there is one (its EDID and args get replaced), else a free one.
pub fn choose_connector(cmdline: &str, conns: &[Connector], monitor_ports: &[String]) -> Option<String> {
    forced_connector(cmdline).or_else(|| suggest_connector(conns, monitor_ports))
}

/// Kernel args of the next and of the running boot.
fn kernel_args() -> String {
    format!("{} {}", boot_args(), read_trim("/proc/cmdline").unwrap_or_default())
}

/// Port for the virtual screen on this PC.
pub fn pick_connector() -> Option<String> {
    choose_connector(&kernel_args(), &list_connectors(), &load_config().monitor_ports)
}

fn ours(arg: Option<(String, String)>) -> Option<String> {
    arg.filter(|(_, f)| f.ends_with(EDID_FILE)).map(|(c, _)| c)
}

/// The name Mutter uses for a DRM connector: "HDMI-A-2" is "HDMI-2", "DVI-D-1" is "DVI-1", DP stays "DP-2".
pub fn mutter_name(drm: &str) -> String {
    ["-A-", "-D-", "-I-"].iter().fold(drm.to_string(), |n, t| n.replacen(t, "-", 1))
}

fn connector_connected(name: &str) -> bool {
    list_connectors().iter().any(|c| c.name == name && c.connected)
}

/// How the screens are arranged now.
#[derive(Debug, Clone, Copy, PartialEq, Serialize)]
pub enum Layout {
    /// Only the tablet shows the desktop.
    TabletOnly,
    /// The tablet shows the same picture as a monitor.
    Mirror,
    /// Tablet and monitor form one wide desktop.
    Extend,
}

/// Layout from Mutter's logical monitors; None when the virtual screen is off.
pub fn layout_of(logical: &[display::LogicalMonitor], virt: &str) -> Option<Layout> {
    let lm = logical.iter().find(|l| l.monitors.iter().any(|m| m == virt))?;
    Some(if lm.monitors.len() > 1 {
        Layout::Mirror
    } else if logical.len() == 1 {
        Layout::TabletOnly
    } else {
        Layout::Extend
    })
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct Status {
    pub config: Config,
    /// Virtual screen port in the boot entries (set up; active after the next restart).
    pub configured: Option<String>,
    /// The virtual screen is running now.
    pub active: bool,
    /// The running virtual screen still has the EDID from before a size change.
    pub edid_changed: bool,
    /// Port the virtual screen is on, or would be set up on.
    pub port: Option<String>,
    pub sunshine_installed: bool,
    pub sunshine_running: bool,
    pub paired: Vec<String>,
    pub ip: String,
    pub hostname: String,
    /// The monitor watcher service runs.
    pub watcher_running: bool,
    /// Download host reachable (only checked while nothing is set up yet).
    pub online: Option<bool>,
    /// Moonlight waits for a PIN; the name it sent (may be empty).
    pub pending: Option<String>,
    /// A tablet streams right now.
    pub streaming: bool,
    /// Size and rate the last tablet asked for (Sunshine log).
    pub client_mode: Option<(u32, u32, u32)>,
    pub layout: Option<Layout>,
    /// Real monitors connected besides the virtual screen.
    pub others: usize,
    /// The default route goes over Wi-Fi.
    pub wifi: bool,
}

impl Status {
    /// The boot entries have our virtual screen. A screen that is only active (e.g. right after "Remove
    /// everything", before the restart) is not set up: it is gone after the next restart.
    pub fn set_up(&self) -> bool {
        self.configured.is_some()
    }

    pub fn reboot_pending(&self) -> bool {
        self.configured.is_some() && (!self.active || self.edid_changed)
    }

    /// Start can run: a port is free (or already forced) and the download works.
    pub fn preflight_error(&self) -> Option<String> {
        if self.set_up() && self.sunshine_installed {
            return None;
        }
        if !self.set_up() && self.port.is_none() {
            return Some(tr("No free video port. Unplug a monitor cable you do not use."));
        }
        (self.online == Some(false)).then(|| tr("No internet. Connect this PC to the internet first."))
    }

    /// "Match my tablet: W x H": the size the tablet asked for, on this port, when it differs.
    pub fn tablet_offer(&self) -> Option<(u32, u32)> {
        let (w, h, _) = self.client_mode?;
        let c = &self.config;
        let fit = size_on_port(w, h, c.refresh, &c.connector);
        (parse_resolution(&format!("{}x{}", w, h)).is_some() && fit != (c.width, c.height)).then_some(fit)
    }
}

/// The download host answers (short timeout).
pub fn online() -> bool {
    run(&["curl", "-sI", "-m", "4", "-o", "/dev/null", "https://download.copr.fedorainfracloud.org/"], Some(6)).ok()
}

/// Network device of the default route (`ip route show default`).
pub fn default_dev(route: &str) -> Option<String> {
    let mut it = route.lines().next()?.split_whitespace();
    it.find(|w| *w == "dev")?;
    it.next().map(String::from)
}

fn on_wifi() -> bool {
    default_dev(&run(&["ip", "route", "show", "default"], Some(5)).stdout).is_some_and(|d| std::path::Path::new(&format!("/sys/class/net/{}/wireless", d)).exists())
}

/// The newest pairing Moonlight started (GET /api/pin): (id, name).
pub fn pending_pairing(v: &serde_json::Value) -> Option<(String, String)> {
    let p = v["pairings"].as_array()?.iter().rev().find(|p| p["id"].is_string())?;
    Some((p["id"].as_str()?.to_string(), p["name"].as_str().unwrap_or("").trim().to_string()))
}

fn sunshine_log() -> String {
    std::fs::read_to_string(home().join(".config/sunshine/sunshine.log")).unwrap_or_default()
}

/// A tablet streams: the last client line in the log is a connect.
pub fn streaming(log: &str) -> bool {
    log.lines().rev().find_map(|l| if l.contains("CLIENT DISCONNECTED") { Some(false) } else if l.contains("CLIENT CONNECTED") { Some(true) } else { None }).unwrap_or(false)
}

/// Mode the last tablet asked for: "mode -- 2160x1620x60" (debug log of the launch request) or "mode=...".
pub fn client_mode(log: &str) -> Option<(u32, u32, u32)> {
    log.lines().rev().find_map(|l| {
        let i = l.find("mode -- ").map(|i| i + 8).or_else(|| l.find("mode=").map(|i| i + 5))?;
        let v: Vec<u32> = l[i..].split(|c: char| c.is_whitespace() || c == '&').next()?.split('x').filter_map(|n| n.parse().ok()).collect();
        (v.len() == 3 && v[0] >= 640 && v[1] >= 480).then(|| (v[0], v[1], v[2]))
    })
}

/// The kernel keeps the EDID it read at boot: a new file on disk (new size) counts after a restart.
pub fn edid_outdated(running: &[u8], on_disk: &[u8]) -> bool {
    !running.is_empty() && !on_disk.is_empty() && running != on_disk
}

fn running_edid(connector: &str) -> Vec<u8> {
    glob::glob(&format!("/sys/class/drm/card*-{}/edid", connector))
        .ok()
        .and_then(|mut g| g.next())
        .and_then(|p| p.ok())
        .and_then(|p| std::fs::read(p).ok())
        .unwrap_or_default()
}

pub fn status() -> Status {
    let running = ours(parse_edid_arg(&read_trim("/proc/cmdline").unwrap_or_default()));
    let paired = read_json(home().join(".config/sunshine/sunshine_state.json"))
        .and_then(|v| v["root"]["named_devices"].as_array().map(|a| a.iter().filter_map(|d| d["name"].as_str().map(String::from)).collect()))
        .unwrap_or_default();
    let configured = ours(parse_edid_arg(&boot_args()));
    let config = load_config();
    let active = running.as_deref().is_some_and(connector_connected);
    let sunshine_installed = super::util::has_cmd("sunshine");
    let sunshine_running = unit_running(SUNSHINE_UNIT);
    let set_up = configured.is_some() || active;
    let log = sunshine_log();
    let state = active.then(|| display::get_current_mutter_state(2)).flatten();
    Status {
        port: configured.clone().or(running.clone()).or_else(pick_connector),
        configured,
        active,
        edid_changed: running.as_deref().is_some_and(|c| edid_outdated(&running_edid(c), &std::fs::read(EDID_PATH).unwrap_or_default())),
        sunshine_installed,
        sunshine_running,
        paired,
        ip: super::remote_desktop::get_local_ip(),
        hostname: pc_name(),
        watcher_running: unit_running(WATCH_UNIT),
        online: (!set_up || !sunshine_installed).then(online),
        pending: sunshine_running.then(|| pending_pairing(&api(&[&format!("{}/api/pin", API)]))).flatten().map(|(_, n)| n),
        streaming: streaming(&log),
        client_mode: client_mode(&log),
        layout: state.as_ref().and_then(|s| layout_of(&s.logical, &mutter_name(&config.connector))),
        others: state.as_ref().map(|s| s.monitors.iter().filter(|m| m.connector != mutter_name(&config.connector)).count()).unwrap_or(0),
        wifi: on_wifi(),
        config,
    }
}

fn unit_running(unit: &str) -> bool {
    // right after login Sunshine is still "activating" (it waits 5 s before it starts)
    matches!(run(&["systemctl", "--user", "is-active", unit], Some(5)).stdout.trim(), "active" | "activating")
}

// ---------------------------------------------------------------- check my setup

#[derive(Debug, Clone, Serialize)]
pub struct Check {
    /// screen, sunshine, ports, watcher, paired
    pub id: &'static str,
    pub ok: bool,
    pub detail: String,
}

/// The firewall lets Moonlight in (or no firewall runs).
fn ports_open() -> bool {
    if sudo(&["firewall-cmd", "--state"], 10).stdout.trim() != "running" {
        return true;
    }
    let open = sudo(&["firewall-cmd", "--list-ports"], 10).stdout;
    let is_open = |p: &&str| open.split_whitespace().any(|o| o == *p);
    PORTS.iter().all(is_open) && !OLD_PORTS.iter().any(is_open)
}

/// "Check my setup": every part the tablet needs.
fn unit_enabled(unit: &str) -> bool {
    run(&["systemctl", "--user", "is-enabled", unit], Some(10)).stdout.trim() == "enabled"
}

/// sunshine.conf still has every setting SUIT wants (nothing missing or changed by hand).
fn settings_current() -> bool {
    let path = sunshine_dir().join("sunshine.conf");
    let now = std::fs::read_to_string(&path).unwrap_or_default();
    merge_conf(&now, &sunshine_settings(&pc_name(), &super::remote_desktop::get_local_ip(), load_config().sharp)) == now
}

pub fn checklist(s: &Status) -> Vec<Check> {
    let c = &s.config;
    let screen_ok = s.configured.is_some() && s.active && !s.edid_changed;
    let screen = if screen_ok {
        format!("{} {} x {} @ {} Hz", c.connector, c.width, c.height, c.refresh)
    } else if s.configured.is_some() {
        tr("Restart needed")
    } else {
        tr("Not set up")
    };
    vec![
        Check { id: "screen", ok: screen_ok, detail: screen },
        Check { id: "sunshine", ok: s.sunshine_installed && s.sunshine_running && unit_enabled(SUNSHINE_UNIT) && settings_current(), detail: if s.sunshine_installed { String::new() } else { tr("Not installed") } },
        Check { id: "ports", ok: ports_open(), detail: String::new() },
        Check { id: "watcher", ok: s.watcher_running && unit_enabled(WATCH_UNIT), detail: String::new() },
        Check { id: "paired", ok: !s.paired.is_empty(), detail: s.paired.join(", ") },
    ]
}

/// One-tap repair for a red check. "screen" only rewrites a missing setup (a restart is up to the user);
/// "paired" needs the tablet.
pub fn fix(id: &str, step: &(dyn Fn(&str) + Sync)) -> (bool, String) {
    match id {
        "sunshine" if !super::util::has_cmd("sunshine") => install_sunshine(step),
        "sunshine" => {
            apply_settings();
            if web_pass().is_none() {
                set_web_login();
            }
            run(&["systemctl", "--user", "enable", SUNSHINE_UNIT], Some(30));
            let ok = user_unit("restart").ok();
            (ok, tr(if ok { "Sunshine restarted." } else { "Sunshine does not start." }))
        }
        "watcher" => {
            let ok = install_watcher();
            (ok, tr(if ok { "Watcher restarted." } else { "The watcher does not start." }))
        }
        "ports" => {
            firewall_ports(true);
            let ok = ports_open();
            (ok, tr(if ok { "Ports open." } else { "Could not open the ports." }))
        }
        "screen" => {
            let s = status();
            if s.configured.is_some() {
                (false, tr("Restart this PC to finish."))
            } else if !s.config.connector.is_empty() {
                write_virtual_screen(&s.config)
            } else {
                (false, tr("Not set up"))
            }
        }
        _ => (false, tr("Pair the tablet in Moonlight.")),
    }
}

/// Quiet repairs whenever the wizard opens: Sunshine and the watcher run, an old autostart moves to the service.
pub fn self_repair() {
    let s = status();
    // older SUIT versions left the web UI open to the network with the login suit/suit
    if s.sunshine_installed && web_pass().is_none() {
        log::info!("Wireless Screen: Sunshine's web UI only on this PC, with its own password");
        apply_settings();
        firewall_ports(true);
        set_web_login();
    }
    if s.sunshine_installed && !unit_enabled(SUNSHINE_UNIT) {
        log::info!("Wireless Screen: enabling Sunshine for the next login");
        user_unit("enable");
    }
    if s.sunshine_installed && !s.sunshine_running {
        log::info!("Wireless Screen: starting Sunshine");
        user_unit("start");
    }
    let old = autostart(WATCH_AUTOSTART).exists();
    if s.set_up() && (old || !s.watcher_running || !unit_enabled(WATCH_UNIT)) {
        log::info!("Wireless Screen: starting the watcher service");
        install_watcher();
    }
}

/// Everything for a bug report ("Copy details").
pub fn details() -> String {
    let s = status();
    let log = sunshine_log();
    let tail: Vec<&str> = log.lines().rev().take(200).collect();
    format!(
        "SUIT Wireless Screen\n\nconfig: {}\n\nstatus: {:#?}\n\nboot args: {}\nrunning args: {}\n\nsunshine.log (last 200 lines):\n{}\n",
        serde_json::to_string(&s.config).unwrap_or_default(),
        s,
        boot_args(),
        read_trim("/proc/cmdline").unwrap_or_default(),
        tail.into_iter().rev().collect::<Vec<_>>().join("\n")
    )
}

/// Status, preflight and checklist as one JSON object (`suit wireless-screen --status`).
pub fn report() -> serde_json::Value {
    let s = status();
    let c = &s.config;
    let mut v = serde_json::to_value(&s).unwrap_or_default();
    v["reboot_pending"] = s.reboot_pending().into();
    v["fitted_size"] = s.port.as_deref().map(|p| size_on_port(c.width, c.height, c.refresh, p)).map(|(w, h)| serde_json::json!([w, h])).unwrap_or_default();
    v["preflight"] = serde_json::json!({ "port": s.port, "online": s.online.unwrap_or_else(online), "error": s.preflight_error() });
    v["rescue_clean"] = rescue_clean().into();
    v["checks"] = serde_json::to_value(checklist(&s)).unwrap_or_default();
    v
}

/// Record the ports that have a monitor now, so the virtual screen never takes one of them.
pub fn detect_monitor_ports() -> Vec<String> {
    let mut cfg = load_config();
    let forced = forced_connector(&kernel_args());
    let found: Vec<String> =
        list_connectors().into_iter().filter(|c| c.connected && c.name != cfg.connector && Some(&c.name) != forced.as_ref()).map(|c| c.name).collect();
    for f in &found {
        if !cfg.monitor_ports.contains(f) {
            cfg.monitor_ports.push(f.clone());
        }
    }
    save_config(&cfg);
    found
}

fn sudo(args: &[&str], timeout: u64) -> Output {
    let mut a = vec!["sudo", "-n"];
    a.extend_from_slice(args);
    run(&a, Some(timeout))
}

fn last_line(o: &Output) -> String {
    o.combined().lines().rev().map(str::trim).find(|l| !l.is_empty()).unwrap_or("").to_string()
}

/// Kernel paths of the rescue boot entries in `grubby --info=ALL`.
pub fn rescue_kernels(info: &str) -> Vec<String> {
    info.lines().filter_map(|l| l.strip_prefix("kernel=")).map(|k| k.trim_matches('"').to_string()).filter(|k| k.contains("-rescue-")).collect()
}

/// Args of each rescue entry in `grubby --info=ALL`.
fn rescue_args(info: &str) -> Vec<String> {
    let mut out = vec![];
    let mut rescue = false;
    for l in info.lines() {
        if let Some(k) = l.strip_prefix("kernel=") {
            rescue = k.contains("-rescue-");
        } else if let Some(a) = l.strip_prefix("args=").filter(|_| rescue) {
            out.push(a.trim_matches('"').to_string());
        }
    }
    out
}

/// The rescue entry boots without the virtual screen (true too when there is none).
pub fn rescue_clean() -> bool {
    rescue_args(&sudo(&["grubby", "--info=ALL"], 20).stdout).iter().all(|a| forced_connector(a).is_none())
}

/// Keep the rescue entry free of the forced port: if the virtual screen ever breaks the picture, "rescue"
/// in the boot menu (Fedora shows it after a failed boot) still starts normally.
fn clean_rescue(args: &str) {
    for k in rescue_kernels(&sudo(&["grubby", "--info=ALL"], 20).stdout) {
        let r = sudo(&["grubby", &format!("--update-kernel={}", k), &format!("--remove-args={}", args)], 60);
        log::info!("Rescue entry {} without the virtual screen: ok={}", k, r.ok());
    }
}

/// Drop every forced-EDID arg (also older ones made by hand) and the `video=PORT:e` of those ports.
fn remove_boot_args(cfg: &Config) -> bool {
    let args = forced_args(cfg);
    sudo(&["grubby", "--update-kernel=ALL", &format!("--remove-args={}", args)], 120).ok()
}

fn forced_args(cfg: &Config) -> String {
    let mut args = vec!["drm.edid_firmware".to_string()];
    let (next, now) = (boot_args(), read_trim("/proc/cmdline").unwrap_or_default());
    let found = [parse_edid_arg(&next).map(|(c, _)| c), parse_edid_arg(&now).map(|(c, _)| c), forced_connector(&next), forced_connector(&now)];
    for c in found.into_iter().flatten().chain([cfg.connector.clone()]) {
        let a = format!("video={}:e", c);
        if !c.is_empty() && !args.contains(&a) {
            args.push(a);
        }
    }
    args.join(" ")
}

fn autostart(name: &str) -> PathBuf {
    home().join(".config/autostart").join(name)
}

fn write_autostart(file: &str, name: &str, arg: &str) -> bool {
    let entry = format!(
        "[Desktop Entry]\nType=Application\nName={}\nExec=bash -c \"cd {} && {} wireless-screen {}\"\nX-GNOME-Autostart-enabled=true\n",
        name,
        project_dir().display(),
        self_exe().display(),
        arg
    );
    write_file(autostart(file), &entry).is_ok()
}

/// Old watcher autostart (before the service); removed on migration.
const WATCH_AUTOSTART: &str = "suit-wireless-screen.desktop";
const RESUME_AUTOSTART: &str = "suit-wireless-resume.desktop";
const WATCH_UNIT: &str = "suit-wireless-screen.service";

pub fn remove_resume_autostart() {
    let _ = std::fs::remove_file(autostart(RESUME_AUTOSTART));
}

fn unit_path() -> PathBuf {
    home().join(".config/systemd/user").join(WATCH_UNIT)
}

/// User service for `suit wireless-screen --watch`, restarted when it fails.
pub fn watcher_unit(exe: &str, dir: &str) -> String {
    format!(
        "# Written by SUIT Wireless Screen\n[Unit]\nDescription=SUIT Wireless Screen monitor layout\nPartOf=graphical-session.target\nAfter=graphical-session.target\n\n[Service]\nWorkingDirectory={}\nExecStart={} wireless-screen --watch\nRestart=on-failure\nRestartSec=3\n\n[Install]\nWantedBy=graphical-session.target\n",
        dir, exe
    )
}

/// Write, enable and (re)start the watcher service; drop the old autostart entry.
pub fn install_watcher() -> bool {
    let text = watcher_unit(&self_exe().display().to_string(), &project_dir().display().to_string());
    if write_file(unit_path(), &text).is_err() {
        return false;
    }
    let _ = std::fs::remove_file(autostart(WATCH_AUTOSTART));
    run(&["systemctl", "--user", "daemon-reload"], Some(30));
    run(&["systemctl", "--user", "enable", WATCH_UNIT], Some(30));
    run(&["systemctl", "--user", "restart", WATCH_UNIT], Some(30)).ok()
}

fn remove_watcher() {
    run(&["systemctl", "--user", "disable", "--now", WATCH_UNIT], Some(30));
    let _ = std::fs::remove_file(unit_path());
    let _ = std::fs::remove_file(autostart(WATCH_AUTOSTART));
    run(&["systemctl", "--user", "daemon-reload"], Some(30));
}

/// Write the EDID and boot setup for a virtual screen on `cfg.connector` and the monitor watcher service.
/// Active after a restart.
pub fn setup_virtual_screen(cfg: &Config) -> (bool, String) {
    let r = write_virtual_screen(cfg);
    if r.0 {
        install_watcher();
    }
    r
}

/// The Start step (GUI and `--setup`): pick the port, fit the size to it, set up the virtual screen.
pub fn setup(width: u32, height: u32, refresh: u32, step: &(dyn Fn(&str) + Sync)) -> (bool, String) {
    detect_monitor_ports();
    let Some(port) = pick_connector() else {
        return (false, tr("No free video port. Unplug a monitor cable you do not use."));
    };
    // HDMI cannot drive every tablet size at full speed: same shape, a bit smaller
    let (width, height) = size_on_port(width, height, refresh, &port);
    step(&trf("Setting up the virtual screen on {port} ({w} x {h})...", &[("port", &port), ("w", &width), ("h", &height)]));
    setup_virtual_screen(&Config { width, height, refresh, connector: port, ..load_config() })
}

/// A new tablet size on the port set up (GUI Apply, "Match my tablet" and `--apply`).
pub fn apply_size(width: u32, height: u32, refresh: u32, step: &(dyn Fn(&str) + Sync)) -> (bool, String) {
    let cfg = load_config();
    if cfg.connector.is_empty() {
        return (false, tr("Not set up"));
    }
    let (width, height) = size_on_port(width, height, refresh, &cfg.connector);
    step(&trf("Setting up the virtual screen on {port} ({w} x {h})...", &[("port", &cfg.connector), ("w", &width), ("h", &height)]));
    write_virtual_screen(&Config { width, height, refresh, ..cfg })
}

/// Only the EDID, initramfs and kernel args (also for a new size later); Sunshine and autostarts stay.
pub fn write_virtual_screen(cfg: &Config) -> (bool, String) {
    let fail = |what: &str, detail: String| {
        log::error!("Wireless Screen setup failed at {}: {}", what, detail);
        (false, trf("Setting up the virtual screen failed: {error}", &[("error", &detail)]))
    };
    // private to this user: in the shared /tmp another account could swap the file before sudo copies it
    let tmp = glib::user_runtime_dir().join(EDID_FILE);
    if let Err(e) = std::fs::write(&tmp, build_edid(cfg.width, cfg.height, cfg.refresh)) {
        return fail("edid", e.to_string());
    }
    let r = sudo(&["install", "-D", "-m", "0644", &tmp.to_string_lossy(), EDID_PATH], 20);
    let _ = std::fs::remove_file(&tmp);
    if !r.ok() {
        return fail("edid", last_line(&r));
    }
    // the GPU driver may load in early boot, so the EDID must be in the initramfs
    if !sudo_write(DRACUT_CONF, &format!("# Written by SUIT Wireless Screen\ninstall_items+=\" {} \"\n", EDID_PATH)) {
        return fail("dracut", DRACUT_CONF.into());
    }
    let r = sudo(&["dracut", "-f"], 900);
    if !r.ok() {
        return fail("dracut", last_line(&r));
    }
    remove_boot_args(cfg);
    // ALL also writes /etc/kernel/cmdline, which new kernels take their args from (kernel updates keep it)
    let args = format!("--args=drm.edid_firmware={c}:edid/{f} video={c}:e", c = cfg.connector, f = EDID_FILE);
    let r = sudo(&["grubby", "--update-kernel=ALL", &args], 120);
    if !r.ok() {
        return fail("grubby", last_line(&r));
    }
    clean_rescue(&forced_args(cfg));
    save_config(cfg);
    update_greeter_layout(cfg);
    // the tablet only shows the desktop of a logged-in user: at the login screen nobody could sign in on it
    let user = super::util::username();
    let autologin = super::power::is_auto_login_enabled(None).0 || super::power::set_auto_login(true, &user, None);
    log::info!("Virtual screen set up on {} ({}x{}@{}), autologin={}", cfg.connector, cfg.width, cfg.height, cfg.refresh, autologin);
    (true, tr(if autologin { "Virtual screen set up. Restart to turn it on." } else { "Virtual screen set up. Restart to turn it on. Turn on Automatic Login in System, or the tablet shows no login screen." }))
}

/// Restart now; SUIT opens this page again after the login.
pub fn restart() -> (bool, String) {
    write_autostart(RESUME_AUTOSTART, "SUIT Wireless Screen Setup", "--resume");
    // sudo first: without a local desktop session (e.g. started over ssh) polkit would ask for a password
    let r = sudo(&["systemctl", "reboot"], 30);
    if r.ok() {
        return (true, String::new());
    }
    super::upgrade::reboot()
}

fn firewall_ports(add: bool) {
    if sudo(&["firewall-cmd", "--state"], 10).stdout.trim() != "running" {
        return;
    }
    for p in PORTS {
        sudo(&["firewall-cmd", "--permanent", &format!("--{}-port={}", if add { "add" } else { "remove" }, p)], 20);
    }
    for p in OLD_PORTS {
        sudo(&["firewall-cmd", "--permanent", &format!("--remove-port={}", p)], 20);
    }
    sudo(&["firewall-cmd", "--reload"], 30);
}

fn web_pass_path() -> PathBuf {
    home().join(".config/suit/sunshine-web-login")
}

/// The web UI password SUIT set on this PC (None before SUIT set one).
fn web_pass() -> Option<String> {
    read_trim(web_pass_path()).filter(|p| !p.is_empty())
}

/// Give Sunshine's web UI a new random password that only SUIT knows. Sunshine is stopped meanwhile: it keeps
/// the login in memory. Paired tablets stay paired.
fn set_web_login() -> bool {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let pass = super::util::random_password();
    let was_running = unit_running(SUNSHINE_UNIT);
    user_unit("stop");
    let r = run(&["sunshine", "--creds", WEB_USER, &pass], Some(60));
    let path = web_pass_path();
    let saved = r.ok()
        && path.parent().is_some_and(|d| std::fs::create_dir_all(d).is_ok())
        && std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(&path)
            .and_then(|mut f| f.write_all(pass.as_bytes()))
            .is_ok();
    if was_running {
        user_unit("start");
    }
    log::info!("Sunshine web login set: {}", if saved { "ok" } else { "failed" });
    if !r.ok() {
        log::error!("sunshine --creds failed: {}", last_line(&r));
    }
    saved
}

fn user_unit(action: &str) -> Output {
    run(&["systemctl", "--user", action, SUNSHINE_UNIT], Some(60))
}

fn sunshine_dir() -> PathBuf {
    home().join(".config/sunshine")
}

/// Bitrate cap in kbit/s: Sharp or Smooth (weak Wi-Fi).
pub fn bitrate(sharp: bool) -> u32 {
    if sharp { 35000 } else { 15000 }
}

/// SUIT's Sunshine settings: KMS capture (GNOME screen sharing waits for a prompt nobody can click on a
/// tablet), the name Moonlight shows, software x264 tuned for low latency on the LAN, no UPnP, the web UI
/// only on this PC (SUIT pairs through it), and debug logging so the size the tablet asks for shows in the log.
pub fn sunshine_settings(name: &str, ip: &str, sharp: bool) -> Vec<(&'static str, String)> {
    vec![
        ("capture", "kms".into()),
        ("sunshine_name", name.into()),
        ("csrf_allowed_origins", format!("https://{}:47990", ip)),
        ("sw_preset", "ultrafast".into()),
        ("sw_tune", "zerolatency".into()),
        ("max_bitrate", bitrate(sharp).to_string()),
        ("upnp", "disabled".into()),
        ("origin_web_ui_allowed", "pc".into()),
        ("min_log_level", "debug".into()),
    ]
}

/// `existing` sunshine.conf with `set` applied: other lines stay, SUIT's keys get their value once.
pub fn merge_conf(existing: &str, set: &[(&str, String)]) -> String {
    let key = |l: &str| l.split_once('=').map(|(k, _)| k.trim().to_string());
    let mut out: Vec<String> = existing
        .lines()
        .filter(|l| !l.starts_with("# Written by SUIT") && !key(l).is_some_and(|k| set.iter().any(|(s, _)| *s == k)))
        .map(String::from)
        .collect();
    out.insert(0, "# Written by SUIT Wireless Screen".into());
    out.extend(set.iter().map(|(k, v)| format!("{} = {}", k, v)));
    out.join("\n") + "\n"
}

/// The perfect settings (install and "Fix it"): sunshine.conf, and a desktop that never blanks, locks or
/// sleeps (the tablet would only show a black or lock screen). Same keys as SUIT's power toggle.
pub fn apply_settings() -> bool {
    let path = sunshine_dir().join("sunshine.conf");
    let conf = merge_conf(&std::fs::read_to_string(&path).unwrap_or_default(), &sunshine_settings(&pc_name(), &super::remote_desktop::get_local_ip(), load_config().sharp));
    let ok = write_file(&path, &conf).is_ok();
    if !super::power::is_power_and_sleep_optimized() {
        super::power::set_power_and_sleep(true);
    }
    run(&["gsettings", "set", "org.gnome.desktop.screensaver", "lock-enabled", "false"], Some(10));
    ok
}

/// Smooth or Sharp: new bitrate, Sunshine restarts.
pub fn set_quality(sharp: bool) -> (bool, String) {
    let mut cfg = load_config();
    cfg.sharp = sharp;
    save_config(&cfg);
    let ok = apply_settings();
    user_unit("try-restart");
    (ok, tr(if sharp { "Sharp picture." } else { "Smooth picture." }))
}

/// Install and start Sunshine with SUIT's settings; `step` gets a progress line.
pub fn install_sunshine(step: &(dyn Fn(&str) + Sync)) -> (bool, String) {
    let fail = |detail: String| {
        log::error!("Sunshine install failed: {}", detail);
        (false, trf("Installing Sunshine failed: {error}", &[("error", &detail)]))
    };
    step("Adding the Sunshine download source...");
    if !sudo_write(COPR_REPO, COPR_TEXT) {
        return fail(COPR_REPO.into());
    }
    step("Downloading and installing Sunshine. This can take a few minutes...");
    let r = sudo(&["dnf", "install", "-y", "Sunshine"], 1800);
    if !r.ok() {
        return fail(last_line(&r));
    }
    step("Configuring Sunshine...");
    if !apply_settings() {
        return fail(sunshine_dir().join("sunshine.conf").display().to_string());
    }
    user_unit("stop");
    // a reinstall keeps the login SUIT already set
    if web_pass().is_none() && !set_web_login() {
        return fail(tr("Could not set the Sunshine login."));
    }
    step("Opening the network ports...");
    firewall_ports(true);
    if load_config().hide_cursor {
        set_hide_cursor(true);
    }
    step("Starting Sunshine...");
    run(&["systemctl", "--user", "daemon-reload"], Some(30));
    let r = run(&["systemctl", "--user", "enable", "--now", SUNSHINE_UNIT], Some(60));
    if !r.ok() {
        return fail(last_line(&r));
    }
    log::info!("Sunshine installed and started");
    (true, tr("Sunshine is installed and running."))
}

fn api(args: &[&str]) -> serde_json::Value {
    api_wait(10, args)
}

/// Like `api`, with a longer wait: Sunshine answers a wrong PIN only after Moonlight gave up (about 15 s).
fn api_wait(secs: u64, args: &[&str]) -> serde_json::Value {
    // the login goes in on stdin: command lines are readable by every account on the PC
    let auth = format!("user = \"{}:{}\"\n", WEB_USER, web_pass().unwrap_or_else(|| OLD_WEB_PASS.into()));
    let origin = format!("Origin: {}", API);
    let m = secs.to_string();
    let mut a = vec!["curl", "-sk", "-m", m.as_str(), "-K", "-", "-H", &origin];
    a.extend_from_slice(args);
    serde_json::from_str(&super::util::run_with_input(&a, &auth, Some(secs + 5)).stdout).unwrap_or_default()
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum PairOutcome {
    Paired,
    /// Sunshine took the PIN but no new tablet showed up: the PIN did not match.
    WrongPin,
    /// Sunshine refused the PIN: the pairing Moonlight started is gone.
    TimedOut,
    /// No answer from Sunshine.
    NoAnswer,
}

/// What a PIN post did: `posted` is Sunshine's status (None = no answer), `new_tablet` whether a paired
/// tablet was added afterwards.
pub fn pair_outcome(posted: Option<bool>, new_tablet: bool) -> PairOutcome {
    match posted {
        None => PairOutcome::NoAnswer,
        // Sunshine answers {"status":false} when the PIN does not match (checked on 2026.914)
        Some(false) => PairOutcome::WrongPin,
        Some(true) if new_tablet => PairOutcome::Paired,
        Some(true) => PairOutcome::TimedOut,
    }
}

fn paired_count() -> usize {
    read_json(sunshine_dir().join("sunshine_state.json")).and_then(|v| v["root"]["named_devices"].as_array().map(|a| a.len())).unwrap_or(0)
}

/// Send the PIN Moonlight shows to Sunshine (the user types it into SUIT, not into Sunshine's web UI).
/// An empty `name` takes the one Moonlight sent, else "Tablet N".
pub fn pair(pin: &str, name: &str) -> (bool, String) {
    let pc = pc_name();
    let Some((id, asked)) = pending_pairing(&api(&[&format!("{}/api/pin", API)])) else {
        return (false, trf("Moonlight is not asking yet. Tap {name} in Moonlight.", &[("name", &pc)]));
    };
    let before = paired_count();
    let name = match (name.trim(), asked.as_str()) {
        ("", "") if before == 0 => tr("Tablet"),
        ("", "") => format!("{} {}", tr("Tablet"), before + 1),
        ("", a) => a.to_string(),
        (n, _) => n.to_string(),
    };
    let t = api(&[&format!("{}/api/csrf-token", API)]);
    let token = t["csrf_token"].as_str().or_else(|| t["token"].as_str()).unwrap_or_default();
    let body = serde_json::json!({ "pin": pin, "name": name, "pairing_id": id }).to_string();
    let csrf = format!("X-CSRF-Token: {}", token);
    let r = api_wait(40, &["-H", "Content-Type: application/json", "-H", &csrf, "-d", &body, &format!("{}/api/pin", API)]);
    let posted = r.get("status").map(|s| *s == true || *s == "true");
    // Sunshine hands the PIN to Moonlight; the tablet is paired once it shows up in the state file
    let mut added = false;
    if posted == Some(true) {
        for _ in 0..30 {
            if paired_count() > before {
                added = true;
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(500));
        }
    }
    let outcome = pair_outcome(posted, added);
    log::info!("Pairing {}: {:?} ({})", name, outcome, r);
    match outcome {
        PairOutcome::Paired => (true, trf("{name} is paired.", &[("name", &name)])),
        PairOutcome::WrongPin => (false, tr("Wrong PIN. Type the new one Moonlight shows.")),
        PairOutcome::TimedOut => (false, trf("Moonlight stopped waiting. Tap {name} in Moonlight again.", &[("name", &pc)])),
        PairOutcome::NoAnswer => (false, tr("Sunshine did not answer. Try again in a moment.")),
    }
}

/// Undo everything: Sunshine, its settings and ports, the virtual screen, the services and autostarts.
/// Safe to run again (only what is left is removed).
pub fn remove_everything() -> (bool, String) {
    let cfg = load_config();
    let mut failed = vec![];
    user_unit("disable");
    user_unit("stop");
    remove_watcher();
    if run(&["rpm", "-q", "Sunshine"], Some(20)).ok() && !sudo(&["dnf", "remove", "-y", "Sunshine"], 900).ok() {
        failed.push("Sunshine");
    }
    firewall_ports(false);
    // ALL covers the rescue entry too; SUIT adds no boot entries and never changes the default one
    if !remove_boot_args(&cfg) {
        failed.push("grubby");
    }
    let initramfs = std::path::Path::new(EDID_PATH).exists() || std::path::Path::new(DRACUT_CONF).exists();
    if !sudo(&["rm", "-f", COPR_REPO, EDID_PATH, DRACUT_CONF], 20).ok() || (initramfs && !sudo(&["dracut", "-f"], 900).ok()) {
        failed.push("dracut");
    }
    if std::fs::read_to_string(GREETER_LAYOUT).is_ok_and(|t| t.contains(GREETER_MARK)) {
        sudo(&["rm", "-f", GREETER_LAYOUT], 20);
    }
    set_hide_cursor(false);
    let _ = std::fs::remove_dir_all(sunshine_dir());
    let _ = std::fs::remove_file(home().join(".config/suit/cursor-theme.prev"));
    let _ = std::fs::remove_file(web_pass_path());
    // an enable link left behind when the package went first
    for l in glob::glob(&home().join(".config/systemd/user/*/").join(SUNSHINE_UNIT).to_string_lossy()).map(|g| g.flatten().collect::<Vec<_>>()).unwrap_or_default() {
        let _ = std::fs::remove_file(l);
    }
    for p in [autostart(WATCH_AUTOSTART), autostart(RESUME_AUTOSTART), config_path()] {
        let _ = std::fs::remove_file(p);
    }
    if failed.is_empty() {
        log::info!("Wireless Screen removed");
        (true, tr("Wireless Screen removed. Restart to turn the virtual screen off."))
    } else {
        log::error!("Wireless Screen removal incomplete: {:?}", failed);
        (false, trf("Could not remove everything: {parts}", &[("parts", &failed.join(", "))]))
    }
}

// ---------------------------------------------------------------- cursor

const BLANK_THEME: &str = "suit-blank";

/// A 1x1 fully transparent Xcursor file (one 24 px nominal image).
pub fn blank_xcursor() -> Vec<u8> {
    const IMAGE: u32 = 0xfffd0002;
    let words: [u32; 16] = [16, 0x10000, 1, IMAGE, 24, 28, 36, IMAGE, 24, 1, 1, 1, 0, 0, 0, 0];
    let mut v = b"Xcur".to_vec();
    v.extend(words.iter().flat_map(|w| w.to_le_bytes()));
    v
}

fn theme_dir() -> PathBuf {
    home().join(".local/share/icons").join(BLANK_THEME)
}

fn cursor_script() -> PathBuf {
    home().join(".config/suit/wireless-cursor.sh")
}

/// Sunshine runs prep commands without a session bus at times, so the script sets the default one.
const CURSOR_SCRIPT: &str = r#"#!/bin/sh
# Written by SUIT Wireless Screen: hide the cursor while Sunshine streams (hide), restore it after (show)
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"
prev="$HOME/.config/suit/cursor-theme.prev"
if [ "$1" = hide ]; then
    t=$(gsettings get org.gnome.desktop.interface cursor-theme)
    [ "$t" = "'suit-blank'" ] || echo "$t" > "$prev"
    gsettings set org.gnome.desktop.interface cursor-theme suit-blank
elif [ "$(gsettings get org.gnome.desktop.interface cursor-theme)" = "'suit-blank'" ]; then
    gsettings set org.gnome.desktop.interface cursor-theme "$(cat "$prev" 2>/dev/null || echo "'Adwaita'")"
    rm -f "$prev"
fi
"#;

/// Add (`cmd` = Some((do, undo))) or drop SUIT's cursor prep-cmd on Sunshine's Desktop app; other
/// prep commands stay. Creates the Desktop app when missing.
pub fn set_cursor_prep(apps: &mut serde_json::Value, cmd: Option<(&str, &str)>) {
    if !apps["apps"].is_array() {
        apps["apps"] = serde_json::json!([]);
    }
    let list = apps["apps"].as_array_mut().unwrap();
    if !list.iter().any(|a| a["name"] == "Desktop") {
        list.insert(0, serde_json::json!({"name": "Desktop", "image-path": "desktop.png"}));
    }
    let desktop = list.iter_mut().find(|a| a["name"] == "Desktop").unwrap();
    let mut prep: Vec<serde_json::Value> = desktop["prep-cmd"].as_array().cloned().unwrap_or_default();
    prep.retain(|p| !p["do"].as_str().unwrap_or("").contains("wireless-cursor.sh"));
    if let Some((d, u)) = cmd {
        prep.push(serde_json::json!({"do": d, "undo": u}));
    }
    match desktop.as_object_mut() {
        Some(o) if prep.is_empty() => {
            o.remove("prep-cmd");
        }
        Some(o) => {
            o.insert("prep-cmd".into(), prep.into());
        }
        None => {}
    }
}

fn write_blank_theme() -> std::io::Result<()> {
    let dir = theme_dir();
    let cursors = dir.join("cursors");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&cursors)?;
    std::fs::write(dir.join("index.theme"), format!("[Icon Theme]
Name={}
", BLANK_THEME))?;
    std::fs::write(cursors.join("blank"), blank_xcursor())?;
    // every Adwaita cursor name, or GNOME falls back to Adwaita for the missing ones
    for e in std::fs::read_dir("/usr/share/icons/Adwaita/cursors")?.flatten() {
        let name = e.file_name();
        if name != "blank" {
            std::os::unix::fs::symlink("blank", cursors.join(name))?;
        }
    }
    Ok(())
}

/// Switch cursor hiding while streaming on or off (theme, script and Sunshine's Desktop app).
pub fn set_hide_cursor(on: bool) -> (bool, String) {
    let script = cursor_script();
    let apps_path = home().join(".config/sunshine/apps.json");
    let mut apps = read_json(&apps_path).unwrap_or_else(|| serde_json::json!({"env": {}, "apps": []}));
    let result = if on {
        let sh = format!("sh {}", script.display());
        write_blank_theme().and_then(|_| write_file(&script, CURSOR_SCRIPT)).map(|_| set_cursor_prep(&mut apps, Some((&format!("{} hide", sh), &format!("{} show", sh)))))
    } else {
        if script.exists() {
            run(&["sh", &script.to_string_lossy(), "show"], Some(10));
        }
        let _ = std::fs::remove_file(&script);
        let _ = std::fs::remove_dir_all(theme_dir());
        set_cursor_prep(&mut apps, None);
        Ok(())
    };
    let result = result.and_then(|_| write_json(&apps_path, &apps));
    let mut cfg = load_config();
    cfg.hide_cursor = on;
    save_config(&cfg);
    // Sunshine reads apps.json when it starts
    run(&["systemctl", "--user", "try-restart", SUNSHINE_UNIT], Some(60));
    match result {
        Ok(_) => (true, tr(if on { "The cursor is hidden while the tablet streams." } else { "The cursor stays visible while the tablet streams." })),
        Err(e) => {
            log::error!("Cursor hiding setup failed: {}", e);
            (false, trf("Could not change the cursor setting: {error}", &[("error", &e)]))
        }
    }
}

// ---------------------------------------------------------------- layout

fn best_mode(m: &PhysMonitor) -> Option<&Mode> {
    m.modes.iter().find(|x| x.preferred).or(m.current.as_ref()).or(m.modes.first())
}

fn mode_of_size(m: &PhysMonitor, w: i32, h: i32) -> Option<&Mode> {
    m.modes.iter().filter(|x| x.w == w && x.h == h).max_by(|a, b| a.rate.total_cmp(&b.rate))
}

// ---------------------------------------------------------------- login screen

/// System-wide layout Mutter falls back to when a user has none; the GDM greeter has no home of its own, so
/// this is how the login screen gets a real monitor as the main screen.
const GREETER_LAYOUT: &str = "/etc/xdg/monitors.xml";
const GREETER_MARK: &str = "<!-- Written by SUIT Wireless Screen -->";

/// Refresh rate (Hz) Mutter reports for the virtual screen's preferred mode, from the EDID timing.
pub fn edid_rate(w: u32, h: u32, refresh: u32) -> f64 {
    let (d, clock) = cvt_rb_dtd(w, h, refresh);
    let vtotal = h + (d[6] as u32 | ((d[7] as u32 & 0x0f) << 8));
    clock as f64 * 1e4 / ((w + 160) * vtotal) as f64
}

/// The login screen layout: the real monitors side by side (the first is the main screen) and the
/// virtual screen after them. Without it GDM takes the virtual screen as the main one and the real
/// monitor stays black. None without a real monitor.
pub fn greeter_layout(cfg: &Config, real: &[&PhysMonitor]) -> Option<String> {
    let spec = |c: &str, v: &str, p: &str, s: &str| format!("<monitorspec><connector>{}</connector><vendor>{}</vendor><product>{}</product><serial>{}</serial></monitorspec>", c, v, p, s);
    let mut x = 0;
    let mut out = String::new();
    let mut add = |sp: String, w: i32, h: i32, rate: f64| {
        out += &format!(
            "    <logicalmonitor><x>{x}</x><y>0</y><scale>1</scale>{}<monitor>{sp}<mode><width>{w}</width><height>{h}</height><rate>{rate:.3}</rate></mode></monitor></logicalmonitor>\n",
            if x == 0 { "<primary>yes</primary>" } else { "" }
        );
        x += w;
    };
    for m in real {
        let md = best_mode(m)?;
        add(spec(&m.connector, &m.vendor, &m.product, &m.serial), md.w, md.h, md.rate);
    }
    if real.is_empty() {
        return None;
    }
    add(spec(&mutter_name(&cfg.connector), "SUI", "SUIT Screen", "0x00000000"), cfg.width as i32, cfg.height as i32, edid_rate(cfg.width, cfg.height, cfg.refresh));
    Some(format!("{GREETER_MARK}\n<monitors version=\"2\">\n  <configuration>\n    <layoutmode>logical</layoutmode>\n{out}  </configuration>\n</monitors>\n"))
}

/// Keep the login screen layout current (setup and whenever the monitors change). Never touches a
/// layout somebody else wrote.
pub fn update_greeter_layout(cfg: &Config) {
    if cfg.connector.is_empty() {
        return;
    }
    let old = std::fs::read_to_string(GREETER_LAYOUT).ok();
    if old.as_deref().is_some_and(|t| !t.contains(GREETER_MARK)) {
        return;
    }
    let virt = mutter_name(&cfg.connector);
    let new = display::get_current_mutter_state(3).and_then(|st| greeter_layout(cfg, &st.monitors.iter().filter(|m| m.connector != virt).collect::<Vec<_>>()));
    match (new, old) {
        (Some(n), o) if o.as_deref() != Some(&n) => log::info!("Login screen layout written: ok={}", sudo_write(GREETER_LAYOUT, &n)),
        (None, Some(_)) => log::info!("Login screen layout removed: ok={}", sudo(&["rm", "-f", GREETER_LAYOUT], 20).ok()),
        _ => {}
    }
}

/// Mirror every monitor onto the virtual screen (or extend), as `cfg.mirror` says. Does nothing until
/// the virtual screen is running.
pub fn apply_layout(cfg: &Config) -> bool {
    apply_layout_as(cfg, if cfg.mirror { Layout::Mirror } else { Layout::Extend })
}

/// "Switch to tablet now" and back: tablet only, or the layout from the settings.
pub fn switch_layout(cfg: &Config, now: Option<Layout>) -> bool {
    if now == Some(Layout::TabletOnly) { apply_layout(cfg) } else { apply_layout_as(cfg, Layout::TabletOnly) }
}

/// Apply `layout` temporarily (like the watcher does).
pub fn apply_layout_as(cfg: &Config, layout: Layout) -> bool {
    let Some(st) = display::get_current_mutter_state(3) else { return false };
    if layout == Layout::TabletOnly {
        let Some(virt) = st.monitors.iter().find(|m| m.connector == mutter_name(&cfg.connector)) else { return false };
        let Some(md) = mode_of_size(virt, cfg.width as i32, cfg.height as i32).or(best_mode(virt)) else { return false };
        let lms = vec![(0, 0, 1.0, 0, true, vec![(virt.connector.clone(), md.id.clone(), HashMap::new())])];
        let ok = display::apply_monitors_config(st.serial, 1, lms);
        log::info!("Wireless Screen layout: tablet only, ok={}", ok);
        return ok;
    }
    let cfg = &Config { mirror: layout == Layout::Mirror, ..cfg.clone() };
    let Some(virt) = st.monitors.iter().find(|m| m.connector == mutter_name(&cfg.connector)) else { return false };
    let others: Vec<&PhysMonitor> = st.monitors.iter().filter(|m| m.connector != mutter_name(&cfg.connector)).collect();
    let all: Vec<&PhysMonitor> = others.iter().copied().chain([virt]).collect();
    let mut sizes = vec![(cfg.width as i32, cfg.height as i32), (1920, 1080), (1280, 720)];
    sizes.extend(virt.modes.iter().map(|m| (m.w, m.h)));
    let common = sizes.into_iter().find(|(w, h)| all.iter().all(|m| mode_of_size(m, *w, *h).is_some()));
    let lms: Vec<LogicalConfig> = match common.filter(|_| cfg.mirror && !others.is_empty()) {
        Some((w, h)) => {
            let phys = all.iter().filter_map(|m| mode_of_size(m, w, h).map(|x| (m.connector.clone(), x.id.clone(), HashMap::new()))).collect();
            vec![(0, 0, 1.0, 0, true, phys)]
        }
        None => {
            let mut x = 0;
            all.iter()
                .filter_map(|m| {
                    let md = best_mode(m)?;
                    let lm = (x, 0, 1.0, 0, x == 0, vec![(m.connector.clone(), md.id.clone(), HashMap::new())]);
                    x += md.w;
                    Some(lm)
                })
                .collect()
        }
    };
    // temporary (1): a persistent config makes GNOME ask "Keep these display settings?" and revert unanswered
    let ok = display::apply_monitors_config(st.serial, 1, lms);
    log::info!("Wireless Screen layout: {} with {} other monitor(s), ok={}", if common.is_some() && cfg.mirror { "mirror" } else { "extend" }, others.len(), ok);
    ok
}

/// `suit wireless-screen --watch` (session autostart): apply the layout whenever the set of monitors changes.
pub fn watch() {
    let Ok(bus) = gio::bus_get_sync(gio::BusType::Session, gio::Cancellable::NONE) else {
        log::error!("Wireless Screen watcher: no session bus");
        return;
    };
    let last: Rc<RefCell<Vec<String>>> = Rc::default();
    let check = Rc::new(move || {
        let cfg = load_config();
        let Some(st) = display::get_current_mutter_state(15) else { return };
        let mut set: Vec<String> = st.monitors.iter().map(|m| m.connector.clone()).collect();
        set.sort();
        // only on a real change: our own ApplyMonitorsConfig also fires MonitorsChanged, and a layout the
        // user picks by hand stays until the next plug or unplug
        if cfg.connector.is_empty() || *last.borrow() == set {
            return;
        }
        *last.borrow_mut() = set;
        update_greeter_layout(&cfg);
        apply_layout(&cfg);
    });
    check();
    let pending = Rc::new(Cell::new(false));
    let _sub = bus.subscribe_to_signal(
        None,
        Some("org.gnome.Mutter.DisplayConfig"),
        Some("MonitorsChanged"),
        Some("/org/gnome/Mutter/DisplayConfig"),
        None,
        gio::DBusSignalFlags::NONE,
        move |_| {
            if pending.replace(true) {
                return;
            }
            let (p, c) = (pending.clone(), check.clone());
            glib::timeout_add_local_once(std::time::Duration::from_millis(1500), move || {
                p.set(false);
                c();
            });
        },
    );
    glib::MainLoop::new(None, false).run();
}
