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
# SUIT for Fedora is detected by its autostart entry (it runs core.display_service),
# not by the config file, which legacy installs may also have written.
if grep -qs "core.display_service" "$HOME/.config/autostart/suit-rotation.desktop"; then
    FEDORA_ACTIVE=1
else
    FEDORA_ACTIVE=0
fi

if [ -f "/etc/udev/rules.d/99-suit-touch.rules" ]; then
    if [ "$FEDORA_ACTIVE" -eq 0 ]; then
        echo "  - Removing legacy /etc/udev/rules.d/99-suit-touch.rules"
        sudo rm -f "/etc/udev/rules.d/99-suit-touch.rules"
        sudo udevadm control --reload-rules || true
        sudo udevadm trigger --subsystem-match=input || true
    else
        echo "  - Preserving active SUIT for Fedora touch calibration rule"
    fi
fi

echo "[3/4] Removing legacy autostart entries..."
for auto in suit-chromium.desktop suit-kiosk.desktop; do
    if [ -f "$HOME/.config/autostart/$auto" ]; then
        echo "  - Removing ~/.config/autostart/$auto"
        rm -f "$HOME/.config/autostart/$auto"
    fi
done
if [ "$FEDORA_ACTIVE" -eq 0 ] && [ -f "$HOME/.config/autostart/suit-rotation.desktop" ]; then
    echo "  - Removing legacy ~/.config/autostart/suit-rotation.desktop"
    rm -f "$HOME/.config/autostart/suit-rotation.desktop"
fi

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
