import os
import stat
import subprocess
import sys
import shutil

# Dynamisch den aktuellen Pfad erkennen
project_dir = os.path.dirname(os.path.abspath(__file__))
venv_dir = os.path.join(project_dir, "venv")
requirements_file = os.path.join(project_dir, "requirements.txt")

def get_desktop_path():
    """Ermittelt den Pfad zum Desktop, egal in welcher Sprache das System läuft."""
    # Versuche den Pfad über die XDG-Konfiguration zu finden (Standard bei Linux)
    try:
        xdg_path = subprocess.check_output(['xdg-user-dir', 'DESKTOP'], universal_newlines=True).strip()
        if os.path.exists(xdg_path):
            return xdg_path
    except Exception:
        pass

    # Fallback-Optionen, falls xdg-user-dir nicht funktioniert
    home = os.path.expanduser("~")
    for folder in ["Desktop", "Schreibtisch", "Bureau", "Escritorio"]:
        path = os.path.join(home, folder)
        if os.path.exists(path):
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
        print("[!] Bitte ausführen: sudo apt install python3-tk python3-dev libdbus-1-dev git libglib2.0-dev build-essential")
        return False
    return True

def setup_environment():
    """Stellt sicher, dass die virtuelle Umgebung und alle Pakete bereit sind."""
    if not os.path.exists(venv_dir):
        print(f"[*] Erstelle Python-Umgebung in {venv_dir}...")
        subprocess.run([sys.executable, "-m", "venv", venv_dir], check=True)
    
    pip_path = os.path.join(venv_dir, "bin", "pip")
    
    print("[*] Aktualisiere Build-Tools...")
    subprocess.run([pip_path, "install", "--upgrade", "pip", "setuptools", "wheel"], check=True)

    if os.path.exists(requirements_file):
        print("[*] Installiere benötigte Pakete aus requirements.txt...")
        subprocess.run([pip_path, "install", "-r", requirements_file], check=True)

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
Exec={project_dir}/venv/bin/python {project_dir}/app.py
Icon={project_dir}/suit-icon.png
Path={project_dir}
Terminal=false
Type=Application
Categories=Utility;
"""

    # Den richtigen Desktop-Pfad für jede Sprache finden
    desktop_dir = get_desktop_path()
    desktop_file_path = os.path.join(desktop_dir, "SUIT.desktop")

    try:
        with open(desktop_file_path, "w") as f:
            f.write(desktop_entry)

        # Ausführbar machen
        st = os.stat(desktop_file_path)
        os.chmod(desktop_file_path, st.st_mode | stat.S_IEXEC)

        # Als vertrauenswürdig markieren (für Ubuntu)
        try:
            subprocess.run(["gio", "set", desktop_file_path, "metadata::trusted", "true"], check=False, stderr=subprocess.DEVNULL)
        except:
            pass

        print(f"[+] Starter erfolgreich erstellt unter: {desktop_file_path}")
        print("[+] Du kannst SUIT jetzt von deinem Desktop/Schreibtisch starten.")
    except Exception as e:
        print(f"[!] Starter konnte nicht erstellt werden: {e}")
