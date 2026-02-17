import os
import stat
import subprocess
import sys

# Erkennt den aktuellen Pfad dynamisch
project_dir = os.path.dirname(os.path.abspath(__file__))
venv_dir = os.path.join(project_dir, "venv")
requirements_file = os.path.join(project_dir, "requirements.txt")

def setup_environment():
    if not os.path.exists(venv_dir):
        print(f"Erstelle venv in {venv_dir}...")
        subprocess.run([sys.executable, "-m", "venv", venv_dir], check=True)
    
    pip_path = os.path.join(venv_dir, "bin", "pip")
    if os.path.exists(requirements_file):
        subprocess.run([pip_path, "install", "-r", requirements_file], check=True)

try:
    setup_environment()
except Exception as e:
    print(f"Fehler: {e}")

# Erstellt den Desktop-Eintrag mit dem dynamisch erkannten Pfad
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

desktop_file_path = os.path.expanduser("~/Desktop/SUIT.desktop")
with open(desktop_file_path, "w") as f:
    f.write(desktop_entry)

# Ausführbar machen
os.chmod(desktop_file_path, os.stat(desktop_file_path).st_mode | stat.S_IEXEC)

# Nur auf einem installierten System versuchen wir das "Trusted" Attribut
try:
    subprocess.run(["gio", "set", desktop_file_path, "metadata::trusted", "true"], check=False)
except:
    pass

print(f"Launcher erfolgreich erstellt in: {desktop_file_path}")
