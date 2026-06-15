#!/bin/bash

# SUIT Installation Script
# This script installs system dependencies and runs the setup.

set -e

echo "--- SUIT Installation ---"

# Detect Distribution
if [ -f /etc/fedora-release ]; then
    DISTRO="fedora"
elif [ -f /etc/debian_version ] || [ -f /etc/lsb-release ]; then
    DISTRO="debian"
else
    echo "[!] Unsupported distribution. Please install dependencies manually."
    DISTRO="unknown"
fi

# 1. Install system dependencies
echo "[*] Installing system dependencies for $DISTRO..."

if [ "$DISTRO" == "fedora" ]; then
    sudo dnf install -y python3-tkinter python3-dbus python3-devel dbus-devel \
    glib2-devel gcc gcc-c++ make pkgconf-pkg-config libX11-devel libxcb-devel \
    libXext-devel libXrender-devel xrandr git
    
    # Add user to input group for hardware access
    sudo usermod -aG input $USER
    echo "[*] Added $USER to 'input' group. You may need to relogin for this to take effect."

elif [ "$DISTRO" == "debian" ]; then
    sudo apt update
    sudo apt install -y python3-tk python3-dbus python3-venv python3-dev \
    libdbus-1-dev git libxcb-cursor0 libglib2.0-dev build-essential pkg-config \
    x11-xserver-utils
fi

# 2. Run the python setup script
echo "[*] Setting up Python environment and launcher..."
# Ensure we are in the script directory
cd "$(dirname "$0")"
python3 create_launcher.py

echo ""
echo "[+] Installation complete!"
echo "[+] You can now launch SUIT from your desktop or application menu."
