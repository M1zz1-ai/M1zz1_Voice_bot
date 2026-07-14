"""
Procedural menu-bar animation frames.

Every state renders the M1zz1 mascot inside a dark circle with a purple ring
(the shared composition from build_assets.py); only the ring effect changes per
state:

  idle        — static icon (assets/icon_idle.png); blink frames are available
                but not looped so idle CPU stays at zero.
  recording   — ring pulses in brightness/thickness toward #B57BFF (~8 fps loop)
  processing  — a bright arc (#E0D4EC) sweeps around the ring (loop)
  success     — ring flashes green (#5DCAA5), short one-shot
  error       — ring flashes red (#E24B4A), short one-shot

Frames render with PIL → PNG on first run, then are reused from
~/.voicebot/cache/frames-v<GEN_VERSION>/. Bump GEN_VERSION whenever the
generators change so old cached PNGs don't shadow the new look.
"""

import logging
import math
import os

from PIL import Image, ImageDraw

from build_assets import (
    MENU_ICON_SIZE,
    RING,
    compose,
    ring_width_for,
)

logger = logging.getLogger("voicebot.animations")

ICON_SIZE = MENU_ICON_SIZE  # 44px @2x

# Bump on every generator tweak — frames live in cache/frames-v<N>/, so old
# versions are simply abandoned (no manual `rm -rf` required).
# v5: mascot-in-a-ring composition (was the green pill / comet look).
GEN_VERSION = 5
CACHE_DIR = os.path.expanduser(f"~/.voicebot/cache/frames-v{GEN_VERSION}")

# Ring effect colours (approved). RGBA tuples.
REC_PULSE = (0xB5, 0x7B, 0xFF, 255)   # recording pulse peak
PROC_ARC  = (0xE0, 0xD4, 0xEC, 255)   # processing sweep arc
SUCCESS   = (0x5D, 0xCA, 0xA5, 255)   # success flash (green)
ERROR     = (0xE2, 0x4B, 0x4A, 255)   # error flash (red)


def _lerp(c0, c1, t):
    """Linear interpolation between two RGBA tuples."""
    return tuple(int(round(a + (b - a) * t)) for a, b in zip(c0, c1))


def _overlay_arc(base, color, start_deg, extent_deg, ring_width):
    """Draw a bright arc on the ring radius, supersampled for smoothness."""
    scale = 4
    size = base.width
    big = size * scale
    layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    w = max(2, int(round(ring_width * scale)))
    off = w / 2.0
    d.arc((off, off, big - 1 - off, big - 1 - off),
          start_deg, start_deg + extent_deg, fill=color, width=w)
    base.alpha_composite(layer.resize((size, size), Image.LANCZOS))


class AnimationGenerator:
    def __init__(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        self._cache = {}

    def get_frames(self, anim_type):
        if anim_type in self._cache:
            return self._cache[anim_type]

        subdir = os.path.join(CACHE_DIR, anim_type)
        if os.path.isdir(subdir):
            files = sorted(
                os.path.join(subdir, f)
                for f in os.listdir(subdir)
                if f.endswith(".png")
            )
            if files:
                self._cache[anim_type] = files
                return files

        os.makedirs(subdir, exist_ok=True)
        generators = {
            "recording": self._gen_recording,
            "processing": self._gen_processing,
            "success": self._gen_success,
            "error": self._gen_error,
        }

        gen = generators.get(anim_type)
        if not gen:
            return []

        frames = gen(subdir)
        self._cache[anim_type] = frames
        logger.info(f"Generated {len(frames)} frames for {anim_type!r}")
        return frames

    def clear_cache(self):
        self._cache.clear()
        import shutil
        if os.path.isdir(CACHE_DIR):
            shutil.rmtree(CACHE_DIR)
        os.makedirs(CACHE_DIR, exist_ok=True)

    # ── Idle (single static frame, exposed for one-off PNG generation) ────

    def render_idle(self):
        """Return a single PIL Image of the idle look — the plain
        mascot-in-a-ring. Used to regenerate assets/icon_idle.png on demand."""
        return compose(ICON_SIZE)

    # ── Recording: ring pulses toward bright purple ──────────────────────

    def _gen_recording(self, outdir, num_frames=16):
        frames = []
        base_w = ring_width_for(ICON_SIZE)
        for i in range(num_frames):
            t = i / num_frames
            factor = 0.5 + 0.5 * math.sin(2 * math.pi * t)
            ring_rgba = _lerp(RING, REC_PULSE, factor)
            ring_width = base_w * (1.0 + 0.55 * factor)
            img = compose(ICON_SIZE, ring_rgba=ring_rgba, ring_width=ring_width)
            path = os.path.join(outdir, f"frame_{i:04d}.png")
            img.save(path, "PNG")
            frames.append(path)
        return frames

    # ── Processing: bright arc sweeping around the ring ──────────────────

    def _gen_processing(self, outdir, num_frames=24):
        frames = []
        base_w = ring_width_for(ICON_SIZE)
        extent = 90  # degrees of visible arc
        for i in range(num_frames):
            t = i / num_frames
            img = compose(ICON_SIZE)  # static dark ring underneath
            _overlay_arc(img, PROC_ARC, -360 * t, extent, base_w * 1.25)
            path = os.path.join(outdir, f"frame_{i:04d}.png")
            img.save(path, "PNG")
            frames.append(path)
        return frames

    # ── Success: green ring flash (one-shot) ─────────────────────────────

    def _gen_success(self, outdir, num_frames=8):
        return self._gen_flash(outdir, num_frames, SUCCESS)

    # ── Error: red ring flash (one-shot) ─────────────────────────────────

    def _gen_error(self, outdir, num_frames=8):
        return self._gen_flash(outdir, num_frames, ERROR)

    def _gen_flash(self, outdir, num_frames, color):
        frames = []
        base_w = ring_width_for(ICON_SIZE)
        for i in range(num_frames):
            t = i / num_frames
            factor = math.sin(math.pi * t)  # 0 → 1 → 0
            ring_rgba = _lerp(RING, color, factor)
            ring_width = base_w * (1.0 + 0.5 * factor)
            img = compose(ICON_SIZE, ring_rgba=ring_rgba, ring_width=ring_width)
            path = os.path.join(outdir, f"frame_{i:04d}.png")
            img.save(path, "PNG")
            frames.append(path)
        return frames
