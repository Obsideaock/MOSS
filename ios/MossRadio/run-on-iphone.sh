#!/usr/bin/env bash
# Build + install this app on the iPhone, and register the build with the
# goodmaps-sync renewal kit so it's kept signed automatically.
set -euo pipefail
cd "$(dirname "$0")"
KIT="${GM_SYNC:-$HOME/goodmaps-sync}/ops/gm-release.sh"
if [[ -f "$KIT" ]]; then
  GM_APP_DIR="$PWD" exec bash "$KIT" "$@"
fi
echo "goodmaps-sync kit not found at $KIT; falling back to plain xtool dev" >&2
export USBMUXD_SOCKET_ADDRESS="${USBMUXD_SOCKET_ADDRESS:-127.0.0.1:27015}"
exec "$HOME/ios-development/tools/xtool" dev
