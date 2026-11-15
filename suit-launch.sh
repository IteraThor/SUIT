#!/usr/bin/env bash
# Starts SUIT. After a Fedora upgrade the binary may no longer find the system libraries it was built
# against (new OpenCV or GTK sonames): then install.sh fetches the binary for the new release (or builds it) first.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$DIR/target/release/suit"
if [ ! -x "$BIN" ] || ! "$BIN" --version >/dev/null 2>&1; then
    notify-send -a "SUIT for Fedora" "SUIT" "Updating SUIT for this system, one moment..." 2>/dev/null || true
    bash "$DIR/install.sh" >>"$DIR/suit-build.log" 2>&1 < /dev/null || {
        notify-send -a "SUIT for Fedora" "SUIT" "Update failed, see $DIR/suit-build.log" 2>/dev/null || true
        exit 1
    }
fi
exec "$BIN" "$@"
