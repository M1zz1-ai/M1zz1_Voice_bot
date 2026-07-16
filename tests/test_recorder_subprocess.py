"""
Subprocess-boundary tests for AudioRecorder (out-of-process capture).

Uses a fake capture worker (no real mic) to verify the parent/child protocol,
bounded start on a mic that won't open, and — the key regression — that
SIGKILLing the capture child mid-recording never loses the parent's frames and
the next recording starts clean.
"""

import os
import signal
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                       # for _fake_capture (spawn import)
sys.path.insert(0, os.path.join(_HERE, "..", "voicebot"))

from recorder import AudioRecorder  # noqa: E402
from _fake_capture import (  # noqa: E402
    fake_capture_worker,
    never_ready_worker,
)


def test_start_stop_roundtrip_collects_frames():
    r = AudioRecorder(sample_rate=16000, worker=fake_capture_worker)
    assert r.start() is True
    assert r.is_recording
    time.sleep(0.3)
    snap = r.snapshot()
    assert snap is not None and len(snap) > 0
    audio = r.stop()
    assert audio is not None and len(audio) > 0
    assert not r.is_recording


def test_start_times_out_when_child_never_ready():
    r = AudioRecorder(sample_rate=16000, worker=never_ready_worker)
    r._START_TIMEOUT = 1.0
    t0 = time.monotonic()
    ok = r.start()
    elapsed = time.monotonic() - t0
    assert ok is False
    assert elapsed < 5.0, "start() must be bounded, not hung"
    assert not r.is_recording


def test_sigkill_midrecording_keeps_frames_and_restarts_clean():
    # THE repro: hard-kill the capture child mid-recording; the parent keeps
    # every frame it already received and the next recording starts clean.
    r = AudioRecorder(sample_rate=16000, worker=fake_capture_worker)
    r._STOP_TIMEOUT = 0.5  # child is dead, don't wait the full window
    for i in range(10):
        assert r.start() is True, f"start failed on loop {i}"
        time.sleep(0.2)
        pid = r._child.pid
        assert r.snapshot() is not None, f"no frames before kill on loop {i}"
        os.kill(pid, signal.SIGKILL)          # hard-kill mid-recording
        audio = r.stop()                      # parent still holds the frames
        assert audio is not None and len(audio) > 0, f"frames lost on loop {i}"
        assert not r.is_recording
