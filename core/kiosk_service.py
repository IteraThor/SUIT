import json
import shutil
import subprocess
from pathlib import Path
from core.logger import get_logger

logger = get_logger("kiosk")

AUTOSTART_DIR = Path.home() / ".config" / "autostart"
DEFAULT_DESKTOP_FILE = AUTOSTART_DIR / "suit-kiosk.desktop"

EXTENSION_ID = "ifmcoidaalmjijjhnpafmjngeceapboj"
NATIVE_HOST_NAME = "com.suit.kiosk"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXTENSION_DIR = PROJECT_ROOT / "kiosk_extension"
HOST_SCRIPT = EXTENSION_DIR / "host" / "suit_kiosk_host.py"

NATIVE_MESSAGING_DIRS = [
    Path.home() / ".config" / "chromium" / "NativeMessagingHosts",
    Path.home() / ".config" / "google-chrome" / "NativeMessagingHosts",
    Path.home() / ".config" / "BraveSoftware" / "Brave-Browser" / "NativeMessagingHosts",
]

class KioskService:
    EXTENSION_ID = EXTENSION_ID
    NATIVE_HOST_NAME = NATIVE_HOST_NAME
    EXTENSION_DIR = EXTENSION_DIR
    HOST_SCRIPT = HOST_SCRIPT

    INSTALLED_EXTENSION_DIR = Path.home() / ".local" / "share" / "suit" / "kiosk_extension"
    CHROMIUM_CONF_PATH = Path("/etc/chromium/chromium.conf")
    SYSTEM_NATIVE_MESSAGING_DIR = Path("/etc/chromium/native-messaging-hosts")
    USER_DESKTOP_DIR = Path.home() / ".local" / "share" / "applications"
    USER_DESKTOP_PATH = USER_DESKTOP_DIR / "chromium-browser.desktop"

    SUIT_MARKER_START = "# >>> SUIT Autodarts In-Page Controls >>>"
    SUIT_MARKER_END = "# <<< SUIT Autodarts In-Page Controls <<<"

    @classmethod
    def get_extension_path(cls) -> Path:
        if cls.INSTALLED_EXTENSION_DIR.exists():
            return cls.INSTALLED_EXTENSION_DIR
        return cls.EXTENSION_DIR

    @classmethod
    def is_permanent_extension_installed(cls, chromium_conf: Path | None = None, desktop_path: Path | None = None) -> bool:
        """Check if the SUIT In-Page Controls extension is permanently configured in Chromium."""
        conf_file = chromium_conf or cls.CHROMIUM_CONF_PATH
        desk_file = desktop_path or cls.USER_DESKTOP_PATH

        if conf_file.exists():
            try:
                content = conf_file.read_text(encoding="utf-8")
                if cls.SUIT_MARKER_START in content:
                    return True
            except Exception:
                pass

        if desk_file.exists():
            try:
                content = desk_file.read_text(encoding="utf-8")
                if "--load-extension" in content:
                    return True
            except Exception:
                pass

        return False

    @classmethod
    def install_permanent_extension(cls, chromium_conf: Path | None = None, desktop_path: Path | None = None) -> tuple[bool, str]:
        """
        Permanently installs the SUIT In-Page Controls extension into Chromium:
        1. Copies extension files to ~/.local/share/suit/kiosk_extension/
        2. Installs Native Messaging Host manifest for user and system
        3. Configures /etc/chromium/chromium.conf with --load-extension
        4. Configures ~/.local/share/applications/chromium-browser.desktop
        """
        conf_file = chromium_conf or cls.CHROMIUM_CONF_PATH
        desk_file = desktop_path or cls.USER_DESKTOP_PATH

        try:
            # 1. Sync extension files to persistent user data directory
            cls.INSTALLED_EXTENSION_DIR.mkdir(parents=True, exist_ok=True)
            if cls.EXTENSION_DIR.exists():
                shutil.copytree(cls.EXTENSION_DIR, cls.INSTALLED_EXTENSION_DIR, dirs_exist_ok=True)
            host_script = cls.INSTALLED_EXTENSION_DIR / "host" / "suit_kiosk_host.py"
            if host_script.exists():
                host_script.chmod(0o755)

            # 2. Install user native messaging host manifests
            cls.ensure_native_messaging_host(host_script_path=host_script)

            # Install system native messaging host manifest if directory exists
            manifest_content = {
                "name": cls.NATIVE_HOST_NAME,
                "description": "SUIT Kiosk System Control Bridge",
                "path": str(host_script.resolve()),
                "type": "stdio",
                "allowed_origins": [
                    f"chrome-extension://{cls.EXTENSION_ID}/"
                ]
            }
            manifest_json = json.dumps(manifest_content, indent=2)
            system_manifest = cls.SYSTEM_NATIVE_MESSAGING_DIR / f"{cls.NATIVE_HOST_NAME}.json"
            try:
                subprocess.run(
                    ["sudo", "-n", "mkdir", "-p", str(cls.SYSTEM_NATIVE_MESSAGING_DIR)],
                    check=False,
                    capture_output=True
                )
                subprocess.run(
                    ["sudo", "-n", "tee", str(system_manifest)],
                    input=manifest_json,
                    text=True,
                    check=False,
                    capture_output=True
                )
            except Exception as e:
                logger.debug("System native messaging manifest write skipped: %s", e)

            # 3. Configure /etc/chromium/chromium.conf
            ext_dir_path = str(cls.INSTALLED_EXTENSION_DIR.resolve())
            if conf_file.exists():
                content = conf_file.read_text(encoding="utf-8")
                if cls.SUIT_MARKER_START not in content:
                    snippet = f"\n{cls.SUIT_MARKER_START}\nCHROMIUM_FLAGS+=\" --load-extension={ext_dir_path}\"\n{cls.SUIT_MARKER_END}\n"
                    # If this is the real system file, write with sudo; if a test file, write directly
                    if conf_file == cls.CHROMIUM_CONF_PATH:
                        subprocess.run(
                            ["sudo", "-n", "tee", "-a", str(conf_file)],
                            input=snippet,
                            text=True,
                            check=True,
                            capture_output=True
                        )
                    else:
                        with conf_file.open("a", encoding="utf-8") as f:
                            f.write(snippet)
                    logger.info("Configured %s with permanent extension", conf_file)

            # 4. User desktop launcher override for GNOME Dash
            desk_file.parent.mkdir(parents=True, exist_ok=True)
            src_desktop = Path("/usr/share/applications/chromium-browser.desktop")
            src_content = ""
            if src_desktop.exists():
                src_content = src_desktop.read_text(encoding="utf-8")
            elif desk_file.exists():
                src_content = desk_file.read_text(encoding="utf-8")

            if src_content:
                lines = src_content.splitlines()
                new_lines = []
                ext_flag = f"--load-extension={ext_dir_path}"
                for line in lines:
                    if line.startswith("Exec=") and ext_flag not in line:
                        if "%U" in line:
                            line = line.replace("%U", f"{ext_flag} %U")
                        else:
                            line = f"{line} {ext_flag}"
                    new_lines.append(line)
                desk_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                logger.info("Created desktop launcher override at %s", desk_file)

            return True, "In-Page Controls extension installed into Chromium."
        except Exception as e:
            logger.exception("Failed installing permanent extension into Chromium")
            return False, f"Installation failed: {e}"

    @classmethod
    def uninstall_permanent_extension(cls, chromium_conf: Path | None = None, desktop_path: Path | None = None) -> tuple[bool, str]:
        """
        Removes permanent SUIT In-Page Controls extension from Chromium:
        1. Strips load-extension snippet from /etc/chromium/chromium.conf
        2. Removes user desktop launcher override
        3. Cleans system native messaging manifest
        """
        conf_file = chromium_conf or cls.CHROMIUM_CONF_PATH
        desk_file = desktop_path or cls.USER_DESKTOP_PATH

        try:
            # 1. Clean /etc/chromium/chromium.conf
            if conf_file.exists():
                content = conf_file.read_text(encoding="utf-8")
                if cls.SUIT_MARKER_START in content:
                    lines = content.splitlines()
                    cleaned_lines = []
                    skipping = False
                    for line in lines:
                        if cls.SUIT_MARKER_START in line:
                            skipping = True
                            continue
                        if cls.SUIT_MARKER_END in line:
                            skipping = False
                            continue
                        if not skipping:
                            cleaned_lines.append(line)
                    new_content = "\n".join(cleaned_lines).strip() + "\n"
                    if conf_file == cls.CHROMIUM_CONF_PATH:
                        subprocess.run(
                            ["sudo", "-n", "tee", str(conf_file)],
                            input=new_content,
                            text=True,
                            check=True,
                            capture_output=True
                        )
                    else:
                        conf_file.write_text(new_content, encoding="utf-8")
                    logger.info("Cleaned %s", conf_file)

            # 2. Remove user desktop launcher override
            if desk_file.exists():
                desk_file.unlink()
                logger.info("Removed %s", desk_file)

            # 3. Remove system native messaging manifest
            system_manifest = cls.SYSTEM_NATIVE_MESSAGING_DIR / f"{cls.NATIVE_HOST_NAME}.json"
            try:
                subprocess.run(["sudo", "-n", "rm", "-f", str(system_manifest)], check=False, capture_output=True)
            except Exception:
                pass

            return True, "In-Page Controls extension removed from Chromium."
        except Exception as e:
            logger.exception("Failed uninstalling permanent extension from Chromium")
            return False, f"Removal failed: {e}"

    @classmethod
    def ensure_native_messaging_host(cls, host_script_path: Path | None = None) -> bool:
        """
        Installs the Native Messaging Host JSON manifest for Chromium, Google Chrome, and Brave.
        """
        script_path = host_script_path or cls.HOST_SCRIPT
        try:
            if script_path.exists():
                script_path.chmod(0o755)

            manifest_content = {
                "name": cls.NATIVE_HOST_NAME,
                "description": "SUIT Kiosk System Control Bridge",
                "path": str(script_path.resolve()),
                "type": "stdio",
                "allowed_origins": [
                    f"chrome-extension://{cls.EXTENSION_ID}/"
                ]
            }

            for host_dir in NATIVE_MESSAGING_DIRS:
                host_dir.mkdir(parents=True, exist_ok=True)
                manifest_file = host_dir / f"{cls.NATIVE_HOST_NAME}.json"
                manifest_file.write_text(json.dumps(manifest_content, indent=2), encoding="utf-8")
                logger.debug(f"Installed native messaging manifest: {manifest_file}")

            logger.info("Ensured SUIT Kiosk native messaging host manifests across browsers")
            return True
        except Exception:
            logger.exception("Failed installing native messaging host manifest")
            return False

    @classmethod
    def get_kiosk_flags(cls, enable_controls: bool = True) -> list[str]:
        flags = [
            "--ozone-platform=wayland",
            "--kiosk",
            "--noerrdialogs",
            "--disable-session-crashed-bubble",
            "--disable-infobars",
            "--check-for-update-interval=31536000"
        ]
        if enable_controls:
            ext_dir = cls.INSTALLED_EXTENSION_DIR if cls.INSTALLED_EXTENSION_DIR.exists() else cls.EXTENSION_DIR
            if ext_dir.exists():
                flags.append(f"--load-extension={ext_dir.resolve()}")
        return flags

    @classmethod
    def clean_stale_singleton_locks(cls) -> None:
        """Safely removes stale Singleton lock files from browser config directories."""
        for name in ["chromium", "google-chrome", "BraveSoftware/Brave-Browser"]:
            pdir = Path.home() / ".config" / name
            if pdir.exists():
                for lock in pdir.glob("Singleton*"):
                    try:
                        lock.unlink(missing_ok=True)
                        logger.debug(f"Removed stale lock: {lock}")
                    except Exception as e:
                        logger.debug(f"Failed removing {lock}: {e}")

    @classmethod
    def ensure_launcher_script(cls) -> Path:
        bin_dir = Path.home() / ".local" / "share" / "suit" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        script_path = bin_dir / "launch-kiosk.sh"
        content = """#!/usr/bin/env bash
# SUIT Autodarts Kiosk Launcher
# Remove stale lock files to ensure unattended boot never hangs after crash or hostname change
rm -f "$HOME/.config/chromium/Singleton"* "$HOME/.config/google-chrome/Singleton"* 2>/dev/null

# Automatically dismiss GNOME Shell Overview on startup so kiosk displays fullscreen seamlessly
(
    for i in $(seq 1 40); do
        if [ "$(busctl --user get-property org.gnome.Shell /org/gnome/Shell org.gnome.Shell OverviewActive 2>/dev/null)" = "b true" ]; then
            gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell --method org.freedesktop.DBus.Properties.Set org.gnome.Shell OverviewActive "<false>" >/dev/null 2>&1
        fi
        sleep 0.25
    done
) &

exec "$@"
"""
        script_path.write_text(content, encoding="utf-8")
        script_path.chmod(0o755)
        return script_path

    @classmethod
    def find_browser_binary(cls) -> str:
        for candidate in ["chromium-browser", "chromium", "google-chrome", "brave-browser"]:
            if shutil.which(candidate):
                return candidate
        return "chromium"

    @classmethod
    def resolve_browser(cls, browser: str | None = None) -> str:
        if not browser:
            return cls.find_browser_binary()
        if not shutil.which(browser):
            detected = cls.find_browser_binary()
            if shutil.which(detected):
                return detected
        return browser

    @classmethod
    def generate_desktop_entry(cls, url: str, browser: str = "chromium", enable_controls: bool = True) -> str:
        launcher = cls.ensure_launcher_script()
        actual_browser = cls.resolve_browser(browser)
        flags = cls.get_kiosk_flags(enable_controls=enable_controls)
        cmd = f'{launcher} {actual_browser} {" ".join(flags)} "{url}"'
        return f"""[Desktop Entry]
Type=Application
Name=Autodarts Kiosk
Exec={cmd}
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
"""

    @classmethod
    def set_autostart(cls, enabled: bool, url: str = "https://play.autodarts.com/", browser: str = "chromium", enable_controls: bool = True, desktop_file: Path | None = None) -> bool:
        target_file = desktop_file or DEFAULT_DESKTOP_FILE
        try:
            if not enabled:
                if target_file.exists():
                    target_file.unlink()
                logger.info(f"Removed kiosk autostart file: {target_file}")
                return True

            if enable_controls:
                cls.ensure_native_messaging_host()

            target_file.parent.mkdir(parents=True, exist_ok=True)
            entry = cls.generate_desktop_entry(url, browser, enable_controls=enable_controls)
            target_file.write_text(entry, encoding="utf-8")
            target_file.chmod(0o644)
            logger.info(f"Created kiosk autostart file: {target_file}")
            return True
        except Exception:
            logger.exception("Failed configuring kiosk autostart")
            return False

    @classmethod
    def is_autostart_enabled(cls, desktop_file: Path | None = None) -> bool:
        target_file = desktop_file or DEFAULT_DESKTOP_FILE
        return target_file.exists()

    @classmethod
    def is_power_controls_enabled(cls, desktop_file: Path | None = None) -> bool:
        target_file = desktop_file or DEFAULT_DESKTOP_FILE
        if not target_file.exists():
            return True
        try:
            content = target_file.read_text(encoding="utf-8")
            return "--load-extension" in content
        except Exception:
            return True

