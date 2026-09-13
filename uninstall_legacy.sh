#!/usr/bin/env bash
set -e

echo "========================================="
echo "   SUIT - Legacy Cleanup & Uninstall     "
echo "========================================="

echo "[1/4] Stopping and removing legacy systemd background services..."
for svc in suit-killswitch.service autoglow.service; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        echo "  - Stopping $svc"
        sudo systemctl stop "$svc" || true
    fi
    if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
        echo "  - Disabling $svc"
        sudo systemctl disable "$svc" || true
    fi
    if [ -f "/etc/systemd/system/$svc" ]; then
        echo "  - Removing /etc/systemd/system/$svc"
        sudo rm -f "/etc/systemd/system/$svc"
    fi
done
sudo systemctl daemon-reload

echo "[2/4] Removing legacy touch udev rules..."
if [ -f "/etc/udev/rules.d/99-suit-touch.rules" ]; then
    echo "  - Removing /etc/udev/rules.d/99-suit-touch.rules"
    sudo rm -f "/etc/udev/rules.d/99-suit-touch.rules"
    sudo udevadm control --reload-rules || true
    sudo udevadm trigger --subsystem-match=input || true
fi

echo "[3/4] Removing legacy autostart entries..."
for auto in suit-rotation.desktop suit-chromium.desktop suit-kiosk.desktop; do
    if [ -f "$HOME/.config/autostart/$auto" ]; then
        echo "  - Removing ~/.config/autostart/$auto"
        rm -f "$HOME/.config/autostart/$auto"
    fi
done

echo "[4/4] Removing legacy desktop launchers..."
for desktop_dir in "$HOME/Desktop" "$HOME/Schreibtisch"; do
    for f in "SUIT.desktop" "de.iterathor.suit.gtk.desktop"; do
        if [ -f "$desktop_dir/$f" ]; then
            echo "  - Removing $desktop_dir/$f"
            rm -f "$desktop_dir/$f"
        fi
    done
done
for f in "SUIT.desktop" "de.iterathor.suit.gtk.desktop"; do
    if [ -f "$HOME/.local/share/applications/$f" ]; then
        echo "  - Removing ~/.local/share/applications/$f"
        rm -f "$HOME/.local/share/applications/$f"
    fi
done

echo "========================================="
echo "Legacy SUIT cleanup completed successfully!"
echo "Your system is clean and ready for SUIT for Fedora."
echo "========================================="
