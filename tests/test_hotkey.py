"""
Hotkey QA — verifies the config → Carbon-registration resolution that
app.py._install_hotkey performs, plus the display round-trip.

No hotkey is actually registered (that needs a running app + Accessibility);
these tests exercise the pure mapping layer: VK_MAP, cocoa_mods_to_carbon,
format_shortcut, and the settings reverse-map round-trip.
"""

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_VOICEBOT = os.path.join(_HERE, "..", "voicebot")
sys.path.insert(0, _VOICEBOT)

import hotkey as hk  # noqa: E402


# Reverse map built exactly like settings_window does, so we can test the
# capture → config keyname → registration round-trip without importing AppKit.
_NAME_PRIORITY = ("esc", "return", "tab", "space", "delete", "up", "down",
                  "left", "right")
_VK_TO_KEYNAME = {}
for _name, _code in hk.VK_MAP.items():
    if _code in _VK_TO_KEYNAME:
        existing = _VK_TO_KEYNAME[_code]
        if len(_name) < len(existing) or _name in _NAME_PRIORITY:
            _VK_TO_KEYNAME[_code] = _name
    else:
        _VK_TO_KEYNAME[_code] = _name


@pytest.mark.parametrize("mods,key,exp_mask,exp_vk", [
    (["cmd", "shift"], "9", hk.cmdKey | hk.shiftKey, 25),          # default
    (["ctrl", "alt"], "space", hk.controlKey | hk.optionKey, 49),  # ctrl+alt+space
    (["cmd"], "f5", hk.cmdKey, 96),                                 # function key
    (["ctrl"], "a", hk.controlKey, 0),                             # single modifier
    (["cmd", "shift"], "up", hk.cmdKey | hk.shiftKey, 126),        # arrow key
])
def test_config_resolves_to_carbon(mods, key, exp_mask, exp_vk):
    vk = hk.VK_MAP.get(key)
    assert vk == exp_vk, f"{key!r} should map to vk {exp_vk}"
    assert hk.cocoa_mods_to_carbon(mods) == exp_mask


def test_default_shortcut_display():
    # Apple HIG modifier order is ⌃⌥⇧⌘ (command last).
    assert hk.format_shortcut(["cmd", "shift"], 25) == "⇧⌘9"


def test_ctrl_alt_space_display():
    assert hk.format_shortcut(["ctrl", "alt"], 49) == "⌃⌥Space"


def test_f5_display():
    assert hk.format_shortcut(["cmd"], 96) == "⌘F5"


@pytest.mark.parametrize("key", ["9", "space", "f5", "a", "up", "escape", "-"])
def test_captured_vk_round_trips_through_config(key):
    """Capture a key → resolve vk → derive config keyname → resolve vk again.
    The final vk must equal the original (what _install_hotkey relies on)."""
    vk = hk.VK_MAP[key]
    keyname = _VK_TO_KEYNAME[vk]
    assert hk.VK_MAP[keyname] == vk


def test_all_vk_map_entries_are_ints():
    for name, code in hk.VK_MAP.items():
        assert isinstance(code, int), f"{name!r} → {code!r} is not an int"


def test_single_modifier_masks_nonzero():
    for m in (["cmd"], ["shift"], ["ctrl"], ["alt"]):
        assert hk.cocoa_mods_to_carbon(m) > 0
