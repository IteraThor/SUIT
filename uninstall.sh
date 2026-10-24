#!/usr/bin/env bash
set -e

echo "========================================="
echo "   SUIT for Fedora - Uninstall & Cleanup "
echo "========================================="

echo "[1/5] Stopping and removing SUIT systemd services..."
if systemctl --user is-active --quiet suit-camera-settings.service 2>/dev/null; then
    echo "  - Stopping suit-camera-settings.service"
    systemctl --user stop suit-camera-settings.service || true
fi
if systemctl --user is-enabled --quiet suit-camera-settings.service 2>/dev/null; then
    echo "  - Disabling suit-camera-settings.service"
    systemctl --user disable suit-camera-settings.service || true
fi
rm -f "$HOME/.config/systemd/user/suit-camera-settings.service"
rm -f "$HOME/.config/systemd/user/default.target.wants/suit-camera-settings.service"
# Camera Optimizer keeper (and the exposure lock watcher of older versions). Optimized settings live in camera
# RAM only, so the cameras return to stock at the next power cycle; profiles in ~/.config/hbvcal are kept.
for unit in suit-camera-keeper.service suit-exposure-lock.service; do
    if systemctl --user is-enabled --quiet "$unit" 2>/dev/null || systemctl --user is-active --quiet "$unit" 2>/dev/null; then
        echo "  - Disabling $unit"
        systemctl --user disable --now "$unit" || true
    fi
    rm -f "$HOME/.config/systemd/user/$unit"
done
systemctl --user daemon-reload || true

echo "[2/5] Removing SUIT autostart entries..."
for auto in suit-rotation.desktop suit-kiosk.desktop suit-chromium.desktop; do
    if [ -f "$HOME/.config/autostart/$auto" ]; then
        echo "  - Removing ~/.config/autostart/$auto"
        rm -f "$HOME/.config/autostart/$auto"
    fi
done

echo "[3/5] Removing SUIT touch calibration udev rules..."
if [ -f "/etc/udev/rules.d/99-suit-touch.rules" ]; then
    echo "  - Removing /etc/udev/rules.d/99-suit-touch.rules"
    sudo rm -f "/etc/udev/rules.d/99-suit-touch.rules" || true
    sudo udevadm control --reload-rules || true
    sudo udevadm trigger --subsystem-match=input || true
fi

echo "[4/5] Removing SUIT desktop launchers..."
for desktop_dir in "$HOME/Desktop" "$HOME/Schreibtisch"; do
    for f in "de.iterathor.suit.gtk.desktop" "SUIT.desktop"; do
        if [ -f "$desktop_dir/$f" ]; then
            echo "  - Removing $desktop_dir/$f"
            rm -f "$desktop_dir/$f"
        fi
    done
done
for f in "de.iterathor.suit.gtk.desktop" "SUIT.desktop"; do
    if [ -f "$HOME/.local/share/applications/$f" ]; then
        echo "  - Removing ~/.local/share/applications/$f"
        rm -f "$HOME/.local/share/applications/$f"
    fi
done

echo "[5/5] Checking configuration files..."
if [ "$1" == "--purge" ] || [ "$1" == "--all" ]; then
    echo "  - Purging SUIT configurations..."
    rm -f "$HOME/.suit_rotation_config.json"
    rm -f "$HOME/.suit_camera_profiles.json" "$HOME/.suit_camera_profiles.json.migrated"
    rm -rf "$HOME/.config/hbvcal"
    rm -f "$HOME/suit.log"
    rm -f "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/suit.log"
else
    echo "  - Preserving camera and touch configurations (~/.suit_*.json)."
    echo "    (Run with --purge to remove configuration files as well)."
fi

echo "========================================="
echo "SUIT for Fedora uninstallation complete!"
echo "You can now safely delete the SUIT repository folder if desired."
echo "========================================="
