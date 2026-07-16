"""
Overlay sleep/wake panel-drop + telemetry tests.

Imports overlay (AppKit), so it runs under the build venv. No NSApplication /
run loop is needed: the not-visible display-change path and the hide-telemetry
path are pure Python over a fake panel/view.
"""

import logging
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "voicebot"))

from overlay import Overlay  # noqa: E402


class FakePanel:
    def __init__(self):
        self.ordered_out = 0

    def orderOut_(self, _):
        self.ordered_out += 1


class FakeView:
    def __init__(self, tick_count=0, state="recording"):
        self._tick_count = tick_count
        self._state = state


def test_display_change_drops_panel_when_hidden():
    ov = Overlay()
    panel = FakePanel()
    ov._panel = panel
    ov._view = FakeView()
    ov._visible = False

    ov._recreate_for_display_change()

    assert ov._panel is None
    assert ov._view is None
    assert panel.ordered_out == 1  # stale panel torn down


def test_drop_panel_tolerates_no_panel():
    ov = Overlay()
    ov._drop_panel()
    assert ov._panel is None
    assert ov._view is None


def test_hide_logs_overlay_session_telemetry(caplog):
    ov = Overlay()
    ov._visible = True
    ov._show_t = time.monotonic() - 1.0
    ov._state_changes = 2
    ov._view = FakeView(tick_count=20)
    ov._panel = FakePanel()

    with caplog.at_level(logging.INFO, logger="voicebot.overlay"):
        ov._hide_impl()

    assert not ov._visible
    msgs = [r.getMessage() for r in caplog.records]
    assert any("overlay session: 2 state changes" in m for m in msgs)


def test_hide_when_not_visible_logs_nothing(caplog):
    ov = Overlay()
    ov._visible = False
    with caplog.at_level(logging.INFO, logger="voicebot.overlay"):
        ov._hide_impl()
    assert not any("overlay session:" in r.getMessage() for r in caplog.records)
