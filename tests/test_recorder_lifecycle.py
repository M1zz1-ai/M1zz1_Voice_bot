"""
Unit tests for AudioRecorder's dead-instance flag (app-compat surface).

With out-of-process capture the parent never wedges, but the app still uses
`mark_dead`/`is_dead` when replacing a recorder. These pure-logic tests open no
subprocess and no audio stream.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_VOICEBOT = os.path.join(_HERE, "..", "voicebot")
sys.path.insert(0, _VOICEBOT)

from recorder import AudioRecorder  # noqa: E402


def test_mark_dead_sets_flag():
    r = AudioRecorder()
    assert r.is_dead is False
    r.mark_dead()
    assert r.is_dead is True


def test_start_on_dead_recorder_returns_false():
    r = AudioRecorder()
    r.mark_dead()
    # Returns fast without spawning a child or touching CoreAudio.
    assert r.start() is False
    assert r.is_recording is False
