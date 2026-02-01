import os
import stat
import subprocess

# Get the directory of the current script
project_dir = os.path.dirname(os.path.abspath(__file__))

# Define the content of the .desktop file
desktop_entry = f"""[Desktop Entry]
Version=1.0
Name=SUIT
Comment=Setup Utilities by IteraThor
Exec={project_dir}/venv/bin/python {project_dir}/suit_test.py
Path={project_dir}
Terminal=false
Type=Application
Categories=Utility;
"""

# Define the path for the .desktop file on the Desktop
desktop_dir = os.path.expanduser("~/Desktop")
os.makedirs(desktop_dir, exist_ok=True)
desktop_file_path = os.path.join(desktop_dir, "SUIT.desktop")

# Write the content to the .desktop file
with open(desktop_file_path, "w") as f:
    f.write(desktop_entry)

# Make the .desktop file executable
st = os.stat(desktop_file_path)
os.chmod(desktop_file_path, st.st_mode | stat.S_IEXEC)

# Mark the desktop file as trusted
try:
    subprocess.run(["gio", "set", desktop_file_path, "metadata::trusted", "true"], check=True)
    print("Desktop file marked as trusted.")
except FileNotFoundError:
    print("`gio` command not found. Could not mark desktop file as trusted.")
except subprocess.CalledProcessError as e:
    print(f"Error marking desktop file as trusted: {e}")

# Refresh Nautilus
try:
    subprocess.run(["nautilus", "-q"], check=True)
    print("Nautilus refreshed.")
except FileNotFoundError:
    # This is not a critical error, so we just inform the user.
    print("'nautilus -q' command failed. You may need to manually refresh your desktop for the icon to update.")
except subprocess.CalledProcessError as e:
    print(f"Error refreshing Nautilus: {e}")


print(f"Desktop launcher created at: {desktop_file_path}")
print("You should now be able to launch SUIT from your desktop.")

