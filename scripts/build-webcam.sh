#!/usr/bin/env bash
# Build "Lumix Remote Camera.app" (host app + CoreMediaIO camera extension), copy it to
# /Applications and ask macOS to activate the extension.
#
#   scripts/build-webcam.sh            # build, install to /Applications, activate
#   scripts/build-webcam.sh --build    # build only (output: macos/build/)
#   scripts/build-webcam.sh --uninstall
#
# Env:
#   LUMIX_TEAM_ID        Apple Developer team id (default: OU of your "Apple Development" cert)
#   LUMIX_BUNDLE_PREFIX  reverse-DNS prefix for the bundle ids (default: io.github.lumixremote)
#   DEVELOPER_DIR        Xcode to use (default: /Applications/Xcode.app/Contents/Developer)
#
# Needs: Xcode, XcodeGen (brew install xcodegen), an Apple Developer Program membership with
# that team signed in to Xcode (camera extensions need the System Extension capability,
# which free accounts don't get).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MAC="$ROOT/macos"
APP_NAME="Lumix Remote Camera.app"
export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
export LUMIX_BUNDLE_PREFIX="${LUMIX_BUNDLE_PREFIX:-io.github.lumixremote}"
MODE="${1:-}"

if [[ "$MODE" == "--uninstall" ]]; then
  if [[ -d "/Applications/$APP_NAME" ]]; then
    open -W -a "/Applications/$APP_NAME" --args --uninstall &
    echo "Asked the app to remove the camera extension; quit it when it says Uninstalled,"
    echo "then: rm -rf \"/Applications/$APP_NAME\""
  fi
  exit 0
fi

if [[ -z "${LUMIX_TEAM_ID:-}" ]]; then
  LUMIX_TEAM_ID="$(security find-certificate -c "Apple Development" -p 2>/dev/null \
    | openssl x509 -noout -subject 2>/dev/null | sed -n 's/.*OU *= *\([A-Z0-9]\{10\}\).*/\1/p' | head -1)"
fi
[[ -n "${LUMIX_TEAM_ID:-}" ]] || { echo "Set LUMIX_TEAM_ID (no Apple Development cert found)" >&2; exit 1; }
export LUMIX_TEAM_ID

command -v xcodegen >/dev/null || { echo "Install XcodeGen: brew install xcodegen" >&2; exit 1; }
(cd "$MAC" && xcodegen generate --quiet)

xcodebuild -project "$MAC/LumixRemoteCamera.xcodeproj" -scheme LumixRemoteCamera \
  -configuration Release -derivedDataPath "$MAC/build/DerivedData" \
  -allowProvisioningUpdates -quiet build

BUILT="$MAC/build/DerivedData/Build/Products/Release/$APP_NAME"
echo "Built: $BUILT"
[[ "$MODE" == "--build" ]] && exit 0

# System extensions only activate from an app inside /Applications.
pkill -f "/Applications/$APP_NAME/Contents/MacOS/" 2>/dev/null || true
rm -rf "/Applications/$APP_NAME"
ditto "$BUILT" "/Applications/$APP_NAME"
open "/Applications/$APP_NAME" --args --install
echo "Opened /Applications/$APP_NAME and requested activation."
echo "First time: approve it in System Settings > General > Login Items & Extensions > Camera Extensions."
