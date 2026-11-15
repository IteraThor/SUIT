//! Fast Boot: individually toggleable boot-time tweaks, each recording its changes for an exact revert,
//! plus a firmware (BIOS) analysis with a vendor-specific guide.
use super::i18n::{tr, trf};
use super::util::{home, read_trim, run, run_with_input, Output};
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

const ISCSI_NODES_DIR: &str = "/var/lib/iscsi/nodes";
const FSTAB_PATH: &str = "/etc/fstab";
const DMI_DIR: &str = "/sys/class/dmi/id";
const EFI_DIR: &str = "/sys/firmware/efi";
const DRACUT_CONF: &str = "/etc/dracut.conf.d/90-suit-fast-boot.conf";
/// The graphics driver only loads in the initrd so the splash can draw; without these the disk mounts sooner.
const DRACUT_CONF_TEXT: &str = "# Written by SUIT Fast Boot\nomit_dracutmodules+=\" plymouth drm \"\n";
/// Only this machine's drivers, and none of the modules a plain unencrypted disk never needs.
const SLIM_CONF: &str = "/etc/dracut.conf.d/91-suit-slim-initrd.conf";
const SLIM_OMIT: [&str; 7] = ["i18n", "memstrack", "prefixdevname", "systemd-battery-check", "systemd-pcrphase", "tpm2-tss", "crypt"];
const FIPS_MODULES: [&str; 4] = ["fips", "fips-crypto-policies", "nss-softokn", "openssl"];
const USER_SESSIONS_UNIT: &str = "systemd-user-sessions.service";
const USER_SESSIONS_SRC: &str = "/usr/lib/systemd/system/systemd-user-sessions.service";
const USER_SESSIONS_OVERRIDE: &str = "/etc/systemd/system/systemd-user-sessions.service";
const GS_PLUGIN_DIRS: [&str; 2] = ["/usr/lib64/gnome-software", "/usr/lib/gnome-software"];

fn state_path() -> PathBuf {
    home().join(".config/suit/fast_boot.json")
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Units,
    Grub,
    Dracut,
    UnitOverride,
}

#[derive(Debug, Clone)]
pub struct Tweak {
    pub id: &'static str,
    pub title: &'static str,
    pub summary: &'static str,
    pub kind: Kind,
    /// disabled, only if enabled now
    pub system_units: &'static [&'static str],
    /// static or D-Bus activated, so only a mask stops them
    pub masked_units: &'static [&'static str],
    /// masked for the user session
    pub user_units: &'static [&'static str],
    /// measured for the estimate besides system_units
    pub blame_units: &'static [&'static str],
    pub default_s: f64,
    pub blocks_login: bool,
    pub recommended: bool,
    pub warning: &'static str,
}

const fn t(id: &'static str, title: &'static str, summary: &'static str) -> Tweak {
    Tweak {
        id,
        title,
        summary,
        kind: Kind::Units,
        system_units: &[],
        masked_units: &[],
        user_units: &[],
        blame_units: &[],
        default_s: 0.2,
        blocks_login: false,
        recommended: true,
        warning: "",
    }
}

