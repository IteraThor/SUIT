import subprocess
import os
import sys

def update_and_start():
    # Erkennt automatisch das aktuelle Verzeichnis (dynamisch)
    project_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Update-Versuch beim Start
    if os.path.exists(os.path.join(project_dir, ".git")):
        try:
            # Versucht ein Pull, bricht nach 10 Sekunden ab falls keine Verbindung besteht
            subprocess.run(["git", "pull"], cwd=project_dir, check=False, timeout=10)
        except Exception:
            pass

    # Pfade dynamisch zur Laufzeit bestimmen
    venv_python = os.path.join(project_dir, "venv", "bin", "python")
    app_script = os.path.join(project_dir, "suit_test.py")
    
    # Nutzt venv-Python falls vorhanden, sonst System-Python
    python_exe = venv_python if os.path.exists(venv_python) else sys.executable
    
    # Ersetzt den aktuellen Prozess (main.py) komplett durch suit_test.py
    # Verhindert, dass zwei Fenster offen bleiben
    os.execv(python_exe, [python_exe, app_script] + sys.argv[1:])

if __name__ == "__main__":
    update_and_start()
