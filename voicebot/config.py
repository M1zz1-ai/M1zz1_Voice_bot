"""
Конфигурация VoiceBot.
Все настройки → ~/.voicebot/config.json (нет секретов: локальный Whisper).
"""

import json
import os

# Single source of truth for the app version. build_app.sh injects this into
# the bundle's CFBundleShortVersionString / CFBundleVersion.
VERSION = "2.1.4"

CONFIG_DIR = os.path.expanduser("~/.voicebot")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
APP_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    # Hotkey — backward-compatible with the old checkbox-based config.
    # `hotkey_mods` is a list of Cocoa modifier names; `hotkey_key` is a
    # human-readable key name (single char or "f5"/"space"/etc).
    # On load, this is resolved against hotkey.VK_MAP.
    "hotkey_mods": ["cmd", "shift"],
    "hotkey_key": "9",

    # Transcription
    "whisper_model": "small",
    "language": "ru",  # "auto" allowed in UI

    # Free model weights from RAM after this many idle minutes (0 = never).
    # Files stay on disk; next dictation re-warms in ~3s. Eases RAM on 8 GB Macs.
    # 15 keeps the model warm across a normal dictation session so back-to-back
    # utterances don't each pay a cold reload; drop it if RAM pressure bites.
    "model_idle_unload_minutes": 15,

    # Recording
    "max_duration_seconds": 300,
    "sample_rate": 16000,

    # UX
    "auto_paste": True,
    "sounds_enabled": True,
    "anim_fps": 8,

    # Context-aware output (OPT-IN, default off). When enabled: before each
    # live commit, probe the focused UI element via Accessibility and only type
    # into a real editable field; non-editable / no focus / password field →
    # silent (collect internally, leave the full text on the clipboard for ⌘V),
    # sticky per recording. When off (default): always type (legacy), except
    # password fields are still never typed into. Either way, the full
    # transcription is always left on the clipboard at the end of a recording.
    "smart_typing": False,

    # Live (streaming) mode: while recording, transcribe the buffer every
    # `live_poll_seconds` and paste any text that's been stable for
    # `live_stability_runs` consecutive runs. Uses the same Whisper engine
    # via a sliding-window strategy — no separate model.
    "live_mode": True,
    "live_poll_seconds": 1.5,
    # Require three consecutive polls to agree on a prefix before committing.
    # 2 was too noisy: Whisper waffling between e.g. "доктор" and "октор" for
    # two polls in a row is enough to commit the wrong variant. 3 catches the
    # second-thought revision before pasting.
    "live_stability_runs": 3,
}


class Config:
    def __init__(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        os.makedirs(os.path.join(CONFIG_DIR, "logs"), exist_ok=True)
        os.makedirs(os.path.join(CONFIG_DIR, "cache", "frames"), exist_ok=True)

        self._data = dict(DEFAULTS)
        self._load_json()

    def _load_json(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    self._data.update(json.load(f))
            except (json.JSONDecodeError, OSError):
                pass

    def save(self):
        with open(CONFIG_FILE, "w") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value

    def update(self, data: dict):
        self._data.update(data)

    @property
    def config_dir(self):
        return CONFIG_DIR

    @property
    def config_file(self):
        return CONFIG_FILE
