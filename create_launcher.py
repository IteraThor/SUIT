from pathlib import Path
import stat
import subprocess
import sys
import shutil

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

def create_desktop_launcher():
    """Generate desktop and application menu entries for SUIT GTK4."""
    desktop_entry = f"""[Desktop Entry]
Version=1.0
Name=SUIT
Comment=Setup Utilities by IteraThor (GTK4 / Libadwaita)
Exec=/usr/bin/python3 {PROJECT_DIR}/app_gtk.py
Icon={PROJECT_DIR}/assets/icons/suit-icon.png
Path={PROJECT_DIR}
Terminal=false
Type=Application
Categories=Utility;Settings;
StartupNotify=true
"""
    
    # 1. Application menu entry
    app_dir = Path.home() / ".local/share/applications"
    app_dir.mkdir(parents=True, exist_ok=True)
    menu_file = app_dir / "SUIT.desktop"
    menu_file.write_text(desktop_entry, encoding="utf-8")
    menu_file.chmod(menu_file.stat().st_mode | stat.S_IEXEC)
    print(f"Application menu launcher created at: {menu_file}")

    # 2. Desktop entry (if Desktop folder exists)
    desktop_dir = get_desktop_path()
    if desktop_dir.exists() and desktop_dir != Path.home():
        desktop_file = desktop_dir / "SUIT.desktop"
        desktop_file.write_text(desktop_entry, encoding="utf-8")
        desktop_file.chmod(desktop_file.stat().st_mode | stat.S_IEXEC)
        try:
            subprocess.run(["gio", "set", str(desktop_file), "metadata::trusted", "true"], check=False, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        print(f"Desktop launcher created at: {desktop_file}")

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
