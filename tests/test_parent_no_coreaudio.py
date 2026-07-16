"""
Proof that the PARENT import graph never links PortAudio/CoreAudio.

Importing the full parent module graph (main → app → everything) must not pull
in sounddevice — capture lives only in the recorder's child process, where
sounddevice is imported lazily inside the worker. Requires the bundle venv
(AppKit/rumps), so it runs there, not in the lean test venv.
"""

import importlib
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "voicebot"))

# Parent-side modules loaded before/at app startup (no capture child).
_PARENT_MODULES = [
    "config", "logger_setup", "permissions", "relaunch", "focus",
    "paster", "sounds", "animations", "hotkey", "overlay",
    "transcriber", "whisper_engine", "recorder", "app", "main",
]


def test_parent_graph_has_no_sounddevice():
    for name in _PARENT_MODULES:
        importlib.import_module(name)
    assert "sounddevice" not in sys.modules, (
        "PortAudio linked in the parent — a module imports sounddevice at "
        "module scope; it must be lazy (child-only)."
    )
    assert "_sounddevice" not in sys.modules
