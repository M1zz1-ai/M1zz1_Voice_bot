"""
Звуковая обратная связь через macOS system sounds.
"""

import logging

import AppKit

logger = logging.getLogger("voicebot.sounds")

SOUNDS = {
    "start":   "Tink",    # Start recording
    "stop":    "Pop",     # Stop recording
    "success": "Glass",   # Transcription done
    "error":   "Basso",   # Error
}


class SoundPlayer:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self._cache = {}

    def play(self, event):
        if not self.enabled:
            return

        sound_name = SOUNDS.get(event)
        if not sound_name:
            return

        try:
            if sound_name not in self._cache:
                sound = AppKit.NSSound.soundNamed_(sound_name)
                if sound:
                    self._cache[sound_name] = sound
                else:
                    logger.debug(f"Sound not found: {sound_name}")
                    return

            self._cache[sound_name].stop()
            self._cache[sound_name].play()
        except Exception as e:
            logger.debug(f"Sound failed: {e}")
