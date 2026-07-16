"""
macOS permission probes: microphone (TCC) + Accessibility.

CRITICAL: the parent process must NEVER touch CoreAudio/PortAudio — opening an
InputStream here can wedge inside Pa_OpenStream and hang startup. So the mic
check queries TCC authorization via AVFoundation (AVCaptureDevice) instead of
opening a stream, and the prompt is triggered ASYNCHRONOUSLY. Actual audio
capture happens only in the recorder's child process.

AVFoundation's pyobjc wrapper isn't bundled (only Cocoa + Quartz are), but
pyobjc-core can message any ObjC class once its framework is loaded — so we
dlopen AVFoundation by path and look the class up dynamically.
"""

import ctypes
import logging
import subprocess

logger = logging.getLogger("voicebot.permissions")

_AV_MEDIA_TYPE_AUDIO = "soun"  # AVMediaTypeAudio
# AVAuthorizationStatus: 0 notDetermined, 1 restricted, 2 denied, 3 authorized.
_AV_STATUS = {0: "undetermined", 1: "denied", 2: "denied", 3: "authorized"}


def _map_status(raw):
    return _AV_STATUS.get(int(raw), "unknown")


def _av_capture_device():
    """AVCaptureDevice ObjC class via pyobjc-core — no pyobjc AVFoundation
    wrapper, no audio stream. Loads the system framework by path so the class
    is messageable."""
    import objc
    ctypes.CDLL(
        "/System/Library/Frameworks/AVFoundation.framework/AVFoundation"
    )
    return objc.lookUpClass("AVCaptureDevice")


def microphone_status():
    """TCC mic authorization: 'authorized' | 'denied' | 'undetermined' |
    'unknown'. Pure query — the parent never opens an audio stream."""
    try:
        dev = _av_capture_device()
        return _map_status(dev.authorizationStatusForMediaType_(
            _AV_MEDIA_TYPE_AUDIO))
    except Exception:
        logger.exception("microphone_status query failed")
        return "unknown"


def request_microphone_access(callback=None):
    """Trigger the mic TCC prompt ASYNCHRONOUSLY (never blocks). `callback`, if
    given, is invoked with a bool on completion (on an arbitrary thread)."""
    try:
        dev = _av_capture_device()

        def handler(granted):
            if callback:
                try:
                    callback(bool(granted))
                except Exception:
                    logger.exception("mic access callback raised")

        dev.requestAccessForMediaType_completionHandler_(
            _AV_MEDIA_TYPE_AUDIO, handler)
    except Exception:
        logger.exception("request_microphone_access failed")


def check_microphone():
    """True unless mic access is explicitly denied. Non-blocking, no stream:
    'undetermined' returns True so startup is never gated on a pending prompt."""
    return microphone_status() != "denied"


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
