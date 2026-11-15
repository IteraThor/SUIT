//! Login keyring that unlocks on its own after an automatic login: a blank gnome-keyring login.keyring, or
//! on Fedora 45+ (oo7) a random password stored as the systemd credential oo7 imports at start-up.
use super::i18n::{tr, trf};
use super::util::{has_cmd, home, run, run_with_input, spawn};
use anyhow::{anyhow, bail, Result};
use gtk::gio;
use gtk::gio::prelude::*;
use gtk::glib::{self, prelude::ToVariant, variant::ObjectPath, Variant, VariantTy};
use std::collections::HashMap;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

const OO7_DAEMON: &str = "/usr/libexec/oo7-daemon";
const SECRETS: &str = "org.freedesktop.secrets";
const SECRETS_PATH: &str = "/org/freedesktop/secrets";
const SERVICE_IFACE: &str = "org.freedesktop.Secret.Service";
const GNOME_IFACE: &str = "org.gnome.keyring.InternalUnsupportedGuiltRiddenInterface";
const COLLECTION_IFACE: &str = "org.freedesktop.Secret.Collection";
const ITEM_IFACE: &str = "org.freedesktop.Secret.Item";
/// oo7-daemon.service has ImportCredential= for this name: oo7 unlocks the login keyring with it at start-up.
pub const OO7_CREDENTIAL: &str = "oo7.keyring-encryption-password";
/// The keyring prompt waits for the user; give up after this long
const PROMPT_TIMEOUT_S: u32 = 300;

fn keyring_dir() -> PathBuf {
    home().join(".local/share/keyrings")
}
fn credential_path() -> PathBuf {
    home().join(".config/credstore.encrypted").join(OO7_CREDENTIAL)
}

type Secret = (ObjectPath, Vec<u8>, Vec<u8>, String);

/// Fedora 45 replaced gnome-keyring with oo7, which keeps its keyrings in keyrings/v1/.
pub fn uses_oo7() -> bool {
    Path::new(OO7_DAEMON).exists()
}

/// gnome-keyring next to oo7: both race for the Secret Service at login.
pub fn gnome_keyring_installed() -> bool {
    has_cmd("gnome-keyring-daemon")
}

/// The keyring password SUIT stored for oo7, or None.
pub fn read_oo7_credential() -> Option<String> {
    let p = credential_path();
    if !p.is_file() {
        return None;
    }
    let r = run(&["systemd-creds", "decrypt", "--user", &format!("--name={}", OO7_CREDENTIAL), &p.to_string_lossy(), "-"], Some(30));
    if r.code != 0 || r.stdout.is_empty() {
        log::warn!("Could not decrypt {}: {}", p.display(), r.stderr.trim());
        return None;
    }
    Some(r.stdout)
}

/// Store the password encrypted for this user with the TPM (or the host key without one).
fn write_oo7_credential(password: &str) -> Result<()> {
    let p = credential_path();
    let dir = p.parent().unwrap();
    std::fs::create_dir_all(dir)?;
    std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))?;
    let tmp = dir.join(format!("{}.tmp", OO7_CREDENTIAL));
    let r = run_with_input(
        &["systemd-creds", "encrypt", "--user", &format!("--name={}", OO7_CREDENTIAL), "-", &tmp.to_string_lossy()],
        password,
        Some(60),
    );
    if r.code != 0 {
        let _ = std::fs::remove_file(&tmp);
        bail!("{}", if r.stderr.trim().is_empty() { "systemd-creds encrypt failed".into() } else { r.stderr.trim().to_string() });
    }
    std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600))?;
    std::fs::rename(&tmp, &p)?;
    Ok(())
}

fn bus() -> Result<gio::DBusConnection> {
    Ok(gio::bus_get_sync(gio::BusType::Session, gio::Cancellable::NONE)?)
}

fn call(b: &gio::DBusConnection, path: &str, iface: &str, method: &str, args: Option<&Variant>, reply: Option<&str>) -> Result<Variant> {
    let ty = reply.map(|r| VariantTy::new(r).unwrap());
    Ok(b.call_sync(Some(SECRETS), path, iface, method, args, ty, gio::DBusCallFlags::NONE, 10000, gio::Cancellable::NONE)?)
}

