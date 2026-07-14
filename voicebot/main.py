#!/usr/bin/env python3
"""
VoiceBot entry point.
Run: python3 main.py  OR  via VoiceBot.app bundle
"""

import os
import sys

# When running inside a PyInstaller .app bundle, sys._MEIPASS points to the
# unpacked resources directory. Add it to path so local modules are found.
_bundle_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _bundle_dir)

from config import Config
from logger_setup import setup_logger
from permissions import check_accessibility, check_microphone, show_alert

BUNDLE_ID = "com.mizz.voicebot"


def _another_instance_running():
    """True if another VoiceBot (same bundle id) is already running.

    Uses NSRunningApplication so it works for the .app bundle regardless of
    how the duplicate was launched (Finder, LaunchAgent, or a leftover
    dist/VoiceBot.app). When run from source there is no registered bundle id,
    so this returns False and never false-positives on `python main.py`.
    """
    try:
        from AppKit import NSRunningApplication
        me = NSRunningApplication.currentApplication().processIdentifier()
        others = [
            a for a in
            NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE_ID)
            if a.processIdentifier() != me
        ]
        return len(others) > 0
    except Exception:
        # Never block startup on a guard failure.
        return False


def _migrate_legacy_env():
    """Remove a stale ~/.voicebot/.env left over from an earlier version."""
    legacy = os.path.expanduser("~/.voicebot/.env")
    if os.path.exists(legacy):
        try:
            os.remove(legacy)
        except OSError:
            pass


def _demo_overlay():
    """Dev smoke test: show the screen-top glow overlay for 5s, then exit."""
    from logger_setup import setup_logger
    logger = setup_logger()
    logger.info("Running overlay demo (recording state, 5s)...")
    from overlay import demo
    demo(5)
    logger.info("Overlay demo finished")


def main():
    if "--demo-overlay" in sys.argv:
        _demo_overlay()
        return

    log_dir = os.path.expanduser("~/.voicebot/logs")
    os.makedirs(log_dir, exist_ok=True)

    # Redirect stdout/stderr only when run as a daemon (no TTY).
    # In terminal — leave attached so errors are visible immediately.
    if not (sys.stdout and sys.stdout.isatty()):
        sys.stdout = open(os.path.join(log_dir, "voicebot_stdout.log"), "a", buffering=1)
        sys.stderr = open(os.path.join(log_dir, "voicebot_stderr.log"), "a", buffering=1)

    logger = setup_logger()
    logger.info("=" * 50)
    logger.info("VoiceBot starting...")

    # Single-instance guard — refuse to launch a second copy (avoids the
    # double menu-bar icon when a LaunchAgent and a manual launch collide).
    if _another_instance_running():
        logger.warning(
            "Another VoiceBot instance is already running — exiting."
        )
        sys.exit(0)

    _migrate_legacy_env()

    # Load config
    cfg = Config()
    cfg.save()  # Write defaults if first run
    logger.info(f"Config: {cfg.config_file}")
    logger.info(f"Whisper model: {cfg['whisper_model']}")

    # Check permissions
    if not check_microphone():
        show_alert(
            "VoiceBot — Микрофон",
            "Нет доступа к микрофону.\n\n"
            "Системные настройки → Конфиденциальность и безопасность → Микрофон\n"
            "Включите тумблер рядом с VoiceBot."
        )
        logger.error("Microphone access denied")
        sys.exit(1)

    if not check_accessibility():
        show_alert(
            "VoiceBot — Универсальный доступ",
            "Нет прав для перехвата горячих клавиш.\n\n"
            "Системные настройки → Конфиденциальность и безопасность → Универсальный доступ\n\n"
            "Удалите VoiceBot из списка (кнопка '-'), добавьте заново ('+') и перезапустите."
        )
        logger.error("Accessibility access denied")
        sys.exit(1)

    # Pre-generate animation frames
    logger.info("Pre-generating animations...")
    from animations import AnimationGenerator
    anim = AnimationGenerator()
    for anim_type in ("recording", "processing", "success", "error"):
        frames = anim.get_frames(anim_type)
        logger.info(f"  {anim_type}: {len(frames)} frames")

    # Run
    logger.info("Starting menu bar app...")
    from app import VoiceBot
    VoiceBot(cfg).run()


if __name__ == "__main__":
    # On macOS, Python's default 'spawn' start method re-execs the frozen
    # binary for each multiprocessing child. Without freeze_support(), the
    # child runs main() again — registers a second hotkey, second menu bar
    # icon, etc. PyInstaller's runtime hook needs this call to short-circuit
    # the child before our main() runs. Triggered by numba/scipy/mlx internals
    # during transcription.
    import multiprocessing
    multiprocessing.freeze_support()
    main()
