"""
MLX Whisper engine: owns the model lifecycle.

Responsibilities:
- Lazy-download model from HuggingFace Hub (mlx-community/whisper-*-mlx)
- Surface state ("idle"/"downloading"/"loading"/"ready"/"transcribing"/"error")
  via an on_state_change callback so UI can show progress.
- Hot-swap model at runtime when user picks a different one in Settings.
- Synchronous transcribe(audio_int16, sr) → str | None. Blocks (up to 120s)
  if the model is still downloading on first run.

mlx-whisper documentation:
  https://github.com/ml-explore/mlx-examples/tree/main/whisper

Audio format: mlx-whisper expects float32 mono at 16 kHz. Our recorder
captures int16 at whatever sample_rate the user configured (default 16 kHz).
We convert here.
"""

import concurrent.futures
import gc
import logging
import os
import re
import threading
import time

import numpy as np

logger = logging.getLogger("voicebot.whisper_engine")


# Common Whisper hallucinations bleeding in from YouTube/TV subtitle training
# data. They surface when the model hits silence, a cut-off, or anything
# out-of-distribution. Stripped only at the *tail* of a transcription —
# extremely unlikely as a natural ending in unscripted speech, but a real
# word inside a sentence shouldn't get blasted away.
_HALLUCINATION_TAIL = re.compile(
    r"(?:"
    r"Продолжение\s+следует"
    r"|Спасибо\s+за\s+(?:просмотр|внимание)"
    r"|Подписывайтесь\s+на\s+(?:канал|нас)"
    r"|Не\s+забудьте\s+подписаться"
    r"|(?:Поставьте|Ставьте)\s+лайк"
    r"|Жмите\s+на\s+колокольчик"
    r"|Субтитры\s+(?:подготовил|создал|сделал|подготовлены)"
    r"|DimaTorzok"
    r"|Игорь\s+Жадан"
    r"|Thanks\s+for\s+watching"
    r"|Please\s+(?:subscribe|like)"
    r"|Like\s+and\s+subscribe"
    r"|Music\s+by"
    r")[\s.,…!?]*$",
    flags=re.IGNORECASE,
)

# Runaway ellipsis: two or more ellipsis groups (the single char "…" or 2-3
# ASCII dots) separated by whitespace. Whisper emits these on long silences.
# We don't match a single "." so legitimate sentence-ending periods stay put.
_ELLIPSIS_RUN = re.compile(r"(?:…|\.{2,3})(?:\s+(?:…|\.{2,3})){1,}")


def _strip_repetition_loop(text):
    """Trim Whisper's repetition loops at the tail.

    On low-energy or out-of-distribution audio, greedy decoding falls into
    cycles like "в качестве в качестве в качестве …". Walks back over the
    word list looking for a 1-4-word block that repeats 3+ times in a row
    at the end, then keeps a single copy of it.
    """
    words = text.split()
    if len(words) < 4:
        return text
    for window in (1, 2, 3, 4):
        if len(words) < window * 3:
            continue
        last = words[-window:]
        if words[-2 * window:-window] != last:
            continue
        if words[-3 * window:-2 * window] != last:
            continue
        # Walk back until the repeating block stops matching.
        i = len(words) - window
        while i >= window and words[i - window:i] == last:
            i -= window
        # Keep everything up to and including ONE copy of the looped block.
        kept = " ".join(words[:i + window])
        return kept
    return text


def _sanitize(text):
    """Strip known Whisper hallucinations and collapse multi-ellipsis runs."""
    if not text:
        return text
    # Multiple hallucination phrases can chain together at the tail; strip
    # iteratively until the regex stops matching. Only trim whitespace after
    # a successful removal — keep punctuation that legitimately separated the
    # last real sentence from the hallucination ("Привет, мир. Спасибо за
    # просмотр!" → "Привет, мир.", not "Привет, мир").
    for _ in range(3):
        after = _HALLUCINATION_TAIL.sub("", text)
        if after == text:
            break
        text = after.rstrip()
    text = _ELLIPSIS_RUN.sub("…", text)
    # Collapse double-spaces. mlx_whisper occasionally emits internal multi-
    # spaces between decoded segments (visible as "слов  слов" in live mode).
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = _strip_repetition_loop(text)
    return text.strip()


