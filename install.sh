#!/usr/bin/env bash
set -e

echo "========================================="
echo "   SUIT for Fedora - Automated Setup    "
echo "========================================="

# 1. Check Fedora environment
if [ ! -f /etc/fedora-release ]; then
    echo "Warning: /etc/fedora-release not detected. SUIT is optimized for Fedora Linux."
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 2. Install native system dependencies via DNF
echo "[1/3] Installing system packages..."
sudo dnf install -y --setopt=install_weak_deps=False \
    python3 \
    python3-gobject \
    libadwaita \
    python3-evdev \
    python3-qrcode \
    python3-opencv \
    v4l-utils \
    git

# 3. Create Application and Desktop Launchers
echo "[2/3] Generating desktop and menu launchers..."
python3 "${SCRIPT_DIR}/create_launcher.py"

# 4. Synchronize active display & touch autostarts
if [ -f "$HOME/.suit_rotation_config.json" ]; then
    echo "  - Synchronizing display & touch autostart..."
    if ! (cd "${SCRIPT_DIR}" && python3 -c "from core.display_service import DisplayService; DisplayService.ensure_autostart()"); then
        echo "  ! Warning: could not synchronize display autostart. Launch SUIT once to retry automatically."
    fi
fi

# 5. Pre-install the switchable SUIT shell extensions (disabled) so GNOME discovers them at next login
if ! (cd "${SCRIPT_DIR}" && python3 -c "from core.desktop_service import DesktopService; DesktopService.install_suit_extensions()"); then
    echo "  ! Warning: could not pre-install the SUIT shell extensions. Launch SUIT once to retry automatically."
fi

# 6. Success confirmation
echo "[3/3] Installation completed successfully!"
echo "========================================="
echo "SUIT is now installed and ready to use."
echo "Launch SUIT from your application menu or Dash favorites."
echo "========================================="
