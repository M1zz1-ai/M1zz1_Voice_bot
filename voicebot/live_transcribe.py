"""
Live (streaming) transcription via a sliding Whisper window.

Whisper isn't natively streaming — it consumes a full audio clip and emits
text. To get a "type as you speak" feel we re-transcribe the growing
recorder buffer every `poll_seconds` and commit only the prefix that two
consecutive runs agree on. That common-prefix stability rule eliminates
flicker: a delta is pasted exactly once and never retracted.

Trade-offs:
- Latency floor ≈ poll_seconds + transcribe_time. At 1.5 s poll + ~0.5 s
  inference on turbo, the first word lands ~2 s after it's spoken.
- Cost: every commit re-runs Whisper on the whole buffer so far. Fine for
  ~30 s sessions; beyond that the window approaches Whisper's 30 s receptive
  field and quality degrades.
- The trailing words of the buffer fluctuate as more context arrives, so we
  never commit the suffix of the latest run — only the common prefix with
  the run before it. The remainder is flushed by `finalize()` after stop.
"""

import logging
import threading

logger = logging.getLogger("voicebot.live")


def _longest_common_prefix(a, b):
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return a[:i]


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

        self._thread = None
        self._stop = threading.Event()
        self._history = []
        self._committed = ""

    @property
    def committed(self):
        return self._committed

    def start(self):
        """Begin polling. The recorder must already be running."""
        self._stop.clear()
        self._history = []
        self._committed = ""
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

        Re-transcribes the complete buffer, then aligns whatever's already on
        screen with the final Whisper output:

        - Compute the longest common prefix of `committed` (already typed)
          and `text` (the final, full-context transcription).
        - Backspace away the committed tail past that prefix — those are the
          characters Whisper revised once it had more audio context.
        - Type the final tail past the same prefix.

        When Whisper didn't revise anything, the LCP equals `committed` and
        we just append the missing suffix — same outcome as the previous
        suffix-only finalize. When it did revise (e.g. "она" → "оно" mid-
        sentence), the diff is small and surgical instead of a full retype.
        """
        if full_audio is None or len(full_audio) < self._sample_rate:
            return
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
        logger.info(
            f"live finalized: corrected -{to_delete}/+{len(to_type)} chars"
        )

    def _loop(self):
        while not self._stop.wait(timeout=self._poll):
            audio = self._recorder.snapshot()
            if audio is None or len(audio) < self._sample_rate:
                continue

            try:
                text = self._engine.transcribe(
                    audio, self._sample_rate, inline=True,
                )
            except Exception:
                logger.exception("live transcribe failed")
                continue

            if not text:
                continue

            self._history.append(text)
            if len(self._history) < self._stability:
                continue

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

            delta = stable[len(self._committed):]
            if not delta:
                continue

            # Each commit's trim leaves a trailing space, and Whisper sometimes
            # emits a leading space on the next chunk too — concatenated they
            # become "слов  слов". Drop the leading whitespace of the delta
            # when the committed tail already ends with whitespace, then
            # resync `stable` so `committed = stable` remains invariant for
            # the next iteration's `delta = stable[len(committed):]`.
            if (self._committed and self._committed[-1].isspace()
                    and delta[:1].isspace()):
                delta = delta.lstrip()
                if not delta:
                    continue
                stable = self._committed + delta

            self._paster.type_text(delta)
            self._committed = stable
            logger.info(f"live committed +{len(delta)} chars")
