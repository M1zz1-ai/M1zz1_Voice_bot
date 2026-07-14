# VoiceBot

Offline, Russian-first voice dictation for macOS. Press a hotkey, speak, and
the transcription is pasted into whatever app has focus — all on-device, no
network, no cloud API. Transcription runs locally on Apple Silicon via
[MLX Whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper).

It lives in the menu bar, shows a subtle screen-top glow while it listens, and
supports live "type as you speak" transcription.

## Features

- **On-device transcription** — Whisper models run locally through Apple MLX;
  audio never leaves your Mac.
- **Menu-bar app** — a small mascot icon with recording / processing / success
  / error states; no Dock clutter.
- **Screen-top glow overlay** — a borderless, click-through purple glow band
  across the top of the screen while recording, so you always know it's
  listening.
- **Live transcription mode** — commits stable text as you speak instead of
  waiting for you to stop.
- **Global hotkey** — Carbon `RegisterEventHotKey` (needs only Accessibility,
  not Input Monitoring). Default `⌘⇧9`, configurable.
- **Model picker** — tiny → large-v3-turbo, chosen in Settings; the default is
  `small` (~466 MB) for lower-RAM Macs. Idle models auto-unload from RAM and
  re-warm on the next dictation.
- **Start at login** — optional LaunchAgent, toggled in Settings.
- **Clipboard-safe** — the transcription is always left in the clipboard, so if
  auto-paste has nowhere to land you can `⌘V` it manually.

## Requirements

- **Apple Silicon Mac** (M1/M2/M3/M4) — MLX does not run on Intel.
- **macOS 13+**
- For building from source: Python 3.10–3.12 (`brew install python@3.12`) and
  Xcode Command Line Tools (`xcode-select --install`).

## Install from source

```bash
git clone <your-fork-url> voicebot
cd voicebot/voicebot
./build_app.sh
```

This builds a self-contained `VoiceBot.app` (and a `.dmg`) into `dist/` and
installs it to `/Applications`. See [BUILD.md](BUILD.md) for details,
distribution/signing notes, and troubleshooting. Whisper model files are **not**
bundled — they download automatically on first launch (~466 MB for the default
`small` model).

## Permissions

On first launch macOS will prompt for:

- **Microphone** — to record your voice.
- **Accessibility** — to register the global hotkey and paste text. After
  granting it, quit and relaunch once.

Both are standard system prompts; VoiceBot stores nothing about them.

## Usage

1. Press the hotkey (default `⌘⇧9`) to start recording — the menu-bar icon
   animates and the top-of-screen glow appears.
2. Speak.
3. Press the hotkey again to stop. The transcription is pasted into the focused
   field (and left in the clipboard).

## Configuration

Settings live in `~/.voicebot/config.json` (also editable via the Settings
window). Keys:

| Key | Default | Meaning |
|-----|---------|---------|
| `hotkey_mods` | `["cmd","shift"]` | Modifier keys for the dictation hotkey |
| `hotkey_key` | `"9"` | Main key for the hotkey |
| `whisper_model` | `"small"` | `tiny`/`base`/`small`/`medium`/`large-v3`/`large-v3-turbo` |
| `language` | `"ru"` | Language code, or `"auto"` |
| `max_duration_seconds` | `300` | Auto-stop after this long |
| `sample_rate` | `16000` | Mic capture sample rate |
| `auto_paste` | `true` | Simulate `⌘V` after transcribing |
| `sounds_enabled` | `true` | Play start/stop/success sounds |
| `anim_fps` | `8` | Menu-bar animation frame rate |
| `live_mode` | `true` | Type text as you speak |
| `live_poll_seconds` | `1.5` | Live re-transcribe interval |
| `live_stability_runs` | `3` | Consecutive agreeing polls before committing |
| `model_idle_unload_minutes` | `15` | Free model RAM after N idle minutes (0 = never) |

Logs: `~/.voicebot/logs/voicebot.log` (also openable from Settings → View Logs).

## Development

```bash
cd voicebot
python3 -m pytest ../tests -q      # unit tests (Pillow + numpy)
python3 main.py --demo-overlay     # preview the glow overlay
```

## Credits

Built by **Bogdan Izumtsev** with the help of the **M1zz1 agent office**.

## License

[MIT](LICENSE) © 2026 Bogdan Izumtsev
