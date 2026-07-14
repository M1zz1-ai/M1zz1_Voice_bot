"""
Thread-safe аудиозапись с поддержкой неразрушающего snapshot для live-режима.
"""

import logging
import threading

import numpy as np
import sounddevice as sd

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
    """Resolve the default input to a physical microphone.

    Falls through three tiers: (1) keep the system default if it's a real
    mic; (2) otherwise prefer a built-in Mac microphone; (3) otherwise the
    first non-suspicious input device. Returns `(index, name)` or
    `(None, None)` if nothing usable was found.
    """
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


class AudioRecorder:
    def __init__(self, sample_rate=16000, channels=1, max_duration=300,
                 on_auto_stop=None):
        self.sample_rate = sample_rate
        self.channels = channels
        self.max_duration = max_duration
        self.on_auto_stop = on_auto_stop  # callback when max duration reached

        # Frames are appended by the PortAudio callback thread; the snapshot
        # / stop reader copies the list under `_frames_lock`. We can't use a
        # plain queue.Queue because live mode needs to peek (read without
        # consuming) while recording continues.
        self._frames = []
        self._frames_lock = threading.Lock()
        self._stream = None
        self._recording = False
        self._lock = threading.Lock()
        self._auto_stop_timer = None

    @property
    def is_recording(self):
        return self._recording

    def start(self):
        """Start recording. Returns True on success."""
        with self._lock:
            if self._recording:
                logger.warning("Already recording")
                return False

            try:
                with self._frames_lock:
                    self._frames = []

                # Pick a physical mic ourselves rather than trusting
                # sd.default — some apps (Virtual Desktop, Loopback, etc.)
                # hijack the default with a silent output device.
                device_idx, device_name = pick_input_device()
                if device_idx is not None:
                    logger.info(
                        f"Input device: [{device_idx}] {device_name!r}"
                    )
                self._stream = sd.InputStream(
                    device=device_idx,
                    samplerate=self.sample_rate,
                    channels=self.channels,
                    dtype="int16",
                    callback=self._callback,
                )
                self._stream.start()
                self._recording = True

                # Auto-stop timer
                self._auto_stop_timer = threading.Timer(
                    self.max_duration, self._auto_stop,
                )
                self._auto_stop_timer.daemon = True
                self._auto_stop_timer.start()

                logger.info("Recording started")
                return True

            except sd.PortAudioError as e:
                logger.error(f"PortAudio error: {e}")
                return False
            except Exception as e:
                logger.error(f"Recording start failed: {e}")
                return False

    def stop(self):
        """Stop recording and return audio as numpy int16 array, or None."""
        with self._lock:
            if not self._recording:
                return None

            self._recording = False

            if self._auto_stop_timer:
                self._auto_stop_timer.cancel()
                self._auto_stop_timer = None

            if self._stream:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception as e:
                    logger.error(f"Stream close error: {e}")
                finally:
                    self._stream = None

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
        """Return audio captured so far, without stopping the stream.

        Used by live-mode transcription to re-run Whisper on the growing
        buffer every poll interval. Returns None until at least one chunk
        has been captured.
        """
        if not self._recording:
            return None
        with self._frames_lock:
            frames = list(self._frames)
        if not frames:
            return None
        return np.concatenate(frames, axis=0).flatten()

    def _callback(self, indata, frames, time_info, status):
        if status:
            logger.warning(f"Audio callback status: {status}")
        if self._recording:
            with self._frames_lock:
                self._frames.append(indata.copy())

    def _auto_stop(self):
        """Called when max_duration reached."""
        logger.warning(f"Max duration ({self.max_duration}s) reached, auto-stopping")
        audio = self.stop()
        if self.on_auto_stop and audio is not None:
            self.on_auto_stop(audio)