fn prop(b: &gio::DBusConnection, path: &str, iface: &str, name: &str) -> Result<Variant> {
    let v = call(b, path, "org.freedesktop.DBus.Properties", "Get", Some(&(iface, name).to_variant()), Some("(v)"))?;
    v.child_value(0).as_variant().ok_or_else(|| anyhow!("bad property"))
}

fn locked(b: &gio::DBusConnection, coll: &str) -> Result<bool> {
    prop(b, coll, COLLECTION_IFACE, "Locked")?.get::<bool>().ok_or_else(|| anyhow!("bad Locked"))
}

fn default_collection(b: &gio::DBusConnection) -> Result<Option<String>> {
    let v = call(b, SECRETS_PATH, SERVICE_IFACE, "ReadAlias", Some(&("default",).to_variant()), Some("(o)"))?;
    let (p,): (ObjectPath,) = v.get().ok_or_else(|| anyhow!("bad alias"))?;
    Ok(if p.as_str() == "/" { None } else { Some(p.as_str().to_string()) })
}

/// Whether oo7's default (login) keyring is locked right now; None when there is none or no Secret Service.
pub fn oo7_login_locked() -> Option<bool> {
    let b = bus().ok()?;
    let c = default_collection(&b).ok()??;
    locked(&b, &c).ok()
}

/// True when the login keyring unlocks on its own at boot.
pub fn oo7_autounlock_ok() -> (bool, String) {
    if gnome_keyring_installed() {
        return (false, "gnome-keyring is installed next to oo7".into());
    }
    if read_oo7_credential().is_none() {
        return (false, "No stored keyring password: the keyring stays locked after an automatic login".into());
    }
    match oo7_login_locked() {
        None => (false, "No login keyring".into()),
        Some(true) => (false, "The stored keyring password does not open the login keyring".into()),
        Some(false) => (true, "Keyring unlocks automatically on boot".into()),
    }
}

fn op(s: &str) -> ObjectPath {
    ObjectPath::try_from(s.to_string()).unwrap()
}

fn plain_session(b: &gio::DBusConnection) -> Result<String> {
    let v = call(b, SECRETS_PATH, SERVICE_IFACE, "OpenSession", Some(&("plain", "".to_variant()).to_variant()), Some("(vo)"))?;
    let p: ObjectPath = v.child_value(1).get().ok_or_else(|| anyhow!("bad session"))?;
    Ok(p.as_str().to_string())
}

fn secret(session: &str, value: &[u8], ct: &str) -> Secret {
    (op(session), Vec::new(), value.to_vec(), ct.to_string())
}

/// Unlock the collection with a password; false when it is wrong.
fn try_master(b: &gio::DBusConnection, session: &str, coll: &str, pw: &str) -> bool {
    let args = (op(coll), secret(session, pw.as_bytes(), "text/plain")).to_variant();
    if call(b, SECRETS_PATH, GNOME_IFACE, "UnlockWithMasterPassword", Some(&args), None).is_err() {
        return false;
    }
    locked(b, coll).map(|l| !l).unwrap_or(false)
}

/// Show a Secret Service prompt and wait for it. Some(true) completed, Some(false) dismissed, None on timeout.
fn run_prompt(b: &gio::DBusConnection, prompt: &str) -> Option<bool> {
    let ctx = glib::MainContext::new();
    ctx.with_thread_default(|| {
        let ml = glib::MainLoop::new(Some(&ctx), false);
        let result = std::rc::Rc::new(std::cell::Cell::new(None::<bool>));
        let (r2, ml2) = (result.clone(), ml.clone());
        #[allow(deprecated)]
        let sub = b.signal_subscribe(
            None,
            Some("org.freedesktop.Secret.Prompt"),
            Some("Completed"),
            Some(prompt),
            None,
            gio::DBusSignalFlags::NONE,
            move |_, _, _, _, _, params| {
                let dismissed = params.child_value(0).get::<bool>().unwrap_or(true);
                r2.set(Some(dismissed));
                ml2.quit();
            },
        );
        let ml3 = ml.clone();
        let src = glib::timeout_source_new_seconds(PROMPT_TIMEOUT_S, None, glib::Priority::DEFAULT, move || {
            ml3.quit();
            glib::ControlFlow::Break
        });
        src.attach(Some(&ctx));
        if call(b, prompt, "org.freedesktop.Secret.Prompt", "Prompt", Some(&("",).to_variant()), None).is_ok() {
            ml.run();
        }
        src.destroy();
        #[allow(deprecated)]
        b.signal_unsubscribe(sub);
        result.get().map(|d| !d)
    })
    .ok()
    .flatten()
}

