import subprocess
import time
import shutil
from pathlib import Path
from core.logger import get_logger

logger = get_logger("keyring")


class KeyringService:
    @staticmethod
    def is_keyring_blank_or_unlocked(keyring_path: Path | None = None) -> tuple[bool, str]:
        """
        Accurately inspects login.keyring on disk to determine if it has a blank
        password (unencrypted) so it will unlock automatically during unattended boot.
        """
        keyring_dir = Path.home() / ".local" / "share" / "keyrings"
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
            return False, f"Could not inspect keyring: {e}"

    @classmethod
    def reset_keyring_to_blank(cls, keyring_path: Path | None = None) -> tuple[bool, str]:
        """
        Automated 1-click fix: Backs up existing login.keyring and creates a fresh,
        unencrypted/blank login.keyring so GNOME Keyring never prompts for a password
        during unattended automatic login.
        """
        if keyring_path is None:
            keyring_dir = Path.home() / ".local" / "share" / "keyrings"
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
            return False, f"Failed resetting keyring: {e}"

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


