import os
import stat
import subprocess
import sys
import shutil

# Dynamically detect the current path (Desktop or /opt/SUIT)
project_dir = os.path.dirname(os.path.abspath(__file__))
venv_dir = os.path.join(project_dir, "venv")
requirements_file = os.path.join(project_dir, "requirements.txt")

def check_system_dependencies():
    """Check if required system packages are installed."""
    missing = []
    # Check for binaries
    for cmd in ["git", "python3"]:
        if shutil.which(cmd) is None:
            missing.append(cmd)
    
    # Check for python-tk (apt check)
    try:
        import tkinter
    except ImportError:
        missing.append("python3-tk")
    
    # Check for customtkinter
    try:
        import customtkinter
    except ImportError:
        # This will be installed in venv, but good to know
        pass

    if missing:
        print(f"\n[!] Missing system dependencies: {', '.join(missing)}")
        print("[!] Please run: sudo apt install python3-tk python3-dev libdbus-1-dev git")
        return False
    return True

def setup_environment():
    """Ensure venv exists and all required packages are installed."""
    if not os.path.exists(venv_dir):
        print(f"[*] Creating Python environment in {venv_dir}...")
        subprocess.run([sys.executable, "-m", "venv", venv_dir], check=True)
    
    pip_path = os.path.join(venv_dir, "bin", "pip")
    if os.path.exists(requirements_file):
        print("[*] Installing required packages from requirements.txt...")
        subprocess.run([pip_path, "install", "-r", requirements_file], check=True)

if __name__ == "__main__":
    print("--- SUIT Setup ---")
    
    if not check_system_dependencies():
        sys.exit(1)

    try:
        setup_environment()
    except Exception as e:
        print(f"[!] Error setting up the environment: {e}")
        sys.exit(1)

    # Content of the .desktop file with dynamic paths
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

    # Path for the desktop file
    desktop_file_path = os.path.expanduser("~/Desktop/SUIT.desktop")

    # Write the desktop file
    try:
        with open(desktop_file_path, "w") as f:
            f.write(desktop_entry)

        # Make it executable
        st = os.stat(desktop_file_path)
        os.chmod(desktop_file_path, st.st_mode | stat.S_IEXEC)

        # Try to mark as trusted (for Ubuntu Desktop)
        try:
            subprocess.run(["gio", "set", desktop_file_path, "metadata::trusted", "true"], check=False, stderr=subprocess.DEVNULL)
        except:
            pass

        print(f"[+] Launcher created successfully at: {desktop_file_path}")
        print("[+] You can now start SUIT from your Desktop.")
    except Exception as e:
        print(f"[!] Failed to create desktop launcher: {e}")