/// NetworkManager-wait-online holds GDM (and so auto-login) until DHCP finishes; Autodarts copes with the
/// network coming up after it starts. Only units that actually start on a normal boot are listed.
pub static TWEAKS: &[Tweak] = &[
    Tweak { system_units: &["NetworkManager-wait-online.service"], default_s: 3.0, blocks_login: true,
        ..t("network_wait", "Skip Network Wait", "Start the desktop without waiting for the network to connect.") },
    Tweak { system_units: &["nfs-client.target", "remote-fs.target"], default_s: 0.3, blocks_login: true,
        ..t("remote_fs", "Skip Network Drives", "Don't wait for NFS network drives before login.") },
    Tweak { system_units: &["iscsi-starter.service", "iscsi-onboot.service", "iscsid.socket", "iscsiuio.socket"], default_s: 0.3, blocks_login: true,
        ..t("iscsi", "Skip iSCSI Storage", "Stop network storage services that nothing here uses.") },
    Tweak { kind: Kind::UnitOverride, blame_units: &["NetworkManager.service"], default_s: 1.2, blocks_login: true, recommended: false,
        warning: "Replaces a system file. Turn off before upgrading to a new Fedora release.",
        ..t("login_network", "Login Before Network", "Start the desktop as soon as the system is up instead of after NetworkManager has started.") },
    Tweak { kind: Kind::Dracut, blame_units: &["dracut-initqueue.service"], default_s: 1.5, blocks_login: true, recommended: false,
        warning: "Rebuilds the boot image. If this disk is moved to another PC, turn it off first.",
        ..t("slim_initrd", "Slim Boot Image", "Only this PC's drivers in the early boot image, and no waiting for USB devices before the disk mounts.") },
    Tweak { kind: Kind::Grub, default_s: 1.0, blocks_login: true,
        ..t("boot_menu", "Hide Boot Menu", "Skip the boot menu countdown. Hold Shift during boot to get it back.") },
    Tweak { system_units: &["abrtd.service", "abrt-journal-core.service", "abrt-oops.service", "abrt-vmcore.service", "abrt-xorg.service",
        "ModemManager.service", "cups.socket", "cups.path", "plocate-updatedb.timer", "dnf-makecache.timer"],
        ..t("unused_services", "Unused Background Services", "Crash reporter, modem manager, printing, file search database and package cache refresh.") },
    Tweak { user_units: &["localsearch-3.service"], default_s: 0.5,
        ..t("file_indexer", "File Indexer", "Stop desktop file indexing, which competes with Autodarts for disk and CPU.") },
    Tweak { system_units: &["smartd.service", "mdmonitor.service", "mcelog.service", "atd.service"],
        ..t("maintenance", "Maintenance Daemons", "Disk health (smartd), RAID monitor, CPU error log and the at job scheduler.") },
    Tweak { system_units: &["rsyslog.service"], default_s: 0.5,
        ..t("rsyslog", "Duplicate System Log", "Turn off rsyslog. The system journal keeps every log anyway.") },
    Tweak { masked_units: &["plymouth-quit-wait.service"], default_s: 0.3,
        ..t("splash_wait", "Don't Wait for Boot Splash", "Stop the boot from waiting for the logo animation to end.") },
    Tweak { masked_units: &["packagekit.service"], default_s: 0.3,
        warning: "GNOME Software can no longer install or update apps. SUIT and dnf still can.",
        ..t("software_updates", "Background Software Updates", "PackageKit stops checking for updates after every login.") },
    // Masking the user unit also stops the app itself: its launcher is D-Bus activated through this unit
    Tweak { user_units: &["gnome-software.service"], default_s: 0.5, recommended: false,
        warning: "The GNOME Software app can't be opened while this is on. Turn this off again to use it.",
        ..t("gnome_software", "GNOME Software App", "Stop GNOME Software (and the firmware updater it pulls in) from starting in the background after login.") },
    Tweak { system_units: &["pcscd.socket", "sssd-kcm.socket"],
        user_units: &["evolution-alarm-notify.service", "org.gnome.SettingsDaemon.Smartcard.service", "org.gnome.SettingsDaemon.Wwan.service",
            "org.gnome.SettingsDaemon.PrintNotifications.service", "org.gnome.SettingsDaemon.UsbProtection.service"],
        default_s: 0.5,
        ..t("desktop_helpers", "Unused Desktop Helpers", "Calendar reminders, smart card, mobile broadband, print notices, USB protection and the Kerberos cache.") },
    Tweak { system_units: &["lvm2-monitor.service", "dm-event.socket", "lvm2-lvmpolld.socket", "raid-check.timer"], default_s: 0.1, blocks_login: true,
        ..t("storage_monitors", "LVM and RAID Monitors", "Volume manager monitors and the weekly RAID check, for disks that use neither.") },
    Tweak { kind: Kind::Dracut, default_s: 1.5, blocks_login: true, recommended: false,
        warning: "Rebuilds the boot image (about a minute). The screen stays black or shows text until login.",
        ..t("splash", "Skip Boot Splash", "Leave the logo animation and graphics driver out of the early boot image so the disk mounts sooner.") },
    Tweak { system_units: &["bluetooth.service", "avahi-daemon.service", "avahi-daemon.socket"], recommended: false,
        warning: "Bluetooth keyboards, controllers and speakers stop working.",
        ..t("bluetooth", "Bluetooth and Network Discovery", "Turn off Bluetooth and Avahi device discovery.") },
    Tweak { system_units: &["firewalld.service"], default_s: 1.5, blocks_login: true, recommended: false,
        warning: "Less secure. Only on a trusted home network behind a router.",
        ..t("firewall", "Firewall", "The network only starts after the firewall is up, and login waits for the network.") },
    Tweak { system_units: &["thermald.service"], default_s: 0.5, recommended: false,
        warning: "Fanless mini PCs and laptops may run hotter before they throttle.",
        ..t("thermald", "Intel Thermal Daemon", "Desktop PCs are cooled by the BIOS fan control. thermald mostly matters on laptops.") },
];

pub fn tweak(id: &str) -> Option<&'static Tweak> {
    TWEAKS.iter().find(|t| t.id == id)
}

/// Key that opens firmware setup at power-on, by DMI vendor
const SETUP_KEYS: [(&str, &str); 16] = [
    ("hp", "F10"), ("hewlett-packard", "F10"), ("dell", "F2"), ("lenovo", "F1 or F2"), ("asus", "Del or F2"),
    ("asustek", "Del or F2"), ("msi", "Del"), ("micro-star", "Del"), ("gigabyte", "Del"), ("asrock", "F2 or Del"),
    ("intel", "F2"), ("acer", "F2"), ("fujitsu", "F2"), ("medion", "F2"), ("beelink", "Del"), ("minisforum", "Del"),
];

