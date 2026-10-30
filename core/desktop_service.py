import os
import copy
import json
import subprocess
import ast
import shutil
from pathlib import Path
from core.logger import get_logger

logger = get_logger("desktop")

EXTENSION_UUID = "touch-scaling@suit"
EXTENSION_SOURCE_DIR = Path(__file__).resolve().parent.parent / "assets" / "touch_scaling_extension"
USER_EXTENSION_DIR = Path.home() / ".local" / "share" / "gnome-shell" / "extensions" / EXTENSION_UUID
HW_OVERLAY_UUID = "hw-overlay@suit"
HW_OVERLAY_SOURCE_DIR = Path(__file__).resolve().parent.parent / "assets" / "hw_overlay_extension"
HW_OVERLAY_USER_DIR = Path.home() / ".local" / "share" / "gnome-shell" / "extensions" / HW_OVERLAY_UUID
# Watched by the extension, which redraws live on every save (see loadConfig in extension.js)
HW_OVERLAY_CONFIG_PATH = Path.home() / ".config" / "suit" / "hw_overlay.json"
MINIMAL_DESKTOP_UUID = "minimal-desktop@suit"
MINIMAL_DESKTOP_SOURCE_DIR = Path(__file__).resolve().parent.parent / "assets" / "minimal_desktop_extension"
MINIMAL_DESKTOP_USER_DIR = Path.home() / ".local" / "share" / "gnome-shell" / "extensions" / MINIMAL_DESKTOP_UUID
HW_OVERLAY_METRICS = ("cpu", "temp", "ram", "gpu", "disk", "net", "ip_local", "ip_tailscale", "load", "uptime",
                      "ad_state", "ad_fps", "ad_cams", "ad_proc")
# Clickable Autodarts controls: start/stop, reset, debug snapshot (opt-in) and the update button,
# which only shows while Autodarts reports a newer release (on by default)
HW_OVERLAY_BUTTONS = ("ad_toggle", "ad_reset", "ad_dump", "ad_update")
HW_OVERLAY_DEFAULTS = {
    "style": "card",
    "position": "top-right",
    "size": "medium",
    "accent": "blue",
    "opacity": 70,
    "meters": True,
    "headings": True,
    "button_colors": False,
    "interval": 2,
    "metrics": {m: m not in ("net", "ip_local", "ip_tailscale", "load", "ad_proc") for m in HW_OVERLAY_METRICS},
    "buttons": {b: b == "ad_update" for b in HW_OVERLAY_BUTTONS},
}
HW_OVERLAY_CHOICES = {
    "style": ("card", "minimal"),
    "position": ("top-right", "top-left", "bottom-right", "bottom-left"),
    "size": ("small", "medium", "large"),
    "accent": ("blue", "teal", "green", "purple", "white"),
    "interval": (1, 2, 5),
}
GTK4_CSS_PATH = Path.home() / ".config" / "gtk-4.0" / "gtk.css"
GTK3_CSS_PATH = Path.home() / ".config" / "gtk-3.0" / "gtk.css"

SUIT_WALLPAPER_ASSET = Path(__file__).resolve().parent.parent / "assets" / "wallpaper_stealth_grey_pattern_ultrasubtle.png"
SUIT_WALLPAPER_INSTALLED = Path.home() / ".local" / "share" / "suit" / "wallpaper_stealth_grey_pattern_ultrasubtle.png"

SUIT_TOUCH_MARKER_START = "/* --- SUIT TOUCH SCALING START --- */"
SUIT_TOUCH_MARKER_END = "/* --- SUIT TOUCH SCALING END --- */"

TOUCH_CSS_BLOCK = f"""{SUIT_TOUCH_MARKER_START}
windowcontrols button,
headerbar button.titlebutton,
.titlebutton {{
  min-width: 26px;
  min-height: 26px;
  padding: 2px;
}}

windowcontrols button image,
headerbar button.titlebutton image,
.titlebutton image {{
  -gtk-icon-size: 14px;
}}
{SUIT_TOUCH_MARKER_END}
"""


