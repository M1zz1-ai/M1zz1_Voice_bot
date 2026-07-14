"""
Unit tests for the pure asset-rendering code (build_assets + animations).

No GUI / MLX — just Pillow. Verifies the mascot composition and the menu-bar
animation frames render at the expected sizes and vary frame-to-frame.
"""

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_VOICEBOT = os.path.join(_HERE, "..", "voicebot")
sys.path.insert(0, _VOICEBOT)

import build_assets  # noqa: E402
import animations  # noqa: E402


def test_compose_size_and_mode():
    img = build_assets.compose(44)
    assert img.size == (44, 44)
    assert img.mode == "RGBA"


def test_render_mascot_fits_box_and_keeps_aspect():
    m = build_assets.render_mascot(28)
    assert m.width == 28
    # rows (21) < cols (23) → height is the smaller dimension
    assert m.height < m.width
    assert m.mode == "RGBA"


def test_idle_icon_has_bright_red_eye():
    """A red eye pixel (R channel of #FF3D5E) must survive into the icon."""
    img = build_assets.compose(88)  # larger → eyes span >1px, easy to find
    px = img.load()
    found_red = any(
        px[x, y][0] > 180 and px[x, y][1] < 110 and px[x, y][3] > 200
        for x in range(img.width) for y in range(img.height)
    )
    assert found_red, "expected a bright-red eye pixel in the composed icon"


def test_eyes_dark_removes_bright_red():
    img = build_assets.compose(88, eyes_dark=True)
    px = img.load()
    bright_red = any(
        px[x, y][0] > 180 and px[x, y][1] < 110 and px[x, y][3] > 200
        for x in range(img.width) for y in range(img.height)
    )
    assert not bright_red, "eyes_dark should replace red eyes with dark pixels"


@pytest.fixture
def gen(tmp_path, monkeypatch):
    monkeypatch.setattr(animations, "CACHE_DIR", str(tmp_path / "frames"))
    return animations.AnimationGenerator()


@pytest.mark.parametrize("anim_type,expected", [
    ("recording", 16),
    ("processing", 24),
    ("success", 8),
    ("error", 8),
])
def test_frame_counts_and_sizes(gen, anim_type, expected):
    from PIL import Image
    frames = gen.get_frames(anim_type)
    assert len(frames) == expected
    for path in frames:
        assert path.endswith(".png")
        with Image.open(path) as im:
            assert im.size == (44, 44)


def test_recording_frames_vary(gen):
    frames = gen.get_frames("recording")
    with open(frames[0], "rb") as f:
        first = f.read()
    # Quarter point is the pulse peak (t=0 and t=0.5 share the same phase).
    with open(frames[len(frames) // 4], "rb") as f:
        peak = f.read()
    assert first != peak, "recording pulse should differ across the loop"


def test_render_idle_returns_full_icon(gen):
    img = gen.render_idle()
    assert img.size == (44, 44)
