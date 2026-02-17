import subprocess
import os
import sys

def update_and_start():
    # Erkennt automatisch das aktuelle Verzeichnis (egal ob /opt/ oder Desktop)
    project_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Update-Check beim Start
    if os.path.exists(os.path.join(project_dir, ".git")):
        try:
            subprocess.run(["git", "pull"], cwd=project_dir, check=False, timeout=10)
        except Exception:
            pass

    # Pfade dynamisch zur Laufzeit bestimmen
    venv_python = os.path.join(project_dir, "venv", "bin", "python")
    app_script = os.path.join(project_dir, "suit_test.py")
    
    python_exe = venv_python if os.path.exists(venv_python) else sys.executable
    
    # Prozess sauber ersetzen (behebt das "zwei Fenster" Problem)
    os.execv(python_exe, [python_exe, app_script] + sys.argv[1:])

if __name__ == "__main__":
    update_and_start()
