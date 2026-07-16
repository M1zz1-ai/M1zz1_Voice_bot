"""Unit tests for the Restart relaunch command construction."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "voicebot"))

from relaunch import BUNDLE_ID, restart_command  # noqa: E402


def test_restart_command_is_detached_delayed_open():
    # Must delay + `open -b` so the relaunch fires AFTER the old process exits
    # (open on a live instance would only focus it).
    assert restart_command() == ["sh", "-c", f"sleep 1; open -b {BUNDLE_ID}"]
    assert BUNDLE_ID == "com.mizz.voicebot"


def test_restart_command_custom_bundle():
    assert restart_command("com.example.app") == [
        "sh", "-c", "sleep 1; open -b com.example.app",
    ]
