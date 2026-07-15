"""
Screen-top glow overlay — the visual "listening" indicator.

A borderless, non-activating, click-through NSPanel spanning the full width of
the main screen across the top ~15% of its height. It floats above normal
windows, joins every Space, ignores the mouse, and NEVER takes focus. The panel
is created lazily on first show and ordered out when idle.

The look (purely visual — no text): a slowly pulsing purple glow band hugging
the top edge with a bright top-edge line, plus purple squares that spawn near
the top edge and fall while fading out. Every square on screen is in motion —
there is no static field, so the overlay can never read as a frozen frame.

States drive colour/behaviour only:
  recording  — full effect, squares spawning
  processing — squares stop spawning (existing ones fall out), glow breathes
               slower
  success    — brief green burst, then everything fades out (~0.5s) and hides
  error      — brief red burst, then fade out and hide
  idle       — hidden (orderOut)

Robustness (a stuck static panel is unacceptable):
  - The NSTimer runs the whole time the panel is visible and is only
    invalidated on hide — a state change never starves it.
  - Every tick is wrapped in try/except; 3 consecutive failures force-hide.
  - A watchdog force-hides if the overlay lingers in a non-recording state
    (success/error/anything else) beyond a couple of seconds, and caps even
    a legitimate "processing" at a generous ceiling — so a slow/failed
    transcription can never leave the panel on screen forever.
  - The panel is owned by the process, so `kill -9` tears it down with the app.

A single NSView drawRect: is driven by an NSTimer capped at ~20 fps. The timer
runs ONLY while the overlay is visible, so idle CPU is zero. Target well under
5% of one M1 core while animating.
"""

import logging
import math
import random
import time

import AppKit
import objc
from Foundation import (
    NSMakeRect,
    NSRunLoop,
    NSRunLoopCommonModes,
    NSThread,
)

logger = logging.getLogger("voicebot.overlay")

# ── Tunables (kept light for the 8GB M1 Air) ──────────────────────────────────
FPS = 20                       # animation frame rate cap
_DT = 1.0 / FPS
BAND_FRACTION = 0.15           # overlay height as a fraction of screen height
PULSE_PERIOD = 0.6             # glow pulse period (s), recording
PULSE_PERIOD_SLOW = 1.2        # glow "breathe" period (s), processing
BURST_SECONDS = 0.5            # success/error burst + fade-out duration

# Watchdog ceilings (seconds). recording is uncapped (dictation can be long).
_WATCHDOG_NONRECORDING = 2.0   # success/error/other must clear fast
_WATCHDOG_PROCESSING = 60.0    # hard ceiling even for a slow transcription

# Glow colours (0-255). Alpha peaks are deliberately a touch below spec to ease
# GPU load while staying clearly visible.
GLOW_RGB = (150, 110, 230)
GLOW_PEAK_ALPHA = 0.72
EDGE_RGB = (210, 190, 255)
EDGE_ALPHA = 0.9
EDGE_THICKNESS = 3             # px bright top-edge line
SQUARE_RGB = (150, 110, 230)

SUCCESS_RGB = (93, 202, 165)   # #5DCAA5
ERROR_RGB = (226, 75, 74)      # #E24B4A

# Falling squares — deliberately sparse, but every one is moving.
_CELL = 8                      # px square (grid pitch for column snapping)
_GAP = 2
_SPAWN_P = 0.7                 # p(spawn 1 this frame)
_SPAWN_EXTRA_P = 0.25          # p(spawn a 2nd this frame)
_MAX_SQUARES = 48
_FADE_PER_FRAME = 0.985


def _run_on_main(block):
    """Run ``block`` on the main thread (AppKit is main-thread only)."""
    if NSThread.isMainThread():
        block()
    else:
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(block)


def _nscolor(rgb, alpha):
    r, g, b = rgb
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
        r / 255.0, g / 255.0, b / 255.0, alpha
    )


