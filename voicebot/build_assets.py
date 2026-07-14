#!/usr/bin/env python3
"""
Asset generation for VoiceBot's menu-bar and app icons.

The mascot is Bogdan's M1zz1 logo, ported from
``agentic-os/frontend/src/mascot.tsx`` (the ``MASCOT_PIXELS`` 23x21 grid) into
plain data here and rendered with Pillow — no React involved. The palette is a
BRIGHTENED variant approved for the icon (the values below intentionally differ
from the darker ones in mascot.tsx).

Icon composition ("variant B"): a dark circle background with a thin purple
ring, the mascot centred inside at ~65% of the circle diameter.

``compose()`` and ``render_mascot()`` are the shared primitives — animations.py
imports them so every menu-bar state (recording / processing / success / error)
draws the same mascot-in-a-ring and only varies the ring effect on top.

Run directly to regenerate the on-disk icons:
    python3 build_assets.py
produces:
    assets/icon_idle.png   — 44px (@2x, macOS renders at 22pt) menu-bar icon
    assets/VoiceBot.icns   — full app iconset (16-1024px)
"""

import os
import shutil
import subprocess
import tempfile

from PIL import Image, ImageDraw

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

# ── Mascot pixel grid (ported from mascot.tsx MASCOT_PIXELS, 2026-07-14) ──────
MASCOT_PIXELS = [
    ".............o.........",
    ".X..........X.........X",
    ".XXX.......XX..o....XXX",
    "..XXXd..XXXs..XX..XXXX.",
    "..XXXXd.ssXsXdX..sXXXX.",
    "...XXXXXXXXXXXXdssXXX..",
    "...XssXXXXXXXXXXssss...",
    "....ssXXXXXXXXXXXss....",
    "....ssXXXXXXXXXXXsss...",
    "oX.sssooXdXXXXXXooss..X",
    ".d.sssrroXnXdXXnrrsss.s",
    "..ssssrRooXdnXorRRsss..",
    "..ssssRRoroddoRoRRsss..",
    "...ssssRRRdssdRRRssss..",
    "...sssoxxxxxxxxxxosss..",
    "...sssWWoxxxxxxoWWsss..",
    "....sssWWWWWWWWWWsss...",
    ".....sssWWWWWWWWsss....",
    ".....ssssxWWWoossss....",
    ".......sssddsssss......",
    ".........sssdds........",
]

# BRIGHTENED palette (approved for the icon — NOT the mascot.tsx values).
_HEX = {
    "X": "#9B7FC4",  # body
    "x": "#8568A8",  # body dim / grin line
    "s": "#6B548C",  # shade
    "d": "#544678",  # deep shade
    "n": "#3A3558",  # dark navy
    "o": "#0C0A12",  # outline
    "W": "#EFE6F7",  # teeth
    "R": "#FF3D5E",  # eye red (bright)
    "r": "#A32848",  # eye dark red
}


def _rgba(hex_str, alpha=255):
    h = hex_str.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


MASCOT_COLORS = {ch: _rgba(hx) for ch, hx in _HEX.items()}

# Composition constants.
BG_CIRCLE = _rgba("#14101E")     # dark circle background
RING = _rgba("#8A6CB0")          # purple ring / border
MENU_ICON_SIZE = 44              # px, @2x retina (macOS shows as 22pt)
MASCOT_FRAC = 0.65               # mascot size as a fraction of circle diameter
_RING_WIDTH_RATIO = 1.5 / 22.0   # ~1.5px at a 22px scale

_COLS = len(MASCOT_PIXELS[0])
_ROWS = len(MASCOT_PIXELS)


def ring_width_for(size):
    """Ring thickness (px) proportional to the icon size (~1.5px at 22pt)."""
    return max(1.0, size * _RING_WIDTH_RATIO)


# ── Rendering primitives ──────────────────────────────────────────────────────

