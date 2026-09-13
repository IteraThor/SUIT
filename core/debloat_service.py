import os
import subprocess
import ast
import re
import shutil
from core.logger import get_logger

logger = get_logger("debloat")


class DebloatService:
    BLOATWARE_PACKAGES = [
        "gnome-contacts", "gnome-weather", "gnome-clocks", "mediawriter",
        "gnome-maps", "simple-scan", "gnome-boxes", "libreoffice-core",
        "showtime", "snapshot", "gnome-characters", "gnome-tour",
        "yelp", "gnome-font-viewer", "papers", "gnome-connections",
        "malcontent-control", "firefox", "gnome-text-editor",
        "gnome-calculator", "gnome-calendar", "loupe", "decibels"
    ]

    APP_FRIENDLY_NAMES = {
        "gnome-contacts": ("Contacts", "Address book and contact management", "contact-new-symbolic"),
        "gnome-weather": ("Weather", "Weather forecasting app", "weather-clear-symbolic"),
        "gnome-clocks": ("Clocks", "Alarm clock, timer, and stopwatch", "preferences-system-time-symbolic"),
        "mediawriter": ("Fedora Media Writer", "USB bootable media creator", "drive-removable-media-symbolic"),
        "gnome-maps": ("Maps", "Desktop mapping application", "mark-location-symbolic"),
        "simple-scan": ("Document Scanner", "Scanning utility", "scanner-symbolic"),
        "gnome-boxes": ("Boxes", "Virtual machine manager", "computer-symbolic"),
        "libreoffice": ("LibreOffice", "Office suite (Writer, Calc, Impress)", "x-office-document-symbolic"),
        "libreoffice-core": ("LibreOffice", "Office suite (Writer, Calc, Impress)", "x-office-document-symbolic"),
        "showtime": ("Showtime", "Video player", "video-x-generic-symbolic"),
        "snapshot": ("Snapshot", "Camera app", "camera-photo-symbolic"),
        "gnome-characters": ("Characters", "Special characters and emoji picker", "accessories-character-map-symbolic"),
        "gnome-tour": ("GNOME Tour", "Desktop onboarding tour", "help-about-symbolic"),
        "yelp": ("Help", "GNOME documentation viewer", "help-browser-symbolic"),
        "gnome-font-viewer": ("Fonts", "Font viewing utility", "preferences-desktop-font-symbolic"),
        "papers": ("Document Viewer (Papers)", "PDF and document reader", "x-office-document-symbolic"),
        "gnome-connections": ("Connections", "Remote desktop client", "network-wired-symbolic"),
        "malcontent-control": ("Parental Controls", "Content restrictions", "system-users-symbolic"),
        "firefox": ("Firefox", "Default web browser", "web-browser-symbolic"),
        "gnome-text-editor": ("Text Editor", "Simple text editor", "accessories-text-editor-symbolic"),
        "gnome-calculator": ("Calculator", "Desktop calculator", "accessories-calculator-symbolic"),
        "gnome-calendar": ("Calendar", "Desktop calendar", "x-office-calendar-symbolic"),
        "loupe": ("Image Viewer (Loupe)", "Default photo viewer", "image-x-generic-symbolic"),
        "decibels": ("Audio Player (Decibels)", "Simple sound player", "audio-x-generic-symbolic")
    }

    @classmethod
    def get_installed_bloatware_details(cls, include_all: bool = False) -> list[dict]:
        installed_full = cls.get_installed_bloatware()
        installed_bases = set()
        for full_pkg in installed_full:
            for key in cls.APP_FRIENDLY_NAMES:
                if full_pkg == key or full_pkg.startswith(key + "-") or full_pkg.startswith(key):
                    installed_bases.add(key)
                    break
            else:
                installed_bases.add(full_pkg.split("-")[0])

        target_keys = cls.BLOATWARE_PACKAGES if (include_all or not installed_full) else [k for k in cls.BLOATWARE_PACKAGES if k in installed_bases]

        details = []
        for key in target_keys:
            info = cls.APP_FRIENDLY_NAMES.get(key)
            if info:
                title, desc = info[0], info[1]
                icon = info[2] if len(info) > 2 else "application-x-executable-symbolic"
            else:
                title = key.replace("gnome-", "").replace("-", " ").capitalize()
                desc = f"Package: {key}"
                icon = "application-x-executable-symbolic"

            full_pkg_name = key
            for f in installed_full:
                if f == key or f.startswith(key + "-"):
                    full_pkg_name = f
                    break

            details.append({
                "package": full_pkg_name,
                "base": key,
                "title": title,
                "description": desc,
                "icon": icon,
                "is_installed": key in installed_bases
            })
        return details

    @classmethod
    def get_installed_bloatware(cls, package_list: list[str] | None = None) -> list[str]:
        pkgs = package_list or cls.BLOATWARE_PACKAGES
        try:
            res = subprocess.run(
                ["rpm", "-q"] + pkgs,
                capture_output=True,
                text=True,
                env={**os.environ, "LC_ALL": "C"}
            )
            installed = []
            for line in res.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith("package ") or "not installed" in line or "nicht installiert" in line:
                    continue
                for p in pkgs:
                    if line == p or line.startswith(p + "-"):
                        installed.append(line)
                        break
            return installed
        except Exception:
            logger.debug("Failed querying installed packages via rpm")
            return []

    @staticmethod
    def clean_dash_favorites() -> bool:
        try:
            res = subprocess.run(["gsettings", "get", "org.gnome.shell", "favorite-apps"], capture_output=True, text=True)
            out = res.stdout.strip()
            if out.startswith("["):
                favs = ast.literal_eval(out)
                new_favs = [f for f in favs if f not in ("org.gnome.Software.desktop", "org.gnome.Nautilus.desktop")]
                if len(new_favs) != len(favs):
                    val_str = str(new_favs).replace('"', "'")
                    subprocess.run(["gsettings", "set", "org.gnome.shell", "favorite-apps", val_str], check=True)
            return True
        except Exception:
            logger.debug("Failed cleaning dash favorites")
            return False

    @staticmethod
    def move_seahorse_to_utilities() -> bool:
        try:
            schema = "org.gnome.desktop.app-folders.folder:/org/gnome/desktop/app-folders/folders/Utilities/"
            res = subprocess.run(["gsettings", "get", schema, "apps"], capture_output=True, text=True)
            out = res.stdout.strip()
            if out.startswith("["):
                apps = ast.literal_eval(out)
                if "org.gnome.seahorse.Application.desktop" not in apps:
                    apps.append("org.gnome.seahorse.Application.desktop")
                    val_str = str(apps).replace('"', "'")
                    subprocess.run(["gsettings", "set", schema, "apps", val_str], check=True)
            return True
        except Exception:
            logger.debug("Failed moving seahorse to utilities folder")
            return False

    @classmethod
    def debloat_packages(cls, packages: list[str], progress_callback=None) -> tuple[bool, str]:
        if not packages:
            return True, "No packages selected for removal."

        try:
            cmd = ["sudo", "-n", "dnf", "remove", "-y"] + packages
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            erasing_pattern = re.compile(r'(?:Erasing|Removing)\s*:\s*(\S+)\s+(\d+)/(\d+)')
            total_items = len(packages)

            if progress_callback:
                progress_callback(0, total_items, "Starting package removal transaction...")

            for line in proc.stdout:
                line_str = line.strip()
                if not line_str:
                    continue
                logger.debug(f"[dnf-debloat] {line_str}")
                m = erasing_pattern.search(line_str)
                if m and progress_callback:
                    pkg_name, cur_str, tot_str = m.groups()
                    try:
                        cur_val = int(cur_str)
                        tot_val = int(tot_str)
                        disp_name = pkg_name.split("-")[0]
                        for k, info in cls.APP_FRIENDLY_NAMES.items():
                            if pkg_name.startswith(k):
                                disp_name = info[0]
                                break
                        progress_callback(cur_val, tot_val, f"Removing {disp_name} ({cur_val}/{tot_val})...")
                    except ValueError:
                        pass
                elif progress_callback:
                    if "Running transaction" in line_str:
                        progress_callback(0, total_items, "Running removal transaction...")
                    elif "Preparing" in line_str:
                        progress_callback(0, total_items, "Preparing transaction...")

            proc.wait()

            if progress_callback:
                progress_callback(total_items, total_items, "Cleaning Dash favorites and app grid...")

            cls.clean_dash_favorites()
            cls.move_seahorse_to_utilities()
            logger.info("Debloat and Dash cleanup completed successfully")
            return True, f"Successfully removed {len(packages)} application(s)."
        except Exception as e:
            logger.exception("Failed debloating selected packages")
            return False, f"Debloat failed: {e}"

    @classmethod
    def debloat_system(cls, package_list: list[str] | None = None, progress_callback=None) -> tuple[bool, str]:
        pkgs = package_list or cls.get_installed_bloatware()
        return cls.debloat_packages(pkgs, progress_callback=progress_callback)

    @staticmethod
    def add_favorite_app(desktop_file: str) -> bool:
        try:
            res = subprocess.run(["gsettings", "get", "org.gnome.shell", "favorite-apps"], capture_output=True, text=True)
            out = res.stdout.strip()
            if out.startswith("["):
                favs = ast.literal_eval(out)
                if desktop_file not in favs:
                    favs.append(desktop_file)
                    val_str = str(favs).replace('"', "'")
                    subprocess.run(["gsettings", "set", "org.gnome.shell", "favorite-apps", val_str], check=True)
            return True
        except Exception:
            logger.debug(f"Failed adding {desktop_file} to favorite apps")
            return False

    @staticmethod
    def is_app_installed(app_name: str) -> bool:
        if app_name == "chromium":
            return shutil.which("chromium-browser") is not None or shutil.which("chromium") is not None
        elif app_name == "tailscale":
            return shutil.which("tailscale") is not None
        return shutil.which(app_name) is not None

    @classmethod
    def get_software_status(cls) -> dict:
        tail_status = cls.get_tailscale_status() if hasattr(cls, "get_tailscale_status") else {}
        return {
            "chromium": cls.is_app_installed("chromium"),
            "tailscale": tail_status.get("installed", False),
            "tailscale_details": tail_status
        }

    @classmethod
    def install_chromium(cls) -> tuple[bool, str]:
        try:
            subprocess.run(["sudo", "-n", "dnf", "install", "-y", "chromium"], check=True, capture_output=True)
            cls.add_favorite_app("chromium-browser.desktop")
            return True, "Chromium installed and pinned to Dash."
        except Exception as e:
            logger.exception("Failed installing Chromium")
            return False, f"Failed installing Chromium: {e}"
