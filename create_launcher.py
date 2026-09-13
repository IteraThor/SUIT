from pathlib import Path
import stat
import subprocess
import sys
import shutil
import ast

PROJECT_DIR = Path(__file__).resolve().parent

def get_desktop_path():
    """Determine desktop path across system locales."""
    try:
        xdg_path = subprocess.check_output(['xdg-user-dir', 'DESKTOP'], universal_newlines=True).strip()
        xdg_path_obj = Path(xdg_path)
        if xdg_path_obj.exists():
            return xdg_path_obj
    except Exception:
        pass

    home = Path.home()
    for folder in ["Desktop", "Schreibtisch", "Bureau", "Escritorio"]:
        path = home / folder
        if path.exists():
            return path
            
    return home

def check_system_dependencies():
    """Verify essential system dependencies are available."""
    missing = []
    for cmd in ["git", "python3"]:
        if shutil.which(cmd) is None:
            missing.append(cmd)
    
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gtk, Adw
    except (ImportError, ValueError):
        missing.append("python3-gobject / libadwaita")
    
    if missing:
        print(f"\nMissing dependencies: {', '.join(missing)}")
        if Path("/etc/fedora-release").exists():
            print("Please run: sudo dnf install -y python3-gobject libadwaita python3-pip python3-evdev python3-pyserial git")
        else:
            print("Please install GTK4 and Libadwaita packages for your distribution.")
        return False
    return True

def install_hicolor_icons():
    """Install SUIT application icon to user hicolor theme for GNOME Shell integration."""
    icon_src = PROJECT_DIR / "assets" / "icons" / "suit-icon.png"
    if not icon_src.exists():
        return

    # Ensure symlink in assets/icons exists as well
    symlink_target = PROJECT_DIR / "assets" / "icons" / "de.iterathor.suit.gtk.png"
    if not symlink_target.exists():
        try:
            symlink_target.symlink_to(icon_src.name)
        except Exception:
            pass

    for size in ["512x512", "256x256", "128x128", "scalable"]:
        target_dir = Path.home() / f".local/share/icons/hicolor/{size}/apps"
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(icon_src, target_dir / "de.iterathor.suit.gtk.png")
            shutil.copy2(icon_src, target_dir / "suit-icon.png")
        except Exception:
            pass

    try:
        subprocess.run(["gtk-update-icon-cache", "-f", "-t", str(Path.home() / ".local/share/icons/hicolor")], check=False, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def create_desktop_launcher():
    """Generate desktop and application menu entries for SUIT GTK4."""
    install_hicolor_icons()

    desktop_entry = f"""[Desktop Entry]
Version=1.0
Name=SUIT for Fedora
Comment=Setup Utilities by IteraThor (GTK4 / Libadwaita)
Exec=/usr/bin/python3 {PROJECT_DIR}/app_gtk.py
Icon=de.iterathor.suit.gtk
Path={PROJECT_DIR}
Terminal=false
Type=Application
Categories=Utility;Settings;
StartupNotify=true
StartupWMClass=de.iterathor.suit.gtk
"""
    
    # 1. Application menu entries (standard app_id name and legacy alias)
    app_dir = Path.home() / ".local/share/applications"
    app_dir.mkdir(parents=True, exist_ok=True)
    
    for filename in ["de.iterathor.suit.gtk.desktop", "SUIT.desktop"]:
        menu_file = app_dir / filename
        menu_file.write_text(desktop_entry, encoding="utf-8")
        menu_file.chmod(menu_file.stat().st_mode | stat.S_IEXEC)
        print(f"Application menu launcher created at: {menu_file}")

    try:
        subprocess.run(["update-desktop-database", str(app_dir)], check=False, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    # 2. Clean up any desktop folder launcher files (GNOME does not use desktop icons)
    desktop_dir = get_desktop_path()
    if desktop_dir.exists() and desktop_dir != Path.home():
        for filename in ["de.iterathor.suit.gtk.desktop", "SUIT.desktop"]:
            desktop_file = desktop_dir / filename
            if desktop_file.exists():
                try:
                    desktop_file.unlink()
                    print(f"Removed unnecessary desktop folder file: {desktop_file}")
                except Exception:
                    pass

    # 3. Automatically pin to GNOME Dash favorites
    pin_to_dash("de.iterathor.suit.gtk.desktop")


def pin_to_dash(desktop_file: str = "de.iterathor.suit.gtk.desktop") -> bool:
    """Pin the SUIT desktop entry to GNOME Shell Dash favorites."""
    try:
        res = subprocess.run(["gsettings", "get", "org.gnome.shell", "favorite-apps"], capture_output=True, text=True)
        out = res.stdout.strip()
        if out.startswith("["):
            favs = ast.literal_eval(out)
            # Remove legacy name if present
            favs = [f for f in favs if f != "SUIT.desktop"]
            if desktop_file not in favs:
                favs.append(desktop_file)
                val_str = str(favs).replace('"', "'")
                subprocess.run(["gsettings", "set", "org.gnome.shell", "favorite-apps", val_str], check=True)
                print(f"Pinned {desktop_file} to GNOME Dash favorites.")
            else:
                print(f"{desktop_file} is already pinned to GNOME Dash favorites.")
            return True
    except Exception as e:
        print(f"Note: Could not automatically pin to Dash favorites: {e}")
        return False

if __name__ == "__main__":
    print("--- SUIT GTK4 Setup ---")
    
    if not check_system_dependencies():
        sys.exit(1)

    try:
        create_desktop_launcher()
        print("Launcher setup completed successfully.")
    except Exception as e:
        print(f"Failed to create launcher: {e}")
        sys.exit(1)
