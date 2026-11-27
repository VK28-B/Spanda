#!/usr/bin/env bash
set -euo pipefail

ARCH="${1:-$(dpkg --print-architecture)}"
HOST_ARCH="$(dpkg --print-architecture)"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LINUX_DIR="$ROOT/linux"
BUILD_DIR="$LINUX_DIR/build-$ARCH"
PKG_DIR="$BUILD_DIR/pkg"
OUT_DEB="$LINUX_DIR/spanda_3.0_${ARCH}.deb"
OUT_PORTABLE="$LINUX_DIR/spanda_3.0_${ARCH}_portable.tar.gz"

case "$ARCH" in
    amd64|arm64) ;;
    *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

if [[ "$HOST_ARCH" != "$ARCH" ]]; then
    echo "This builder must run on a native $ARCH host (detected: $HOST_ARCH)." >&2
    exit 1
fi

for required in "$ROOT/SysAnalytics.py" "$ROOT/Spanda.png" "$ROOT/Spanda.ico"; do
    if [[ ! -f "$required" ]]; then
        echo "Missing required file: $required" >&2
        exit 1
    fi
done

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/dist" "$BUILD_DIR/work" "$BUILD_DIR/spec" \
         "$PKG_DIR/DEBIAN" "$PKG_DIR/usr/bin" "$PKG_DIR/usr/lib/spanda" \
         "$PKG_DIR/usr/share/applications" \
         "$PKG_DIR/usr/share/metainfo" \
         "$PKG_DIR/usr/share/pixmaps" \
         "$PKG_DIR/usr/share/icons/hicolor"

python -m PyInstaller \
    --name spanda \
    --onefile \
    --windowed \
    --clean \
    --noconfirm \
    --distpath "$BUILD_DIR/dist" \
    --workpath "$BUILD_DIR/work" \
    --specpath "$BUILD_DIR/spec" \
    --add-data "$ROOT/Spanda.png:." \
    --add-data "$ROOT/Spanda.ico:." \
    --hidden-import dearpygui \
    --hidden-import duckdb \
    --hidden-import psutil \
    --hidden-import PIL \
    --hidden-import GPUtil \
    "$ROOT/SysAnalytics.py"

install -Dm755 "$BUILD_DIR/dist/spanda" "$PKG_DIR/usr/lib/spanda/spanda"
install -Dm755 "$LINUX_DIR/usr/bin/spanda" "$PKG_DIR/usr/bin/spanda"
install -Dm644 "$LINUX_DIR/usr/share/applications/spanda.desktop" \
    "$PKG_DIR/usr/share/applications/spanda.desktop"
install -Dm644 "$LINUX_DIR/usr/share/metainfo/spanda.appdata.xml" \
    "$PKG_DIR/usr/share/metainfo/spanda.appdata.xml"
install -Dm644 "$ROOT/Spanda.png" "$PKG_DIR/usr/share/pixmaps/spanda.png"
install -Dm755 "$LINUX_DIR/DEBIAN/postinst" "$PKG_DIR/DEBIAN/postinst"

python - "$ROOT/Spanda.png" "$PKG_DIR/usr/share/icons/hicolor" <<'PY'
import os
import sys
from PIL import Image

source, target_root = sys.argv[1], sys.argv[2]
with Image.open(source) as image:
    image = image.convert("RGBA")
    for size in (256, 128, 64, 48, 32, 16):
        target_dir = os.path.join(target_root, f"{size}x{size}", "apps")
        os.makedirs(target_dir, exist_ok=True)
        scaled = image.resize((size, size), Image.LANCZOS)
        scaled.save(os.path.join(target_dir, "spanda.png"))
PY

find "$PKG_DIR" -type d -exec chmod 755 {} +
INSTALLED_SIZE="$(du -sk "$PKG_DIR" | cut -f1)"
sed -e "s/@ARCH@/$ARCH/" \
    -e "s/@INSTALLED_SIZE@/$INSTALLED_SIZE/" \
    "$LINUX_DIR/DEBIAN/control" > "$PKG_DIR/DEBIAN/control"
chmod 644 "$PKG_DIR/DEBIAN/control"

dpkg-deb --root-owner-group --build "$PKG_DIR" "$OUT_DEB"

tar -czf "$OUT_PORTABLE" \
    -C "$BUILD_DIR/dist" spanda \
    -C "$ROOT" Spanda.png

echo "Built: $OUT_DEB"
echo "Built: $OUT_PORTABLE"