def _clear_mlx_caches():
    """Drop both module-level caches that hold the MLX model reference so
    RAM/VRAM is freed. MUST run on the MLX worker thread so the model's GPU
    stream is released cleanly. Safe to call when nothing is loaded."""
    try:
        from mlx_whisper import load_models
        if hasattr(load_models, "_models"):
            load_models._models.clear()
        from mlx_whisper.transcribe import ModelHolder
        ModelHolder.model = None
        ModelHolder.model_path = None
    except Exception:
        logger.exception("MLX cache clear failed")


class _MLXWorker:
    """Single-threaded executor that owns ALL MLX GPU calls.

    MLX's GPU streams are thread-local: load weights in thread A, run a
    forward pass from thread B, and you get
        RuntimeError: There is no Stream(gpu, N) in current thread.
    Funneling warmup, transcribe, and cache-clear through one persistent
    worker keeps the stream context consistent across the engine.
    """

    def __init__(self):
        self._exec = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="mlx",
        )

    def submit(self, fn, *args, **kwargs):
        return self._exec.submit(fn, *args, **kwargs)

    def shutdown(self):
        self._exec.shutdown(wait=False)


# Each entry: (HuggingFace repo, approximate download size in MB).
# Sizes are used only for the on-disk progress estimate; off by ~5–10% is fine.
MODELS = {
    "tiny":           ("mlx-community/whisper-tiny-mlx",            75),
    "base":           ("mlx-community/whisper-base-mlx",           142),
    "small":          ("mlx-community/whisper-small-mlx",          466),
    "medium":         ("mlx-community/whisper-medium-mlx",        1500),
    "large-v3":       ("mlx-community/whisper-large-v3-mlx",      3100),
    "large-v3-turbo": ("mlx-community/whisper-large-v3-turbo",     800),
}

DEFAULT_MODEL = "large-v3-turbo"

STATE_IDLE        = "idle"
STATE_DOWNLOADING = "downloading"
STATE_LOADING     = "loading"
STATE_READY       = "ready"
STATE_TRANSCRIBING = "transcribing"
STATE_ERROR       = "error"


def _hf_cache_dir(repo):
    """Where huggingface_hub stores `repo` on disk."""
    # repo "org/name" → models--org--name
    safe = "models--" + repo.replace("/", "--")
    base = os.environ.get(
        "HF_HUB_CACHE",
        os.path.expanduser("~/.cache/huggingface/hub"),
    )
    return os.path.join(base, safe)


def _dir_size_bytes(path):
    """Recursively sum file sizes under `path`. Returns 0 if missing."""
    total = 0
    if not os.path.isdir(path):
        return 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _model_already_downloaded(repo):
    """Heuristic: is there a fully-fetched snapshot for this repo?"""
    snap_dir = os.path.join(_hf_cache_dir(repo), "snapshots")
    if not os.path.isdir(snap_dir):
        return False
    for entry in os.listdir(snap_dir):
        full = os.path.join(snap_dir, entry)
        if os.path.isdir(full) and os.listdir(full):
            return True
    return False


def _clean_partial_downloads(repo):
    """Remove *.incomplete blob files left by interrupted HF downloads."""
    blobs = os.path.join(_hf_cache_dir(repo), "blobs")
    if not os.path.isdir(blobs):
        return
    for name in os.listdir(blobs):
        if name.endswith(".incomplete"):
            try:
                os.remove(os.path.join(blobs, name))
                logger.info(f"Cleaned partial blob: {name}")
            except OSError:
                pass


