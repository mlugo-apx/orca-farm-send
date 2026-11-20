#!/bin/sh
# Rebuild and copy the plugin over an existing install. First-time install goes through
# OrcaSlicer: Plugins > Install local plugin > dist/farm_send/farm_send.py
set -e
cd "$(dirname "$0")"
python3 build.py
# The bundle is what OrcaSlicer runs, so it must pass the tests itself before it is installed.
# One run, with its full output kept: a failure that does not repeat can still be read afterwards.
LOG="$(pwd)/dist/last-test-run.log"
if FARM_SEND_BUNDLE="$(pwd)/dist/farm_send/farm_send.py" python3 tests/test_farm.py > "$LOG" 2>&1; then
  tail -3 "$LOG"
else
  grep -E "^(FAIL|ERROR):" "$LOG" || true
  tail -3 "$LOG"
  echo "Bundle failed its tests; not installing. Full output: $LOG"
  exit 1
fi
TARGET="${ORCA_DATA_DIR:-$HOME/.config/OrcaSlicer}/orca_plugins/farm_send.py/farm_send.py"
if [ ! -f "$TARGET" ]; then
  echo "Not installed yet. In OrcaSlicer: Plugins > Install local plugin > $(pwd)/dist/farm_send/farm_send.py"
  exit 1
fi
cp dist/farm_send/farm_send.py "$TARGET"
echo "Updated $TARGET"
echo "In OrcaSlicer: Plugins > untick and re-tick Farm Send to load the new build."
