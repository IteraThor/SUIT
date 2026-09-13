#!/usr/bin/env bash
set -e

echo "========================================="
echo "   SUIT for Fedora - Automated Setup    "
echo "========================================="

# 1. Check Fedora environment
if [ ! -f /etc/fedora-release ]; then
    echo "Warning: /etc/fedora-release not detected. SUIT is optimized for Fedora Linux."
fi

# 2. Clean up legacy SUIT artifacts if present
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${SCRIPT_DIR}/uninstall_legacy.sh" ]; then
    "${SCRIPT_DIR}/uninstall_legacy.sh"
fi

# 3. Install native system dependencies via DNF
echo "[1/3] Installing system packages..."
sudo dnf install -y --setopt=install_weak_deps=False \
    python3 \
    python3-gobject \
    libadwaita \
    python3-dbus \
    python3-evdev \
    python3-pyserial \
    python3-qrcode \
    python3-opencv \
    git

# 4. Create Application and Desktop Launchers
echo "[2/3] Generating desktop and menu launchers..."
python3 "${SCRIPT_DIR}/create_launcher.py"

# 4. Success confirmation
echo "[3/3] Installation completed successfully!"
echo "========================================="
echo "SUIT is now installed and ready to use."
echo "Launch SUIT from your application menu or desktop icon."
echo "========================================="
