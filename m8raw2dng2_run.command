#!/bin/bash
# m8raw2dng2 launcher (macOS) - see README "Launchers" for setup and flags.
cd "$(dirname "$0")"

PYTHON="python3"
SCRIPT="./m8raw2dng2.py"
INPUT="/Users/you/Photos/M8/RAW"
OUTPUT=""
FLAGS="-v -p -b -s --no-crop --cfa RGGB"

if [ ! -f "$SCRIPT" ]; then
  echo "[ERROR] Script not found: $SCRIPT"
  read -n 1 -s -r -p "Press any key to close."
  exit 1
fi
echo "Running: $FLAGS -i \"$INPUT\""
if [ -n "$OUTPUT" ]; then
  "$PYTHON" "$SCRIPT" $FLAGS -i "$INPUT" -o "$OUTPUT"
else
  "$PYTHON" "$SCRIPT" $FLAGS -i "$INPUT"
fi
