"""
Detached self-relaunch for the Restart menu action.

`open -b <bundle>` on the SAME bundle while the old instance is still alive just
focuses it — it won't spawn a fresh process. So we hand the relaunch to a
detached shell that sleeps briefly (letting the old process exit first) and only
then runs `open`. `start_new_session=True` detaches it from the dying app so it
survives our own termination.
"""

import subprocess

BUNDLE_ID = "com.mizz.voicebot"


def restart_command(bundle_id=BUNDLE_ID):
    """The detached relaunch command (list form, for Popen)."""
    return ["sh", "-c", f"sleep 1; open -b {bundle_id}"]


def relaunch_detached(bundle_id=BUNDLE_ID):
    """Spawn the detached relauncher. Call this, then quit the app."""
    subprocess.Popen(restart_command(bundle_id), start_new_session=True)
