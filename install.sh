#!/bin/sh
# Rebuild and copy the plugin over an existing install. First-time install goes through
# OrcaSlicer: Plugins > Install local plugin > dist/farm_send/farm_send.py
set -e
cd "$(dirname "$0")"
python3 build.py
TARGET="${ORCA_DATA_DIR:-$HOME/.config/OrcaSlicer}/orca_plugins/farm_send.py/farm_send.py"
if [ ! -f "$TARGET" ]; then
  echo "Not installed yet. In OrcaSlicer: Plugins > Install local plugin > $(pwd)/dist/farm_send/farm_send.py"
  exit 1
fi
cp dist/farm_send/farm_send.py "$TARGET"
echo "Updated $TARGET"
echo "In OrcaSlicer: Plugins > untick and re-tick Farm Send to load the new build."
