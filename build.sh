#!/usr/bin/env bash
#
# build.sh:       LINUX EXECUTABLE GENERATOR
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-09-21
# VERSION:        0.1.2


# Builds the single "starscope" Linux binary (PERCEPTOR + STARSCOPE) via PyInstaller.
# Usage: ./build.sh
#        ./build.sh --install-desktop path/to/icon.png
# Output: dist/starscope

set -euo pipefail
cd "$(dirname "$0")"

if [ "$(id -u)" -eq 0 ]; then
  echo "WARNING: running as root -- PyInstaller 7.0 will refuse this." >&2
fi

ICON=""
if [ "${1:-}" = "--install-desktop" ]; then
  ICON="${2:-}"
  if [ -z "$ICON" ] || [ ! -f "$ICON" ]; then
    echo "Usage: $0 --install-desktop <path-to-icon-image>" >&2
    exit 1
  fi
fi

missing=()
for f in source/perceptor.py source/starscope.py source/jira_ticket.py static/perceptor.css static/starscope.css; do
  [ -f "$f" ] || missing+=("$f")
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "ERROR: missing required file(s) relative to build.sh:" >&2
  printf '  %s\n' "${missing[@]}" >&2
  exit 1
fi

pip install --break-system-packages --quiet pyinstaller python-docx

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

pyinstaller --onefile --name starscope \
  --add-data "$(pwd)/static:static" \
  --workpath "$WORKDIR" \
  --specpath "$WORKDIR" \
  source/starscope.py

echo
echo "Built: dist/starscope"
echo "Optional (drop next to the binary before running, not required):"
echo "  jira_config.json  -- enables Jira ticket creation"
echo "  measure           -- enables the Measure Read button"

if [ -n "$ICON" ]; then
  BINARY="$(realpath dist/starscope)"
  WORKDIR="$HOME/Starscope"
  mkdir -p "$WORKDIR"

  for f in jira_config.json measure; do
    if [ -f "dist/$f" ]; then
      mv "dist/$f" "$WORKDIR/"
      echo "Moved dist/$f -> $WORKDIR/$f"
    fi
  done

  ICON_DIR="$HOME/.local/share/icons"
  mkdir -p "$ICON_DIR"
  ICON_EXT="${ICON##*.}"
  ICON_DEST="$ICON_DIR/starscope.$ICON_EXT"
  cp "$ICON" "$ICON_DEST"

  DESKTOP_DIR="$HOME/.local/share/applications"
  mkdir -p "$DESKTOP_DIR"
  DESKTOP_FILE="$DESKTOP_DIR/starscope.desktop"

  cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=STARSCOPE
Comment=Test campaign creator (includes Perceptor's test-card editor)
Exec=$BINARY
Path=$WORKDIR
Icon=$ICON_DEST
Terminal=false
Categories=Development;Engineering;
EOF
  chmod +x "$DESKTOP_FILE"

  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
  fi

  echo
  echo "Installed. STARSCOPE should now appear in your application menu/search --"
  echo "right-click it there to pin it to the dash/sidebar."
  echo "Working directory: $WORKDIR"
  echo "Binary:            $BINARY"
fi