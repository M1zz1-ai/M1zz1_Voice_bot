"""
Thin adapter — delegates to WhisperEngine.

Kept as a separate module so app.py doesn't have to know about MLX details.
Signature `transcribe(audio_data, sample_rate)` is preserved from the
previous n8n-based version, so app.py needs no changes to its call sites.
"""

import logging

logger = logging.getLogger("voicebot.transcriber")


class Transcriber:
    def __init__(self, engine, language="ru"):
        self._engine = engine
        self.language = language
        # Keep language in sync with the engine so the user's setting wins.
        engine.language = None if language == "auto" else language

    def transcribe(self, audio_data, sample_rate=16000):
        """Return transcribed text, or None on error / not ready."""
        try:
            return self._engine.transcribe(audio_data, sample_rate)
        except Exception:
            logger.exception("Transcription failed")
            return None
