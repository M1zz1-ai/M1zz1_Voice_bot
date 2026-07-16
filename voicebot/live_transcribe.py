"""
Live (streaming) transcription via a sliding Whisper window.

Whisper isn't natively streaming — it consumes a full audio clip and emits
text. To get a "type as you speak" feel we re-transcribe the recorder buffer
every `poll_seconds` and commit only the prefix that two consecutive runs
agree on. That common-prefix stability rule eliminates flicker: a delta is
pasted exactly once and never retracted.

Sliding window: we never re-transcribe the whole buffer. Once the open window
outgrows Whisper's ~30 s receptive field, `committed_offset` advances so each
poll only ever transcribes the recent tail (`audio[committed_offset:]`). This
bounds per-poll inference to ~2 s regardless of dictation length — otherwise
the MLX thread saturates the GIL on long recordings and starves the 20 fps
overlay / menu-bar animation timers (stuttering, vanishing squares).

mlx_whisper (inline mode) hands back text without per-token timestamps, so the
offset advances by a proportional estimate of the committed audio fraction —
a deliberately simple heuristic. Because the stability history is
window-relative, it is reset every time the window start moves.

Trade-offs:
- Latency floor ≈ poll_seconds + transcribe_time. At 1.5 s poll + ~0.5 s
  inference on turbo, the first word lands ~2 s after it's spoken.
- The trailing words of a window fluctuate as more context arrives, so we
  never commit the suffix of the latest run — only the common prefix with
  the run before it. The remainder is flushed by `finalize()` after stop.
"""

import logging
import threading

logger = logging.getLogger("voicebot.live")


# Live sliding-window length (samples set per sample_rate at construction).
# Kept below Whisper's 30 s receptive field so each poll stays in-distribution.
_WINDOW_SECONDS = 28


def _longest_common_prefix(a, b):
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return a[:i]


def _suffix_prefix_overlap(committed, text):
    """Longest L such that ``committed[-L:] == text[:L]``.

    After the window slid, a tail-window transcription re-covers audio whose
    text is already committed. Appending only ``text[overlap:]`` guarantees the
    overlapping region is never pasted twice (the -0/+297 duplication bug).
    """
    max_l = min(len(committed), len(text))
    for length in range(max_l, 0, -1):
        if committed[-length:] == text[:length]:
            return length
    return 0


# Characters that mark a "safe" commit boundary: a space or end-of-sentence
# punctuation. Anything before one of these is a fully-formed token Whisper
# is unlikely to retract.
_SAFE_BOUNDARY = set(" \t\n.,!?;:—–-)]}»")


def _trim_to_boundary(text):
    """Cut `text` back to the last safe boundary character.

    Without this, the LCP between two consecutive Whisper runs can land
    mid-word, e.g. committing "пишешь.д" because both runs agreed on
    those characters before the next one diverged. A later revision then
    produced "пишешь.аю" because we'd already committed "пишешь.д" and
    only the suffix "аю" was new. Trimming forces commits to align with
    word/sentence breaks, so partial words always wait one more poll.
    Returns "" if no boundary is found yet.
    """
    if not text:
        return ""
    # Find the last index where text[i] is a boundary character.
    for i in range(len(text) - 1, -1, -1):
        if text[i] in _SAFE_BOUNDARY:
            return text[:i + 1]
    return ""