/// Generic BIOS guide; vendors with a known menu layout override the "where" hints
const BIOS_STEPS: [(&str, &str, &str); 9] = [
    ("fast_boot", "Turn on Fast Boot", "Skips most hardware checks at power-on. Often called Fast Boot or Quick Boot."),
    ("pxe", "Turn off Network (PXE) Boot", "Stops the firmware from preparing to boot over the network."),
    ("order", "Put Fedora first in the boot order", "The firmware then goes straight to the disk instead of trying USB or network first."),
    ("delay", "Set boot and POST delays to 0", "Some boards wait a few seconds for a key press before booting."),
    ("legacy", "Turn off Legacy / CSM support", "Pure UEFI mode skips loading old option ROMs."),
    ("logo", "Turn off the full-screen logo", "Often called Quiet Boot or Full Screen Logo."),
    ("devices", "Turn off unused onboard devices", "Serial ports, card readers or a second network card each add a little start-up time."),
    ("usb_boot", "Turn off booting from USB", "The firmware then stops checking every USB device (cameras, hubs, touchscreen) for something to boot."),
    ("amt", "Turn off Intel AMT remote management", "Business PCs with Intel vPro start the management engine's network features at every power-on."),
];

/// HP's menu locations; None = this vendor has no such option.
fn hp_hint(key: &str) -> Option<Option<&'static str>> {
    Some(match key {
        "fast_boot" => Some("Advanced > Boot Options > Fast Boot."),
        "pxe" => Some("Advanced > Boot Options > untick Network (PXE) Boot."),
        "order" => Some("Advanced > Boot Options > UEFI Boot Sources: move the Fedora entry to the top."),
        "delay" => Some("Advanced > Boot Options > POST Delay and Startup Menu Delay to 0."),
        "legacy" => Some("Advanced > Secure Boot Configuration > Legacy Support Disable."),
        "logo" => None,
        "devices" => Some("Advanced > Built-In Device Options and Port Options."),
        "usb_boot" => Some("Advanced > Boot Options > untick USB Storage Boot."),
        "amt" => Some("Advanced > Remote Management Options > Active Management (AMT) off."),
        _ => return None,
    })
}

fn r(cmd: &[&str]) -> Output {
    run(cmd, Some(600))
}

fn unit_state(unit: &str, user: bool) -> String {
    let mut a = vec!["systemctl"];
    if user {
        a.push("--user");
    }
    a.extend(["is-enabled", unit]);
    let s = run(&a, Some(10)).stdout.trim().to_string();
    if s.is_empty() { "not-found".into() } else { s }
}

fn iscsi_in_use() -> bool {
    match std::fs::read_dir(ISCSI_NODES_DIR) {
        Ok(mut d) => d.next().is_some(),
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => true, // can't tell, so treat it as in use
        Err(_) => false,
    }
}

fn network_mounts_in_fstab() -> bool {
    let Ok(c) = std::fs::read_to_string(FSTAB_PATH) else { return true };
    c.lines().any(|l| {
        let f: Vec<&str> = l.split_whitespace().collect();
        f.len() >= 4 && !f[0].starts_with('#') && (["nfs", "cifs", "smb"].iter().any(|p| f[2].starts_with(p)) || f[3].contains("_netdev"))
    })
}

fn lsblk_types() -> Option<Vec<String>> {
    let o = r(&["lsblk", "-rno", "TYPE"]);
    if o.code < 0 {
        return None;
    }
    Some(o.stdout.split_whitespace().map(String::from).collect())
}

fn disk_encrypted() -> bool {
    lsblk_types().map(|t| t.iter().any(|x| x == "crypt")).unwrap_or(true)
}

fn lvm_or_raid_in_use() -> bool {
    lsblk_types().map(|t| t.iter().any(|x| x == "lvm" || x.starts_with("raid") || x.starts_with("md"))).unwrap_or(true)
}

/// True if any mounted btrfs spans several disks; those need the btrfs dracut module to assemble.
fn btrfs_multi_device() -> bool {
    let Ok(rd) = std::fs::read_dir("/sys/fs/btrfs") else { return false };
    rd.flatten().any(|fs| std::fs::read_dir(fs.path().join("devices")).map(|d| d.count() > 1).unwrap_or(false))
}

fn fips_on() -> bool {
    read_trim("/proc/sys/crypto/fips_enabled").as_deref() == Some("1")
        || std::fs::read_to_string("/proc/cmdline").map(|c| c.split_whitespace().any(|w| w == "fips=1")).unwrap_or(false)
}

fn slim_text() -> String {
    let mut omit: Vec<&str> = SLIM_OMIT.to_vec();
    if !fips_on() {
        omit.extend(FIPS_MODULES);
    }
    if !btrfs_multi_device() {
        omit.push("btrfs");
    }
    format!("# Written by SUIT Fast Boot\nhostonly=\"yes\"\nhostonly_mode=\"strict\"\nomit_dracutmodules+=\" {} \"\n", omit.join(" "))
}

