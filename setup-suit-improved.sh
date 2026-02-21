#!/bin/bash
LOG="/tmp/suit_setup.log"
exec > >(tee -a "$LOG") 2>&1

# Function to send desktop notifications to the user
notify() {
    # Run as the logged-in user to show on their desktop
    # We try to detect the current user if autodarts isn't the only one
    CURRENT_USER=$(whoami)
    sudo -u "$CURRENT_USER" DISPLAY=:0 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u "$CURRENT_USER")/bus notify-send "SUIT Setup" "$1" 2>/dev/null
}

echo "SUIT Setup started: $(date)"
notify "Starting installation... waiting for internet."

# 1. Wait for GitHub specifically (using curl for better reliability)
while ! curl -s --head https://github.com | grep -q "200 OK"; do
    echo "Waiting for GitHub connectivity..."
    sleep 5
done

# 2. Determine paths
USER_DESKTOP=$(xdg-user-dir DESKTOP)
TARGET_DIR="$USER_DESKTOP/SUIT"

# 3. Download (Atomic)
echo "Checking target directory: $TARGET_DIR"
if [ ! -d "$TARGET_DIR/.git" ]; then
    echo "Cloning SUIT repository..."
    # If the directory exists but is empty or broken, remove it first
    [ -d "$TARGET_DIR" ] && rm -rf "$TARGET_DIR"
    
    if git clone https://github.com/IteraThor/SUIT.git "$TARGET_DIR"; then
        notify "Download successful."
    else
        notify "Download failed! Will retry on next boot."
        exit 1
    fi
fi

cd "$TARGET_DIR" || exit

# 4. Installation
chmod +x install.sh
echo "Starting System Installation..."

# Using -n (non-interactive) to avoid hanging if sudo fails
if sudo -n ./install.sh; then
    notify "System dependencies installed."
else
    echo "Sudo failed or requires password. Skipping system-wide apt install."
    # We still try to run the local user setup (virtualenv etc)
    if [ -f "create_launcher.py" ]; then
        python3 create_launcher.py
    fi
fi

# 5. Launcher Trust (GNOME specific)
ICON_PATH="$USER_DESKTOP/SUIT.desktop"
if [ -f "$ICON_PATH" ]; then
    echo "Setting launcher permissions..."
    chmod +x "$ICON_PATH"
    gio set "$ICON_PATH" metadata::trusted true 2>/dev/null
fi

notify "Setup complete! SUIT is ready on your Desktop."

# 6. Cleanup (Only if we reached the end successfully)
echo "Cleaning up autostart entries..."
rm -f "$HOME/.config/autostart/suit-setup.desktop"
sudo -n rm -f "/etc/xdg/autostart/suit-setup.desktop"
