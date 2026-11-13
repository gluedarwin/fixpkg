#!/usr/bin/env bash
# build_binary.sh — produce the standalone `fix` binary (Linux x86_64,
# macOS also works: PyInstaller ships universal2 wheels).
#
# PyInstaller is pinned (pyinstaller==6.22.3, verified on PyPI) and is
# installed into a local .venv, never into the system Python: on modern
# distros the system pip is externally managed (PEP 668) and a bare
# `pip install pyinstaller` refuses to run. If creating the venv fails
# (e.g. python3-venv is not installed), the script falls back to a
# per-user install with --break-system-packages and says so loudly.
set -euo pipefail
cd "$(dirname "$0")/.."

PYINSTALLER_VERSION="6.22.3"
VENV=".venv"

ensure_pyinstaller() {
    if [ -x "$VENV/bin/pyinstaller" ]; then
        echo "==> using PyInstaller from $VENV (already installed)"
        PYI=("$VENV/bin/pyinstaller")
        return
    fi
    if python3 -m venv "$VENV" 2>/dev/null; then
        echo "==> created $VENV; installing pyinstaller==$PYINSTALLER_VERSION"
        "$VENV/bin/pip" install --quiet "pyinstaller==$PYINSTALLER_VERSION"
        PYI=("$VENV/bin/pyinstaller")
        return
    fi
    echo "==> WARNING: could not create $VENV (is python3-venv installed?)."
    echo "==> Falling back to a per-user install with --break-system-packages."
    echo "==> This writes only into ~/.local, never into the system Python."
    python3 -m pip install --user --break-system-packages \
        "pyinstaller==$PYINSTALLER_VERSION"
    PYI=(python3 -m PyInstaller)
}

ensure_pyinstaller

echo "==> running smoke tests (source mode)"
bash tests/smoke.sh

echo "==> compiling with PyInstaller (--onefile --strip)"
"${PYI[@]}" --onefile --clean --strip --name fix fix.py
cp dist/fix fix
chmod +x fix

echo "==> re-running smoke tests against the binary"
FIX_BIN=./fix bash tests/smoke.sh

echo
ls -lh fix
echo "binary ready: $(pwd)/fix"
