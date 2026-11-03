import os
import secrets
import subprocess
import time
import shutil
from pathlib import Path
from core.logger import get_logger
from core.i18n import tr

logger = get_logger("keyring")

KEYRING_DIR = Path.home() / ".local" / "share" / "keyrings"
OO7_DAEMON = Path("/usr/libexec/oo7-daemon")
OO7_KEYRING_DIR = KEYRING_DIR / "v1"
OO7_BACKUP_DIR = KEYRING_DIR / "suit-backup"
SECRETS = "org.freedesktop.secrets"
SECRETS_PATH = "/org/freedesktop/secrets"
SERVICE_IFACE = "org.freedesktop.Secret.Service"
GNOME_IFACE = "org.gnome.keyring.InternalUnsupportedGuiltRiddenInterface"
# oo7-daemon.service has ImportCredential= for this name: oo7 unlocks the login keyring with it at start-up,
# which is the only way it unlocks after an automatic login (nothing types a password then).
OO7_CREDENTIAL = "oo7.keyring-encryption-password"
CREDSTORE_DIR = Path.home() / ".config" / "credstore.encrypted"
OO7_CREDENTIAL_PATH = CREDSTORE_DIR / OO7_CREDENTIAL
# The keyring prompt waits for the user; give up after this long
PROMPT_TIMEOUT_S = 300


def _session_bus():
    from gi.repository import Gio
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