class LiveTranscriber:
    def __init__(self, recorder, engine, paster, sample_rate=16000,
                 poll_seconds=1.5, stability_runs=2):
        self._recorder = recorder
        self._engine = engine
        self._paster = paster
        self._sample_rate = sample_rate
        self._poll = float(poll_seconds)
        self._stability = max(2, int(stability_runs))

        self._window_samples = int(_WINDOW_SECONDS * sample_rate)

        self._thread = None
        self._stop = threading.Event()
        self._history = []
        self._committed = ""

        # Sliding-window state. `_window_start` is the sample offset of the
        # open window; `_window_committed` is the portion of `_committed` that
        # belongs to that window (an invariant suffix of `_committed`).
        self._window_start = 0
        self._window_committed = ""

    @property
    def committed(self):
        return self._committed

    def start(self):
        """Begin polling. The recorder must already be running."""
        self._stop.clear()
        self._history = []
        self._committed = ""
        self._window_start = 0
        self._window_committed = ""
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="live-transcribe",
        )
        self._thread.start()
        logger.info("Live transcribe started")

    def stop(self):
        """Stop the polling loop. Safe to call from any thread."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Live transcribe stopped")

    def finalize(self, full_audio):
        """End-of-recording reconciliation pass.

        Two cases, keyed on whether the window ever slid:

        - Window never slid (`_window_start == 0`): the fresh transcription is
          a full-context pass over the whole message and `_committed` is that
          whole message, so a longest-common-prefix diff (backspace the revised
          tail, type the corrected tail) is valid and gives the best quality.

        - Window slid: the fresh transcription covers only the trailing window,
          which re-transcribes audio whose text is already committed. Comparing
          it to absolute `_committed` with LCP is INVALID — it duplicated a
          whole tail chunk. Instead we detect the suffix of `_committed` that
          overlaps the transcription's prefix and type only what follows, never
          re-typing already-committed text.
        """
        if full_audio is None or len(full_audio) < self._sample_rate:
            return

        if self._window_start == 0:
            self._finalize_full(full_audio)
        else:
            self._finalize_tail(full_audio)

    def _finalize_full(self, full_audio):
        """Full-message reconciliation (window never slid): LCP diff."""
        try:
            text = self._engine.transcribe(
                full_audio, self._sample_rate, inline=True,
            ) or ""
        except Exception:
            logger.exception("live finalize failed")
            return

        if text == self._committed:
            return

        common = _longest_common_prefix(text, self._committed)
        to_delete = len(self._committed) - len(common)
        to_type = text[len(common):]

        if to_delete > 0:
            self._paster.backspace(to_delete)
        if to_type:
            self._paster.type_text(to_type)

        self._committed = text
        self._window_committed = text
        logger.info(
            f"live finalized: corrected -{to_delete}/+{len(to_type)} chars"
        )

    def _finalize_tail(self, full_audio):
        """Tail-window reconciliation (window slid): append only the
        genuinely-uncommitted suffix via suffix/prefix overlap. Never
        backspaces across the slid boundary and never re-types the message."""
        tail = full_audio[self._window_start:]
        if len(tail) < self._sample_rate:
            return
        try:
            text = self._engine.transcribe(
                tail, self._sample_rate, inline=True,
            ) or ""
        except Exception:
            logger.exception("live finalize failed")
            return

        if not text:
            return

        overlap = _suffix_prefix_overlap(self._committed, text)
        to_type = text[overlap:]
        if to_type:
            self._paster.type_text(to_type)
            self._committed += to_type
            self._window_committed += to_type
        logger.info(
            f"live finalized (tail): overlap={overlap} +{len(to_type)} chars"
        )

    def _loop(self):
        while not self._stop.wait(timeout=self._poll):
            audio = self._recorder.snapshot()
            if audio is None:
                continue

            total = len(audio)
            window_audio = audio[self._window_start:]
            if len(window_audio) < self._sample_rate:
                continue

            try:
                text = self._engine.transcribe(
                    window_audio, self._sample_rate, inline=True,
                )
            except Exception:
                logger.exception("live transcribe failed")
                continue

            if not text:
                continue

            self._ingest(text, total)

    def _ingest(self, text, total):
        """Process one window transcription: fold it into the stability
        history, commit the newly-stable delta, then slide the window if it
        outgrew the receptive field.

        Split out from `_loop` so the sliding-window logic is unit-testable
        without threads or a real engine. `total` is the full buffer length in
        samples; `text` is the transcription of `audio[_window_start:]`.
        """
        self._history.append(text)
        if len(self._history) >= self._stability:
            # The stable prefix is what the last N runs all agree on.
            # Fold longest_common_prefix across the tail of the history.
            stable = self._history[-1]
            for prev in self._history[-self._stability:-1]:
                stable = _longest_common_prefix(stable, prev)
                if not stable:
                    break

            # Pull back to the last word/punctuation boundary so we never
            # commit a half-formed token Whisper might revise.
            stable = _trim_to_boundary(stable)

            delta = stable[len(self._window_committed):]
            if delta:
                # Each commit's trim leaves a trailing space, and Whisper
                # sometimes emits a leading space on the next chunk too —
                # concatenated they become "слов  слов". Drop the leading
                # whitespace of the delta when the committed tail already ends
                # with whitespace, then resync `stable` so
                # `_window_committed = stable` stays the invariant.
                if (self._window_committed
                        and self._window_committed[-1].isspace()
                        and delta[:1].isspace()):
                    delta = delta.lstrip()
                    if delta:
                        stable = self._window_committed + delta

                if delta:
                    self._paster.type_text(delta)
                    self._committed += delta
                    self._window_committed = stable
                    logger.info(f"live committed +{len(delta)} chars")

        self._maybe_slide(total, text)

    def _maybe_slide(self, total, window_text):
        """Advance the window start once the buffer outgrows the receptive
        field, so each poll only transcribes the recent tail.

        `committed_offset` advances by the audio fraction we've already
        committed (proportional estimate — mlx_whisper gives no per-token
        timestamps), but never by less than what's needed to bound the window
        to `_window_samples`. The stability history is window-relative, so it
        resets on every move.
        """
        window_len = total - self._window_start
        if window_len <= self._window_samples:
            return

        # Floor: guarantee the new window fits `_window_samples` even if commit
        # lag left little committed — bounds per-poll inference regardless of
        # dictation length.
        floor = window_len - self._window_samples
        if self._window_committed and window_text:
            consumed = int(
                (len(self._window_committed) / len(window_text)) * window_len
            )
        else:
            consumed = floor
        consumed = min(max(consumed, floor), window_len)

        self._window_start += consumed
        self._history = []
        self._window_committed = ""
        logger.info(
            "live window slid +%.1fs (start=%.1fs)",
            consumed / self._sample_rate,
            self._window_start / self._sample_rate,
        )
