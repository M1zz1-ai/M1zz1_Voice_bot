"""
Проверка macOS permissions: микрофон + Accessibility.
Пассивные probes, без программных prompt'ов (они крашатся на macOS 26
через hand-rolled ctypes). Запросы делает сам macOS когда мы реально
открываем микрофон / регистрируем хоткей.
"""

import ctypes
import logging
import subprocess

logger = logging.getLogger("voicebot.permissions")


def check_microphone():
    try:
        import sounddevice as sd
        with sd.InputStream(samplerate=16000, channels=1, dtype="int16"):
            pass
        logger.info("Microphone access: OK")
        return True
    except Exception as e:
        logger.error(f"Microphone access denied: {e}")
        return False


def check_accessibility():
    """Read-only probe via AXIsProcessTrusted. No system prompt triggered."""
    try:
        app_services = ctypes.cdll.LoadLibrary(
            '/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices'
        )
        app_services.AXIsProcessTrusted.restype = ctypes.c_bool
        ok = bool(app_services.AXIsProcessTrusted())
        logger.info(f"Accessibility access: {'OK' if ok else 'DENIED'}")
        return ok
    except Exception as e:
        logger.error(f"Accessibility check failed: {e}")
        return False


def show_alert(title, message):
    script = (
        'on run argv\n'
        '  display dialog (item 2 of argv) with title (item 1 of argv) '
        'buttons {"OK"} default button "OK"\n'
        'end run'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script, "--", title, message],
            check=False, timeout=15,
        )
    except Exception:
        pass