class KeyringService:
    @staticmethod
    def uses_oo7() -> bool:
        """Fedora 45 replaced gnome-keyring with oo7. oo7 keeps its keyrings in keyrings/v1/ and no longer
        reads the old login.keyring, so the keyring has to be checked and changed through the Secret Service."""
        return OO7_DAEMON.exists()

    @staticmethod
    def gnome_keyring_installed() -> bool:
        """gnome-keyring next to oo7: both start at login (D-Bus activation, autostart, PAM) and race for the
        Secret Service, so which keyring a session gets is luck."""
        return shutil.which("gnome-keyring-daemon") is not None

    @staticmethod
    def read_oo7_credential() -> str | None:
        """The keyring password SUIT stored for oo7, or None when there is none (or it cannot be decrypted)."""
        if not OO7_CREDENTIAL_PATH.is_file():
            return None
        try:
            res = subprocess.run(["systemd-creds", "decrypt", "--user", f"--name={OO7_CREDENTIAL}",
                                  str(OO7_CREDENTIAL_PATH), "-"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("Could not decrypt %s: %s", OO7_CREDENTIAL_PATH, e)
            return None
        if res.returncode != 0 or not res.stdout:
            logger.warning("Could not decrypt %s: %s", OO7_CREDENTIAL_PATH, res.stderr.strip())
            return None
        return res.stdout

    @staticmethod
    def _write_oo7_credential(password: str) -> None:
        """Store the password encrypted for this user with the TPM (or the host key without one)."""
        CREDSTORE_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(CREDSTORE_DIR, 0o700)
        tmp = OO7_CREDENTIAL_PATH.with_name(OO7_CREDENTIAL + ".tmp")
        res = subprocess.run(["systemd-creds", "encrypt", "--user", f"--name={OO7_CREDENTIAL}", "-", str(tmp)],
                             input=password, capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(res.stderr.strip() or "systemd-creds encrypt failed")
        os.chmod(tmp, 0o600)
        tmp.replace(OO7_CREDENTIAL_PATH)

    @classmethod
    def oo7_autounlock_ok(cls) -> tuple[bool, str]:
        """True when the login keyring unlocks on its own at boot: oo7 only, SUIT's credential in place,
        and the keyring open now (a credential that does not match would leave it locked)."""
        if cls.gnome_keyring_installed():
            return False, "gnome-keyring is installed next to oo7"
        if cls.read_oo7_credential() is None:
            return False, "No stored keyring password: the keyring stays locked after an automatic login"
        locked = cls.oo7_login_locked()
        if locked is None:
            return False, "No login keyring"
        if locked:
            return False, "The stored keyring password does not open the login keyring"
        return True, "Keyring unlocks automatically on boot"

    @staticmethod
    def _default_collection(bus) -> str | None:
        from gi.repository import Gio, GLib
        res = bus.call_sync(SECRETS, SECRETS_PATH, "org.freedesktop.Secret.Service", "ReadAlias",
                            GLib.Variant("(s)", ("default",)), GLib.VariantType("(o)"),
                            Gio.DBusCallFlags.NONE, 5000, None)
        path = res.unpack()[0]
        return None if path == "/" else path

    @classmethod
    def oo7_login_locked(cls) -> bool | None:
        """Whether oo7's default (login) keyring is locked right now; None when there is none or no Secret Service.

        On an automatic login nothing types a password, so the keyring is only unlocked when it has a blank one."""
        try:
            from gi.repository import Gio, GLib
            bus = _session_bus()
            path = cls._default_collection(bus)
            if not path:
                return None
            res = bus.call_sync(SECRETS, path, "org.freedesktop.DBus.Properties", "Get",
                                GLib.Variant("(ss)", ("org.freedesktop.Secret.Collection", "Locked")),
                                GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 5000, None)
            return bool(res.unpack()[0])
        except Exception as e:
            logger.debug("Could not read the oo7 keyring state: %s", e)
            return None

    @staticmethod
    def _run_prompt(bus, prompt_path: str) -> bool | None:
        """Show a Secret Service prompt and wait for it. True when completed, False when dismissed, None on timeout."""
        from gi.repository import Gio, GLib
        ctx = GLib.MainContext.new()
        ctx.push_thread_default()
        try:
            loop = GLib.MainLoop.new(ctx, False)
            result = {}

            def on_completed(_conn, _sender, _path, _iface, _signal, params):
                result["dismissed"] = bool(params.unpack()[0])
                loop.quit()

            sub = bus.signal_subscribe(None, "org.freedesktop.Secret.Prompt", "Completed", prompt_path, None,
                                       Gio.DBusSignalFlags.NONE, on_completed)
            timeout = GLib.timeout_source_new_seconds(PROMPT_TIMEOUT_S)
            timeout.set_callback(lambda *_: (loop.quit(), False)[1])
            timeout.attach(ctx)
            try:
                bus.call_sync(SECRETS, prompt_path, "org.freedesktop.Secret.Prompt", "Prompt",
                              GLib.Variant("(s)", ("",)), None, Gio.DBusCallFlags.NONE, 5000, None)
                loop.run()
            finally:
                timeout.destroy()
                bus.signal_unsubscribe(sub)
            if "dismissed" not in result:
                return None
            return not result["dismissed"]
        finally:
            ctx.pop_thread_default()

    # ------------------------------------------------------------------ oo7: unlock at boot

    @staticmethod
    def _call(bus, path, iface, method, args=None, reply=None):
        from gi.repository import Gio, GLib
        return bus.call_sync(SECRETS, path, iface, method, args,
                             GLib.VariantType(reply) if reply else None, Gio.DBusCallFlags.NONE, 10000, None)

    @classmethod
    def _plain_session(cls, bus) -> str:
        from gi.repository import GLib
        res = cls._call(bus, SECRETS_PATH, SERVICE_IFACE, "OpenSession",
                        GLib.Variant("(sv)", ("plain", GLib.Variant("s", ""))), "(vo)")
        return res.unpack()[1]

    @staticmethod
    def _secret(session: str, value: bytes, content_type: str = "text/plain"):
        from gi.repository import GLib
        return (session, b"", value, content_type)

    @classmethod
    def _prop(cls, bus, path, iface, name):
        from gi.repository import GLib
        return cls._call(bus, path, "org.freedesktop.DBus.Properties", "Get",
                         GLib.Variant("(ss)", (iface, name)), "(v)").unpack()[0]

    @classmethod
    def _try_master(cls, bus, session, collection, password: str) -> bool:
        """Unlock the collection with a password; False when it is wrong."""
        from gi.repository import GLib
        try:
            cls._call(bus, SECRETS_PATH, GNOME_IFACE, "UnlockWithMasterPassword",
                      GLib.Variant("(o(oayays))", (collection, cls._secret(session, password.encode()))))
        except Exception:
            return False
        return cls._prop(bus, collection, "org.freedesktop.Secret.Collection", "Locked") is False

    @classmethod
    def _read_items(cls, bus, session, collection) -> list[dict]:
        from gi.repository import GLib
        items = cls._prop(bus, collection, "org.freedesktop.Secret.Collection", "Items")
        if not items:
            return []
        secrets_ = cls._call(bus, SECRETS_PATH, SERVICE_IFACE, "GetSecrets",
                             GLib.Variant("(aoo)", (items, session)), "(a{o(oayays)})").unpack()[0]
        out = []
        for item in items:
            if item not in secrets_:
                continue
            _s, _params, value, ctype = secrets_[item]
            out.append({"label": cls._prop(bus, item, "org.freedesktop.Secret.Item", "Label"),
                        "attributes": dict(cls._prop(bus, item, "org.freedesktop.Secret.Item", "Attributes")),
                        "value": bytes(value), "content_type": ctype or "text/plain"})
        return out

    @classmethod
    def _write_items(cls, bus, session, collection, items: list[dict]) -> int:
        from gi.repository import GLib
        written = 0
        for it in items:
            props = {"org.freedesktop.Secret.Item.Label": GLib.Variant("s", it["label"]),
                     "org.freedesktop.Secret.Item.Attributes": GLib.Variant("a{ss}", it["attributes"])}
            try:
                cls._call(bus, collection, "org.freedesktop.Secret.Collection", "CreateItem",
                          GLib.Variant("(a{sv}(oayays)b)", (props, cls._secret(session, it["value"], it["content_type"]), True)),
                          "(oo)")
                written += 1
            except Exception as e:
                logger.warning("Could not copy keyring item %r: %s", it["label"], e)
        return written

    @staticmethod
    def _remove_gnome_keyring() -> None:
        res = subprocess.run(["sudo", "-n", "dnf", "remove", "-y", "gnome-keyring"],
                             capture_output=True, text=True, timeout=300)
        if res.returncode != 0:
            raise RuntimeError("dnf remove gnome-keyring: " + (res.stderr.strip().splitlines() or ["failed"])[-1])
        subprocess.run(["pkill", "-x", "gnome-keyring-d"], capture_output=True)
        logger.info("Removed gnome-keyring: oo7 is Fedora's Secret Service now")

    @staticmethod
    def _secrets_owner() -> str | None:
        """Process name serving org.freedesktop.secrets right now ('oo7-daemon', 'gnome-keyring-d'), or None."""
        try:
            from gi.repository import Gio, GLib
            bus = _session_bus()
            pid = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                "GetConnectionUnixProcessID", GLib.Variant("(s)", (SECRETS,)),
                                GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]
            return Path(f"/proc/{pid}/comm").read_text().strip()
        except Exception:
            return None

    @staticmethod
    def _restart_oo7() -> None:
        res = subprocess.run(["systemctl", "--user", "restart", "oo7-daemon.service"],
                             capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            raise RuntimeError("oo7-daemon restart failed: " + res.stderr.strip())

    @classmethod
    def setup_oo7_autounlock(cls) -> tuple[bool, str]:
        """Make oo7 unlock the login keyring at boot without a typed password.

        A random keyring password goes into the systemd credential oo7 imports at start-up. If the keyring
        cannot simply be switched to it, its items are copied into a fresh login keyring that uses it (the
        old file is kept in keyrings/suit-backup/). Ends with a restart of oo7, which must come back unlocked.
        """
        try:
            if cls.gnome_keyring_installed():
                cls._remove_gnome_keyring()
                # Restart only to take the Secret Service over: a restart locks the keyring like a reboot,
                # and while it is still open SUIT can keep its entries without asking for the old password.
                if cls._secrets_owner() != "oo7-daemon":
                    cls._restart_oo7()

            password = cls.read_oo7_credential()
            if password is None:
                password = secrets.token_urlsafe(32)
                cls._write_oo7_credential(password)
                logger.info("Stored a new keyring password in %s", OO7_CREDENTIAL_PATH)

            bus = _session_bus()
            session = cls._plain_session(bus)
            collection = cls._default_collection(bus)
            if not collection:
                return False, tr("No login keyring was found.")
            locked = cls._prop(bus, collection, "org.freedesktop.Secret.Collection", "Locked")

            if locked and not (cls._try_master(bus, session, collection, password)
                               or cls._try_master(bus, session, collection, "")):
                # Only the user knows the old password: oo7 asks for it
                from gi.repository import GLib
                res = cls._call(bus, SECRETS_PATH, SERVICE_IFACE, "Unlock", GLib.Variant("(ao)", ([collection],)), "(aoo)")
                prompt = res.unpack()[1]
                if prompt != "/":
                    cls._run_prompt(bus, prompt)
                locked = cls._prop(bus, collection, "org.freedesktop.Secret.Collection", "Locked")

            items = [] if locked else cls._read_items(bus, session, collection)
            if locked:
                logger.warning("Login keyring stays locked: starting a fresh one, its saved entries are lost")

            # Cheapest path: the keyring may already use the stored password or an empty one
            from gi.repository import GLib
            for old in (password, ""):
                if locked:
                    break
                try:
                    cls._call(bus, SECRETS_PATH, GNOME_IFACE, "ChangeWithMasterPassword",
                              GLib.Variant("(o(oayays)(oayays))", (collection, cls._secret(session, old.encode()),
                                                                   cls._secret(session, password.encode()))))
                    if cls._verify_after_restart():
                        logger.info("Login keyring now uses the stored password (%d entries kept)", len(items))
                        return True, tr("The keyring now unlocks automatically on boot.")
                    break
                except Exception:
                    continue

            # Fresh login keyring with the stored password, the old entries copied over
            name = collection.rsplit("/", 1)[-1]
            src = OO7_KEYRING_DIR / f"{name}.keyring"
            backup = OO7_BACKUP_DIR / time.strftime("%Y%m%d-%H%M%S")
            subprocess.run(["systemctl", "--user", "stop", "oo7-daemon.service"], capture_output=True, timeout=60)
            if src.exists():
                backup.mkdir(parents=True, exist_ok=True)
                os.chmod(OO7_BACKUP_DIR, 0o700)
                shutil.move(str(src), str(backup / src.name))
                logger.info("Moved the old login keyring to %s", backup)
            # restart, not start: a client may have woken oo7 up again (with the old file) while it was stopped
            cls._restart_oo7()

            bus = _session_bus()
            session = cls._plain_session(bus)
            collection = cls._default_collection(bus)
            if not collection:
                return False, tr("No login keyring was found.")
            if cls._prop(bus, collection, "org.freedesktop.Secret.Collection", "Locked"):
                cls._try_master(bus, session, collection, password)
            copied = cls._write_items(bus, session, collection, items)
            if not cls._verify_after_restart():
                return False, tr("The keyring still does not unlock on its own. The old keyring is in {path}.").format(
                    path=backup)
            logger.info("Fresh login keyring with the stored password, %d of %d entries copied", copied, len(items))
            if locked:
                return True, tr("The keyring now unlocks automatically on boot. Its old entries could not be opened "
                                "and are kept in {path}.").format(path=backup)
            return True, tr("The keyring now unlocks automatically on boot ({count} saved entries kept).").format(
                count=copied)
        except Exception as e:
            logger.exception("Setting up the oo7 keyring unlock failed")
            return False, tr("Failed resetting keyring: {error}").format(error=e)

    @classmethod
    def _verify_after_restart(cls) -> bool:
        """What a reboot does: oo7 starts, imports the credential and must come up unlocked."""
        cls._restart_oo7()
        return cls.oo7_login_locked() is False

    @classmethod
    def is_keyring_blank_or_unlocked(cls, keyring_path: Path | None = None) -> tuple[bool, str]:
        """
        Accurately inspects login.keyring on disk to determine if it has a blank
        password (unencrypted) so it will unlock automatically during unattended boot.
        """
        if keyring_path is None and cls.uses_oo7():
            return cls.oo7_autounlock_ok()

        keyring_dir = KEYRING_DIR
        target = keyring_path or (keyring_dir / "login.keyring")

        if not target.exists():
            default_file = keyring_dir / "default"
            if default_file.exists():
                name = default_file.read_text(encoding="utf-8").strip()
                alt_target = keyring_dir / f"{name}.keyring"
                if alt_target.exists():
                    target = alt_target

        if not target.exists():
            return False, "Keyring not initialized (will prompt for password on first use)"

        try:
            raw = target.read_bytes()
            if raw.startswith(b"GnomeKeyring\n\r\0\n"):
                return False, "Password-protected (legacy binary format)"

            content = raw.decode("utf-8", errors="ignore")
            if "hash-iterations=" in content or "salt=" in content:
                return False, "Password-protected (manual unlock required on boot)"

            if "[keyring]" in content:
                return True, "Keyring has blank password (unlocked on boot)"

            return False, "Keyring uninitialized or unrecognized format"
        except Exception as e:
            logger.debug(f"Failed reading keyring file: {e}")
            return False, tr("Could not inspect keyring: {error}").format(error=e)

    @classmethod
    def reset_keyring_to_blank(cls, keyring_path: Path | None = None) -> tuple[bool, str]:
        """
        Automated 1-click fix: Backs up existing login.keyring and creates a fresh,
        unencrypted/blank login.keyring so GNOME Keyring never prompts for a password
        during unattended automatic login.
        """
        # SystemService calls this with itself as cls, so name KeyringService directly
        if keyring_path is None and KeyringService.uses_oo7():
            return KeyringService.setup_oo7_autounlock()
        if keyring_path is None:
            keyring_dir = KEYRING_DIR
            keyring_path = keyring_dir / "login.keyring"
        else:
            keyring_dir = keyring_path.parent

        try:
            keyring_dir.mkdir(parents=True, exist_ok=True)

            # Backup existing if present
            if keyring_path.exists():
                bak_path = keyring_path.with_suffix(".keyring.bak")
                shutil.copy2(keyring_path, bak_path)
                logger.info(f"Backed up existing keyring to {bak_path}")

            blank_content = (
                "[keyring]\n"
                "display-name=Login\n"
                f"ctime={int(time.time())}\n"
                "mtime=0\n"
                "lock-on-idle=false\n"
                "lock-after=false\n"
            )
            keyring_path.write_text(blank_content, encoding="utf-8")
            keyring_path.chmod(0o600)

            # Set as default collection
            default_path = keyring_dir / "default"
            default_path.write_text("login\n", encoding="utf-8")
            default_path.chmod(0o600)

            # Reload/restart gnome-keyring-daemon if running
            try:
                subprocess.run(["killall", "-HUP", "gnome-keyring-daemon"], capture_output=True)
            except Exception:
                pass

            logger.info("Created blank unencrypted login.keyring successfully")
            return True, "Keyring password removed. It will now unlock automatically on boot."
        except Exception as e:
            logger.exception("Failed resetting keyring to blank")
            return False, tr("Failed resetting keyring: {error}").format(error=e)

    @staticmethod
    def is_seahorse_installed() -> bool:
        return shutil.which("seahorse") is not None

    @staticmethod
    def install_seahorse() -> bool:
        try:
            subprocess.run(["sudo", "-n", "dnf", "install", "-y", "seahorse"], check=True, capture_output=True)
            logger.info("Installed seahorse package")
            return True
        except Exception:
            logger.exception("Failed installing seahorse")
            return False

    @staticmethod
    def launch_seahorse() -> bool:
        try:
            subprocess.Popen(["seahorse"])
            logger.info("Launched seahorse process")
            return True
        except Exception:
            logger.exception("Failed launching seahorse")
            return False


