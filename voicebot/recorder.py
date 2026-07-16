"""
Out-of-process audio capture.

The parent (app) process must NEVER talk to CoreAudio. On macOS 26 a stop that
wedges inside PortAudio's teardown (AudioOutputUnitStop vs the CoreAudio IO
thread's startStopCallback) poisons the whole process: the zombie teardown
thread holds the process-side HAL mutex, so every subsequent HAL call —
including a fresh `sd.InputStream(...)` — blocks forever. Disposable recorders
can't help; only process death makes coreaudiod release the device.

So a small capture-helper CHILD process owns the InputStream and streams raw
int16 frames back over a pipe. The parent accumulates them into `_frames`
exactly as before; snapshot()/live mode read the parent-side buffer, and
LiveTranscriber + Whisper stay in-parent, unchanged.

stop() asks the child to stop and, if it doesn't ACK within a couple seconds,
SIGKILLs it — kernel teardown forces coreaudiod to release the device. That's
the only reliable unwedger, and it's harmless because the parent already holds
every streamed frame. Because the parent never opens a stream, its HAL state is
never poisoned and the next recording spawns a fresh, clean child.

`sounddevice` is imported only inside the child code path — the parent process
never links PortAudio through this module.
"""

import logging
import multiprocessing
import threading

import numpy as np

logger = logging.getLogger("voicebot.recorder")


# Names we never want to pick as a microphone, even if they're flagged as
# default — these are loopback / virtual-output devices that some apps
# register so they can hand audio off (Virtual Desktop, BlackHole, Soundflower,
# aggregate sinks). With Whisper, recording from them yields silence and
# every transcribe comes back empty.
_SUSPICIOUS_INPUT_HINTS = (
    "speakers", "output", "loopback", "blackhole", "soundflower",
    "aggregate", "monitor",
)

# Hints that strongly suggest a real on-device microphone we should prefer
# when the system default is suspicious.
_BUILTIN_INPUT_HINTS = ("macbook", "built-in", "imac", "mac mini", "mac studio")


def _is_real_mic(device):
    if device["max_input_channels"] <= 0:
        return False
    name = device["name"].lower()
    return not any(h in name for h in _SUSPICIOUS_INPUT_HINTS)


def pick_input_device():
    """Resolve the default input to a physical microphone. CHILD-ONLY: imports
    sounddevice, so it must never run in the parent process.

    Falls through three tiers: (1) keep the system default if it's a real
    mic; (2) otherwise prefer a built-in Mac microphone; (3) otherwise the
    first non-suspicious input device. Returns `(index, name)` or
    `(None, None)` if nothing usable was found.
    """
    import sounddevice as sd

    try:
        devices = sd.query_devices()
        default = sd.default.device
        default_idx = default[0] if isinstance(default, (tuple, list)) else default
        if not isinstance(default_idx, int):
            default_idx = -1
    except Exception as e:
        logger.warning(f"sounddevice query failed: {e}")
        return None, None

    if 0 <= default_idx < len(devices) and _is_real_mic(devices[default_idx]):
        return default_idx, devices[default_idx]["name"]

    for i, d in enumerate(devices):
        name_lower = d["name"].lower()
        if _is_real_mic(d) and any(h in name_lower for h in _BUILTIN_INPUT_HINTS):
            return i, d["name"]

    for i, d in enumerate(devices):
        if _is_real_mic(d):
            return i, d["name"]

    if 0 <= default_idx < len(devices):
        # Nothing clean; surface the default so the user at least sees
        # *something* in logs even if it'll yield silence.
        return default_idx, devices[default_idx]["name"]
    return None, None


# ── Child-process capture worker ────────────────────────────────────────────
#
# Wire protocol (child → parent, length-prefixed via Connection.send_bytes):
#   b"R" + <device name>   stream opened, capture live
#   b"D" + <int16 bytes>   one audio frame
#   b"E" + <message>       failed to open the device
#   b"X"                   stopped cleanly (ACK)
# Parent → child:
#   b"S"                   please stop

