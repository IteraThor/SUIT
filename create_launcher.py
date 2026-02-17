import os
import stat
import subprocess
import sys

# Verzeichnis des aktuellen Skripts ermitteln
project_dir = os.path.dirname(os.path.abspath(__file__))
venv_dir = os.path.join(project_dir, "venv")
requirements_file = os.path.join(project_dir, "requirements.txt")

def setup_environment():
    """Stellt sicher, dass venv existiert und alle Pakete installiert sind."""
    if not os.path.exists(venv_dir):
        print("Erstelle Python-Umgebung (venv)...")
        subprocess.run([sys.executable, "-m", "venv", venv_dir], check=True)
    
    pip_path = os.path.join(venv_dir, "bin", "pip")
    if os.path.exists(requirements_file):
        print("Installiere benötigte Pakete aus requirements.txt...")
        subprocess.run([pip_path, "install", "-r", requirements_file], check=True)

# Vor dem Erstellen des Launchers die Umgebung prüfen/erstellen
try:
    setup_environment()
except Exception as e:
    print(f"Fehler beim Einrichten der Umgebung: {e}")

# Inhalt der .desktop Datei - angepasst auf main.py für den Bootstrap-Start
desktop_entry = f"""[Desktop Entry]
Version=1.0
Name=SUIT
Comment=Setup Utilities by IteraThor
Exec={project_dir}/venv/bin/python {project_dir}/main.py
Icon={project_dir}/suit-icon.png
Path={project_dir}
Terminal=false
Type=Application
Categories=Utility;
"""

# Pfad für den Desktop
desktop_dir = os.path.expanduser("~/Desktop")
os.makedirs(desktop_dir, exist_ok=True)
desktop_file_path = os.path.join(desktop_dir, "SUIT.desktop")

# Datei schreiben
with open(desktop_file_path, "w") as f:
    f.write(desktop_entry)

# Ausführbar machen
st = os.stat(desktop_file_path)
os.chmod(desktop_file_path, st.st_mode | stat.S_IEXEC)

# Als vertrauenswürdig markieren (versuchen)
try:
    subprocess.run(["gio", "set", desktop_file_path, "metadata::trusted", "true"], check=True)
    print("Desktop-Datei als vertrauenswürdig markiert.")
except Exception:
    print("Hinweis: Markierung als 'trusted' via gio fehlgeschlagen. Bitte ggf. manuell 'Starten erlauben' klicken.")

print(f"Launcher wurde erstellt unter: {desktop_file_path}")