/// The packaged unit without network.target in After=, or None if there is nothing to drop.
pub fn user_sessions_text() -> Option<String> {
    let text = std::fs::read_to_string(USER_SESSIONS_SRC).ok()?;
    let mut changed = false;
    let lines: Vec<String> = text
        .lines()
        .map(|l| {
            if let Some(rest) = l.strip_prefix("After=") {
                if rest.split_whitespace().any(|u| u == "network.target") {
                    changed = true;
                    return format!("After={}", rest.split_whitespace().filter(|u| *u != "network.target").collect::<Vec<_>>().join(" "));
                }
            }
            l.to_string()
        })
        .collect();
    if !changed {
        return None;
    }
    Some(format!("# Written by SUIT Fast Boot: login no longer waits for network.target\n{}\n", lines.join("\n")))
}

fn gnome_software_uses_packagekit() -> bool {
    let plugins: Vec<String> = GS_PLUGIN_DIRS
        .iter()
        .flat_map(|d| glob::glob(&format!("{}/plugins-*/libgs_plugin_*.so", d)).map(|g| g.flatten().collect::<Vec<_>>()).unwrap_or_default())
        .filter_map(|p| p.file_name().map(|n| n.to_string_lossy().to_string()))
        .collect();
    // No plugins found: can't tell what starts PackageKit, so keep the tweak
    plugins.is_empty() || plugins.iter().any(|p| p == "libgs_plugin_packagekit.so")
}

fn dracut_target(t: &Tweak) -> (&'static str, String) {
    if t.id == "slim_initrd" { (SLIM_CONF, slim_text()) } else { (DRACUT_CONF, DRACUT_CONF_TEXT.into()) }
}

fn grub_fastboot_set() -> bool {
    r(&["sudo", "-n", "grub2-editenv", "list"]).stdout.lines().any(|l| l.trim() == "fastboot=1")
}

// --- state ---

fn units(e: &Value, k: &str) -> Vec<String> {
    e[k].as_array().map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect()).unwrap_or_default()
}

fn load_state() -> Map<String, Value> {
    let st = match std::fs::read_to_string(state_path()) {
        Err(_) => return Map::new(),
        Ok(s) => match serde_json::from_str::<Value>(&s) {
            Ok(v) => v,
            Err(_) => {
                log::error!("Corrupt Fast Boot state file: {}", state_path().display());
                return Map::new();
            }
        },
    };
    let mut tweaks = if st.get("tweaks").is_some() { st["tweaks"].as_object().cloned().unwrap_or_default() } else { migrate_v1(&st) };
    // software_updates used to mask gnome-software.service too; that mask now has its own switch
    if let Some(old) = tweaks.get_mut("software_updates") {
        let mut uu = units(old, "user_units");
        if uu.iter().any(|u| u == "gnome-software.service") && !tweaks.contains_key("gnome_software") {
            uu.retain(|u| u != "gnome-software.service");
            tweaks.get_mut("software_updates").unwrap()["user_units"] = json!(uu);
            tweaks.insert(
                "gnome_software".into(),
                json!({"user_units": ["gnome-software.service"], "saved_s": tweak("gnome_software").unwrap().default_s}),
            );
        }
    }
    tweaks
}

/// The single Fast Boot switch stored flat lists; spread them over the tweaks they belong to.
fn migrate_v1(old: &Value) -> Map<String, Value> {
    let mut tw: Map<String, Value> = Map::new();
    for (key, sel) in [("system_units", 0), ("user_units", 1)] {
        for u in units(old, key) {
            if let Some(owner) = TWEAKS.iter().find(|t| if sel == 0 { t.system_units.contains(&u.as_str()) } else { t.user_units.contains(&u.as_str()) }) {
                let e = tw.entry(owner.id.to_string()).or_insert_with(|| json!({}));
                let mut l = units(e, key);
                l.push(u);
                e[key] = json!(l);
            }
        }
    }
    if old["grub_fastboot"].as_bool().unwrap_or(false) {
        tw.insert("boot_menu".into(), json!({"grub_fastboot": true}));
    }
    tw
}

fn save_state(tw: &Map<String, Value>) {
    if tw.is_empty() {
        let _ = std::fs::remove_file(state_path());
        return;
    }
    let _ = super::util::write_file(state_path(), &serde_json::to_string_pretty(&json!({"tweaks": tw})).unwrap());
}

pub fn applied_count() -> usize {
    load_state().len()
}

pub fn is_fast_boot_enabled() -> bool {
    applied_count() > 0
}

/// (applied, total) Fast Boot tweaks, cheap enough for the System page row.
pub fn get_boot_tweak_counts() -> (usize, usize) {
    (applied_count(), TWEAKS.len())
}

// --- measurements ---