def _capture_worker(conn, sample_rate, channels, blocksize):
    """Runs in the CHILD process. Owns the InputStream; streams frames to the
    parent until told to stop. Never trusted to shut CoreAudio down cleanly —
    the parent SIGKILLs it if teardown wedges."""
    import sounddevice as sd

    try:
        device_idx, device_name = pick_input_device()

        def _callback(indata, frames, time_info, status):
            try:
                conn.send_bytes(b"D" + indata.tobytes())
            except Exception:
                pass

        stream = sd.InputStream(
            device=device_idx,
            samplerate=sample_rate,
            channels=channels,
            dtype="int16",
            blocksize=blocksize,
            callback=_callback,
        )
        stream.start()
        conn.send_bytes(b"R" + (device_name or "").encode("utf-8", "replace"))
    except Exception as e:
        try:
            conn.send_bytes(b"E" + str(e).encode("utf-8", "replace"))
        except Exception:
            pass
        return

    # Wait for the parent's stop command, then tear the stream down. If abort()
    # wedges here the parent's SIGKILL is the backstop — that's the whole point
    # of running capture out of process.
    try:
        while True:
            if conn.poll(0.2):
                if conn.recv_bytes()[:1] == b"S":
                    break
    except Exception:
        pass

    try:
        stream.abort()
        stream.close()
    except Exception:
        pass
    try:
        conn.send_bytes(b"X")
    except Exception:
        pass