class WhisperEngine:
    """Single owner of the active MLX Whisper model."""

    def __init__(self, model_name=DEFAULT_MODEL, language=None,
                 on_state_change=None, idle_unload_minutes=5):
        if model_name not in MODELS:
            logger.warning(
                f"Unknown model {model_name!r}, falling back to {DEFAULT_MODEL!r}"
            )
            model_name = DEFAULT_MODEL

        self._model_name = model_name
        self._repo, self._size_mb = MODELS[model_name]
        self.language = language  # None = auto-detect

        self._state = STATE_IDLE
        self._error_detail = ""
        self._download_progress = 0.0

        self._lock = threading.Lock()
        self._ready_event = threading.Event()
        self._on_state_change = on_state_change

        self._load_thread = None
        self._progress_thread = None
        self._progress_stop = threading.Event()

        # Warmup: app.py kicks this off on hotkey press so weights are in
        # MLX memory by the time the user stops speaking.
        self._warmed = False
        self._warming = False

        # One persistent thread owns every MLX GPU call (see _MLXWorker).
        self._mlx = _MLXWorker()

        # Serializes inference. Live-mode (inline) polls skip rather than queue
        # when a prior inference is still running, so slow turbo inference on
        # 8GB never backs up and stalls the stop→finalize pass.
        self._infer_lock = threading.Lock()

        # Idle auto-unload: after this many minutes with no transcribe/warmup
        # call, free the model weights from RAM (files stay on disk, state
        # stays READY, next dictation pays the ~3s warm load). 0 disables.
        self._idle_unload_seconds = max(0, int(idle_unload_minutes)) * 60
        self._idle_timer = None

        # If files are already on disk from a previous session, mark ready
        # lazily — actual MLX load happens on first transcribe().
        if _model_already_downloaded(self._repo):
            self._set_state(STATE_READY)

    # ── Public properties ────────────────────────────────────────────────

    @property
    def state(self):
        return self._state

    @property
    def model_name(self):
        return self._model_name

    @property
    def is_ready(self):
        return self._state == STATE_READY

    @property
    def download_progress(self):
        return self._download_progress

    @property
    def error_detail(self):
        return self._error_detail

    # ── State plumbing ───────────────────────────────────────────────────

    def _set_state(self, state, detail=""):
        with self._lock:
            self._state = state
            if state == STATE_ERROR:
                self._error_detail = detail
            if state == STATE_READY:
                self._ready_event.set()
            else:
                # ready → not-ready transitions clear the event
                if state in (STATE_DOWNLOADING, STATE_LOADING, STATE_IDLE):
                    self._ready_event.clear()

        if self._on_state_change:
            try:
                self._on_state_change({
                    "state": state,
                    "progress": self._download_progress,
                    "detail": detail,
                    "model": self._model_name,
                })
            except Exception:
                logger.exception("on_state_change callback raised")

    def _emit_progress(self):
        """Throttled progress update on the same callback."""
        if self._on_state_change:
            try:
                self._on_state_change({
                    "state": self._state,
                    "progress": self._download_progress,
                    "detail": "",
                    "model": self._model_name,
                })
            except Exception:
                logger.exception("on_state_change progress raised")

    # ── Download / load ──────────────────────────────────────────────────

    def ensure_loaded(self, blocking=False):
        """Begin model download/load in the background. Idempotent.

        If `blocking=True`, wait until ready (or timeout 300s).
        """
        with self._lock:
            if self._state in (STATE_DOWNLOADING, STATE_LOADING):
                if blocking:
                    self._ready_event.wait(timeout=300)
                return
            if self._state == STATE_READY:
                return

        self._load_thread = threading.Thread(
            target=self._download_and_load, daemon=True,
        )
        self._load_thread.start()

        if blocking:
            self._ready_event.wait(timeout=300)

    def _download_and_load(self):
        """Background: download from HF Hub if needed."""
        _clean_partial_downloads(self._repo)

        already_present = _model_already_downloaded(self._repo)
        if not already_present:
            self._set_state(STATE_DOWNLOADING)
            self._start_progress_poll()
        else:
            self._set_state(STATE_LOADING)

        try:
            from huggingface_hub import snapshot_download
            snapshot_download(repo_id=self._repo)
        except Exception as e:
            logger.exception(f"Model download failed for {self._repo}")
            self._stop_progress_poll()
            self._set_state(
                STATE_ERROR,
                f"Network/HF failure: {e}",
            )
            return

        self._stop_progress_poll()

        # mlx-whisper loads weights inside transcribe() on first call; we
        # don't need to pre-warm. Just mark ready.
        self._download_progress = 1.0
        self._set_state(STATE_READY)
        logger.info(f"Model ready: {self._repo}")

    def _start_progress_poll(self):
        """Poll on-disk size every 1.0s while downloading."""
        if self._progress_thread and self._progress_thread.is_alive():
            return

        self._progress_stop.clear()
        expected_bytes = self._size_mb * 1024 * 1024

        def _poll():
            while not self._progress_stop.is_set():
                size = _dir_size_bytes(_hf_cache_dir(self._repo))
                pct = min(1.0, size / max(expected_bytes, 1))
                if abs(pct - self._download_progress) >= 0.01:
                    self._download_progress = pct
                    self._emit_progress()
                if self._progress_stop.wait(timeout=1.0):
                    break

        self._progress_thread = threading.Thread(target=_poll, daemon=True)
        self._progress_thread.start()

    def _stop_progress_poll(self):
        self._progress_stop.set()
        if self._progress_thread:
            self._progress_thread.join(timeout=2.0)
            self._progress_thread = None

    # ── Warmup ───────────────────────────────────────────────────────────

    def kickoff_warmup(self):
        """Start loading weights into MLX memory in a background thread.

        Called from app.py the moment the user begins recording — while they
        speak (~3 s on turbo), `mlx_whisper.transcribe.ModelHolder.get_model`
        reads weights.safetensors from disk into MLX. The subsequent real
        transcribe() then finds the model hot in the ModelHolder cache
        instead of paying a ~3 s cold load.

        Idempotent: no-op if already warmed or in flight. No-op when files
        aren't on disk yet (download path will handle that case).
        """
        # Record-start counts as activity even if weights are already hot.
        self.mark_active()
        with self._lock:
            if self._warmed or self._warming:
                return
            if self._state not in (STATE_READY, STATE_TRANSCRIBING):
                return
            self._warming = True

        self._mlx.submit(self._do_warmup)

    def _do_warmup(self):
        try:
            import mlx.core as mx
            from mlx_whisper.transcribe import ModelHolder
            t0 = time.time()
            ModelHolder.get_model(self._repo, dtype=mx.float16)
            self._warmed = True
            logger.info(
                f"Warmup: weights loaded in {time.time() - t0:.1f}s "
                f"({self._repo})"
            )
        except Exception:
            logger.exception("Warmup failed")
        finally:
            self._warming = False
        # Weights are hot now — arm the idle-unload countdown.
        self._reset_idle_timer()

    # ── Idle auto-unload ─────────────────────────────────────────────────

    def set_idle_unload_minutes(self, minutes):
        """Reconfigure the idle-unload window (0 disables). Re-arms the timer."""
        self._idle_unload_seconds = max(0, int(minutes)) * 60
        if self._idle_unload_seconds:
            self._reset_idle_timer()
        else:
            self._cancel_idle_timer()

    def mark_active(self):
        """Reset the idle-unload countdown — call on every transcribe/warmup."""
        if self._idle_unload_seconds:
            self._reset_idle_timer()

    def _reset_idle_timer(self):
        if not self._idle_unload_seconds:
            return
        with self._lock:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
            self._idle_timer = threading.Timer(
                self._idle_unload_seconds, self._idle_unload,
            )
            self._idle_timer.daemon = True
            self._idle_timer.start()

    def _cancel_idle_timer(self):
        with self._lock:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None

    def _idle_unload(self):
        """Free model weights after the idle window elapses.

        Runs the clear on the single MLX worker thread, which serialises with
        transcribe/warmup — so it can never tear down weights mid-call. State
        stays READY (files remain on disk); next dictation re-warms in ~3s.
        Live mode keeps this from firing mid-recording because each poll
        transcribe resets the timer well inside the idle window.
        """
        with self._lock:
            if self._state != STATE_READY:
                return
            if not self._warmed and not self._warming:
                return  # nothing loaded to free
        try:
            self._mlx.submit(_clear_mlx_caches).result(timeout=10)
        except Exception:
            logger.exception("Idle auto-unload: cache clear failed")
            return
        gc.collect()
        self._warmed = False
        self._warming = False
        logger.info(
            f"Idle auto-unload: freed {self._repo} weights after "
            f"{self._idle_unload_seconds // 60} min idle"
        )

    # ── Transcription ────────────────────────────────────────────────────

    def transcribe(self, audio_int16, sample_rate=16000, *, inline=False):
        """Synchronous. Returns text or None on error / timeout.

        `inline=True` skips the READY → TRANSCRIBING → READY state
        transitions — used by live-mode polling so the menu bar icon stays
        on the "Recording" animation throughout instead of flickering to
        "Processing" every poll.
        """
        # Any transcribe (including live-mode polls) resets the idle countdown.
        self.mark_active()
        # If we're still downloading/loading, block briefly for first-run UX.
        if self._state != STATE_READY:
            if self._state in (STATE_IDLE, STATE_ERROR):
                # Nothing in flight; kick off and wait.
                self.ensure_loaded(blocking=False)
            if not self._ready_event.wait(timeout=120):
                logger.warning(
                    f"Engine not ready after 120s wait (state={self._state})"
                )
                return None

        # Inline (live) polls skip if inference is already busy — never queue.
        # Non-inline (finalize / one-shot) waits its turn.
        if inline:
            if not self._infer_lock.acquire(blocking=False):
                logger.debug("live transcribe skipped: inference busy")
                return None
        else:
            self._infer_lock.acquire()

        if not inline:
            self._set_state(STATE_TRANSCRIBING)
        try:
            text = self._do_transcribe(audio_int16, sample_rate)
            if not inline:
                self._set_state(STATE_READY)
            return text
        except Exception as e:
            logger.exception("Transcription failed")
            if not inline:
                self._set_state(STATE_ERROR, str(e))
            return None
        finally:
            self._infer_lock.release()

    def _do_transcribe(self, audio_int16, sample_rate):
        """Heavy lifting — runs in caller thread."""
        # Convert int16 → float32 in [-1, 1]
        audio_f32 = audio_int16.astype(np.float32) / 32768.0

        if sample_rate != 16000:
            try:
                import scipy.signal as sps
                audio_f32 = sps.resample_poly(audio_f32, 16000, sample_rate)
            except Exception as e:
                logger.error(f"Resample failed: {e}, sending raw")

        kwargs = {
            "path_or_hf_repo": self._repo,
            "fp16": True,
            # Greedy decoding only. The default temperature fallback chain
            # (0.0, 0.2, …, 1.0) makes mlx_whisper re-sample whenever the
            # compression_ratio or logprob thresholds trip, which during a
            # streaming session means consecutive polls on the *same* audio
            # prefix produce *different* texts. That's catastrophic for our
            # longest-common-prefix stability rule: one poll says "доктор",
            # the next "октор", LCP truncates to before "д/о", and the
            # follow-up polls converge on the wrong variant.
            #
            # Pinning to a single greedy temperature. A tuple fallback chain
            # in mlx_whisper interacts badly with `no_speech_threshold` and
            # `logprob_threshold`: on the real mic stream (less clean than
            # the pre-recorded test set) the threshold trips, the model
            # falls back through the chain, and every segment ends up
            # discarded — transcribe() returns an empty string. A single
            # temperature=0.0 yields deterministic decoding without that
            # gating chain; the repetition-loop detector in `_sanitize`
            # handles the rare runaway "слово слово слово…" tail.
            "temperature": 0.0,
        }
        if self.language:
            kwargs["language"] = self.language

        # Run on the dedicated MLX worker so the GPU stream matches the
        # thread that loaded the weights (warmup or a prior transcribe).
        def _work():
            import mlx_whisper
            return mlx_whisper.transcribe(audio_f32, **kwargs)

        dur = len(audio_f32) / 16000.0
        was_warm = self._warmed  # False = this call pays the cold weight load
        t0 = time.time()
        result = self._mlx.submit(_work).result(timeout=120)
        infer = time.time() - t0
        # Weights are resident now (mlx_whisper loads them inside transcribe on
        # a cold call) — record it for the idle-unload gate and future timing.
        self._warmed = True
        text = _sanitize(result.get("text", "").strip())
        speed = dur / infer if infer > 0 else 0.0
        logger.info(
            "transcribe: audio=%.1fs warm=%s infer=%.2fs (%.1fx realtime) "
            "→ %d chars", dur, was_warm, infer, speed, len(text),
        )
        return text

    # ── Hot swap ─────────────────────────────────────────────────────────

    def switch_model(self, new_name):
        """Tear down current model and load a new one (in background)."""
        if new_name == self._model_name:
            logger.info(f"switch_model: already on {new_name}, ignoring")
            return

        if new_name not in MODELS:
            logger.error(f"Unknown model {new_name!r}")
            return

        logger.info(f"Switching model: {self._model_name} → {new_name}")
        self._stop_progress_poll()
        self._cancel_idle_timer()

        # Drop the module-level caches that hold the model reference so
        # RAM/VRAM is freed. Must run on the MLX worker thread so the
        # model's GPU stream is released cleanly.
        try:
            self._mlx.submit(_clear_mlx_caches).result(timeout=10)
        except Exception:
            logger.exception("switch_model: cache clear submit failed")
        gc.collect()

        self._warmed = False
        self._warming = False

        self._model_name = new_name
        self._repo, self._size_mb = MODELS[new_name]
        self._download_progress = 0.0
        self._set_state(STATE_IDLE)

        self.ensure_loaded(blocking=False)

    def shutdown(self):
        self._stop_progress_poll()
        self._cancel_idle_timer()

        try:
            self._mlx.submit(_clear_mlx_caches).result(timeout=5)
        except Exception:
            pass
        self._mlx.shutdown()
