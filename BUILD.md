# VoiceBot — How to build a distributable .app / .dmg

## Requirements

- **Apple Silicon Mac** (M1/M2/M3/M4) — MLX does not run on Intel
- **macOS 13+**
- **Python 3.10, 3.11, or 3.12** — install via `brew install python@3.12`
- **Xcode Command Line Tools** — `xcode-select --install`

## One command

```bash
cd voicebot
chmod +x build_app.sh
./build_app.sh
```

Build time: ~2 minutes. Output in `dist/`:

| File | Purpose |
|------|---------|
| `VoiceBot.app` | The app bundle (also installed to `/Applications`) |
| `VoiceBot-2.0.0.dmg` | Drag-to-/Applications installer (preferred for sharing) |
| `VoiceBot-2.0.0.zip` | Same .app zipped (Telegram/email-friendly) |

The `.app` includes Python and all dependencies (MLX, mlx-whisper, scipy, numpy, PyObjC, rumps). **No** Whisper model files are bundled — they download automatically (~466 MB for the default `small` model; `large-v3-turbo` ~800 MB) on first launch on each user's machine.

## Distribute

Send `dist/VoiceBot-2.0.0.dmg` to whomever you want.

### What the recipient does

1. Open the DMG → drag VoiceBot into Applications.
2. **First launch only:** right-click VoiceBot in /Applications → **Open** → confirm "Open".
   - This is macOS Gatekeeper. Standard for any unsigned indie app. One-time bypass.
   - Eliminating this warning requires an **Apple Developer ID** ($99/yr) + notarization.
3. macOS prompts for **Microphone** — Allow.
4. macOS prompts for **Universal Access (Accessibility)** — Allow, then quit + relaunch.
5. Menu bar icon shows `⏬ Downloading model… NN%` (~466 MB for the default `small`, one-time per machine).
6. Press the hotkey (default `⌘⇧9`) to dictate.

### What macOS handles natively

- Permission prompts (Microphone, Accessibility) — system dialogs
- App icon, version, bundle ID — shown in About + Activity Monitor
- Launch-on-login — controlled via Settings → Login Items (the LaunchAgent plist is optional, only if user runs `install.sh`)

## Optional: proper distribution signing (no Gatekeeper warning)

For "just works, no scary warning" UX, you need:

1. **Apple Developer Program** — $99/year — https://developer.apple.com/programs/
2. Replace `--sign -` in `build_app.sh` with `--sign "Developer ID Application: Your Name (TEAMID)"`
3. After signing, **notarize** via `xcrun notarytool submit dist/VoiceBot-2.0.0.dmg --wait`
4. Staple the ticket: `xcrun stapler staple dist/VoiceBot-2.0.0.dmg`

This is the only way to ship a Mac app without recipients seeing the unidentified-developer warning. Standard indie distribution.

## Avoiding duplicate instances (two menu-bar icons)

VoiceBot enforces a **single instance** at startup (by bundle id
`com.mizz.voicebot` via `NSRunningApplication`) — a second launch logs
`Another VoiceBot instance is already running — exiting.` and quits. Two icons
almost always mean two *different* bundles or a stale launcher:

- `build_app.sh` now auto-cleans before installing: it unloads + removes any
  stale `~/Library/LaunchAgents/com.voicebot.plist` (an old `KeepAlive`
  LaunchAgent that relaunches the app) and `pkill`s any running copy — the
  installed one and the `dist/VoiceBot.app` build output — before swapping the
  bundle.
- If you ever see two icons manually: quit both, then
  `launchctl unload ~/Library/LaunchAgents/com.voicebot.plist 2>/dev/null;
  rm -f ~/Library/LaunchAgents/com.voicebot.plist`, delete any stray
  `VoiceBot.app` outside `/Applications` (check Spotlight), and relaunch the
  one in `/Applications`.
- `install.sh` (the optional LaunchAgent-based auto-start) and the `.app` are
  two different launch paths — don't run both.

## Troubleshooting

- **`pyinstaller: command not found`** → `pip install pyinstaller`. The build script does this automatically.
- **`ImportError: cannot import name 'mlx'`** at build time → you're on an Intel Mac. MLX requires Apple Silicon.
- **App crashes on launch with no log** → check `~/.voicebot/logs/voicebot_stderr.log`. If empty, run from terminal: `/Applications/VoiceBot.app/Contents/MacOS/VoiceBot` to see the crash trace.
- **`KeyError: 'gpt2'`** at first transcribe → `tiktoken_ext` missed bundling. Already in `VoiceBot.spec` hiddenimports, but verify the bundle has `Contents/Frameworks/tiktoken_ext/`.
- **`mlx.metallib` not found at runtime** → `collect_data_files('mlx')` may have changed location in a newer mlx release. Check `Contents/Frameworks/mlx/lib/` for the file.
