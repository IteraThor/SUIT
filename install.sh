#!/bin/bash

# SUIT Installation Script
# This script installs system dependencies and runs the setup.

set -e

echo "--- SUIT Installation ---"

# 1. Install system dependencies
echo "[*] Installing system dependencies..."
sudo apt update
sudo apt install -y python3-tk python3-dbus python3-venv python3-dev libdbus-1-dev git libxcb-cursor0 libglib2.0-dev build-essential pkg-config

# 2. Run the python setup script
echo "[*] Setting up Python environment and launcher..."
python3 create_launcher.py

echo ""
echo "[+] Installation complete!"
echo "[+] You can now launch SUIT from your desktop or application menu."
