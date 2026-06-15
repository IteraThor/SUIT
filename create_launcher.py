from pathlib import Path
import stat
import subprocess
import sys
import shutil

# Dynamisch den aktuellen Pfad erkennen
PROJECT_DIR = Path(__file__).resolve().parent
VENV_DIR = PROJECT_DIR / "venv"
REQUIREMENTS_FILE = PROJECT_DIR / "requirements.txt"

def get_desktop_path():
    """Ermittelt den Pfad zum Desktop, egal in welcher Sprache das System läuft."""
    # Versuche den Pfad über die XDG-Konfiguration zu finden (Standard bei Linux)
    try:
        xdg_path = subprocess.check_output(['xdg-user-dir', 'DESKTOP'], universal_newlines=True).strip()
        xdg_path_obj = Path(xdg_path)
        if xdg_path_obj.exists():
            return xdg_path_obj
    except Exception:
        pass

    # Fallback-Optionen, falls xdg-user-dir nicht funktioniert
    home = Path.home()
    for folder in ["Desktop", "Schreibtisch", "Bureau", "Escritorio"]:
        path = home / folder
        if path.exists():
            return path
            
    return home

def check_system_dependencies():
    """Prüft, ob benötigte System-Pakete installiert sind."""
    missing = []
    for cmd in ["git", "python3"]:
        if shutil.which(cmd) is None:
            missing.append(cmd)
    
    try:
        import tkinter
    except ImportError:
        missing.append("python3-tk")
    
    if missing:
        print(f"\n[!] Fehlende Abhängigkeiten: {', '.join(missing)}")
        if Path("/etc/fedora-release").exists():
            print("[!] Bitte ausführen: sudo dnf install python3-tkinter python3-devel dbus-devel git glib2-devel gcc gcc-c++ make")
        else:
            print("[!] Bitte ausführen: sudo apt install python3-tk python3-dev libdbus-1-dev git libglib2.0-dev build-essential")
        return False
    return True

def setup_environment():
    """Stellt sicher, dass die virtuelle Umgebung und alle Pakete bereit sind."""
    if not VENV_DIR.exists():
        print(f"[*] Erstelle Python-Umgebung in {VENV_DIR}...")
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
    
    pip_path = VENV_DIR / "bin" / "pip"
    
    print("[*] Aktualisiere Build-Tools...")
    subprocess.run([str(pip_path), "install", "--upgrade", "pip", "setuptools", "wheel"], check=True)

    if REQUIREMENTS_FILE.exists():
        print("[*] Installiere benötigte Pakete aus requirements.txt...")
        subprocess.run([str(pip_path), "install", "-r", str(REQUIREMENTS_FILE)], check=True)

if __name__ == "__main__":
    print("--- SUIT Setup ---")
    
    if not check_system_dependencies():
        sys.exit(1)

    try:
        setup_environment()
    except Exception as e:
        print(f"[!] Fehler beim Setup: {e}")
        sys.exit(1)

    # Inhalt der .desktop Datei
    desktop_entry = f"""[Desktop Entry]
Version=1.0
Name=SUIT
Comment=Setup Utilities by IteraThor
Exec={PROJECT_DIR}/venv/bin/python {PROJECT_DIR}/app.py
Icon={PROJECT_DIR}/suit-icon.png
Path={PROJECT_DIR}
Terminal=false
Type=Application
Categories=Utility;
"""

    # Den richtigen Desktop-Pfad für jede Sprache finden
    desktop_dir = get_desktop_path()
    desktop_file_path = desktop_dir / "SUIT.desktop"

    try:
        with open(desktop_file_path, "w") as f:
            f.write(desktop_entry)

        # Ausführbar machen
        st = desktop_file_path.stat()
        desktop_file_path.chmod(st.st_mode | stat.S_IEXEC)

        # Als vertrauenswürdig markieren (für Ubuntu)
        try:
            subprocess.run(["gio", "set", str(desktop_file_path), "metadata::trusted", "true"], check=False, stderr=subprocess.DEVNULL)
        except:
            pass

        # Kopie in den Application-Ordner für das Startmenü
        app_dir = Path.home() / ".local/share/applications"
        if not app_dir.exists():
            app_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(str(desktop_file_path), str(app_dir / "SUIT.desktop"))

        print(f"[+] Starter erfolgreich erstellt unter: {desktop_file_path}")
        print("[+] Eine Kopie wurde im Anwendungsmenü gespeichert.")
    except Exception as e:
        print(f"[!] Starter konnte nicht erstellt werden: {e}")