/// '1min 2.5s', '743ms' or '3.2s' -> seconds.
fn seconds(text: &str) -> f64 {
    let re = regex::Regex::new(r"([\d.]+)\s*(min|ms|s)\b").unwrap();
    re.captures_iter(text)
        .map(|c| c[1].parse::<f64>().unwrap_or(0.0) * match &c[2] { "min" => 60.0, "ms" => 0.001, _ => 1.0 })
        .sum()
}

/// Per-phase times of the last boot from systemd-analyze, e.g. {firmware: 12.4, ..., total: 33.3}.
pub fn get_boot_report() -> Option<HashMap<String, f64>> {
    let out = r(&["systemd-analyze"]).stdout;
    let line = out.lines().find(|l| l.starts_with("Startup finished in"))?;
    let (parts, total) = line["Startup finished in".len()..].rsplit_once('=')?;
    let re = regex::Regex::new(r"^\s*(.+?)\s*\((\w+)\)").unwrap();
    let mut rep = HashMap::new();
    for chunk in parts.split('+') {
        if let Some(c) = re.captures(chunk) {
            rep.insert(c[2].to_string(), seconds(&c[1]));
        }
    }
    rep.insert("total".into(), seconds(total));
    Some(rep)
}

pub fn get_last_boot_time() -> Option<f64> {
    get_boot_report().and_then(|r| r.get("total").copied())
}

fn blame() -> HashMap<String, f64> {
    let re = regex::Regex::new(r"^\s*(.+?)\s+(\S+\.(?:service|socket|timer|target|path|mount))\s*$").unwrap();
    r(&["systemd-analyze", "blame"])
        .stdout
        .lines()
        .filter_map(|l| re.captures(l).map(|c| (c[2].to_string(), seconds(&c[1]))))
        .collect()
}

fn estimate(t: &Tweak, b: &HashMap<String, f64>) -> f64 {
    let m: f64 = t.system_units.iter().chain(t.blame_units).map(|u| b.get(*u).copied().unwrap_or(0.0)).sum();
    if m >= 0.1 { (m * 10.0).round() / 10.0 } else { t.default_s }
}

// --- tweaks ---

fn unavailable_reason(t: &Tweak) -> &'static str {
    match t.id {
        "iscsi" if iscsi_in_use() => return "iSCSI storage is set up on this machine.",
        "remote_fs" if network_mounts_in_fstab() => return "Network drives are listed in /etc/fstab.",
        "splash" if disk_encrypted() => return "Disk encryption needs the boot splash for its password prompt.",
        "slim_initrd" if disk_encrypted() => return "Disk encryption needs the full boot image.",
        "storage_monitors" if lvm_or_raid_in_use() => return "LVM or RAID storage is set up on this machine.",
        "software_updates" if !gnome_software_uses_packagekit() => {
            return "GNOME Software uses dnf5 here, so PackageKit doesn't start after login."
        }
        _ => {}
    }
    match t.kind {
        Kind::UnitOverride => {
            if Path::new(USER_SESSIONS_OVERRIDE).exists() {
                return "Already active on this system.";
            }
            if user_sessions_text().is_none() {
                return "Already off on this system.";
            }
        }
        Kind::Grub if grub_fastboot_set() => return "Already active on this system.",
        Kind::Units => {
            let any = t.system_units.iter().any(|u| unit_state(u, false) == "enabled")
                || t.masked_units.iter().any(|u| !matches!(unit_state(u, false).as_str(), "masked" | "not-found"))
                || t.user_units.iter().any(|u| !matches!(unit_state(u, true).as_str(), "masked" | "not-found"));
            if !any {
                return "Already off on this system.";
            }
        }
        _ => {}
    }
    ""
}

fn exists_root(p: &str) -> bool {
    r(&["test", "-e", p]).code == 0
}

/// Drop recorded changes that were undone outside SUIT; None when nothing is left.
fn still_applied(t: &Tweak, e: &Value) -> Option<Value> {
    let mut kept = json!({});
    if let Some(s) = e.get("saved_s") {
        kept["saved_s"] = s.clone();
    }
    // A failed is-enabled reads as not-found or unknown: keep the record rather than lose track of a change
    let keep = |s: &str| matches!(s, "masked" | "not-found" | "unknown");
    let sys: Vec<String> = units(e, "system_units").into_iter().filter(|u| unit_state(u, false) != "enabled").collect();
    let msk: Vec<String> = units(e, "masked_units").into_iter().filter(|u| keep(&unit_state(u, false))).collect();
    let usr: Vec<String> = units(e, "user_units").into_iter().filter(|u| keep(&unit_state(u, true))).collect();
    for (k, v) in [("system_units", sys), ("masked_units", msk), ("user_units", usr)] {
        if !v.is_empty() {
            kept[k] = json!(v);
        }
    }
    if e["grub_fastboot"].as_bool().unwrap_or(false) {
        kept["grub_fastboot"] = json!(true); // grubenv needs root to read; revert copes with it being unset
    }
    if e["dracut_conf"].as_bool().unwrap_or(false) && exists_root(dracut_target(t).0) {
        kept["dracut_conf"] = json!(true);
    }
    if e["unit_override"].as_bool().unwrap_or(false) && exists_root(USER_SESSIONS_OVERRIDE) {
        kept["unit_override"] = json!(true);
    }
    if kept.as_object().unwrap().keys().any(|k| k != "saved_s") { Some(kept) } else { None }
}

