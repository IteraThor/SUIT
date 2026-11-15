#!/usr/bin/env bash
set -e

echo "========================================="
echo "   SUIT for Fedora (Rust) - Automated Setup"
echo "========================================="

if [ ! -f /etc/fedora-release ]; then
    echo "Warning: /etc/fedora-release not detected. SUIT is optimized for Fedora Linux."
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TOOLS="v4l-utils pipewire-utils git"
BUILD_PACKAGES="cargo rust gtk4-devel libadwaita-devel vte291-gtk4-devel opencv-devel clang-devel"

# Without a terminal (the in-app update, the switch from the Python SUIT) sudo cannot ask for a password: use the
# polkit dialog instead
SUDO="sudo"
if [ ! -t 0 ] && ! sudo -n true 2>/dev/null; then
    SUDO="pkexec"
fi
install_packages() {
    if rpm -q "$@" >/dev/null 2>&1; then
        echo "  - All packages are already installed."
    else
        $SUDO dnf install -y --setopt=install_weak_deps=False "$@"
    fi
}

# 1. The prebuilt binary for this commit (seconds), else a local build (minutes, needs the build packages)
echo "[1/4] Getting SUIT..."
BUILD=1
if "${SCRIPT_DIR}/fetch-binary.sh"; then
    echo "[2/4] Installing system packages..."
    install_packages $TOOLS $(cat "${SCRIPT_DIR}/target/release/runtime-packages.txt")
    if "${SCRIPT_DIR}/target/release/suit" --version >/dev/null 2>&1; then
        BUILD=0
    else
        echo "  - The prebuilt binary does not start on this system: building instead."
    fi
fi
if [ "$BUILD" = 1 ]; then
    echo "[2/4] Installing build packages and building SUIT (this takes a few minutes)..."
    install_packages $TOOLS $BUILD_PACKAGES

    # opencv-devel pulls in opencascade-draw (via opencv-viz and vtk), a CAD test console that would show up in
    # the App Grid. It cannot be removed without breaking OpenCV, so hide its menu entry with a user override.
    for f in opencascade-draw.desktop; do
        if [ -f "/usr/share/applications/$f" ] && [ ! -f "$HOME/.local/share/applications/$f" ]; then
            mkdir -p "$HOME/.local/share/applications"
            { cat "/usr/share/applications/$f"; printf 'NoDisplay=true\nX-SUIT-Hidden=true\n'; } > "$HOME/.local/share/applications/$f"
            echo "  - Hid build dependency launcher $f from the App Grid"
        fi
    done

    (cd "${SCRIPT_DIR}" && cargo build --release)
fi
chmod +x "${SCRIPT_DIR}/suit-launch.sh"

# 3. Application and desktop launchers
echo "[3/4] Generating desktop and menu launchers..."
"${SCRIPT_DIR}/target/release/suit" create-launcher

# 4. Display & touch autostart and the switchable shell extensions (disabled, discovered at next login)
if [ -f "$HOME/.suit_rotation_config.json" ]; then
    echo "  - Synchronizing display & touch autostart..."
    "${SCRIPT_DIR}/target/release/suit" ensure-autostart || \
        echo "  ! Warning: could not synchronize display autostart. Launch SUIT once to retry automatically."
fi
"${SCRIPT_DIR}/target/release/suit" install-extensions || \
    echo "  ! Warning: could not pre-install the SUIT shell extensions. Launch SUIT once to retry automatically."

echo "[4/4] Installation completed successfully!"
echo "========================================="
echo "SUIT is now installed and ready to use."
echo "Launch SUIT from your application menu or Dash favorites."
echo "========================================="
