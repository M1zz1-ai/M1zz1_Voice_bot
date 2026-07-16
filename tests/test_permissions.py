"""
Tests for the stream-free microphone permission probe.

The AVFoundation query can't run headless deterministically, so we test the
AVAuthorizationStatus mapping and the non-blocking check_microphone logic with
microphone_status monkeypatched. Also proves the parent import graph never
pulls in sounddevice/PortAudio.
"""

import importlib
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "voicebot"))

import permissions  # noqa: E402


def test_status_mapping():
    assert permissions._map_status(0) == "undetermined"
    assert permissions._map_status(1) == "denied"       # restricted
    assert permissions._map_status(2) == "denied"
    assert permissions._map_status(3) == "authorized"
    assert permissions._map_status(99) == "unknown"


def test_check_microphone_only_false_when_denied(monkeypatch):
    for status, expected in [("authorized", True), ("undetermined", True),
                             ("denied", False), ("unknown", True)]:
        monkeypatch.setattr(permissions, "microphone_status",
                            lambda s=status: s)
        assert permissions.check_microphone() is expected


def test_permissions_module_imports_no_sounddevice():
    # Importing the permission probe must not load PortAudio.
    importlib.import_module("permissions")
    assert "sounddevice" not in sys.modules
