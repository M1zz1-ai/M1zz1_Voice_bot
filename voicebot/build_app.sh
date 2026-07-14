#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# VoiceBot — production build & distribution script.
#
# Produces (in voicebot/dist/):
#   1. VoiceBot.app             — self-contained app bundle
#   2. VoiceBot-2.0.0.dmg       — drag-to-/Applications installer (preferred)
#   3. VoiceBot-2.0.0.zip       — zipped .app for Telegram/email (fallback)
#
# Also installs to /Applications/VoiceBot.app for your own use.
#
# Run on:  Apple Silicon Mac (M1/M2/M3/M4), macOS 13+, Python 3.10/3.11/3.12
# Build time: ~2 min (first run) / ~30 s (incremental).
# Output DMG: ~150 MB (Whisper model NOT bundled — downloads on first run
# on each user's machine).
# ─────────────────────────────────────────────────────────────────────────────

set -e

# ── Refuse sudo ───────────────────────────────────────────────────────────────
# pip + venv must NOT run as root: caches break, perms get inverted, and
# PyInstaller 7.0 will hard-block sudo. /Applications is writable by any
# admin user without sudo, so this script never needs root.
if [[ $EUID -eq 0 ]]; then
    echo "❌ Don't run this script with sudo."
    echo "   Just run:  ./build_app.sh"
    echo "   /Applications is writable for admin users without sudo."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="VoiceBot"
APP_VERSION="2.0.0"
BUNDLE_ID="com.mizz.voicebot"
APP_PATH="$SCRIPT_DIR/dist/$APP_NAME.app"
DMG_PATH="$SCRIPT_DIR/dist/$APP_NAME-$APP_VERSION.dmg"
ZIP_PATH="$SCRIPT_DIR/dist/$APP_NAME-$APP_VERSION.zip"
INSTALL_PATH="/Applications/$APP_NAME.app"
VENV_DIR="$SCRIPT_DIR/.build_venv"

# ── Sanity checks ─────────────────────────────────────────────────────────────

if [[ "$(uname)" != "Darwin" ]]; then
    echo "❌ This script must run on macOS. Current OS: $(uname)"
    exit 1
fi

if [[ "$(uname -m)" != "arm64" ]]; then
    echo "❌ Apple Silicon (M1/M2/M3/M4) required. Current arch: $(uname -m)"
    echo "   MLX does not run on Intel Macs."
    exit 1
fi

