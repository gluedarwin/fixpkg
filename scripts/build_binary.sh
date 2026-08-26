#!/usr/bin/env bash
# build_binary.sh — produce the standalone `fix` binary (Linux x86_64).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> installing PyInstaller if needed"
python3 -m pip show pyinstaller >/dev/null 2>&1 || python3 -m pip install pyinstaller

echo "==> running smoke tests (source mode)"
bash tests/smoke.sh

echo "==> compiling with PyInstaller (--onefile --strip)"
python3 -m PyInstaller --onefile --clean --strip --name fix fix.py
cp dist/fix fix
chmod +x fix

echo "==> re-running smoke tests against the binary"
FIX_BIN=./fix bash tests/smoke.sh

echo
ls -lh fix
echo "binary ready: $(pwd)/fix"
