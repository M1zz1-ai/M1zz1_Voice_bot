#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_SRC="$SCRIPT_DIR/com.mizz.voicebot.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.mizz.voicebot.plist"

# Remove any legacy agent from before the rename so it can't double-launch.
launchctl unload "$HOME/Library/LaunchAgents/com.voicebot.plist" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.voicebot.plist"

echo "=== VoiceBot Installer ==="

# 1. Find Python 3
PYTHON=""
for candidate in /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10 python3 /usr/local/bin/python3; do
  if command -v "$candidate" &>/dev/null; then
    PYTHON=$(command -v "$candidate")
    break
  fi
done

if [ -z "$PYTHON" ]; then
  echo "❌ Python 3 не найден. Установите: brew install python"
  exit 1
fi
echo "✅ Python: $PYTHON ($($PYTHON --version))"

# 2. Install dependencies
echo "📦 Устанавливаю зависимости..."
$PYTHON -m pip install -q -r "$SCRIPT_DIR/requirements.txt"
echo "✅ Зависимости установлены"

# 3. Create ~/.voicebot/ structure
echo "📁 Создаю ~/.voicebot/..."
mkdir -p "$HOME/.voicebot/logs"
mkdir -p "$HOME/.voicebot/cache/frames"

# 4. Resolve the __HOME__ placeholder to this user's real home
PLIST_TMP="$SCRIPT_DIR/com.mizz.voicebot.plist.tmp"
sed "s|__HOME__|$HOME|g" "$PLIST_SRC" > "$PLIST_TMP"

# 5. Install LaunchAgent
echo "⚙️  Устанавливаю LaunchAgent..."
mkdir -p "$HOME/Library/LaunchAgents"
cp "$PLIST_TMP" "$PLIST_DST"
rm "$PLIST_TMP"

# Reload
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

echo ""
echo "✅ VoiceBot запущен!"
echo "   Иконка появится в строке меню (вверху справа)"
echo "   Горячая клавиша: Cmd+Shift+9"
echo ""
echo "📋 Управление:"
echo "   Остановить:  launchctl unload $PLIST_DST"
echo "   Запустить:   launchctl load $PLIST_DST"
echo "   Конфиг:      $HOME/.voicebot/config.json"
echo "   Логи:        tail -f $HOME/.voicebot/logs/voicebot.log"