struct Item {
    label: String,
    attributes: HashMap<String, String>,
    value: Vec<u8>,
    content_type: String,
}

fn read_items(b: &gio::DBusConnection, session: &str, coll: &str) -> Result<Vec<Item>> {
    let items: Vec<ObjectPath> = prop(b, coll, COLLECTION_IFACE, "Items")?.get().unwrap_or_default();
    if items.is_empty() {
        return Ok(vec![]);
    }
    let v = call(b, SECRETS_PATH, SERVICE_IFACE, "GetSecrets", Some(&(items.clone(), op(session)).to_variant()), Some("(a{o(oayays)})"))?;
    let secrets: HashMap<ObjectPath, Secret> = v.child_value(0).get().unwrap_or_default();
    let mut out = Vec::new();
    for it in items {
        let Some(s) = secrets.get(&it) else { continue };
        out.push(Item {
            label: prop(b, it.as_str(), ITEM_IFACE, "Label")?.get().unwrap_or_default(),
            attributes: prop(b, it.as_str(), ITEM_IFACE, "Attributes")?.get().unwrap_or_default(),
            value: s.2.clone(),
            content_type: if s.3.is_empty() { "text/plain".into() } else { s.3.clone() },
        });
    }
    Ok(out)
}

fn write_items(b: &gio::DBusConnection, session: &str, coll: &str, items: &[Item]) -> usize {
    let mut n = 0;
    for it in items {
        let mut props: HashMap<String, Variant> = HashMap::new();
        props.insert("org.freedesktop.Secret.Item.Label".into(), it.label.to_variant());
        props.insert("org.freedesktop.Secret.Item.Attributes".into(), it.attributes.to_variant());
        let args = (props, secret(session, &it.value, &it.content_type), true).to_variant();
        match call(b, coll, COLLECTION_IFACE, "CreateItem", Some(&args), Some("(oo)")) {
            Ok(_) => n += 1,
            Err(e) => log::warn!("Could not copy keyring item {:?}: {}", it.label, e),
        }
    }
    n
}

fn remove_gnome_keyring() -> Result<()> {
    let r = run(&["sudo", "-n", "dnf", "remove", "-y", "gnome-keyring"], Some(300));
    if r.code != 0 {
        bail!("dnf remove gnome-keyring: {}", r.stderr.trim().lines().last().unwrap_or("failed"));
    }
    run(&["pkill", "-x", "gnome-keyring-d"], Some(5));
    log::info!("Removed gnome-keyring: oo7 is Fedora's Secret Service now");
    Ok(())
}

/// Process name serving org.freedesktop.secrets right now ('oo7-daemon', 'gnome-keyring-d'), or None.
fn secrets_owner() -> Option<String> {
    let b = bus().ok()?;
    let v = b
        .call_sync(
            Some("org.freedesktop.DBus"),
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "GetConnectionUnixProcessID",
            Some(&(SECRETS,).to_variant()),
            Some(VariantTy::new("(u)").unwrap()),
            gio::DBusCallFlags::NONE,
            5000,
            gio::Cancellable::NONE,
        )
        .ok()?;
    let (pid,): (u32,) = v.get()?;
    super::util::read_trim(format!("/proc/{}/comm", pid))
}

fn restart_oo7() -> Result<()> {
    let r = run(&["systemctl", "--user", "restart", "oo7-daemon.service"], Some(60));
    if r.code != 0 {
        bail!("oo7-daemon restart failed: {}", r.stderr.trim());
    }
    Ok(())
}