class DesktopService:
    EXTENSION_UUID = EXTENSION_UUID
    EXTENSION_SOURCE_DIR = EXTENSION_SOURCE_DIR
    USER_EXTENSION_DIR = USER_EXTENSION_DIR
    HW_OVERLAY_UUID = HW_OVERLAY_UUID
    HW_OVERLAY_SOURCE_DIR = HW_OVERLAY_SOURCE_DIR
    HW_OVERLAY_USER_DIR = HW_OVERLAY_USER_DIR
    HW_OVERLAY_CONFIG_PATH = HW_OVERLAY_CONFIG_PATH
    HW_OVERLAY_METRICS = HW_OVERLAY_METRICS
    HW_OVERLAY_BUTTONS = HW_OVERLAY_BUTTONS
    HW_OVERLAY_DEFAULTS = HW_OVERLAY_DEFAULTS
    MINIMAL_DESKTOP_UUID = MINIMAL_DESKTOP_UUID
    MINIMAL_DESKTOP_SOURCE_DIR = MINIMAL_DESKTOP_SOURCE_DIR
    MINIMAL_DESKTOP_USER_DIR = MINIMAL_DESKTOP_USER_DIR
    GTK4_CSS_PATH = GTK4_CSS_PATH
    GTK3_CSS_PATH = GTK3_CSS_PATH
    SUIT_TOUCH_MARKER_START = SUIT_TOUCH_MARKER_START
    SUIT_TOUCH_MARKER_END = SUIT_TOUCH_MARKER_END
    TOUCH_CSS_BLOCK = TOUCH_CSS_BLOCK

    @staticmethod
    def is_dark_mode_enabled() -> bool:
        try:
            res = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"], capture_output=True, text=True)
            return "prefer-dark" in res.stdout
        except Exception:
            return False

    @staticmethod
    def is_suit_wallpaper_set() -> bool:
        try:
            res = subprocess.run(["gsettings", "get", "org.gnome.desktop.background", "picture-uri-dark"], capture_output=True, text=True)
            return "wallpaper_stealth_grey_pattern_ultrasubtle" in res.stdout
        except Exception:
            return False

    @staticmethod
    def is_map_wallpaper_set() -> bool:
        """Legacy check kept for backwards compatibility."""
        try:
            res = subprocess.run(["gsettings", "get", "org.gnome.desktop.background", "picture-uri-dark"], capture_output=True, text=True)
            return "map-d.svg" in res.stdout
        except Exception:
            return False

    @staticmethod
    def is_system_app_folder_configured() -> bool:
        try:
            res = subprocess.run(["gsettings", "get", "org.gnome.desktop.app-folders", "folder-children"], capture_output=True, text=True)
            return "System" in res.stdout
        except Exception:
            return False

    @classmethod
    def is_touch_scaling_enabled(
        cls,
        gtk4_path: Path | None = None,
        ext_uuid: str | None = None
    ) -> bool:
        path = gtk4_path or cls.GTK4_CSS_PATH

        gtk_enabled = False
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
                gtk_enabled = cls.SUIT_TOUCH_MARKER_START in content
            except Exception:
                gtk_enabled = False

        try:
            res = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "text-scaling-factor"], capture_output=True, text=True)
            line = res.stdout.strip().splitlines()[0] if res.stdout.strip() else "1.0"
            factor = float(line)
            scale_enabled = factor > 1.05
        except Exception:
            scale_enabled = False

        return gtk_enabled and scale_enabled

    @staticmethod
    def is_osk_enabled() -> bool:
        try:
            res = subprocess.run(["gsettings", "get", "org.gnome.desktop.a11y.applications", "screen-keyboard-enabled"], capture_output=True, text=True)
            return "true" in res.stdout.lower()
        except Exception:
            return False

    @staticmethod
    def set_osk(enabled: bool):
        val = "true" if enabled else "false"
        subprocess.run(["gsettings", "set", "org.gnome.desktop.a11y.applications", "screen-keyboard-enabled", val], check=False)

    @staticmethod
    def get_button_layout() -> str:
        try:
            res = subprocess.run(["gsettings", "get", "org.gnome.desktop.wm.preferences", "button-layout"], capture_output=True, text=True)
            return res.stdout.strip().strip("'\"")
        except Exception:
            return "appmenu:close"

    @staticmethod
    def set_button_layout(layout: str):
        subprocess.run(["gsettings", "set", "org.gnome.desktop.wm.preferences", "button-layout", f"'{layout}'"], check=False)

    @classmethod
    def set_touch_scaling(
        cls,
        enabled: bool,
        source_dir: Path | None = None,
        target_dir: Path | None = None,
        gtk4_path: Path | None = None,
        gtk3_path: Path | None = None,
        ext_uuid: str | None = None,
        scale_factor: float = 1.2
    ) -> tuple[bool, str]:
        uuid = ext_uuid or cls.EXTENSION_UUID
        src = source_dir or cls.EXTENSION_SOURCE_DIR
        dest = target_dir or cls.USER_EXTENSION_DIR
        p4 = gtk4_path or cls.GTK4_CSS_PATH
        p3 = gtk3_path or cls.GTK3_CSS_PATH

        try:
            if enabled:
                # 1. On-Screen Keyboard (OSK)
                cls.set_osk(True)

                # 2. Window Controls Button Layout (Minimize, Maximize, Close)
                cls.set_button_layout("appmenu:minimize,maximize,close")

                # 3. Text scaling factor for instant UI/panel enlargement
                subprocess.run(["gsettings", "set", "org.gnome.desktop.interface", "text-scaling-factor", str(scale_factor)], check=False)

                # 4. Install and enable the GNOME Shell extension
                DesktopService.enable_extension(uuid, src, dest)

                # 5. Inject GTK CSS
                for p in (p4, p3):
                    p.parent.mkdir(parents=True, exist_ok=True)
                    existing = p.read_text(encoding="utf-8") if p.exists() else ""
                    if cls.SUIT_TOUCH_MARKER_START in existing:
                        start_idx = existing.find(cls.SUIT_TOUCH_MARKER_START)
                        end_idx = existing.find(cls.SUIT_TOUCH_MARKER_END)
                        if end_idx != -1:
                            new_content = existing[:start_idx] + cls.TOUCH_CSS_BLOCK.strip() + existing[end_idx + len(cls.SUIT_TOUCH_MARKER_END):]
                        else:
                            new_content = existing[:start_idx] + cls.TOUCH_CSS_BLOCK.strip()
                    else:
                        new_content = (existing.rstrip() + "\n\n" + cls.TOUCH_CSS_BLOCK).strip() + "\n"
                    p.write_text(new_content, encoding="utf-8")

                logger.info("Touchscreen tweaks enabled successfully")
                return True, "Touchscreen tweaks, window controls, and on-screen keyboard applied."

            else:
                # 1. Disable On-Screen Keyboard (OSK)
                cls.set_osk(False)

                # 2. Reset Window Controls Button Layout back to close only
                cls.set_button_layout("appmenu:close")

                # 3. Reset text scaling factor back to default 1.0
                subprocess.run(["gsettings", "set", "org.gnome.desktop.interface", "text-scaling-factor", "1.0"], check=False)

                # 4. Disable GNOME Shell extension
                DesktopService.disable_extension(uuid)

                # 5. Strip GTK CSS
                for p in (p4, p3):
                    if p.exists():
                        content = p.read_text(encoding="utf-8")
                        if cls.SUIT_TOUCH_MARKER_START in content:
                            start_idx = content.find(cls.SUIT_TOUCH_MARKER_START)
                            end_idx = content.find(cls.SUIT_TOUCH_MARKER_END)
                            if end_idx != -1:
                                cleaned = content[:start_idx] + content[end_idx + len(cls.SUIT_TOUCH_MARKER_END):]
                            else:
                                cleaned = content[:start_idx]
                            cleaned = cleaned.strip()
                            if cleaned:
                                p.write_text(cleaned + "\n", encoding="utf-8")
                            else:
                                p.unlink(missing_ok=True)

                logger.info("Touchscreen tweaks disabled successfully")
                return True, "Touchscreen tweaks and on-screen keyboard restored to default."

        except Exception as e:
            logger.exception("Failed configuring touchscreen scaling")
            return False, f"Failed updating touch scaling: {e}"

    @staticmethod
    def _get_enabled_extensions() -> list[str]:
        res = subprocess.run(["gsettings", "get", "org.gnome.shell", "enabled-extensions"], capture_output=True, text=True)
        out = res.stdout.strip()
        return ast.literal_eval(out) if out.startswith("[") else []

    @staticmethod
    def _set_enabled_extensions(enabled_list: list[str]):
        subprocess.run(["gsettings", "set", "org.gnome.shell", "enabled-extensions", str(enabled_list).replace('"', "'")], check=False)

    @staticmethod
    def install_extension_files(src: Path, dest: Path) -> bool:
        """Copy a bundled shell extension into the user extension folder without enabling it.

        GNOME Shell on Wayland only discovers extension folders at login, so SUIT installs
        the files ahead of time; a switch in SUIT can then enable it instantly.
        Returns True when files were added or changed.
        """
        if not src.exists():
            return False
        changed = False
        dest.mkdir(parents=True, exist_ok=True)
        for f in src.iterdir():
            if not f.is_file():
                continue
            target = dest / f.name
            if not target.exists() or target.read_bytes() != f.read_bytes():
                shutil.copy2(str(f), str(target))
                changed = True
        if changed:
            logger.info("Shell extension files installed to %s", dest)
        return changed

    @classmethod
    def install_suit_extensions(cls) -> None:
        """Pre-install every switchable SUIT shell extension (disabled) for discovery at next login."""
        cls.install_hw_overlay_files()
        cls.install_minimal_desktop_files()

    @staticmethod
    def is_extension_loaded(uuid: str) -> bool:
        """True when the running GNOME Shell knows the extension (discovered at login)."""
        try:
            res = subprocess.run(["gnome-extensions", "info", uuid], capture_output=True, text=True)
            return res.returncode == 0
        except Exception:
            return False

    @staticmethod
    def extension_needs_relogin(uuid: str, dest: Path) -> bool:
        """True when newer extension files are installed than the running GNOME Shell has loaded.

        The shell keeps an extension's code from login, so updated files only take effect
        after logging out and back in."""
        try:
            on_disk = json.loads((dest / "metadata.json").read_text(encoding="utf-8")).get("version")
            res = subprocess.run(
                ["gnome-extensions", "info", uuid],
                capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"}
            )
        except (OSError, ValueError):
            return False
        for line in res.stdout.splitlines():
            key, _, val = line.strip().partition(":")
            if key == "Version" and val.strip().isdigit() and isinstance(on_disk, int):
                return int(val) < on_disk
        return False

    @classmethod
    def is_extension_enabled(cls, uuid: str, dest: Path) -> bool:
        try:
            return (dest / "extension.js").exists() and uuid in cls._get_enabled_extensions()
        except Exception:
            return False

    @classmethod
    def enable_extension(cls, uuid: str, src: Path, dest: Path) -> str:
        """Install and enable a bundled extension.

        Returns "active" when it runs now, "relogin" when the shell only picks it up at the
        next login, or "failed" when the shell knows it but could not start it."""
        loaded = cls.is_extension_loaded(uuid)
        cls.install_extension_files(src, dest)
        subprocess.run(["gsettings", "set", "org.gnome.shell", "disable-user-extensions", "false"], check=False)

        if loaded:
            # The shell updates enabled-extensions itself; writing the list here too
            # could race with it and drop other extensions.
            subprocess.run(["gnome-extensions", "enable", uuid], check=False)
        else:
            enabled_list = cls._get_enabled_extensions()
            if uuid not in enabled_list:
                enabled_list.append(uuid)
                cls._set_enabled_extensions(enabled_list)

        res = subprocess.run(
            ["gnome-extensions", "info", uuid],
            capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"}
        )
        logger.info("Extension %s enabled (loaded=%s)", uuid, loaded)
        if "ACTIVE" in res.stdout:
            return "active"
        # GNOME Shell on Wayland only discovers new extension folders at login
        return "failed" if loaded else "relogin"

    @classmethod
    def disable_extension(cls, uuid: str) -> None:
        if cls.is_extension_loaded(uuid):
            subprocess.run(["gnome-extensions", "disable", uuid], check=False)
        else:
            enabled_list = cls._get_enabled_extensions()
            if uuid in enabled_list:
                enabled_list.remove(uuid)
                cls._set_enabled_extensions(enabled_list)
        logger.info("Extension %s disabled", uuid)

    @classmethod
    def install_hw_overlay_files(cls, source_dir: Path | None = None, target_dir: Path | None = None) -> bool:
        return cls.install_extension_files(source_dir or cls.HW_OVERLAY_SOURCE_DIR, target_dir or cls.HW_OVERLAY_USER_DIR)

    @classmethod
    def hw_overlay_needs_relogin(cls, target_dir: Path | None = None, ext_uuid: str | None = None) -> bool:
        return cls.extension_needs_relogin(ext_uuid or cls.HW_OVERLAY_UUID, target_dir or cls.HW_OVERLAY_USER_DIR)

    @classmethod
    def is_hw_overlay_enabled(cls, target_dir: Path | None = None, ext_uuid: str | None = None) -> bool:
        return cls.is_extension_enabled(ext_uuid or cls.HW_OVERLAY_UUID, target_dir or cls.HW_OVERLAY_USER_DIR)

    @classmethod
    def set_hw_overlay(
        cls,
        enabled: bool,
        source_dir: Path | None = None,
        target_dir: Path | None = None,
        ext_uuid: str | None = None
    ) -> tuple[bool, str]:
        """Install and enable (or disable) the GNOME Shell extension that draws live
        hardware and Autodarts camera stats on the desktop wallpaper."""
        uuid = ext_uuid or cls.HW_OVERLAY_UUID
        try:
            if not enabled:
                cls.disable_extension(uuid)
                return True, "Hardware overlay hidden."
            state = cls.enable_extension(uuid, source_dir or cls.HW_OVERLAY_SOURCE_DIR, target_dir or cls.HW_OVERLAY_USER_DIR)
            if state == "active":
                return True, "Hardware overlay shown on the desktop."
            if state == "failed":
                return False, "Hardware overlay failed to start. Check the GNOME Shell log."
            return True, "Hardware overlay installed. Log out and back in once to show it."
        except Exception as e:
            logger.exception("Failed configuring hardware overlay")
            return False, f"Failed updating hardware overlay: {e}"

    @classmethod
    def install_minimal_desktop_files(cls, source_dir: Path | None = None, target_dir: Path | None = None) -> bool:
        return cls.install_extension_files(source_dir or cls.MINIMAL_DESKTOP_SOURCE_DIR, target_dir or cls.MINIMAL_DESKTOP_USER_DIR)

    @classmethod
    def minimal_desktop_needs_relogin(cls, target_dir: Path | None = None, ext_uuid: str | None = None) -> bool:
        """True when the switch is on but the running shell has not loaded the extension yet,
        or runs older files than are installed."""
        uuid = ext_uuid or cls.MINIMAL_DESKTOP_UUID
        dest = target_dir or cls.MINIMAL_DESKTOP_USER_DIR
        if cls.is_extension_enabled(uuid, dest) and not cls.is_extension_loaded(uuid):
            return True
        return cls.extension_needs_relogin(uuid, dest)

    @classmethod
    def is_minimal_desktop_enabled(cls, target_dir: Path | None = None, ext_uuid: str | None = None) -> bool:
        return cls.is_extension_enabled(ext_uuid or cls.MINIMAL_DESKTOP_UUID, target_dir or cls.MINIMAL_DESKTOP_USER_DIR)

    @classmethod
    def set_minimal_desktop(
        cls,
        enabled: bool,
        source_dir: Path | None = None,
        target_dir: Path | None = None,
        ext_uuid: str | None = None
    ) -> tuple[bool, str]:
        """Install and enable (or disable) the extension that adds the desktop dock and
        hides top bar and quick settings items an Autodarts rig never needs."""
        uuid = ext_uuid or cls.MINIMAL_DESKTOP_UUID
        try:
            if not enabled:
                cls.disable_extension(uuid)
                return True, "Minimal desktop turned off."
            state = cls.enable_extension(
                uuid, source_dir or cls.MINIMAL_DESKTOP_SOURCE_DIR, target_dir or cls.MINIMAL_DESKTOP_USER_DIR)
            if state == "active":
                return True, "Minimal desktop turned on."
            if state == "failed":
                return False, "Minimal desktop failed to start. Check the GNOME Shell log."
            return True, "Minimal desktop installed. Log out and back in once to apply it."
        except Exception as e:
            logger.exception("Failed configuring minimal desktop")
            return False, f"Failed updating minimal desktop: {e}"

    @staticmethod
    def _normalize_hw_overlay_config(raw) -> dict:
        """Defaults with every valid value from `raw` laid over them; unknown or bad values are dropped."""
        cfg = copy.deepcopy(HW_OVERLAY_DEFAULTS)
        if not isinstance(raw, dict):
            return cfg
        for key, choices in HW_OVERLAY_CHOICES.items():
            val = raw.get(key)
            if not isinstance(val, bool) and val in choices:
                cfg[key] = val
        for key in ("meters", "headings", "button_colors"):
            if isinstance(raw.get(key), bool):
                cfg[key] = raw[key]
        opacity = raw.get("opacity")
        if isinstance(opacity, (int, float)) and not isinstance(opacity, bool):
            cfg["opacity"] = int(min(100, max(0, opacity)))
        metrics = raw.get("metrics")
        if isinstance(metrics, dict):
            for m in HW_OVERLAY_METRICS:
                if isinstance(metrics.get(m), bool):
                    cfg["metrics"][m] = metrics[m]
        buttons = raw.get("buttons")
        if isinstance(buttons, dict):
            for b in HW_OVERLAY_BUTTONS:
                if isinstance(buttons.get(b), bool):
                    cfg["buttons"][b] = buttons[b]
        return cfg

    @classmethod
    def get_hw_overlay_config(cls, path: Path | None = None) -> dict:
        p = path or cls.HW_OVERLAY_CONFIG_PATH
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        return cls._normalize_hw_overlay_config(raw)

    @classmethod
    def save_hw_overlay_config(cls, cfg: dict, path: Path | None = None) -> dict:
        """Write the overlay look and metric choices; the running extension picks them up at once.

        Written to a temp file and renamed so the extension never reads a half-written file.
        Returns the config as stored."""
        p = path or cls.HW_OVERLAY_CONFIG_PATH
        clean = cls._normalize_hw_overlay_config(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, p)
        return clean

    @classmethod
    def get_visuals_status(cls) -> dict:
        dm = getattr(cls, "is_dark_mode_enabled", DesktopService.is_dark_mode_enabled)()
        is_suit = getattr(cls, "is_suit_wallpaper_set", DesktopService.is_suit_wallpaper_set)()
        is_map = getattr(cls, "is_map_wallpaper_set", DesktopService.is_map_wallpaper_set)()
        wp = is_suit or is_map
        grid = getattr(cls, "is_system_app_folder_configured", DesktopService.is_system_app_folder_configured)()
        return {
            "dark_mode": dm,
            "wallpaper": wp,
            "app_grid": grid,
            "all_ok": dm and wp and grid
        }

    @classmethod
    def apply_desktop_visuals(cls) -> tuple[bool, str]:
        try:
            # 1. Wallpaper — install bundled asset to stable user path, then apply
            SUIT_WALLPAPER_INSTALLED.parent.mkdir(parents=True, exist_ok=True)
            if SUIT_WALLPAPER_ASSET.exists():
                shutil.copy2(str(SUIT_WALLPAPER_ASSET), str(SUIT_WALLPAPER_INSTALLED))
            wallpaper_uri = f"'file://{SUIT_WALLPAPER_INSTALLED}'"
            subprocess.run(["gsettings", "set", "org.gnome.desktop.background", "picture-uri", wallpaper_uri], check=False)
            subprocess.run(["gsettings", "set", "org.gnome.desktop.background", "picture-uri-dark", wallpaper_uri], check=False)
            subprocess.run(["gsettings", "set", "org.gnome.desktop.background", "picture-options", "'zoom'"], check=False)

            # 2. Dark mode
            subprocess.run(["gsettings", "set", "org.gnome.desktop.interface", "color-scheme", "'prefer-dark'"], check=False)

            # 3. Organize App Grid
            apps_to_add = [
                "org.gnome.Ptyxis.desktop",
                "org.gnome.Terminal.desktop",
                "Waydroid.desktop",
                "waydroid.desktop",
                "waydroid.com.aurora.store.desktop",
                "org.gnome.seahorse.Application.desktop",
                "seahorse.desktop"
            ]

            # 3a. Ensure folder exists in children list
            res_child = subprocess.run(["gsettings", "get", "org.gnome.desktop.app-folders", "folder-children"], capture_output=True, text=True)
            out_child = res_child.stdout.strip()
            children = ast.literal_eval(out_child) if out_child.startswith("[") else []
            if "System" not in children:
                children.append("System")
                subprocess.run(["gsettings", "set", "org.gnome.desktop.app-folders", "folder-children", str(children).replace('"', "'")], check=False)

            # 3b. Configure System folder name and visibility
            schema_path = "org.gnome.desktop.app-folders.folder:/org/gnome/desktop/app-folders/folders/System/"
            subprocess.run(["gsettings", "set", schema_path, "name", "'X-GNOME-System.directory'"], check=False)
            subprocess.run(["gsettings", "set", schema_path, "translate", "true"], check=False)

            # 3c. Update apps list
            res_apps = subprocess.run(["gsettings", "get", schema_path, "apps"], capture_output=True, text=True)
            out_apps = res_apps.stdout.strip()
            current_apps = ast.literal_eval(out_apps) if out_apps.startswith("[") else []
            new_apps = list(set(current_apps + apps_to_add))
            subprocess.run(["gsettings", "set", schema_path, "apps", str(new_apps).replace('"', "'")], check=False)

            # 3d. Remove from Utilities to prevent duplication
            util_path = "org.gnome.desktop.app-folders.folder:/org/gnome/desktop/app-folders/folders/Utilities/"
            res_util = subprocess.run(["gsettings", "get", util_path, "apps"], capture_output=True, text=True)
            out_util = res_util.stdout.strip()
            util_apps = ast.literal_eval(out_util) if out_util.startswith("[") else []
            clean_util = [a for a in util_apps if a not in apps_to_add]
            if len(clean_util) != len(util_apps):
                subprocess.run(["gsettings", "set", util_path, "apps", str(clean_util).replace('"', "'")], check=False)

            logger.info("Visuals and App Grid organized successfully")
            return True, "Desktop visuals and App Grid organized successfully."
        except Exception as e:
            logger.exception("Failed setting desktop visuals")
            return False, f"Failed applying visuals: {e}"

