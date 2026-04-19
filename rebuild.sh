#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  Claude Counter — Quick Rebuild + Reinstall
#  Run this from the claude-menubar folder when you update claude_counter.py
# ─────────────────────────────────────────────────────────────────────────────
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   Claude Counter — Rebuild & Reinstall       ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# Kill any running instance
echo "▶ Stopping any running Claude Counter..."
pkill -f "Claude Counter" 2>/dev/null || true
sleep 1

# Delete BOTH the bad cached key (peanuts) and any stale log
echo "▶ Clearing stale caches..."
rm -f ~/.claude_counter_key.bin && echo "  ✓ Cleared key cache"
rm -f /tmp/claude_counter.log   && echo "  ✓ Cleared debug log"

# Activate venv
source venv/bin/activate

# Install/update deps (includes keyring this time)
echo "▶ Updating dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
pip install --quiet py2app
echo "  ✓ Dependencies ready"

# Clean old build
rm -rf build dist
echo "  ✓ Cleaned build artifacts"

# Rebuild
echo "▶ Building .app bundle (30-60s)..."
python3 setup.py py2app --quiet 2>&1 | tail -5

if [ -d "dist/Claude Counter.app" ]; then
    echo "  ✓ Build succeeded"

    # Remove old install
    rm -rf "/Applications/Claude Counter.app" 2>/dev/null || true
    rm -rf "$HOME/Applications/Claude Counter.app" 2>/dev/null || true

    # Copy to Applications
    cp -r "dist/Claude Counter.app" "/Applications/Claude Counter.app" 2>/dev/null \
      || cp -r "dist/Claude Counter.app" "$HOME/Applications/Claude Counter.app"
    echo "  ✓ Installed to /Applications"

    echo ""
    echo "▶ Starting Claude Counter..."
    echo ""
    echo "  ⚠️  IMPORTANT — When the Keychain dialog appears:"
    echo "     Click 'Always Allow' (NOT 'Allow')"
    echo "     After that, the key is cached and the dialog NEVER appears again."
    echo ""
    open "/Applications/Claude Counter.app" 2>/dev/null \
      || open "$HOME/Applications/Claude Counter.app"

    sleep 5
    echo ""
    echo "  ── Debug log (last 20 lines) ──────────────────"
    tail -20 /tmp/claude_counter.log 2>/dev/null || echo "  (log not yet written)"
    echo "  ───────────────────────────────────────────────"
    echo ""
    echo "  To watch live: tail -f /tmp/claude_counter.log"
else
    echo "  ✗ Build failed. Running directly instead..."
    python3 claude_counter.py
fi

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  ✅ Done! Check your menu bar for 🪙         ║"
echo "╚══════════════════════════════════════════════╝"
