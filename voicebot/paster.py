"""
Вставка текста через NSPasteboard.

The transcription is always LEFT in the clipboard after a dictation: auto-paste
attempts a ⌘V, but when no input field has focus the paste is a no-op and the
user can ⌘V the text manually later. (We used to restore the previous clipboard
after 0.8s, which silently lost the transcription when nothing was focused.)
"""

import logging
import subprocess
import time

import AppKit
import Quartz

logger = logging.getLogger("voicebot.paster")

# Physical virtual keycode of the V key on ANSI hardware. CGEventPost takes a
# raw VK, not a character, so this works under ANY active keyboard layout
# (Russian, Greek, Dvorak, …). The character-based `osascript keystroke "v"`
# we used before re-mapped through the active input source and produced the
# wrong event on Cyrillic layouts.
_VK_V = 9
_VK_BACKSPACE = 51  # Mac "delete" key — destructive backspace, not fn+delete


class TextPaster:
    def __init__(self, auto_paste=True):
        self.auto_paste = auto_paste
        self.last_text = ""
        self._pb = AppKit.NSPasteboard.generalPasteboard()

    def paste(self, text):
        """Copy text to the clipboard and, if auto_paste is on, paste it.

        The text is left in the clipboard afterwards (no restore) so it's
        always available for a manual ⌘V when auto-paste had nowhere to land.
        """
        self.last_text = text
        self._set_clipboard(text)

        if self.auto_paste:
            time.sleep(0.05)
            self._simulate_paste()

        logger.info(f"Pasted: {text[:60]}...")

    def copy(self, text):
        """Put text on the clipboard WITHOUT pasting it.

        The ⌘V recovery path: used at the end of every dictation (and for
        silent output mode) so the full transcription is always available even
        when nothing was typed into a field.
        """
        self.last_text = text
        self._set_clipboard(text)
        logger.info(f"Copied to clipboard: {text[:60]}...")

    def _set_clipboard(self, text):
        try:
            self._pb.clearContents()
            self._pb.setString_forType_(text, AppKit.NSPasteboardTypeString)
        except Exception as e:
            logger.warning(f"NSPasteboard failed, fallback to pbcopy: {e}")
            proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            proc.communicate(text.encode("utf-8"))

    @staticmethod
    def _simulate_paste():
        """Synthesize ⌘V at the HID layer via CGEventPost.

        Bypasses System Events / the active keyboard input source entirely,
        so paste works the same on US, Russian, etc. Setting only the
        Command flag (no carryover Shift from the hotkey) keeps target apps
        from treating it as ⌘⇧V (paste-and-match-style or worse).
        """
        try:
            src = Quartz.CGEventSourceCreate(
                Quartz.kCGEventSourceStateHIDSystemState
            )
            down = Quartz.CGEventCreateKeyboardEvent(src, _VK_V, True)
            Quartz.CGEventSetFlags(down, Quartz.kCGEventFlagMaskCommand)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)

            up = Quartz.CGEventCreateKeyboardEvent(src, _VK_V, False)
            Quartz.CGEventSetFlags(up, Quartz.kCGEventFlagMaskCommand)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
        except Exception as e:
            logger.error(f"CGEventPost paste failed: {e}")

    def backspace(self, n, char_delay=0.004):
        """Press Backspace `n` times via CGEvent.

        Used by live finalize to roll back commits whose middle Whisper later
        revised, before retyping the corrected tail. Small per-key delay so
        target apps don't drop events during a burst.
        """
        if n <= 0:
            return
        try:
            src = Quartz.CGEventSourceCreate(
                Quartz.kCGEventSourceStateHIDSystemState
            )
        except Exception as e:
            logger.error(f"backspace: source create failed: {e}")
            return

        for _ in range(n):
            try:
                down = Quartz.CGEventCreateKeyboardEvent(src, _VK_BACKSPACE, True)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
                up = Quartz.CGEventCreateKeyboardEvent(src, _VK_BACKSPACE, False)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
            except Exception as e:
                logger.error(f"backspace step failed: {e}")
                return
            if char_delay > 0:
                time.sleep(char_delay)

    def type_text(self, text, char_delay=0.012):
        """Type `text` character-by-character via Unicode key events.

        Used by live-mode commits: no clipboard interaction (so the user's
        clipboard is never touched), independent of active keyboard layout
        (Unicode is attached straight to the key event), and produces a
        gentle typewriter effect at ~80 chars/s.

        Skips non-BMP characters (emoji surrogate pairs) — Whisper output
        is plain text so this isn't normally hit.
        """
        if not text:
            return
        try:
            src = Quartz.CGEventSourceCreate(
                Quartz.kCGEventSourceStateHIDSystemState
            )
        except Exception as e:
            logger.error(f"type_text: source create failed: {e}")
            return

        for ch in text:
            try:
                down = Quartz.CGEventCreateKeyboardEvent(src, 0, True)
                Quartz.CGEventKeyboardSetUnicodeString(down, len(ch), ch)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)

                up = Quartz.CGEventCreateKeyboardEvent(src, 0, False)
                Quartz.CGEventKeyboardSetUnicodeString(up, len(ch), ch)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
            except Exception as e:
                logger.error(f"type_text: char {ch!r} failed: {e}")
                continue
            if char_delay > 0:
                time.sleep(char_delay)

        self.last_text = (self.last_text or "") + text
        logger.info(f"Typed: {text[:60]}...")