# Find a base Python 3.10/3.11/3.12 on the system (not inside the venv).
SYSTEM_PYTHON=""
for candidate in \
    /opt/homebrew/bin/python3.12 \
    /opt/homebrew/bin/python3.11 \
    /opt/homebrew/bin/python3.10 \
    python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        CAND=$(command -v "$candidate")
        VERS=$("$CAND" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")
        if [[ "$(echo "$VERS" | awk -F. '{print $1*100+$2}')" -ge 310 ]]; then
            SYSTEM_PYTHON="$CAND"
            break
        fi
    fi
done

if [[ -z "$SYSTEM_PYTHON" ]]; then
    echo "❌ Python 3.10+ not found. Install:"
    echo "   brew install python@3.12"
    exit 1
fi
echo "🐍 System Python: $SYSTEM_PYTHON ($("$SYSTEM_PYTHON" --version))"

# ── Build venv ────────────────────────────────────────────────────────────────
# CRITICAL: we build inside an isolated venv so PyInstaller only sees the
# dependencies we actually need. Without this, PyInstaller's module graph
# walks the entire system site-packages and pulls in torch/tensorflow/etc.
# from other unrelated projects, which then fail with x86_64-vs-arm64
# binary mismatches (e.g. google.protobuf x86_64 .so → arm64 .app crash).

if [[ ! -d "$VENV_DIR" ]] || [[ ! -x "$VENV_DIR/bin/python3" ]]; then
    echo "🐍 Creating clean build venv at $VENV_DIR"
    rm -rf "$VENV_DIR"
    "$SYSTEM_PYTHON" -m venv "$VENV_DIR"
else
    echo "🐍 Reusing existing build venv at $VENV_DIR"
fi

VENV_PYTHON="$VENV_DIR/bin/python3"

echo "📦 Installing build deps into venv (no global pollution)..."
"$VENV_PYTHON" -m pip install --quiet --upgrade pip wheel
"$VENV_PYTHON" -m pip install --quiet pyinstaller
"$VENV_PYTHON" -m pip install --quiet -r "$SCRIPT_DIR/requirements.txt"

# Sanity-print what's in the venv
echo "📋 Venv packages:"
"$VENV_PYTHON" -m pip list --format=columns | sed 's/^/   /'

# ── Clean & build ─────────────────────────────────────────────────────────────

echo "🔨 Building $APP_NAME.app with PyInstaller..."
cd "$SCRIPT_DIR"
rm -rf build dist __pycache__
"$VENV_PYTHON" -m PyInstaller --clean --noconfirm VoiceBot.spec

if [[ ! -d "$APP_PATH" ]]; then
    echo "❌ PyInstaller did not produce $APP_PATH"
    exit 1
fi
echo "✅ Built: $APP_PATH"

# ── Codesign (ad-hoc) ─────────────────────────────────────────────────────────
# Ad-hoc signing gives the bundle a stable identity in macOS's TCC
# (Microphone + Accessibility) database. For TRUE production distribution
# (no Gatekeeper warning), replace `--sign -` with a Developer ID + notarize.

echo "🔏 Codesigning ad-hoc..."
codesign --force --deep --sign - \
    --entitlements "$SCRIPT_DIR/entitlements.plist" \
    --options runtime \
    "$APP_PATH" 2>&1 | grep -v "replacing existing signature" || true

codesign --verify --deep --strict "$APP_PATH" && echo "✅ Codesign verified"

# ── Clean up stale installs (avoid double menu-bar icons) ─────────────────────
# A leftover KeepAlive LaunchAgent or an old bundle can silently run a second
# copy alongside the freshly installed one — the classic "two mascot icons" bug.
STALE_PLIST="$HOME/Library/LaunchAgents/com.voicebot.plist"
if [[ -f "$STALE_PLIST" ]]; then
    echo "🧹 Removing stale LaunchAgent: $STALE_PLIST"
    launchctl unload "$STALE_PLIST" 2>/dev/null || true
    rm -f "$STALE_PLIST"
fi
# Kill any running VoiceBot (installed OR the just-built dist copy) before we
# swap the bundle, so no old instance lingers.
pkill -f "VoiceBot.app/Contents/MacOS/VoiceBot" 2>/dev/null || true
sleep 1

# ── Install locally ───────────────────────────────────────────────────────────

echo "📦 Installing to /Applications..."
rm -rf "$INSTALL_PATH"
cp -R "$APP_PATH" "$INSTALL_PATH"
echo "✅ Installed: $INSTALL_PATH"

tccutil reset Accessibility "$BUNDLE_ID" 2>/dev/null || true
tccutil reset Microphone "$BUNDLE_ID" 2>/dev/null || true

# ── Build DMG installer ───────────────────────────────────────────────────────

echo "💾 Building DMG installer..."
rm -f "$DMG_PATH"

DMG_STAGE="$SCRIPT_DIR/dist/dmg_stage"
rm -rf "$DMG_STAGE"
mkdir -p "$DMG_STAGE"
cp -R "$APP_PATH" "$DMG_STAGE/"
ln -s /Applications "$DMG_STAGE/Applications"

hdiutil create \
    -volname "$APP_NAME" \
    -srcfolder "$DMG_STAGE" \
    -ov \
    -format UDZO \
    -fs HFS+ \
    "$DMG_PATH" >/dev/null

rm -rf "$DMG_STAGE"

if [[ ! -f "$DMG_PATH" ]]; then
    echo "❌ DMG creation failed"
    exit 1
fi
echo "✅ DMG: $DMG_PATH ($(du -h "$DMG_PATH" | cut -f1))"

# ── Build ZIP fallback ────────────────────────────────────────────────────────

echo "🗜  Building ZIP fallback..."
rm -f "$ZIP_PATH"
# ditto preserves bundle structure + codesign (better than `zip -r`)
ditto -c -k --keepParent "$APP_PATH" "$ZIP_PATH"
echo "✅ ZIP: $ZIP_PATH ($(du -h "$ZIP_PATH" | cut -f1))"

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════════════════"
echo " ✅ BUILD COMPLETE — VoiceBot $APP_VERSION"
echo "═══════════════════════════════════════════════════════════════════════"
echo ""
echo " 📨 To distribute:"
echo "    Preferred:  $DMG_PATH"
echo "    Fallback:   $ZIP_PATH  (for Telegram/email — .app is a folder)"
echo ""
echo " 🚀 Local install:"
echo "    $INSTALL_PATH  (launch from /Applications)"
echo ""
echo " ⚠️  First-launch UX for any user receiving this build:"
echo "    1. Open the DMG, drag VoiceBot.app into Applications."
echo "    2. First launch: right-click the app → Open → confirm 'Open'."
echo "       (Gatekeeper warning — one time only. Apple Developer ID"
echo "        signing would remove this, costs \$99/yr.)"
echo "    3. macOS prompts for Microphone — Allow."
echo "    4. macOS prompts for Accessibility — Allow + restart VoiceBot."
echo "    5. Menu bar shows '⏬ Downloading model… NN%' (~800 MB, one time)."
echo "    6. Done — press the hotkey to dictate."
echo ""
