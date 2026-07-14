#!/usr/bin/env python3
"""
Offline regression harness for the live transcription pipeline.

Plays a recorded audio file at wall-clock speed through MockRecorder while a
real LiveTranscriber polls, transcribes, and "types" into a CapturePaster.
Reports what the live loop committed character-by-character vs. the full
non-streaming transcription of the same audio, so we can spot lost words,
mid-word commits, glued tokens, and hallucination leakage.

Usage:
    python3 test_live.py path/to/voice.ogg
    python3 test_live.py tests/audio/voice_01.ogg --poll 1.5 --stability 2

Requires soundfile in the venv (already installed) and the project's existing
WhisperEngine / LiveTranscriber / TextPaster modules. No GUI bringup — pure
engine + live loop.
"""

import argparse
import difflib
import os
import sys
import threading
import time

import numpy as np
import soundfile as sf

# Make the project modules importable when running from /tests/
_HERE = os.path.dirname(os.path.abspath(__file__))
_VOICEBOT = os.path.join(_HERE, "..", "voicebot")
sys.path.insert(0, _VOICEBOT)

from live_transcribe import LiveTranscriber  # noqa: E402
from whisper_engine import WhisperEngine  # noqa: E402


SAMPLE_RATE = 16000


def load_audio_int16(path):
    """Read any libsndfile-supported file → mono int16 @ 16 kHz."""
    audio, sr = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != SAMPLE_RATE:
        from scipy.signal import resample_poly
        audio = resample_poly(audio, SAMPLE_RATE, sr)
    audio = np.clip(audio, -1.0, 1.0)
    return (audio * 32767.0).astype(np.int16)


class MockRecorder:
    """Replay int16 audio so `snapshot()` returns the chunk that *would* have
    been captured by now if a real mic stream were running at this point in
    wall-clock time."""

    def __init__(self, audio_int16, sample_rate=SAMPLE_RATE):
        self._audio = audio_int16
        self.sample_rate = sample_rate
        self._start_time = None
        self.is_recording = False

    def start(self):
        self.is_recording = True
        self._start_time = time.monotonic()
        return True

    def stop(self):
        self.is_recording = False
        return self._audio  # always return the full buffer

    def snapshot(self):
        if not self.is_recording or self._start_time is None:
            return None
        elapsed = time.monotonic() - self._start_time
        end = min(int(elapsed * self.sample_rate), len(self._audio))
        if end <= 0:
            return None
        return self._audio[:end]


class CapturePaster:
    """Mimics TextPaster but writes into an in-memory cursor."""

    def __init__(self):
        self._buf = []  # list of chars for cheap backspace
        self.events = []
        self.last_text = ""
        self._lock = threading.Lock()

    @property
    def output(self):
        return "".join(self._buf)

    def type_text(self, text, char_delay=0.0):
        if not text:
            return
        with self._lock:
            self._buf.extend(text)
            self.events.append(("type", text))

    def backspace(self, n, char_delay=0.0):
        if n <= 0:
            return
        with self._lock:
            if n >= len(self._buf):
                self.events.append(("backspace", len(self._buf)))
                self._buf.clear()
            else:
                self.events.append(("backspace", n))
                del self._buf[-n:]

    def paste(self, text, **kwargs):
        # Live path uses type_text; this is a fallback in case something
        # still routes through the old API.
        self.type_text(text)


def _color(c, txt):
    codes = {"r": 31, "g": 32, "y": 33, "c": 36, "m": 35, "d": 2}
    return f"\033[{codes.get(c, 0)}m{txt}\033[0m"


def _print_diff(live, full):
    """Inline char-level diff between live and full transcription."""
    sm = difflib.SequenceMatcher(a=live, b=full, autojunk=False)
    parts = []
    for op, a0, a1, b0, b1 in sm.get_opcodes():
        if op == "equal":
            parts.append(live[a0:a1])
        elif op == "delete":
            parts.append(_color("r", f"[-{live[a0:a1]!r}]"))
        elif op == "insert":
            parts.append(_color("g", f"[+{full[b0:b1]!r}]"))
        elif op == "replace":
            parts.append(_color("y", f"[{live[a0:a1]!r}→{full[b0:b1]!r}]"))
    print("".join(parts))


def run(path, poll_seconds=1.5, stability_runs=2, model="large-v3-turbo",
        language="ru"):
    print(_color("c", f"[load] {path}"))
    audio = load_audio_int16(path)
    duration = len(audio) / SAMPLE_RATE
    print(_color("c", f"[load] {duration:.1f}s @ {SAMPLE_RATE} Hz, "
                       f"{len(audio)} samples"))

    print(_color("c", f"[engine] loading {model} ({language})..."))
    engine = WhisperEngine(model_name=model, language=language)
    engine.ensure_loaded(blocking=True)

    recorder = MockRecorder(audio)
    paster = CapturePaster()
    live = LiveTranscriber(
        recorder=recorder, engine=engine, paster=paster,
        sample_rate=SAMPLE_RATE,
        poll_seconds=poll_seconds, stability_runs=stability_runs,
    )

    print(_color("c", f"[live] poll={poll_seconds}s stability={stability_runs}"))
    t0 = time.monotonic()
    recorder.start()
    live.start()

    # Wall-clock playback. Add a small grace so the last poll catches the tail.
    time.sleep(duration + 0.2)

    live.stop()
    full_audio = recorder.stop()
    live.finalize(full_audio)

    elapsed = time.monotonic() - t0
    print(_color("c", f"[live] done in {elapsed:.1f}s, "
                       f"{len(paster.events)} paste events"))

    # Full-context reference transcription
    print(_color("c", "[ref] full-buffer transcribe (non-streaming)..."))
    full = engine.transcribe(audio, SAMPLE_RATE, inline=True) or ""

    live_out = paster.output

    print()
    print(_color("m", "── EVENTS ──"))
    for ev in paster.events:
        kind, payload = ev
        if kind == "type":
            print(f"  + {payload!r}")
        elif kind == "backspace":
            print(f"  ⌫ {payload}")
        else:
            print(f"  ? {ev}")

    print()
    print(_color("m", "── LIVE  ──"))
    print(live_out)
    print()
    print(_color("m", "── FULL  ──"))
    print(full)
    print()
    print(_color("m", "── DIFF (red = only in live, green = only in full) ──"))
    _print_diff(live_out, full)

    if live_out == full:
        print()
        print(_color("g", "✓ PERFECT MATCH"))
        return 0
    # Cheap similarity metric
    sm = difflib.SequenceMatcher(None, live_out, full)
    ratio = sm.ratio()
    print()
    print(_color("y", f"≠ similarity {ratio:.3f} "
                      f"(live={len(live_out)}ch, full={len(full)}ch)"))
    return 1


def main():
    p = argparse.ArgumentParser()
    p.add_argument("path", help="audio file (wav, ogg, mp3, flac, m4a…)")
    p.add_argument("--poll", type=float, default=1.5,
                   help="live_poll_seconds (default 1.5)")
    p.add_argument("--stability", type=int, default=2,
                   help="live_stability_runs (default 2)")
    p.add_argument("--model", default="large-v3-turbo")
    p.add_argument("--language", default="ru")
    args = p.parse_args()
    raise SystemExit(run(
        args.path, args.poll, args.stability, args.model, args.language,
    ))


if __name__ == "__main__":
    main()