def render_mascot(box_px, eyes_dark=False):
    """Render the mascot grid into a transparent image fitting a ``box_px`` box.

    Pixel-art is scaled with NEAREST so the blocks stay crisp. When
    ``eyes_dark`` is True the red eye pixels (R/r) are swapped for the dark
    outline colour — used for the idle blink frames.
    """
    grid = Image.new("RGBA", (_COLS, _ROWS), (0, 0, 0, 0))
    px = grid.load()
    dark = MASCOT_COLORS["o"]
    for y, row in enumerate(MASCOT_PIXELS):
        for x, ch in enumerate(row):
            color = MASCOT_COLORS.get(ch)
            if color is None:
                continue
            if eyes_dark and ch in ("R", "r"):
                color = dark
            px[x, y] = color

    # Width is the limiting dimension (cols > rows); keep aspect ratio.
    target_w = max(1, int(round(box_px)))
    target_h = max(1, int(round(box_px * _ROWS / _COLS)))
    return grid.resize((target_w, target_h), Image.NEAREST)


def circle_base(size, bg_rgba=BG_CIRCLE, ring_rgba=RING, ring_width=None):
    """Anti-aliased dark circle with a purple ring, supersampled 4x."""
    if ring_width is None:
        ring_width = ring_width_for(size)
    scale = 4
    big = size * scale
    base = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
    d.ellipse((0, 0, big - 1, big - 1), fill=bg_rgba)
    rw = max(1, int(round(ring_width * scale)))
    off = rw / 2.0
    d.ellipse((off, off, big - 1 - off, big - 1 - off),
              outline=ring_rgba, width=rw)
    return base.resize((size, size), Image.LANCZOS)


def compose(size, *, ring_rgba=RING, ring_width=None, bg_rgba=BG_CIRCLE,
            mascot_frac=MASCOT_FRAC, eyes_dark=False):
    """Full icon: circle + ring with the mascot centred inside.

    Returns an RGBA ``Image`` of ``size`` x ``size``.
    """
    base = circle_base(size, bg_rgba=bg_rgba, ring_rgba=ring_rgba,
                       ring_width=ring_width)
    mascot = render_mascot(size * mascot_frac, eyes_dark=eyes_dark)
    ox = (size - mascot.width) // 2
    oy = (size - mascot.height) // 2
    base.alpha_composite(mascot, (ox, oy))
    return base


# ── On-disk icon builders ─────────────────────────────────────────────────────

def build_idle_icon():
    """Regenerate assets/icon_idle.png (44px @2x menu-bar icon)."""
    os.makedirs(ASSETS, exist_ok=True)
    out = os.path.join(ASSETS, "icon_idle.png")
    compose(MENU_ICON_SIZE).save(out, "PNG")
    print(f"  icon_idle.png  ({MENU_ICON_SIZE}x{MENU_ICON_SIZE})  -> {out}")
    return out


# (name, pixel size) pairs required for a macOS .icns iconset.
_ICNS_SIZES = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]


def build_icns():
    """Regenerate assets/VoiceBot.icns from the same composition via iconutil."""
    os.makedirs(ASSETS, exist_ok=True)
    out = os.path.join(ASSETS, "VoiceBot.icns")
    tmp = tempfile.mkdtemp(prefix="voicebot_iconset_")
    iconset = os.path.join(tmp, "VoiceBot.iconset")
    os.makedirs(iconset)
    try:
        for name, px in _ICNS_SIZES:
            # Slightly larger mascot for the app icon so it reads at a glance.
            compose(px, mascot_frac=0.70).save(
                os.path.join(iconset, name), "PNG"
            )
        subprocess.run(
            ["iconutil", "-c", "icns", "-o", out, iconset],
            check=True, capture_output=True,
        )
        print(f"  VoiceBot.icns  (16-1024px)  -> {out}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


if __name__ == "__main__":
    print("Rendering VoiceBot mascot assets...")
    build_idle_icon()
    build_icns()
    print("Done. Menu-bar animation frames regenerate on next app launch "
          "(animations.py cache).")
