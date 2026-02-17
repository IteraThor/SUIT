import os
import stat
import subprocess
import sys

# Erkennt den aktuellen Pfad dynamisch (Desktop oder /opt/SUIT)
project_dir = os.path.dirname(os.path.abspath(__file__))
venv_dir = os.path.join(project_dir, "venv")
requirements_file = os.path.join(project_dir, "requirements.txt")

def setup_environment():
    """Stellt sicher, dass venv existiert und alle Pakete installiert sind."""
    if not os.path.exists(venv_dir):
        print(f"Erstelle Python-Umgebung in {venv_dir}...")
        subprocess.run([sys.executable, "-m", "venv", venv_dir], check=True)
    
    pip_path = os.path.join(venv_dir, "bin", "pip")
    if os.path.exists(requirements_file):
        print("Installiere benötigte Pakete...")
        subprocess.run([pip_path, "install", "-r", requirements_file], check=True)

try:
    setup_environment()
except Exception as e:
    print(f"Fehler beim Einrichten der Umgebung: {e}")

# Inhalt der .desktop Datei mit dynamischen Pfaden
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
desktop_file_path = os.path.expanduser("~/Desktop/SUIT.desktop")

# Datei schreiben
with open(desktop_file_path, "w") as f:
    f.write(desktop_entry)

# Ausführbar machen
st = os.stat(desktop_file_path)
os.chmod(desktop_file_path, st.st_mode | stat.S_IEXEC)

# Versuchen, als vertrauenswürdig zu markieren (für Ubuntu Desktop)
try:
    subprocess.run(["gio", "set", desktop_file_path, "metadata::trusted", "true"], check=False)
except:
    pass

print(f"Launcher wurde erstellt unter: {desktop_file_path}")