/// What a reboot does: oo7 starts, imports the credential and must come up unlocked.
fn verify_after_restart() -> bool {
    restart_oo7().is_ok() && oo7_login_locked() == Some(false)
}

fn random_password() -> String {
    use rand::Rng;
    const A: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut r = rand::rng();
    (0..43).map(|_| A[r.random_range(0..A.len())] as char).collect()
}

/// Make oo7 unlock the login keyring at boot without a typed password (copying items into a fresh keyring
/// when it cannot simply be switched; the old file is kept in keyrings/suit-backup/).
pub fn setup_oo7_autounlock() -> (bool, String) {
    match setup_oo7_inner() {
        Ok(r) => r,
        Err(e) => {
            log::error!("Setting up the oo7 keyring unlock failed: {}", e);
            (false, trf("Failed resetting keyring: {error}", &[("error", &e)]))
        }
    }
}

fn setup_oo7_inner() -> Result<(bool, String)> {
    if gnome_keyring_installed() {
        remove_gnome_keyring()?;
        // a restart locks the keyring like a reboot: only to take the Secret Service over
        if secrets_owner().as_deref() != Some("oo7-daemon") {
            restart_oo7()?;
        }
    }
    let password = match read_oo7_credential() {
        Some(p) => p,
        None => {
            let p = random_password();
            write_oo7_credential(&p)?;
            log::info!("Stored a new keyring password in {}", credential_path().display());
            p
        }
    };
    let b = bus()?;
    let session = plain_session(&b)?;
    let Some(coll) = default_collection(&b)? else { return Ok((false, tr("No login keyring was found."))) };
    let mut is_locked = locked(&b, &coll)?;
    if is_locked && !(try_master(&b, &session, &coll, &password) || try_master(&b, &session, &coll, "")) {
        // Only the user knows the old password: oo7 asks for it
        let v = call(&b, SECRETS_PATH, SERVICE_IFACE, "Unlock", Some(&(vec![op(&coll)],).to_variant()), Some("(aoo)"))?;
        let prompt: ObjectPath = v.child_value(1).get().ok_or_else(|| anyhow!("bad prompt"))?;
        if prompt.as_str() != "/" {
            run_prompt(&b, prompt.as_str());
        }
        is_locked = locked(&b, &coll)?;
    }
    let items = if is_locked { vec![] } else { read_items(&b, &session, &coll)? };
    if is_locked {
        log::warn!("Login keyring stays locked: starting a fresh one, its saved entries are lost");
    }
    // Cheapest path: the keyring may already use the stored password or an empty one
    if !is_locked {
        for old in [password.as_str(), ""] {
            let args = (op(&coll), secret(&session, old.as_bytes(), "text/plain"), secret(&session, password.as_bytes(), "text/plain")).to_variant();
            if call(&b, SECRETS_PATH, GNOME_IFACE, "ChangeWithMasterPassword", Some(&args), None).is_ok() {
                if verify_after_restart() {
                    log::info!("Login keyring now uses the stored password ({} entries kept)", items.len());
                    return Ok((true, tr("The keyring now unlocks automatically on boot.")));
                }
                break;
            }
        }
    }
    // Fresh login keyring with the stored password, the old entries copied over
    let name = coll.rsplit('/').next().unwrap_or("login").to_string();
    let src = keyring_dir().join("v1").join(format!("{}.keyring", name));
    let backup_root = keyring_dir().join("suit-backup");
    let backup = backup_root.join(chrono::Local::now().format("%Y%m%d-%H%M%S").to_string());
    run(&["systemctl", "--user", "stop", "oo7-daemon.service"], Some(60));
    if src.exists() {
        std::fs::create_dir_all(&backup)?;
        std::fs::set_permissions(&backup_root, std::fs::Permissions::from_mode(0o700))?;
        std::fs::rename(&src, backup.join(src.file_name().unwrap()))?;
        log::info!("Moved the old login keyring to {}", backup.display());
    }
    // restart, not start: a client may have woken oo7 up again (with the old file) while it was stopped
    restart_oo7()?;
    let b = bus()?;
    let session = plain_session(&b)?;
    let Some(coll) = default_collection(&b)? else { return Ok((false, tr("No login keyring was found."))) };
    if locked(&b, &coll)? {
        try_master(&b, &session, &coll, &password);
    }
    let copied = write_items(&b, &session, &coll, &items);
    if !verify_after_restart() {
        return Ok((false, trf("The keyring still does not unlock on its own. The old keyring is in {path}.", &[("path", &backup.display())])));
    }
    log::info!("Fresh login keyring with the stored password, {} of {} entries copied", copied, items.len());
    if is_locked {
        return Ok((
            true,
            trf(
                "The keyring now unlocks automatically on boot. Its old entries could not be opened and are kept in {path}.",
                &[("path", &backup.display())],
            ),
        ));
    }
    Ok((true, trf("The keyring now unlocks automatically on boot ({count} saved entries kept).", &[("count", &copied)])))
}

