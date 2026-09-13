import subprocess
import ast
import shutil
from pathlib import Path
from core.logger import get_logger

logger = get_logger("desktop")

EXTENSION_UUID = "touch-scaling@suit"
EXTENSION_SOURCE_DIR = Path(__file__).resolve().parent.parent / "assets" / "touch_scaling_extension"
USER_EXTENSION_DIR = Path.home() / ".local" / "share" / "gnome-shell" / "extensions" / EXTENSION_UUID
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
  min-width: 48px;
  min-height: 48px;
  padding: 8px;
}}

windowcontrols button image,
headerbar button.titlebutton image,
.titlebutton image {{
  -gtk-icon-size: 20px;
}}
{SUIT_TOUCH_MARKER_END}
"""


class DesktopService:
    EXTENSION_UUID = EXTENSION_UUID
    EXTENSION_SOURCE_DIR = EXTENSION_SOURCE_DIR
    USER_EXTENSION_DIR = USER_EXTENSION_DIR
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

                # 4. Copy extension files
                dest.mkdir(parents=True, exist_ok=True)
                if src.exists():
                    shutil.copytree(src, dest, dirs_exist_ok=True)

                # 5. Enable user extensions setting
                subprocess.run(["gsettings", "set", "org.gnome.shell", "disable-user-extensions", "false"], check=False)

                # 6. Add to enabled-extensions list
                res = subprocess.run(["gsettings", "get", "org.gnome.shell", "enabled-extensions"], capture_output=True, text=True)
                out = res.stdout.strip()
                enabled_list = ast.literal_eval(out) if out.startswith("[") else []
                if uuid not in enabled_list:
                    enabled_list.append(uuid)
                    subprocess.run(["gsettings", "set", "org.gnome.shell", "enabled-extensions", str(enabled_list).replace('"', "'")], check=False)

                # Try gnome-extensions enable command
                subprocess.run(["gnome-extensions", "enable", uuid], check=False)

                # 7. Inject GTK CSS
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
                subprocess.run(["gnome-extensions", "disable", uuid], check=False)
                res = subprocess.run(["gsettings", "get", "org.gnome.shell", "enabled-extensions"], capture_output=True, text=True)
                out = res.stdout.strip()
                enabled_list = ast.literal_eval(out) if out.startswith("[") else []
                if uuid in enabled_list:
                    enabled_list.remove(uuid)
                    subprocess.run(["gsettings", "set", "org.gnome.shell", "enabled-extensions", str(enabled_list).replace('"', "'")], check=False)

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

