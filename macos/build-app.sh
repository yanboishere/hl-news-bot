#!/bin/bash
# Build HLNewsBot.app from the Swift package. Run on macOS 13+ with Xcode command line tools:
#   cd macos && ./build-app.sh
# Output: macos/build/HLNewsBot.app  (drag to /Applications if you like)
set -euo pipefail
cd "$(dirname "$0")"
swift build -c release
BIN="$(swift build -c release --show-bin-path)/HLNewsBot"
APP="build/HLNewsBot.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/HLNewsBot"
cp Sources/HLNewsBot/Resources/Info.plist "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleExecutable HLNewsBot" "$APP/Contents/Info.plist" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Add :CFBundleExecutable string HLNewsBot" "$APP/Contents/Info.plist"
# Ad-hoc sign so Gatekeeper lets it run locally without a developer certificate.
codesign --force --sign - "$APP" 2>/dev/null || true
echo "built $APP"
echo "open it with:  open $APP"