fn reconcile(tw: &mut Map<String, Value>, ids: &[String]) -> bool {
    let mut changed = false;
    for id in ids {
        let (Some(e), Some(t)) = (tw.get(id).cloned(), tweak(id)) else { continue };
        let kept = still_applied(t, &e);
        if kept.as_ref() != Some(&e) {
            changed = true;
            log::info!("Fast Boot tweak {} was partly undone outside SUIT, now: {:?}", id, kept);
            match kept {
                None => {
                    tw.remove(id);
                }
                Some(k) => {
                    tw.insert(id.clone(), k);
                }
            }
        }
    }
    changed
}

#[derive(Debug, Clone)]
pub struct TweakRow {
    pub id: String,
    pub title: String,
    pub summary: String,
    pub warning: String,
    pub recommended: bool,
    pub blocks_login: bool,
    pub applied: bool,
    pub unavailable: String,
    pub estimate_s: f64,
}

pub fn get_tweaks() -> Vec<TweakRow> {
    let mut tw = load_state();
    let ids: Vec<String> = tw.keys().cloned().collect();
    if reconcile(&mut tw, &ids) {
        save_state(&tw);
    }
    let b = blame();
    TWEAKS
        .iter()
        .map(|t| {
            let e = tw.get(t.id);
            TweakRow {
                id: t.id.into(),
                title: t.title.into(),
                summary: t.summary.into(),
                warning: t.warning.into(),
                recommended: t.recommended,
                blocks_login: t.blocks_login,
                applied: e.is_some(),
                unavailable: if e.is_some() { String::new() } else { unavailable_reason(t).into() },
                estimate_s: match e {
                    Some(e) => e["saved_s"].as_f64().unwrap_or(t.default_s),
                    None => estimate(t, &b),
                },
            }
        })
        .collect()
}

fn push(e: &mut Value, k: &str, u: &str) {
    let mut l = units(e, k);
    l.push(u.into());
    e[k] = json!(l);
}

/// Apply what is not applied yet, record it in entry, and return what failed.
fn apply(t: &Tweak, e: &mut Value) -> Vec<String> {
    let mut failed = Vec::new();
    // Only touch what is enabled now, so revert never enables something the user had off
    for u in t.system_units {
        if units(e, "system_units").iter().any(|x| x == u) || unit_state(u, false) != "enabled" {
            continue;
        }
        if r(&["sudo", "-n", "systemctl", "disable", u]).code == 0 { push(e, "system_units", u) } else { failed.push(u.to_string()) }
    }
    for u in t.masked_units {
        if units(e, "masked_units").iter().any(|x| x == u) || matches!(unit_state(u, false).as_str(), "masked" | "not-found") {
            continue;
        }
        if r(&["sudo", "-n", "systemctl", "mask", u]).code == 0 { push(e, "masked_units", u) } else { failed.push(u.to_string()) }
    }
    for u in t.user_units {
        if units(e, "user_units").iter().any(|x| x == u) || matches!(unit_state(u, true).as_str(), "masked" | "not-found") {
            continue;
        }
        if r(&["systemctl", "--user", "mask", u]).code == 0 { push(e, "user_units", u) } else { failed.push(u.to_string()) }
    }
    if t.kind == Kind::Grub && !e["grub_fastboot"].as_bool().unwrap_or(false) && !grub_fastboot_set() {
        if r(&["sudo", "-n", "grub2-editenv", "-", "set", "fastboot=1"]).code == 0 {
            e["grub_fastboot"] = json!(true);
        } else {
            failed.push("boot menu timeout".into());
        }
    }
    if t.kind == Kind::Dracut && !e["dracut_conf"].as_bool().unwrap_or(false) {
        let (conf, text) = dracut_target(t);
        let wrote = run_with_input(&["sudo", "-n", "tee", conf], &text, Some(20)).code == 0;
        if wrote && r(&["sudo", "-n", "dracut", "-f"]).code == 0 {
            e["dracut_conf"] = json!(true);
        } else {
            r(&["sudo", "-n", "rm", "-f", conf]);
            failed.push("boot image rebuild".into());
        }
    }
    if t.kind == Kind::UnitOverride && !e["unit_override"].as_bool().unwrap_or(false) {
        let wrote = user_sessions_text().map(|tx| run_with_input(&["sudo", "-n", "tee", USER_SESSIONS_OVERRIDE], &tx, Some(20)).code == 0).unwrap_or(false);
        if wrote && r(&["sudo", "-n", "systemctl", "daemon-reload"]).code == 0 {
            e["unit_override"] = json!(true);
        } else {
            r(&["sudo", "-n", "rm", "-f", USER_SESSIONS_OVERRIDE]);
            failed.push(USER_SESSIONS_UNIT.into());
        }
    }
    failed
}

