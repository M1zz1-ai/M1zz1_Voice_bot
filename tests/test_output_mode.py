"""
Unit tests for the context-aware output mode.

The AX focus probe can't run headless, so it's isolated behind FocusProber and
these tests drive OutputGate (and LiveTranscriber's gating) with a fake prober
that returns scripted focus states.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "voicebot"))

from focus import (  # noqa: E402
    EDITABLE,
    SECURE,
    SILENT,
    OutputGate,
    _KAX_ERROR_CANNOT_COMPLETE,
    _KAX_ERROR_NO_VALUE,
    _run_probe,
)
from live_transcribe import LiveTranscriber  # noqa: E402

SR = 16000


class FakeProber:
    """Returns scripted states; the last value repeats once exhausted."""

    def __init__(self, states):
        self._states = list(states)

    def _next(self):
        if len(self._states) > 1:
            return self._states.pop(0)
        return self._states[0] if self._states else SILENT

    def probe_info(self):
        return {"state": self._next(), "role": "FakeRole", "subrole": "",
                "settable": False, "err": None, "app": "test"}

    def probe(self):
        return self.probe_info()["state"]


class FakePaster:
    def __init__(self):
        self._buf = []
        self.events = []

    @property
    def output(self):
        return "".join(self._buf)

    def type_text(self, text, char_delay=0.0):
        if text:
            self._buf.extend(text)
            self.events.append(("type", text))

    def backspace(self, n, char_delay=0.0):
        n = min(n, len(self._buf))
        if n > 0:
            del self._buf[-n:]
            self.events.append(("backspace", n))


# ── OutputGate state machine ────────────────────────────────────────────────

def test_editable_allows_typing():
    g = OutputGate(FakeProber([EDITABLE]), smart_typing=True)
    assert g.allow_typing() is True
    assert g.is_silent is False


def test_secure_never_types_and_is_sticky_even_in_legacy():
    # Password field must never be typed into, regardless of smart_typing.
    g = OutputGate(FakeProber([SECURE, EDITABLE]), smart_typing=False)
    assert g.allow_typing() is False
    assert g.is_silent is True
    assert g.allow_typing() is False  # sticky


def test_smart_silent_is_sticky_when_focus_returns():
    g = OutputGate(FakeProber([SILENT, EDITABLE, EDITABLE]), smart_typing=True)
    assert g.allow_typing() is False
    assert g.is_silent is True
    # Focus came back to an editable field, but the recording stays silent.
    assert g.allow_typing() is False


def test_legacy_types_into_non_editable():
    g = OutputGate(FakeProber([SILENT]), smart_typing=False)
    assert g.allow_typing() is True
    assert g.is_silent is False


def test_reset_clears_silent_and_updates_smart():
    g = OutputGate(FakeProber([SILENT]), smart_typing=True)
    g.allow_typing()
    assert g.is_silent is True
    g.reset(smart_typing=False)
    assert g.is_silent is False


# ── LiveTranscriber gating ──────────────────────────────────────────────────

def _live(gate, paster):
    return LiveTranscriber(
        recorder=None, engine=None, paster=paster, sample_rate=SR,
        poll_seconds=1.5, stability_runs=2, gate=gate,
    )


def test_live_silent_collects_without_typing():
    paster = FakePaster()
    live = _live(OutputGate(FakeProber([SILENT]), smart_typing=True), paster)
    live._ingest("привет мир ", total=3 * SR)
    live._ingest("привет мир как", total=4 * SR)
    # Text is committed internally (recoverable via clipboard) but not typed.
    assert live.committed == "привет мир "
    assert paster.output == ""
    assert live.output_silent is True


def test_live_editable_types():
    paster = FakePaster()
    live = _live(OutputGate(FakeProber([EDITABLE]), smart_typing=True), paster)
    live._ingest("привет мир ", total=3 * SR)
    live._ingest("привет мир как", total=4 * SR)
    assert paster.output == "привет мир "
    assert live.output_silent is False


# ── Electron activation-retry state machine (_run_probe + fake AX ops) ───────

class FakeOps:
    def __init__(self, focused_seq, bundle="com.test.electron", pid=123,
                 classify_state=EDITABLE, ready=True, trusted=True):
        self._seq = list(focused_seq)  # (err, token) per focused() call
        self._bundle = bundle
        self._pid = pid
        self._classify_state = classify_state
        self._ready = ready
        self._trusted = trusted
        self.activations = []
        self.slept = 0.0
        self.released = []

    def ready(self):
        return self._ready

    def is_trusted(self):
        return self._trusted

    def frontmost(self):
        return self._pid, self._bundle

    def focused(self, pid):
        return self._seq.pop(0) if self._seq else (_KAX_ERROR_NO_VALUE, None)

    def activate(self, pid, attr):
        self.activations.append(attr)

    def classify(self, token):
        return {"state": self._classify_state, "role": "AXTextField",
                "subrole": "", "settable": True}

    def release(self, token):
        self.released.append(token)

    def sleep(self, seconds):
        self.slept += seconds


def test_probe_editable_without_activation():
    ops = FakeOps([(0, "TOK")])
    info = _run_probe(ops, set())
    assert info["state"] == EDITABLE
    assert ops.activations == []
    assert ops.released == ["TOK"]


def test_probe_activates_on_novalue_then_classifies():
    ops = FakeOps([(_KAX_ERROR_NO_VALUE, None), (0, "TOK")])
    activated = set()
    info = _run_probe(ops, activated)
    assert info["state"] == EDITABLE
    assert ops.activations == ["AXManualAccessibility"]
    assert "com.test.electron" in activated


def test_probe_falls_back_to_enhanced_ui():
    ops = FakeOps([(_KAX_ERROR_NO_VALUE, None),
                   (_KAX_ERROR_NO_VALUE, None), (0, "TOK")])
    info = _run_probe(ops, set())
    assert info["state"] == EDITABLE
    assert ops.activations == ["AXManualAccessibility", "AXEnhancedUserInterface"]


def test_probe_unprobeable_after_activation_fails():
    ops = FakeOps([(_KAX_ERROR_NO_VALUE, None)] * 3)
    info = _run_probe(ops, set())
    assert info["state"] == SILENT
    assert info["unprobeable"] is True
    assert ops.activations == ["AXManualAccessibility", "AXEnhancedUserInterface"]


def test_activation_cached_per_bundle():
    ops = FakeOps([(_KAX_ERROR_NO_VALUE, None)])
    info = _run_probe(ops, {"com.test.electron"})  # already activated
    assert info["unprobeable"] is True
    assert ops.activations == []  # not re-activated


def test_cannot_complete_is_unprobeable_no_activation():
    ops = FakeOps([(_KAX_ERROR_CANNOT_COMPLETE, None)])
    info = _run_probe(ops, set())
    assert info["state"] == SILENT
    assert info["unprobeable"] is True
    assert ops.activations == []  # only NoValue triggers activation


def test_live_no_gate_types_legacy():
    # No gate injected → always type (back-compat with existing tests).
    paster = FakePaster()
    live = _live(None, paster)
    live._ingest("привет мир ", total=3 * SR)
    live._ingest("привет мир как", total=4 * SR)
    assert paster.output == "привет мир "
    assert live.output_silent is False