class GlowOverlayView(AppKit.NSView):
    """Custom view that paints the glow band and the falling squares.

    All animation state lives here; ``tick_`` advances it and requests a redraw.
    """

    def initWithFrame_(self, frame):
        self = objc.super(GlowOverlayView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._state = "recording"
        self._state_since = time.monotonic()
        self._phase = 0.0
        self._spawn = True
        self._pulse_period = PULSE_PERIOD
        self._squares = []          # falling squares (the ONLY drawn squares)
        self._global_alpha = 1.0
        self._flash_rgb = None      # burst tint, or None
        self._bursting = False
        self._burst_elapsed = 0.0
        self._tick_errors = 0
        self._draw_div = 0          # redraw throttle counter (processing)
        self._on_finish = None      # controller callback → force hide
        return self

    # ── Non-activating / transparent behaviour ───────────────────────────

    def isOpaque(self):
        return False

    def acceptsFirstResponder(self):
        return False

    # ── State control (called from the controller, main thread) ──────────

    @objc.python_method
    def reset_for_recording(self):
        self._state = "recording"
        self._state_since = time.monotonic()
        self._spawn = True
        self._pulse_period = PULSE_PERIOD
        self._global_alpha = 1.0
        self._flash_rgb = None
        self._bursting = False
        self._burst_elapsed = 0.0
        self._tick_errors = 0
        self._squares = []

    @objc.python_method
    def set_state(self, state):
        if state == "recording":
            self.reset_for_recording()
            return
        self._state = state
        self._state_since = time.monotonic()
        if state == "processing":
            self._spawn = False
            self._pulse_period = PULSE_PERIOD_SLOW
        elif state in ("success", "error"):
            self._spawn = False
            self._bursting = True
            self._burst_elapsed = 0.0
            self._flash_rgb = SUCCESS_RGB if state == "success" else ERROR_RGB

    # ── Per-frame update (ObjC timer target) ──────────────────────────────

    def tick_(self, timer):
        try:
            self._tick_body()
            self._tick_errors = 0
        except Exception:
            self._tick_errors += 1
            logger.exception("overlay tick failed (%d)", self._tick_errors)
            if self._tick_errors >= 3:
                logger.error("overlay: 3 consecutive tick errors — force hide")
                self._force_hide()

    @objc.python_method
    def _tick_body(self):
        self._phase += _DT

        if self._spawn and len(self._squares) < _MAX_SQUARES:
            self._spawn_squares()
        self._advance_squares()

        # Burst fade-out (success/error) → hide when fully transparent.
        if self._bursting:
            self._burst_elapsed += _DT
            self._global_alpha = max(
                0.0, 1.0 - self._burst_elapsed / BURST_SECONDS
            )
            if self._burst_elapsed >= BURST_SECONDS:
                self._force_hide()
                return

        # Watchdog: never let a non-recording state linger on screen forever.
        if self._state != "recording":
            elapsed = time.monotonic() - self._state_since
            limit = (_WATCHDOG_PROCESSING if self._state == "processing"
                     else _WATCHDOG_NONRECORDING)
            if elapsed > limit:
                logger.warning(
                    "overlay watchdog: force-hide (state=%s, %.1fs > %.1fs)",
                    self._state, elapsed, limit,
                )
                self._force_hide()
                return

        # Perf mercy on the 8GB Air: redraw at ~5fps while "processing"
        # (GIL-bound MLX inference starves a 20fps Python tick). Animation
        # state still advances every tick, so motion is preserved.
        self._draw_div = (self._draw_div + 1) % 4
        if self._state == "processing" and self._draw_div != 0:
            return
        self.setNeedsDisplay_(True)

    @objc.python_method
    def _force_hide(self):
        cb = self._on_finish
        if cb is not None:
            cb()

    @objc.python_method
    def _spawn_squares(self):
        w = self.bounds().size.width
        h = self.bounds().size.height
        n = 1 if random.random() < _SPAWN_P else 0
        if random.random() < _SPAWN_EXTRA_P:
            n += 1
        pitch = _CELL + _GAP
        for _ in range(n):
            col = random.randint(0, max(1, int(w // pitch) - 1))
            size = 16 if random.random() < 0.2 else 8
            self._squares.append({
                "x": col * pitch,
                # spawn right at the top edge so they fall the full band
                "y": h - random.uniform(0, h * 0.12),
                "size": size,
                "vy": random.uniform(35.0, 85.0),
                "a": 0.85,
            })

    @objc.python_method
    def _advance_squares(self):
        alive = []
        for s in self._squares:
            s["y"] -= s["vy"] * _DT
            s["a"] *= _FADE_PER_FRAME
            if s["a"] > 0.03 and s["y"] > -s["size"]:
                alive.append(s)
        self._squares = alive

    # ── Drawing ───────────────────────────────────────────────────────────

    def drawRect_(self, rect):
        bounds = self.bounds()
        w = bounds.size.width
        h = bounds.size.height
        ga = self._global_alpha
        if ga <= 0.0:
            return

        pulse = 0.5 + 0.5 * math.sin(
            2 * math.pi * self._phase / self._pulse_period
        )

        # Glow band — vertical gradient, strongest at the top, fading down.
        glow_rgb = self._flash_rgb or GLOW_RGB
        peak = GLOW_PEAK_ALPHA * (0.6 + 0.4 * pulse) * ga
        top_color = _nscolor(glow_rgb, peak)
        bottom_color = _nscolor(glow_rgb, 0.0)
        gradient = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
            bottom_color, top_color
        )
        if gradient is not None:
            gradient.drawInRect_angle_(NSMakeRect(0, 0, w, h), 90.0)

        # Bright top-edge line.
        edge_rgb = self._flash_rgb or EDGE_RGB
        _nscolor(edge_rgb, EDGE_ALPHA * ga).setFill()
        AppKit.NSBezierPath.bezierPathWithRect_(
            NSMakeRect(0, h - EDGE_THICKNESS, w, EDGE_THICKNESS)
        ).fill()

        # Falling squares — the only squares on screen, all in motion.
        sq_rgb = self._flash_rgb or SQUARE_RGB
        for s in self._squares:
            _nscolor(sq_rgb, s["a"] * ga).setFill()
            AppKit.NSBezierPath.bezierPathWithRect_(
                NSMakeRect(s["x"], s["y"], s["size"], s["size"])
            ).fill()


class Overlay:
    """Controller: owns the NSPanel + view and the animation timer.

    Public methods are thread-safe — they marshal onto the main thread. The
    timer is created on show and invalidated on hide so idle CPU is zero.
    """

    def __init__(self):
        self._panel = None
        self._view = None
        self._timer = None
        self._watchdog = None
        self._visible = False

    # ── Public API ────────────────────────────────────────────────────────

    def show(self):
        """Show the overlay in the recording state (lazy-creates the panel)."""
        _run_on_main(self._show_impl)

    def set_state(self, state):
        """Update the visual state. 'idle' hides; burst states auto-hide."""
        if state == "idle":
            self.hide()
            return
        _run_on_main(lambda: self._set_state_impl(state))

    def hide(self):
        _run_on_main(self._hide_impl)

    # ── Main-thread implementations ───────────────────────────────────────

    def _ensure_panel(self):
        if self._panel is not None:
            return True
        screen = AppKit.NSScreen.mainScreen()
        if screen is None:
            logger.warning("No main screen; overlay unavailable")
            return False
        f = screen.frame()
        band_h = f.size.height * BAND_FRACTION
        rect = NSMakeRect(
            f.origin.x, f.origin.y + f.size.height - band_h,
            f.size.width, band_h,
        )
        # Borderless + non-activating panel.
        style = (AppKit.NSWindowStyleMaskBorderless
                 | AppKit.NSWindowStyleMaskNonactivatingPanel)
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, AppKit.NSBackingStoreBuffered, False,
        )
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setIgnoresMouseEvents_(True)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setHidesOnDeactivate_(False)
        self._apply_behavior(panel)

        view = GlowOverlayView.alloc().initWithFrame_(
            NSMakeRect(0, 0, f.size.width, band_h)
        )
        view._on_finish = self._on_view_finished
        panel.setContentView_(view)

        self._panel = panel
        self._view = view
        return True

    def _apply_behavior(self, panel):
        # setFloatingPanel_(True) forces the level down to NSFloatingWindowLevel,
        # so assert the intended NSStatusWindowLevel AFTER it. CanJoinAllSpaces
        # is level-independent, but it is only reliably honoured while the panel
        # actually carries the behaviour — so re-assert it on every show, since
        # a Space switch can otherwise leave the panel pinned to one Space.
        panel.setFloatingPanel_(True)
        panel.setLevel_(AppKit.NSStatusWindowLevel)
        panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorStationary
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
            | AppKit.NSWindowCollectionBehaviorIgnoresCycle
        )

    def _start_timer(self):
        if self._timer is not None:
            return
        self._timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            _DT, self._view, b"tick:", None, True,
        )

    def _stop_timer(self):
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None

    # ── Independent watchdog ──────────────────────────────────────────────
    # Decoupled from the drawing timer: if tick_ starves (heavy load), this
    # still force-hides on the ceiling. Runs in NSRunLoopCommonModes so modal
    # / event-tracking run-loop modes don't pause it.

    def _start_watchdog(self):
        if self._watchdog is not None:
            return
        t = AppKit.NSTimer.timerWithTimeInterval_repeats_block_(
            0.5, True, lambda _t: self._watchdog_fire(),
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(t, NSRunLoopCommonModes)
        self._watchdog = t

    def _stop_watchdog(self):
        if self._watchdog is not None:
            self._watchdog.invalidate()
            self._watchdog = None

    def _watchdog_fire(self):
        v = self._view
        if v is None or not self._visible:
            return
        state = v._state
        if state == "recording":
            return
        elapsed = time.monotonic() - v._state_since
        limit = (_WATCHDOG_PROCESSING if state == "processing"
                 else _WATCHDOG_NONRECORDING)
        if elapsed > limit:
            logger.warning(
                "overlay watchdog (independent): force-hide "
                "(state=%s, %.1fs > %.1fs)", state, elapsed, limit,
            )
            self._hide_impl()

    def _show_impl(self):
        if not self._ensure_panel():
            return
        # Re-assert level + Spaces behaviour every show — a prior Space switch
        # or level change can drop CanJoinAllSpaces / the intended level.
        self._apply_behavior(self._panel)
        self._view.reset_for_recording()
        # orderFrontRegardless never steals focus (unlike makeKeyAndOrderFront).
        self._panel.orderFrontRegardless()
        self._visible = True
        self._start_timer()
        self._start_watchdog()

    def _set_state_impl(self, state):
        if not self._visible:
            # Burst/processing without a prior show — bring it up first.
            self._show_impl()
        if self._view is not None:
            self._view.set_state(state)

    def _hide_impl(self):
        self._stop_timer()
        self._stop_watchdog()
        if self._panel is not None:
            self._panel.orderOut_(None)
        self._visible = False

    def _on_view_finished(self):
        # Called from the view's tick_ (main thread) on burst-complete,
        # watchdog trip, or repeated tick errors.
        self._hide_impl()


# ── Dev entrypoint ────────────────────────────────────────────────────────────

def demo(seconds=8):
    """Cycle recording → processing → success → hide as a visual smoke test.

    Wired to ``main.py --demo-overlay``. Exercises the full state machine so a
    stuck/frozen panel or leftover residue is obvious.
    """
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    overlay = Overlay()

    # Scripted state timeline (seconds from start).
    overlay.show()
    logger.info("overlay demo: recording")
    AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
        seconds * 0.45, False,
        lambda t: (overlay.set_state("processing"),
                   logger.info("overlay demo: processing")),
    )
    AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
        seconds * 0.75, False,
        lambda t: (overlay.set_state("success"),
                   logger.info("overlay demo: success")),
    )

    def _stop():
        overlay.hide()
        AppKit.NSApp.stop_(None)
        # stop_ only takes effect on the next event — post a dummy one to wake
        # the run loop so app.run() returns promptly instead of hanging.
        ev = AppKit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            AppKit.NSEventTypeApplicationDefined, (0.0, 0.0), 0, 0.0, 0,
            None, 0, 0, 0,
        )
        AppKit.NSApp.postEvent_atStart_(ev, True)

    AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
        float(seconds), False, lambda t: _stop(),
    )
    logger.info("overlay demo: running %ss cycle", seconds)
    app.run()
    logger.info("overlay demo: done")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    demo(8)