/// Undo what entry recorded; return what is still applied (empty when fully reverted).
fn revert(t: &Tweak, e: &Value) -> Map<String, Value> {
    let mut rem = json!({});
    for u in units(e, "system_units") {
        if r(&["sudo", "-n", "systemctl", "enable", &u]).code != 0 {
            push(&mut rem, "system_units", &u);
        }
    }
    for u in units(e, "masked_units") {
        if r(&["sudo", "-n", "systemctl", "unmask", &u]).code != 0 {
            push(&mut rem, "masked_units", &u);
        }
    }
    for u in units(e, "user_units") {
        if r(&["systemctl", "--user", "unmask", &u]).code != 0 {
            push(&mut rem, "user_units", &u);
        }
    }
    if e["grub_fastboot"].as_bool().unwrap_or(false) && r(&["sudo", "-n", "grub2-editenv", "-", "unset", "fastboot"]).code != 0 {
        rem["grub_fastboot"] = json!(true);
    }
    if e["dracut_conf"].as_bool().unwrap_or(false) {
        let conf = if t.id == "slim_initrd" { SLIM_CONF } else { DRACUT_CONF };
        let removed = r(&["sudo", "-n", "rm", "-f", conf]).code == 0;
        if !(removed && r(&["sudo", "-n", "dracut", "-f"]).code == 0) {
            rem["dracut_conf"] = json!(true);
        }
    }
    if e["unit_override"].as_bool().unwrap_or(false) {
        let removed = r(&["sudo", "-n", "rm", "-f", USER_SESSIONS_OVERRIDE]).code == 0;
        if !(removed && r(&["sudo", "-n", "systemctl", "daemon-reload"]).code == 0) {
            rem["unit_override"] = json!(true);
        }
    }
    rem.as_object().cloned().unwrap_or_default()
}

pub fn set_tweak(id: &str, enabled: bool) -> (bool, String) {
    let Some(t) = tweak(id) else { return (false, format!("unknown tweak {}", id)) };
    let title = tr(t.title);
    let mut tw = load_state();
    if reconcile(&mut tw, &[id.to_string()]) {
        save_state(&tw);
    }
    if enabled {
        let est = estimate(t, &blame());
        let mut e = tw.get(id).cloned().unwrap_or_else(|| json!({}));
        let failed = apply(t, &mut e);
        if e.get("saved_s").is_none() {
            e["saved_s"] = json!(est);
        }
        tw.insert(id.into(), e.clone());
        save_state(&tw);
        log::info!("Fast Boot tweak {} applied: {}, failed: {:?}", id, e, failed);
        if !failed.is_empty() {
            return (false, trf("{title} partly applied, failed: {failed}", &[("title", &title), ("failed", &failed.join(", "))]));
        }
        return (true, trf("{title} on. Takes effect on next boot.", &[("title", &title)]));
    }
    let Some(e) = tw.get(id).cloned() else { return (true, trf("{title} already off.", &[("title", &title)])) };
    let mut rem = revert(t, &e);
    if !rem.is_empty() {
        // Keep what is still applied so the switch stays on and a retry finishes the job
        rem.insert("saved_s".into(), json!(e["saved_s"].as_f64().unwrap_or(t.default_s)));
        tw.insert(id.into(), Value::Object(rem.clone()));
        save_state(&tw);
        log::error!("Fast Boot tweak {} revert incomplete: {:?}", id, rem);
        return (false, trf("Could not fully revert {title}. Try again.", &[("title", &title)]));
    }
    tw.remove(id);
    save_state(&tw);
    log::info!("Fast Boot tweak {} reverted", id);
    (true, trf("{title} off. Original setting restored.", &[("title", &title)]))
}

/// On: apply every recommended tweak that is available. Off: revert everything applied.
pub fn set_fast_boot(enabled: bool) -> (bool, String) {
    let ids: Vec<String> = if enabled {
        get_tweaks().into_iter().filter(|t| t.recommended && !t.applied && t.unavailable.is_empty()).map(|t| t.id).collect()
    } else {
        load_state().keys().cloned().collect()
    };
    let failed: Vec<&str> = ids.iter().filter(|i| !set_tweak(i, enabled).0).map(|i| tweak(i).unwrap().title).collect();
    if !failed.is_empty() {
        return (false, trf("Some tweaks failed: {failed}", &[("failed", &failed.join(", "))]));
    }
    if enabled {
        (true, "Recommended tweaks on. Takes effect on next boot.".into())
    } else {
        (true, "All Fast Boot tweaks reverted.".into())
    }
}

// --- firmware ---

#[derive(Debug, Default)]
struct EfiInfo {
    current: Option<String>,
    order: Vec<String>,
    timeout: i64,
    entries: HashMap<String, (String, bool)>,
}

