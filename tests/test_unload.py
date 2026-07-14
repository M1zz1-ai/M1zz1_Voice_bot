"""
Unit tests for WhisperEngine idle auto-unload logic.

No MLX: the single MLX worker is replaced with a fake that records submits, so
the gating and timer-arming logic can be checked deterministically.
"""

import os
import sys
import threading

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_VOICEBOT = os.path.join(_HERE, "..", "voicebot")
sys.path.insert(0, _VOICEBOT)

import whisper_engine as we  # noqa: E402


class _FakeFuture:
    def __init__(self, value=None):
        self._value = value

    def result(self, timeout=None):
        return self._value


class _FakeMLX:
    """Stand-in for _MLXWorker that records submitted callables."""

    def __init__(self):
        self.calls = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append(fn)
        return _FakeFuture()

    def shutdown(self):
        pass


@pytest.fixture
def engine(monkeypatch):
    # Force a clean, deterministic state regardless of what's on disk.
    monkeypatch.setattr(we, "_model_already_downloaded", lambda repo: False)
    eng = we.WhisperEngine(idle_unload_minutes=5)
    eng._mlx = _FakeMLX()
    return eng


def test_disabled_when_zero(monkeypatch):
    monkeypatch.setattr(we, "_model_already_downloaded", lambda repo: False)
    eng = we.WhisperEngine(idle_unload_minutes=0)
    eng._mlx = _FakeMLX()
    eng.mark_active()
    assert eng._idle_timer is None
    assert eng._idle_unload_seconds == 0


def test_mark_active_arms_timer(engine):
    engine.mark_active()
    assert isinstance(engine._idle_timer, threading.Timer)
    engine._cancel_idle_timer()
    assert engine._idle_timer is None


def test_set_idle_unload_minutes_reconfigures(engine):
    engine.set_idle_unload_minutes(0)
    assert engine._idle_unload_seconds == 0
    assert engine._idle_timer is None
    engine.set_idle_unload_minutes(2)
    assert engine._idle_unload_seconds == 120
    assert isinstance(engine._idle_timer, threading.Timer)
    engine._cancel_idle_timer()


def test_idle_unload_frees_when_warmed(engine):
    engine._state = we.STATE_READY
    engine._warmed = True
    engine._idle_unload()
    assert engine._warmed is False
    assert engine._mlx.calls == [we._clear_mlx_caches]


def test_idle_unload_noop_when_not_warmed(engine):
    engine._state = we.STATE_READY
    engine._warmed = False
    engine._warming = False
    engine._idle_unload()
    assert engine._mlx.calls == []


def test_idle_unload_noop_when_not_ready(engine):
    engine._state = we.STATE_DOWNLOADING
    engine._warmed = True
    engine._idle_unload()
    assert engine._mlx.calls == []
    # weights flag untouched — nothing was freed
    assert engine._warmed is True


def test_inline_transcribe_skips_when_busy(engine):
    """A live-mode (inline) poll must skip — not queue — when inference is
    already running, so slow inference can't back up the stop→finalize pass."""
    import numpy as np
    engine._state = we.STATE_READY
    engine._ready_event.set()
    called = []
    engine._do_transcribe = lambda *a, **k: called.append(1) or "x"
    engine._infer_lock.acquire()  # simulate an in-flight inference
    try:
        out = engine.transcribe(
            np.zeros(16000, dtype=np.int16), 16000, inline=True
        )
    finally:
        engine._infer_lock.release()
    assert out is None
    assert called == [], "inline poll must not run inference while busy"
    engine._cancel_idle_timer()


def test_inline_transcribe_runs_and_releases_when_free(engine):
    import numpy as np
    engine._state = we.STATE_READY
    engine._ready_event.set()
    engine._do_transcribe = lambda *a, **k: "hello"
    out = engine.transcribe(
        np.zeros(16000, dtype=np.int16), 16000, inline=True
    )
    assert out == "hello"
    # lock must be released after the call
    assert engine._infer_lock.acquire(blocking=False)
    engine._infer_lock.release()
    engine._cancel_idle_timer()
