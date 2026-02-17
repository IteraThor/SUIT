import subprocess
import os
import sys

def update_and_start():
    project_dir = os.path.dirname(os.path.abspath(__file__))
    
    if os.path.exists(os.path.join(project_dir, ".git")):
        print("Prüfe auf Updates...")
        try:
            subprocess.run(["git", "pull"], cwd=project_dir, check=False, timeout=10)
        except Exception as e:
            print(f"Update fehlgeschlagen: {e}")

    venv_python = os.path.join(project_dir, "venv", "bin", "python")
    app_script = os.path.join(project_dir, "suit_test.py")
    
    python_exe = venv_python if os.path.exists(venv_python) else sys.executable
    
    os.execl(python_exe, python_exe, app_script, *sys.argv[1:])

if __name__ == "__main__":
    update_and_start()
