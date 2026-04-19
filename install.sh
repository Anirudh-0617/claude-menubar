#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  Claude Counter — macOS Menu Bar App — One-click installer
#  Run this in Terminal from the claude-menubar folder.
# ─────────────────────────────────────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   Claude Counter — Menu Bar App Installer        ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ── 1. Check Python ──────────────────────────────────────────────────────────
echo "▶ Checking Python 3..."
if ! command -v python3 &>/dev/null; then
  echo "  ✗ Python 3 not found. Install from https://python.org or via 'brew install python'"
  exit 1
fi
PYTHON=$(command -v python3)
echo "  ✓ $($PYTHON --version)"

# ── 2. Check / create venv ───────────────────────────────────────────────────
echo "▶ Setting up virtual environment..."
if [ ! -d "venv" ]; then
  $PYTHON -m venv venv
  echo "  ✓ venv created"
else
  echo "  ✓ venv exists"
fi

source venv/bin/activate
pip install --quiet --upgrade pip

# ── 3. Install dependencies ──────────────────────────────────────────────────
echo "▶ Installing dependencies (rumps, requests, pyobjc)..."
pip install --quiet -r requirements.txt
echo "  ✓ Dependencies installed"

# ── 4. Option: run directly or build .app ────────────────────────────────────
echo ""
echo "How would you like to run Claude Counter?"
echo "  1) Run directly now (python script, stays running in Terminal)"
echo "  2) Build a standalone .app and open it (recommended)"
echo ""
read -p "Enter 1 or 2: " choice

if [ "$choice" = "2" ]; then
  echo ""
  echo "▶ Building .app bundle (this takes ~30–60 seconds)..."
  pip install --quiet py2app

  # Generate a simple ICNS if none exists
  if [ ! -f "AppIcon.icns" ]; then
    python3 - <<'PYEOF'
import struct, zlib, math, subprocess, os, tempfile

def make_png(size, color=(204, 120, 92)):
    img = []
    cx = cy = size / 2
    r  = size * 0.42
    for y in range(size):
        row = []
        for x in range(size):
            dx, dy = x - cx, y - cy
            dist = math.sqrt(dx*dx + dy*dy)
            if dist <= r:
                c = list(color) + [255]
            else:
                c = [0, 0, 0, 0]
            row += c
        img.append(bytes([0] + row))
    raw = b''.join(img)
    compressed = zlib.compress(raw, 9)
    def chunk(name, data):
        c = name + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    png = b'\x89PNG\r\n\x1a\n'
    png += chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0))
    png += chunk(b'IDAT', compressed)
    png += chunk(b'IEND', b'')
    return png

tmpdir = tempfile.mkdtemp()
iconset = os.path.join(tmpdir, 'AppIcon.iconset')
os.makedirs(iconset)

sizes = [16, 32, 64, 128, 256, 512, 1024]
for sz in sizes:
    with open(f'{iconset}/icon_{sz}x{sz}.png', 'wb') as f:
        f.write(make_png(sz))
    with open(f'{iconset}/icon_{sz}x{sz}@2x.png', 'wb') as f:
        f.write(make_png(min(sz*2, 1024)))

os.system(f'iconutil -c icns {iconset} -o AppIcon.icns 2>/dev/null || true')
import shutil; shutil.rmtree(tmpdir, ignore_errors=True)
PYEOF
    echo "  ✓ Icon generated"
  fi

  rm -rf build dist
  python3 setup.py py2app --quiet 2>&1 | tail -5

  if [ -d "dist/Claude Counter.app" ]; then
    echo ""
    echo "  ✓ App built: dist/Claude Counter.app"
    echo ""
    echo "▶ Copying to /Applications..."
    cp -r "dist/Claude Counter.app" "/Applications/Claude Counter.app" 2>/dev/null || \
      cp -r "dist/Claude Counter.app" "$HOME/Applications/Claude Counter.app" 2>/dev/null || true
    echo "  ✓ Installed!"
    echo ""
    echo "▶ Opening Claude Counter..."
    open "/Applications/Claude Counter.app" 2>/dev/null || \
      open "$HOME/Applications/Claude Counter.app" 2>/dev/null || \
      open "dist/Claude Counter.app"
  else
    echo "  ✗ Build failed. Falling back to direct run..."
    choice="1"
  fi
fi

if [ "$choice" = "1" ]; then
  echo ""
  echo "▶ Starting Claude Counter..."
  echo "  (Close this Terminal to stop it, or press Ctrl+C)"
  echo ""
  python3 claude_counter.py
fi

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  ✅ Claude Counter is running in your menu bar!              ║"
echo "║  Look for 🪙 in the top-right of your screen.               ║"
echo "╚══════════════════════════════════════════════════════════════╝"
