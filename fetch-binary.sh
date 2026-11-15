#!/usr/bin/env bash
# Put the prebuilt SUIT binary for this commit, Fedora release and CPU into target/release/suit and write the
# Fedora packages it needs to target/release/runtime-packages.txt. CI (.github/workflows/binaries.yml) uploads
# one per commit on main to the "binaries" release.
#
#   fetch-binary.sh              download for HEAD; exit 1 when there is none (install.sh then builds)
#   fetch-binary.sh --exists C   exit 0 when a binary for commit C is published for this machine
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_URL="${SUIT_BINARY_URL:-https://github.com/IteraThor/SUIT/releases/download/binaries}"

# shellcheck disable=SC1091
. /etc/os-release
asset() { echo "suit-$1-fc${VERSION_ID}-$(uname -m).tar.gz"; }

if [ "${1:-}" = "--exists" ]; then
    exec curl -fsIL --connect-timeout 5 --max-time 10 -o /dev/null "$BASE_URL/$(asset "$2")"
fi

cd "$DIR"
# A binary built from main does not contain local changes to the code
if ! git diff --quiet HEAD -- src Cargo.toml Cargo.lock 2>/dev/null; then
    echo "  - Local source changes: building instead of downloading."
    exit 1
fi
NAME="$(asset "$(git rev-parse HEAD)")"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if ! curl -fsSL --retry 2 --connect-timeout 10 --max-time 300 -o "$TMP/$NAME" "$BASE_URL/$NAME"; then
    echo "  - No prebuilt binary $NAME: building instead."
    exit 1
fi
tar -xzf "$TMP/$NAME" -C "$TMP" suit runtime-packages.txt
mkdir -p target/release
# rename, so a running SUIT keeps its old binary until it restarts
install -m 755 "$TMP/suit" target/release/.suit.new && mv -f target/release/.suit.new target/release/suit
mv -f "$TMP/runtime-packages.txt" target/release/runtime-packages.txt
echo "  - Downloaded prebuilt binary $NAME."