class AudioRecorder:
    """Supervises an out-of-process capture child. Public API is unchanged from
    the old in-process recorder (start/stop/snapshot/is_recording)."""

    # Max wait for the child to confirm the mic opened. A failed/slow start
    # returns False in this bound so the app can flash an error instead of
    # hanging silently.
    _START_TIMEOUT = 3.0
    # Max wait for the child to ACK a stop before we SIGKILL it.
    _STOP_TIMEOUT = 2.0

    def __init__(self, sample_rate=16000, channels=1, max_duration=300,
                 on_auto_stop=None, worker=_capture_worker):
        self.sample_rate = sample_rate
        self.channels = channels
        self.max_duration = max_duration
        self.on_auto_stop = on_auto_stop  # callback when max duration reached
        self._worker = worker  # injectable for tests (fake, no real mic)
        self._blocksize = max(1, int(sample_rate * 0.1))  # ~100 ms frames

        # Frames appended by the pipe-reader thread; snapshot/stop copy the
        # list under `_frames_lock`.
        self._frames = []
        self._frames_lock = threading.Lock()

        # Parent-side supervisor state. `_state_lock` serialises start/stop
        # transitions; it is NEVER held across a CoreAudio call (the parent
        # makes none), so it cannot wedge.
        self._state_lock = threading.Lock()
        self._recording = False
        self._dead = False

        self._child = None
        self._parent_conn = None
        self._reader_thread = None
        self._auto_stop_timer = None

        self._ready_event = threading.Event()
        self._stopped_ack = threading.Event()
        self._start_error = None
        self._device_name = None

    @property
    def is_recording(self):
        return self._recording

    @property
    def is_dead(self):
        return self._dead

    def mark_dead(self):
        """Flag this recorder as unusable. Retained for app compatibility; with
        out-of-process capture the parent never wedges, so this is rarely
        needed — but a fresh instance is always cheap and safe."""
        if not self._dead:
            self._dead = True
            logger.warning("AudioRecorder marked dead — must be replaced")

    def start(self):
        """Spawn a capture child and wait for it to confirm the mic opened.
        Returns True on success, False (fast, bounded) on any failure."""
        if self._dead:
            logger.error("start() on a dead recorder — needs replacement")
            return False

        with self._state_lock:
            if self._recording:
                logger.warning("Already recording")
                return False

            with self._frames_lock:
                self._frames = []
            self._ready_event.clear()
            self._stopped_ack.clear()
            self._start_error = None
            self._device_name = None

            ctx = multiprocessing.get_context("spawn")
            self._parent_conn, child_conn = ctx.Pipe()
            self._child = ctx.Process(
                target=self._worker,
                args=(child_conn, self.sample_rate, self.channels,
                      self._blocksize),
                daemon=True,
            )
            self._child.start()
            child_conn.close()  # parent keeps only its end

            self._reader_thread = threading.Thread(
                target=self._reader, args=(self._parent_conn,),
                daemon=True, name="capture-reader",
            )
            self._reader_thread.start()

            if not self._ready_event.wait(timeout=self._START_TIMEOUT):
                logger.error(
                    "capture child did not report ready within %.0fs — "
                    "killing it", self._START_TIMEOUT,
                )
                self._kill_child()
                return False
            if self._start_error:
                logger.error(
                    f"capture child failed to open mic: {self._start_error}"
                )
                self._kill_child()
                return False

            self._recording = True

            self._auto_stop_timer = threading.Timer(
                self.max_duration, self._auto_stop,
            )
            self._auto_stop_timer.daemon = True
            self._auto_stop_timer.start()

            logger.info(
                f"Recording started (child pid={self._child.pid}, "
                f"device={self._device_name!r})"
            )
            return True

    def stop(self):
        """Stop capture and return the audio as numpy int16, or None.

        Sends a stop command; if the child doesn't ACK within `_STOP_TIMEOUT`
        it is SIGKILLed. Either way the parent already holds every frame, so no
        audio is lost."""
        with self._state_lock:
            if not self._recording:
                return None
            self._recording = False

            if self._auto_stop_timer:
                self._auto_stop_timer.cancel()
                self._auto_stop_timer = None

            self._stopped_ack.clear()
            try:
                if self._parent_conn is not None:
                    self._parent_conn.send_bytes(b"S")
            except Exception:
                pass

            if not self._stopped_ack.wait(timeout=self._STOP_TIMEOUT):
                logger.error(
                    "capture child did not ACK stop within %.0fs — SIGKILL",
                    self._STOP_TIMEOUT,
                )
            # Always ensure the child is gone (SIGKILL if still alive): kernel
            # teardown is what makes coreaudiod release the device.
            self._kill_child()

            with self._frames_lock:
                frames = self._frames
                self._frames = []

            if not frames:
                logger.warning("No audio frames captured")
                return None

            audio = np.concatenate(frames, axis=0).flatten()
            duration = len(audio) / self.sample_rate
            logger.info(f"Captured {duration:.1f}s ({len(frames)} chunks)")
            return audio

    def snapshot(self):
        """Return audio captured so far, without stopping. Reads the
        parent-side buffer — no CoreAudio involved."""
        if not self._recording:
            return None
        with self._frames_lock:
            frames = list(self._frames)
        if not frames:
            return None
        return np.concatenate(frames, axis=0).flatten()

    # ── Internals ──────────────────────────────────────────────────────────

    def _reader(self, conn):
        """Drain frames/control messages from the child until the pipe closes
        (child exited or was killed)."""
        while True:
            try:
                msg = conn.recv_bytes()
            except (EOFError, OSError):
                break
            if not msg:
                continue
            tag = msg[:1]
            if tag == b"D":
                frame = np.frombuffer(msg[1:], dtype=np.int16)
                with self._frames_lock:
                    self._frames.append(frame)
            elif tag == b"R":
                self._device_name = msg[1:].decode("utf-8", "replace")
                self._ready_event.set()
            elif tag == b"E":
                self._start_error = msg[1:].decode("utf-8", "replace")
                self._ready_event.set()
            elif tag == b"X":
                self._stopped_ack.set()

    def _kill_child(self):
        child = self._child
        try:
            if child is not None and child.is_alive():
                child.kill()  # SIGKILL — the only reliable coreaudiod unwedger
            if child is not None:
                child.join(timeout=2)
        except Exception:
            pass
        try:
            if self._parent_conn is not None:
                self._parent_conn.close()
        except Exception:
            pass
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1)
        self._child = None
        self._parent_conn = None
        self._reader_thread = None

    def _auto_stop(self):
        """Called when max_duration reached."""
        logger.warning(f"Max duration ({self.max_duration}s) reached, auto-stopping")
        audio = self.stop()
        if self.on_auto_stop and audio is not None:
            self.on_auto_stop(audio)
