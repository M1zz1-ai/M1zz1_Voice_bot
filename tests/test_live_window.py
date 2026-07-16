"""
Unit tests for the sliding-window logic in live_transcribe.LiveTranscriber.

LiveTranscriber is pure Python: we drive `_ingest` / `finalize` directly with
a fake paster (and, for finalize, a fake engine) instead of threads and a real
mlx-whisper model. Covers window advance, history reset on slide, and
tail-only finalize.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_VOICEBOT = os.path.join(_HERE, "..", "voicebot")
sys.path.insert(0, _VOICEBOT)

from live_transcribe import (  # noqa: E402
    LiveTranscriber,
    _suffix_prefix_overlap,
)


SR = 16000


class FakePaster:
    """In-memory cursor mirroring TextPaster's type_text / backspace."""

    def __init__(self):
        self._buf = []
        self.events = []

    @property
    def output(self):
        return "".join(self._buf)

    def type_text(self, text, char_delay=0.0):
        if not text:
            return
        self._buf.extend(text)
        self.events.append(("type", text))

    def backspace(self, n, char_delay=0.0):
        if n <= 0:
            return
        n = min(n, len(self._buf))
        self.events.append(("backspace", n))
        if n:
            del self._buf[-n:]


class FakeEngine:
    """Returns a preset text for the next transcribe() call."""

    def __init__(self, text=""):
        self.text = text
        self.calls = []

    def transcribe(self, audio, sample_rate, *, inline=False):
        self.calls.append(len(audio))
        return self.text


def _make(stability=2, engine=None):
    return LiveTranscriber(
        recorder=None,
        engine=engine or FakeEngine(),
        paster=FakePaster(),
        sample_rate=SR,
        poll_seconds=1.5,
        stability_runs=stability,
    )


# ── Basic commit (stability) ────────────────────────────────────────────────

def test_ingest_commits_stable_prefix():
    live = _make(stability=2)
    # Two agreeing runs → stable prefix committed (trimmed to boundary).
    live._ingest("привет мир ", total=3 * SR)
    live._ingest("привет мир как", total=4 * SR)
    assert live.committed == "привет мир "
    assert live._paster.output == "привет мир "


def test_ingest_needs_stability_runs_before_committing():
    live = _make(stability=2)
    live._ingest("привет ", total=2 * SR)
    # Only one run so far — nothing committed yet.
    assert live.committed == ""


# ── Window advance ──────────────────────────────────────────────────────────

def test_window_does_not_slide_under_receptive_field():
    live = _make()
    live._ingest("слово ", total=10 * SR)
    live._ingest("слово два ", total=12 * SR)
    assert live._window_start == 0


def test_window_slides_once_buffer_exceeds_window():
    live = _make()
    live._committed = "committed text "
    live._window_committed = "committed text "
    # Buffer at 40 s (> 28 s window) forces a slide.
    live._maybe_slide(total=40 * SR, window_text="committed text and more tail")
    assert live._window_start > 0
    # New window must fit within the receptive field.
    assert (40 * SR - live._window_start) <= live._window_samples


def test_slide_floor_bounds_window_even_with_no_commit():
    live = _make()
    # Nothing committed in this window but it's already 50 s long.
    live._maybe_slide(total=50 * SR, window_text="")
    remaining = 50 * SR - live._window_start
    assert remaining <= live._window_samples


# ── History reset on slide ──────────────────────────────────────────────────

def test_history_resets_when_window_moves():
    live = _make()
    live._history = ["a", "b", "c"]
    live._committed = "some committed words "
    live._window_committed = "some committed words "
    live._maybe_slide(total=45 * SR, window_text="some committed words tail here")
    assert live._history == []
    assert live._window_committed == ""
    # Global committed text is preserved across the slide.
    assert live.committed == "some committed words "


# ── Finalize: short recording uses full audio ───────────────────────────────

def test_finalize_short_reconciles_full_message():
    live = _make(engine=FakeEngine("привет мир полностью"))
    live._committed = "привет мир"
    live._window_committed = "привет мир"
    live.finalize(full_audio=[0] * (10 * SR))  # 10 s ≤ receptive field
    # Appended the missing suffix; did not retype the shared prefix.
    assert live.committed == "привет мир полностью"
    assert live._paster.output == " полностью"


# ── Finalize: long recording reconciles only the tail ───────────────────────

def test_finalize_long_touches_only_tail_window():
    engine = FakeEngine("CCC fixed")
    live = _make(engine=engine)
    # Simulate a long session: early text frozen, only "CCC" is the open tail.
    live._committed = "AAA BBB CCC"
    live._window_committed = "CCC"
    live._window_start = 30 * SR
    live.finalize(full_audio=[0] * (60 * SR))  # 60 s > receptive field

    # Frozen prefix untouched; whole message not retyped.
    assert live.committed == "AAA BBB CCC fixed"
    # Engine saw only the tail window (60s - 30s = 30s), not the full 60 s.
    assert engine.calls == [30 * SR]
    # Only the tail delta was typed; the frozen "AAA BBB " never re-typed.
    typed = "".join(t for kind, t in live._paster.events if kind == "type")
    assert typed == " fixed"


def test_finalize_noop_when_tail_matches():
    engine = FakeEngine("CCC")
    live = _make(engine=engine)
    live._committed = "AAA BBB CCC"
    live._window_committed = "CCC"
    live._window_start = 30 * SR
    live.finalize(full_audio=[0] * (60 * SR))
    assert live._paster.events == []
    assert live.committed == "AAA BBB CCC"


# ── DEFECT 2 regression: finalize must not duplicate the overlapping tail ────

def test_suffix_prefix_overlap_basic():
    assert _suffix_prefix_overlap("скоро будет запись", "запись пошла") == 6
    assert _suffix_prefix_overlap("abcdef", "defghi") == 3
    assert _suffix_prefix_overlap("abc", "abc") == 3
    assert _suffix_prefix_overlap("abc", "xyz") == 0
    assert _suffix_prefix_overlap("", "abc") == 0


def test_finalize_tail_does_not_duplicate_overlap():
    # Reproduces the -0/+297 bug: the tail-window transcription re-covers audio
    # that is already committed. Only the genuinely-new suffix must be typed.
    committed = "давайте начнём. Проверка окна прошла. Продолжаем работу"
    tail_text = "окна прошла. Продолжаем работу дальше"
    live = _make(engine=FakeEngine(tail_text))
    live._committed = committed
    live._window_committed = "Продолжаем работу"
    live._window_start = 30 * SR

    live.finalize(full_audio=[0] * (60 * SR))

    # Only the new suffix pasted — the overlapping chunk is NOT typed twice.
    assert live._paster.output == " дальше"
    assert "прошла" not in live._paster.output
    assert live.committed == committed + " дальше"


def test_finalize_tail_uses_global_committed_when_window_committed_reset():
    # Exact failure state from the log: a slide reset _window_committed to ""
    # but _committed still holds the tail. The old code compared against "" and
    # re-typed the whole 297-char window; the fix anchors on _committed.
    committed = "первая часть сообщения уже напечатана и запись почти готова"
    tail_text = "и запись почти готова закончить"
    live = _make(engine=FakeEngine(tail_text))
    live._committed = committed
    live._window_committed = ""          # reset by the last slide
    live._window_start = 30 * SR

    live.finalize(full_audio=[0] * (60 * SR))

    assert live._paster.output == " закончить"
    assert live.committed == committed + " закончить"
