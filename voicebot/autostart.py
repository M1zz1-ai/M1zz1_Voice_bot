"""
Start-at-login via a per-user LaunchAgent.

The agent lives at ~/Library/LaunchAgents/com.mizz.voicebot.plist and launches
the installed /Applications/VoiceBot.app on login. `KeepAlive` is deliberately
FALSE — a KeepAlive agent relaunching alongside a manual launch was the old
"two menu-bar icons" bug; the single-instance guard in main.py is only a
backstop, not the mechanism.

Enable/disable is driven by the "Start at login" checkbox in Settings:
  enable()  → write the plist (paths resolved for THIS user, no hardcoding),
              then `launchctl load`
  disable() → `launchctl unload` + remove the plist
Quitting from the menu calls unload_session() to stop the loaded agent for the
current session while LEAVING the plist in place, so login autostart survives.
"""

import logging
import os
import subprocess

logger = logging.getLogger("voicebot.autostart")

LABEL = "com.mizz.voicebot"
PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
APP_BINARY = "/Applications/VoiceBot.app/Contents/MacOS/VoiceBot"
LOG_DIR = os.path.expanduser("~/.voicebot/logs")

# Paths are filled in at write time with the current user's real locations, so
# nothing user-specific is ever hardcoded in the shipped source.
_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{binary}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <false/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>{log_dir}/launchd_stdout.log</string>
  <key>StandardErrorPath</key>
  <string>{log_dir}/launchd_stderr.log</string>
</dict>
</plist>
"""


def is_enabled():
    """True if the login LaunchAgent is installed."""
    return os.path.exists(PLIST_PATH)


def enable():
    """Write the LaunchAgent and load it. Idempotent."""
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(PLIST_PATH, "w") as f:
        f.write(_PLIST_TEMPLATE.format(
            label=LABEL, binary=APP_BINARY, log_dir=LOG_DIR,
        ))
    # Reload cleanly in case an old copy was already loaded.
    subprocess.run(["launchctl", "unload", PLIST_PATH],
                   check=False, capture_output=True)
    subprocess.run(["launchctl", "load", PLIST_PATH],
                   check=False, capture_output=True)
    logger.info("Start-at-login enabled: %s", PLIST_PATH)


def disable():
    """Unload and remove the LaunchAgent."""
    if os.path.exists(PLIST_PATH):
        subprocess.run(["launchctl", "unload", PLIST_PATH],
                       check=False, capture_output=True)
        try:
            os.remove(PLIST_PATH)
        except OSError:
            logger.exception("Failed to remove %s", PLIST_PATH)
    logger.info("Start-at-login disabled")


def set_enabled(flag):
    if flag:
        enable()
    else:
        disable()


def unload_session():
    """Stop the loaded agent for this session WITHOUT removing the plist.

    Called on Quit so the app doesn't get relaunched during the session, while
    the login autostart (RunAtLoad) still fires next login.
    """
    if os.path.exists(PLIST_PATH):
        subprocess.run(["launchctl", "unload", PLIST_PATH],
                       check=False, capture_output=True)