fn efi_boot_entries() -> EfiInfo {
    let mut i = EfiInfo::default();
    let re = regex::Regex::new(r"^Boot([0-9A-Fa-f]{4})(\*?)\s+(.*?)(?:\t|\s{2,}|$)").unwrap();
    for line in r(&["efibootmgr"]).stdout.lines() {
        if let Some(v) = line.strip_prefix("BootCurrent:") {
            i.current = Some(v.trim().into());
        } else if let Some(v) = line.strip_prefix("BootOrder:") {
            i.order = v.split(',').map(|x| x.trim().to_string()).filter(|x| !x.is_empty()).collect();
        } else if line.starts_with("Timeout:") {
            i.timeout = regex::Regex::new(r"\d+").unwrap().find(line).and_then(|m| m.as_str().parse().ok()).unwrap_or(0);
        } else if let Some(c) = re.captures(line) {
            i.entries.insert(c[1].to_string(), (c[3].trim().to_string(), &c[2] == "*"));
        }
    }
    i
}

fn dmi(field: &str) -> String {
    read_trim(Path::new(DMI_DIR).join(field)).unwrap_or_default()
}

#[derive(Debug, Clone)]
pub struct BiosStep {
    pub title: String,
    pub detail: String,
    pub r#where: String,
}

#[derive(Debug, Clone)]
pub struct FirmwareAnalysis {
    pub firmware_s: Option<f64>,
    /// unknown | good | ok | slow
    pub rating: String,
    pub vendor: String,
    pub model: String,
    pub setup_key: String,
    pub is_efi: bool,
    pub findings: Vec<String>,
    pub steps: Vec<BiosStep>,
}

/// How long the firmware took, what this tool can see that slows it, and a BIOS guide for this board.
pub fn analyze_firmware(report: Option<&HashMap<String, f64>>) -> FirmwareAnalysis {
    let own;
    let rep = match report {
        Some(r) => Some(r),
        None => {
            own = get_boot_report();
            own.as_ref()
        }
    };
    let fw = rep.and_then(|r| r.get("firmware").copied());
    let (vendor, model) = (dmi("sys_vendor"), dmi("product_name"));
    let vkey = SETUP_KEYS.iter().find(|(k, _)| vendor.to_lowercase().starts_with(k)).map(|(k, _)| *k).unwrap_or("");
    let is_hp = vkey == "hp" || vkey == "hewlett-packard";
    let is_efi = Path::new(EFI_DIR).exists();
    let mut findings = Vec::new();
    let efi = if is_efi { efi_boot_entries() } else { EfiInfo::default() };
    // Only entries in the boot order are tried; HP keeps the NIC entries active even with PXE off
    let net = regex::RegexBuilder::new(r"\b(ipv4|ipv6|pxe|network)\b").case_insensitive(true).build().unwrap();
    if efi.order.iter().filter_map(|i| efi.entries.get(i)).any(|(n, a)| *a && net.is_match(n)) {
        findings.push(tr("Network (PXE) boot is on, so the network card is prepared every start."));
    }
    if let (Some(first), Some(cur)) = (efi.order.first(), &efi.current) {
        if first != cur {
            let name = efi.entries.get(first).map(|e| e.0.clone()).unwrap_or_else(|| first.clone());
            findings.push(trf("'{name}' is tried before Fedora in the boot order.", &[("name", &name)]));
        }
    }
    if efi.timeout > 0 {
        findings.push(trf("The firmware boot menu waits {seconds} s.", &[("seconds", &efi.timeout)]));
    }
    if !is_efi {
        findings.push(tr("Boots in Legacy mode, usually slower than UEFI."));
    }
    let rating = match fw {
        None => "unknown",
        Some(s) if s <= 4.0 => "good",
        Some(s) if s <= 8.0 => "ok",
        _ => "slow",
    };
    let steps = BIOS_STEPS
        .iter()
        .filter_map(|(k, title, detail)| {
            let w = if is_hp { hp_hint(k).unwrap_or(Some("")) } else { Some("") };
            w.map(|w| BiosStep { title: title.to_string(), detail: detail.to_string(), r#where: w.to_string() })
        })
        .collect();
    FirmwareAnalysis {
        firmware_s: fw,
        rating: rating.into(),
        vendor,
        model,
        setup_key: SETUP_KEYS.iter().find(|(k, _)| *k == vkey).map(|(_, v)| v.to_string()).unwrap_or_else(|| "Del or F2".into()),
        is_efi,
        findings,
        steps,
    }
}

pub fn reboot_to_firmware() -> (bool, String) {
    let o = r(&["systemctl", "reboot", "--firmware-setup"]);
    if o.code != 0 {
        log::error!("Reboot into firmware setup failed: {}", o.stderr.trim());
        return (false, "Could not restart into the BIOS. Press the setup key at power-on instead.".into());
    }
    (true, "Restarting into the BIOS...".into())
}
