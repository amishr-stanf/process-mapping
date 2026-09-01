#!/usr/bin/env bash
# Build the workflow-mapper app on macOS.
#   bash packaging/build_mac.sh
# Output: dist/workflow-mapper.app (and dist/workflow-mapper)
#
# Requires:
#   pip install pyinstaller pystray Pillow pyobjc-framework-Cocoa pyobjc-framework-Quartz
#
# Note the ':' path separator in --add-data (Windows uses ';').

set -e
cd "$(dirname "$0")/.."   # repo root

echo "Regenerating icons..."
python packaging/make_icons.py

# Optionally build an .icns from the 512px PNG if iconutil is available.
ICON_ARG=()
if command -v iconutil >/dev/null 2>&1 && [ -f packaging/icon-512.png ]; then
  echo "Building icon.icns..."
  ICONSET=packaging/icon.iconset
  rm -rf "$ICONSET"; mkdir -p "$ICONSET"
  sips -z 16 16     packaging/icon-512.png --out "$ICONSET/icon_16x16.png"     >/dev/null
  sips -z 32 32     packaging/icon-512.png --out "$ICONSET/icon_16x16@2x.png"  >/dev/null
  sips -z 128 128   packaging/icon-512.png --out "$ICONSET/icon_128x128.png"   >/dev/null
  sips -z 256 256   packaging/icon-512.png --out "$ICONSET/icon_256x256.png"   >/dev/null
  sips -z 512 512   packaging/icon-512.png --out "$ICONSET/icon_512x512.png"   >/dev/null
  iconutil -c icns "$ICONSET" -o packaging/icon.icns
  ICON_ARG=(--icon packaging/icon.icns)
fi

echo "Building app with PyInstaller..."
python -m PyInstaller --noconfirm --clean --onefile --windowed \
  --name workflow-mapper \
  "${ICON_ARG[@]}" \
  --add-data "ui/prototype.html:ui" \
  --add-data "ui/admin.html:ui" \
  --hidden-import sensors_mac --hidden-import sensors_null --hidden-import screen --hidden-import auth \
  tray.py

echo ""
echo "Done. Artifacts in dist/ (workflow-mapper.app and workflow-mapper)."
echo "First run: grant Accessibility + Screen Recording in System Settings > Privacy."