/// Does the login keyring unlock by itself during an unattended boot?
pub fn is_keyring_blank_or_unlocked(path: Option<&Path>) -> (bool, String) {
    if path.is_none() && uses_oo7() {
        return oo7_autounlock_ok();
    }
    let dir = keyring_dir();
    let mut target = path.map(|p| p.to_path_buf()).unwrap_or_else(|| dir.join("login.keyring"));
    if !target.exists() {
        if let Some(n) = super::util::read_trim(dir.join("default")) {
            let alt = dir.join(format!("{}.keyring", n));
            if alt.exists() {
                target = alt;
            }
        }
    }
    let Ok(raw) = std::fs::read(&target) else {
        return (false, "Keyring not initialized (will prompt for password on first use)".into());
    };
    if raw.starts_with(b"GnomeKeyring\n\r\0\n") {
        return (false, "Password-protected (legacy binary format)".into());
    }
    let c = String::from_utf8_lossy(&raw);
    if c.contains("hash-iterations=") || c.contains("salt=") {
        return (false, "Password-protected (manual unlock required on boot)".into());
    }
    if c.contains("[keyring]") {
        return (true, "Keyring has blank password (unlocked on boot)".into());
    }
    (false, "Keyring uninitialized or unrecognized format".into())
}

/// One-click fix: a blank login.keyring (gnome-keyring) or the oo7 credential setup.
pub fn reset_keyring_to_blank(path: Option<&Path>) -> (bool, String) {
    if path.is_none() && uses_oo7() {
        return setup_oo7_autounlock();
    }
    let kp = path.map(|p| p.to_path_buf()).unwrap_or_else(|| keyring_dir().join("login.keyring"));
    let dir = kp.parent().unwrap().to_path_buf();
    let r = (|| -> std::io::Result<()> {
        std::fs::create_dir_all(&dir)?;
        if kp.exists() {
            let bak = kp.with_extension("keyring.bak");
            std::fs::copy(&kp, &bak)?;
            log::info!("Backed up existing keyring to {}", bak.display());
        }
        let blank = format!(
            "[keyring]\ndisplay-name=Login\nctime={}\nmtime=0\nlock-on-idle=false\nlock-after=false\n",
            super::util::now_s() as i64
        );
        std::fs::write(&kp, blank)?;
        std::fs::set_permissions(&kp, std::fs::Permissions::from_mode(0o600))?;
        let d = dir.join("default");
        std::fs::write(&d, "login\n")?;
        std::fs::set_permissions(&d, std::fs::Permissions::from_mode(0o600))?;
        run(&["killall", "-HUP", "gnome-keyring-daemon"], Some(5));
        Ok(())
    })();
    match r {
        Ok(_) => {
            log::info!("Created blank unencrypted login.keyring successfully");
            (true, "Keyring password removed. It will now unlock automatically on boot.".into())
        }
        Err(e) => (false, trf("Failed resetting keyring: {error}", &[("error", &e)])),
    }
}

pub fn is_seahorse_installed() -> bool {
    has_cmd("seahorse")
}

pub fn install_seahorse() -> bool {
    let ok = run(&["sudo", "-n", "dnf", "install", "-y", "seahorse"], None).ok();
    if ok {
        log::info!("Installed seahorse package");
    }
    ok
}

pub fn launch_seahorse() -> bool {
    spawn(&["seahorse"])
}
