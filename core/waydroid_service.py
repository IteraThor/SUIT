import os
import sys
import subprocess
import time
import shutil
import zipfile
import tempfile
import re
import ast
from pathlib import Path
from core.logger import get_logger

logger = get_logger("waydroid")


try:
    from core.private.darts_scorer_service import DartsScorerService
    HAS_PRIVATE_DARTS_SCORER = True
except ImportError:
    DartsScorerService = None
    HAS_PRIVATE_DARTS_SCORER = False


class WaydroidService:
    AURORA_APK_URL = "https://gitlab.com/-/project/6922885/uploads/b9f5d827145461a2195699660545160a/AuroraStore-4.8.3.apk"
    APKEEP_BINARY_URL = getattr(DartsScorerService, "APKEEP_BINARY_URL", "")

    @staticmethod
    def is_waydroid_installed() -> bool:
        return shutil.which("waydroid") is not None

    @staticmethod
    def is_waydroid_initialized() -> bool:
        return Path("/var/lib/waydroid/images/system.img").exists() or Path("/var/lib/waydroid/system.img").exists()

    @staticmethod
    def is_aurora_store_installed() -> bool:
        desktop = Path.home() / ".local/share/applications/waydroid.com.aurora.store.desktop"
        if desktop.exists():
            return True
        try:
            res = subprocess.run(
                ["sudo", "-n", "waydroid", "shell"],
                input="pm list packages com.aurora.store\n",
                text=True,
                capture_output=True,
                timeout=3
            )
            return "package:com.aurora.store" in res.stdout
        except Exception:
            return False

    @staticmethod
    def is_darts_scorer_installed() -> bool:
        desktop = Path.home() / ".local/share/applications/waydroid.de.muetzner.dartsscorer.desktop"
        if desktop.exists():
            return True
        try:
            res = subprocess.run(
                ["sudo", "-n", "lxc-attach", "-P", "/var/lib/waydroid/lxc", "-n", "waydroid", "--", "pm", "list", "packages", "de.muetzner.dartsscorer"],
                capture_output=True,
                text=True,
                timeout=3
            )
            return "package:de.muetzner.dartsscorer" in res.stdout
        except Exception:
            return False

    @staticmethod
    def is_arm_translation_installed() -> bool:
        overlay_so = Path("/var/lib/waydroid/overlay/system/lib/libndk_translation.so")
        if overlay_so.exists():
            return True
        try:
            res = subprocess.run(
                ["sudo", "-n", "lxc-attach", "-P", "/var/lib/waydroid/lxc", "-n", "waydroid", "--", "getprop", "ro.product.cpu.abilist"],
                capture_output=True,
                text=True,
                timeout=3
            )
            return "arm" in res.stdout.lower()
        except Exception:
            return False

    @classmethod
    def get_waydroid_status(cls) -> dict:
        installed = cls.is_waydroid_installed()
        initialized = cls.is_waydroid_initialized() if installed else False
        if not initialized:
            return {
                "installed": installed,
                "initialized": False,
                "aurora": False,
                "darts_scorer": False,
                "arm_translation": False,
                "ready": False
            }
        aurora = cls.is_aurora_store_installed()
        darts_scorer = cls.is_darts_scorer_installed()
        arm_translation = cls.is_arm_translation_installed()
        return {
            "installed": installed,
            "initialized": initialized,
            "aurora": aurora,
            "darts_scorer": darts_scorer,
            "arm_translation": arm_translation,
            "ready": installed and initialized and (darts_scorer or aurora)
        }

    @staticmethod
    def get_system_locale() -> str:
        try:
            res = subprocess.run(["localectl", "status"], capture_output=True, text=True)
            for line in res.stdout.splitlines():
                if "System Locale" in line or "LANG=" in line:
                    if "LANG=" in line:
                        lang = line.split("LANG=")[1].split()[0].replace('"', '')
                        if "_" in lang:
                            parts = lang.split(".")[0].split("_")
                            return f"{parts[0]}-{parts[1]}"
                        return lang
        except Exception:
            pass
        return "de-DE" if "de" in (os.environ.get("LANG", "").lower()) else "en-US"

    @classmethod
    def install_waydroid(cls, locale: str | None = None) -> tuple[bool, str]:
        try:
            loc = locale or cls.get_system_locale()
            subprocess.run(["sudo", "-n", "dnf", "install", "-y", "waydroid"], check=True, capture_output=True)
            subprocess.run(["sudo", "-n", "systemctl", "enable", "--now", "waydroid-container"], check=True, capture_output=True)
            init_cmd = [
                "sudo", "-n", "waydroid", "init",
                "-s", "VANILLA",
                "-c", "https://ota.waydro.id/system",
                "-v", "https://ota.waydro.id/vendor"
            ]
            subprocess.run(init_cmd, check=True, capture_output=True)
            subprocess.run(["sudo", "-n", "waydroid", "prop", "set", "persist.sys.locale", loc], check=False)
            logger.info(f"Waydroid installed and initialized with locale {loc}")
            return True, f"Waydroid installed and initialized ({loc})."
        except Exception as e:
            logger.exception("Failed installing Waydroid")
            return False, f"Failed installing Waydroid: {e}"

    @classmethod
    def waydroid_debloat_and_seamless(cls) -> tuple[bool, str]:
        try:
            loc = cls.get_system_locale()
            subprocess.run(["sudo", "-n", "waydroid", "shell", "settings", "put", "system", "system_locales", loc], check=False)
            
            disables = [
                "pm disable-user --user 0 org.lineageos.jelly",
                "pm disable-user --user 0 com.android.calculator2",
                "pm disable-user --user 0 com.android.providers.calendar",
                "pm disable-user --user 0 org.lineageos.etar",
                "pm disable-user --user 0 org.lineageos.aperture",
                "pm disable-user --user 0 com.android.deskclock",
                "pm disable-user --user 0 com.android.contacts",
                "pm disable-user --user 0 com.android.documentsui",
                "pm disable-user --user 0 com.android.gallery3d",
                "pm disable-user --user 0 org.lineageos.eleven",
                "pm disable-user --user 0 org.lineageos.recorder",
                "am force-stop com.android.launcher3",
                "settings put global policy_control immersive.full=*"
            ]
            payload = "\n".join(disables)
            subprocess.run(f"printf '{payload}' | sudo -n waydroid shell", shell=True, check=False)
            
            # Configure native fullscreen mode rather than floating freeform window
            subprocess.run(["waydroid", "prop", "set", "persist.waydroid.multi_windows", "false"], check=False)
            subprocess.run(["waydroid", "prop", "set", "persist.waydroid.dark_theme", "true"], check=False)

            # Match display resolution for true fullscreen rendering
            width, height = cls.get_primary_screen_resolution()
            subprocess.run(["waydroid", "prop", "set", "persist.waydroid.width", str(width)], check=False)
            subprocess.run(["waydroid", "prop", "set", "persist.waydroid.height", str(height)], check=False)

            # Enable GNOME F11 fullscreen keybinding if unset
            try:
                subprocess.run(["gsettings", "set", "org.gnome.desktop.wm.keybindings", "toggle-fullscreen", "['F11']"], check=False)
            except Exception:
                pass

            logger.info(f"Waydroid debloat and fullscreen configured ({width}x{height})")
            return True, "Waydroid debloat and fullscreen configured."
        except Exception as e:
            logger.exception("Failed configuring Waydroid debloat and fullscreen")
            return False, f"Failed configuring Waydroid debloat: {e}"

    @classmethod
    def install_aurora_store(cls) -> tuple[bool, str]:
        try:
            bin_dir = Path.home() / ".local/share/suit/bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            apk_path = bin_dir / "AuroraStore-4.8.3.apk"
            if not apk_path.exists():
                subprocess.run(["wget", "-q", "-O", str(apk_path), cls.AURORA_APK_URL], check=True)

            # Ensure waydroid container service is running
            subprocess.run(["sudo", "-n", "systemctl", "start", "waydroid-container"], check=False)

            # Ensure waydroid session is active
            status_res = subprocess.run(["waydroid", "status"], capture_output=True, text=True)
            if "Session:\tRUNNING" not in status_res.stdout and "Session: RUNNING" not in status_res.stdout:
                subprocess.Popen(["waydroid", "session", "start"])
                time.sleep(3)

            # Install APK directly into Waydroid via native app install command
            proc = subprocess.run(
                ["waydroid", "app", "install", str(apk_path)],
                capture_output=True,
                text=True
            )
            if proc.returncode != 0:
                logger.warning(f"waydroid app install failed: {proc.stderr}. Trying pm install fallback...")
                # Fallback to direct pm install via root shell
                waydroid_tmp = Path.home() / ".local/share/waydroid/data/local/tmp"
                waydroid_tmp.mkdir(parents=True, exist_ok=True)
                target_apk = waydroid_tmp / "aurora.apk"
                shutil.copy2(apk_path, target_apk)
                target_apk.chmod(0o666)
                subprocess.run(
                    ["sudo", "-n", "waydroid", "shell"],
                    input="pm install -r /data/local/tmp/aurora.apk\n",
                    text=True,
                    capture_output=True
                )

            # Grant essential installation and storage permissions to Aurora Store
            perms_payload = (
                "appops set com.aurora.store REQUEST_INSTALL_PACKAGES allow\n"
                "pm grant com.aurora.store android.permission.READ_EXTERNAL_STORAGE\n"
                "pm grant com.aurora.store android.permission.WRITE_EXTERNAL_STORAGE\n"
                "appops set com.aurora.store MANAGE_EXTERNAL_STORAGE allow\n"
            )
            subprocess.run(
                ["sudo", "-n", "waydroid", "shell"],
                input=perms_payload,
                text=True,
                capture_output=True,
                check=False
            )

            # Ensure desktop launcher exists for GNOME App Grid and SUIT launcher
            apps_dir = Path.home() / ".local/share/applications"
            apps_dir.mkdir(parents=True, exist_ok=True)
            desktop_file = apps_dir / "waydroid.com.aurora.store.desktop"
            if not desktop_file.exists():
                desktop_content = (
                    "[Desktop Entry]\n"
                    "Name=Aurora Store\n"
                    "Comment=Open source client for Google Play Store\n"
                    "Exec=waydroid app launch com.aurora.store\n"
                    "Icon=com.aurora.store\n"
                    "Terminal=false\n"
                    "Type=Application\n"
                    "Categories=Utility;\n"
                )
                desktop_file.write_text(desktop_content, encoding="utf-8")
                subprocess.run(["update-desktop-database", str(apps_dir)], check=False)

            logger.info("Aurora Store installed into Waydroid successfully")
            return True, "Aurora Store installed into Waydroid."
        except Exception as e:
            logger.exception("Failed installing Aurora Store")
            return False, f"Failed installing Aurora Store: {e}"

    @classmethod
    def full_waydroid_setup(cls) -> tuple[bool, str]:
        ok, msg = cls.install_waydroid()
        if not ok:
            return False, msg
        cls.waydroid_debloat_and_seamless()
        ok_a, msg_a = cls.install_aurora_store()
        return True, "Waydroid, seamless mode, and Aurora Store installed successfully."

    @classmethod
    def install_libndk(cls) -> tuple[bool, str]:
        try:
            logger.info("Installing libndk ARM translation layer for Waydroid...")
            # Stop user session while modifying container rootfs/overlay
            subprocess.run(["waydroid", "session", "stop"], check=False, capture_output=True)
            script_dir = Path(tempfile.gettempdir()) / "waydroid_script"
            if script_dir.exists():
                shutil.rmtree(script_dir, ignore_errors=True)

            subprocess.run(["git", "clone", "--depth=1", "https://github.com/casualsnek/waydroid_script", str(script_dir)], check=True, capture_output=True)
            venv_dir = script_dir / "venv"
            subprocess.run(["python3", "-m", "venv", str(venv_dir)], check=True, capture_output=True)
            pip_bin = venv_dir / "bin/pip"
            py_bin = venv_dir / "bin/python"
            subprocess.run([str(pip_bin), "install", "-r", str(script_dir / "requirements.txt")], check=True, capture_output=True)
            subprocess.run(["sudo", "-n", str(py_bin), str(script_dir / "main.py"), "install", "libndk"], check=True, capture_output=True)

            # Clean up temporary clone & unpack directories to conserve disk space
            shutil.rmtree(script_dir, ignore_errors=True)
            subprocess.run(["sudo", "-n", "rm", "-rf", "/tmp/libndkunpack", str(Path.home() / ".cache/waydroid-script")], check=False)

            # Restart waydroid container service so the overlay and properties take effect
            subprocess.run(["sudo", "-n", "systemctl", "restart", "waydroid-container"], check=True, capture_output=True)
            logger.info("libndk ARM translation installed successfully")
            return True, "ARM translation (libndk) installed."
        except Exception as e:
            logger.exception("Failed installing libndk ARM translation")
            return False, f"Failed installing ARM translation: {e}"

    @classmethod
    def has_private_darts_scorer(cls) -> bool:
        return HAS_PRIVATE_DARTS_SCORER

    @classmethod
    def is_darts_scorer_installed(cls) -> bool:
        if DartsScorerService:
            return DartsScorerService.is_darts_scorer_installed()
        desktop = Path.home() / ".local/share/applications/waydroid.de.muetzner.dartsscorer.desktop"
        return desktop.exists()

    @classmethod
    def ensure_apkeep(cls) -> Path | None:
        if DartsScorerService:
            return DartsScorerService.ensure_apkeep()
        return None

    @classmethod
    def wait_for_android_package_service(cls, timeout: int = 60) -> bool:
        if DartsScorerService:
            return DartsScorerService.wait_for_android_package_service(timeout=timeout)
        return False

    @classmethod
    def install_darts_scorer(cls, progress_callback=None) -> tuple[bool, str]:
        if DartsScorerService:
            return DartsScorerService.install_darts_scorer(progress_callback=progress_callback)
        return False, "Darts Scorer automated installer is not available."

    @staticmethod
    def get_primary_screen_resolution() -> tuple[int, int]:
        try:
            from core.display_service import DisplayService
            monitors = DisplayService.get_monitors()
            if monitors:
                prim = next((m for m in monitors if m.get("is_primary")), monitors[0])
                w, h = prim.get("w", 0), prim.get("h", 0)
                rot = prim.get("rotation", 0)
                if rot in (1, 3):
                    w, h = h, w
                if w > 0 and h > 0:
                    return w, h
        except Exception:
            pass

        try:
            import xml.etree.ElementTree as ET
            mon_xml = Path.home() / ".config/monitors.xml"
            if mon_xml.exists():
                root = ET.fromstring(mon_xml.read_text())
                for lm in root.iter("logicalmonitor"):
                    rot = "normal"
                    for t in lm.iter("rotation"):
                        rot = (t.text or "").strip()
                    for mode in lm.iter("mode"):
                        w = int(mode.findtext("width", "0"))
                        h = int(mode.findtext("height", "0"))
                        if rot in ("right", "left"):
                            return h, w
                        return w, h
        except Exception:
            pass

        return 1200, 1920

    @classmethod
    def pin_darts_scorer_to_dash(cls) -> bool:
        if DartsScorerService:
            return DartsScorerService.pin_darts_scorer_to_dash()
        return False

    @classmethod
    def ensure_darts_scorer_launcher(cls) -> Path | None:
        if DartsScorerService:
            return DartsScorerService.ensure_darts_scorer_launcher()
        return None

    @classmethod
    def full_darts_scorer_setup(cls, progress_callback=None) -> tuple[bool, str]:
        if not HAS_PRIVATE_DARTS_SCORER:
            return False, "Darts Scorer automated setup is not available."
        try:
            # Step 1: Ensure Waydroid is installed and initialized
            if not cls.is_waydroid_installed() or not cls.is_waydroid_initialized():
                if progress_callback:
                    progress_callback("Installing Waydroid packages...", 0.10)
                logger.info("Setting up Waydroid base engine...")
                ok_inst, msg_inst = cls.install_waydroid()
                if not ok_inst:
                    return False, msg_inst

            # Step 2: Ensure debloat, native fullscreen resolution and preferences configured
            if progress_callback:
                progress_callback("Configuring fullscreen resolution...", 0.30)
            logger.info("Configuring fullscreen resolution and debloat...")
            cls.waydroid_debloat_and_seamless()

            # Step 3: Ensure ARM translation layer (libndk) is active
            if not cls.is_arm_translation_installed():
                if progress_callback:
                    progress_callback("Installing ARM translation (libndk)...", 0.45)
                logger.info("Installing ARM translation layer...")
                ok_ndk, msg_ndk = cls.install_libndk()
                if not ok_ndk:
                    return False, msg_ndk

            # Step 4: Ensure container & session active
            if progress_callback:
                progress_callback("Starting Android container...", 0.60)
            subprocess.run(["sudo", "-n", "systemctl", "start", "waydroid-container"], check=False)
            status_res = subprocess.run(["waydroid", "status"], capture_output=True, text=True)
            if "Session:\tRUNNING" not in status_res.stdout and "Session: RUNNING" not in status_res.stdout:
                subprocess.run(["waydroid", "session", "stop"], capture_output=True, text=True)
                subprocess.Popen(["waydroid", "session", "start"])
                time.sleep(3)

            # Step 5: Install Darts Scorer if not yet installed
            if not cls.is_darts_scorer_installed():
                ok_apk, msg_apk = cls.install_darts_scorer(progress_callback=progress_callback)
                if not ok_apk:
                    return False, msg_apk

            # Step 6: Create fullscreen launcher and pin to GNOME Dash
            if progress_callback:
                progress_callback("Pinning Darts Scorer to Dash...", 0.95)
            cls.ensure_darts_scorer_launcher()
            cls.pin_darts_scorer_to_dash()

            if progress_callback:
                progress_callback("Darts Scorer is ready!", 1.0)

            return True, "Darts Scorer installed and ready to launch."
        except Exception as e:
            logger.exception("Full Darts Scorer setup failed")
            return False, f"Setup failed: {e}"

    @staticmethod
    def is_virtual_machine() -> tuple[bool, str]:
        try:
            res = subprocess.run(["systemd-detect-virt"], capture_output=True, text=True)
            virt = res.stdout.strip()
            if res.returncode == 0 and virt and virt != "none":
                return True, virt
        except Exception:
            pass
        return False, "none"

    @classmethod
    def launch_aurora_store(cls) -> tuple[bool, str]:
        try:
            is_vm, virt_type = cls.is_virtual_machine()

            c_check = subprocess.run(["systemctl", "is-active", "waydroid-container"], capture_output=True, text=True)
            if c_check.stdout.strip() != "active":
                subprocess.run(["sudo", "-n", "systemctl", "start", "waydroid-container"], check=False)

            status_res = subprocess.run(["waydroid", "status"], capture_output=True, text=True)
            if "Session:\tRUNNING" not in status_res.stdout and "Session: RUNNING" not in status_res.stdout:
                subprocess.run(["waydroid", "session", "stop"], capture_output=True, text=True)
                subprocess.Popen(["waydroid", "session", "start"])
                time.sleep(2)

            subprocess.Popen(["waydroid", "app", "launch", "com.aurora.store"])
            if is_vm:
                return True, f"Launched Aurora Store. Note: Running in a virtual machine ({virt_type}). Waydroid requires physical GPU hardware acceleration (Intel/AMD) to render UI windows."
            return True, "Launched Aurora Store. Search for 'Darts Scorer' to install or open."
        except Exception as e:
            logger.exception("Failed launching Aurora Store")
            return False, f"Failed launching Aurora Store: {e}"

    @classmethod
    def launch_darts_scorer(cls) -> tuple[bool, str]:
        try:
            if cls.is_darts_scorer_installed():
                launcher = cls.ensure_darts_scorer_launcher()
                cls.pin_darts_scorer_to_dash()
                if launcher:
                    subprocess.Popen([str(launcher)])
                    is_vm, virt_type = cls.is_virtual_machine()
                    if is_vm:
                        return True, f"Launched Darts Scorer. Note: Running in a virtual machine ({virt_type})."
                    return True, "Launched Darts Scorer."
        except Exception:
            pass
        return cls.launch_aurora_store()
