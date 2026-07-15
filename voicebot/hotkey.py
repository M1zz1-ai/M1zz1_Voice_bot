"""
Carbon RegisterEventHotKey wrapper for global hotkeys on macOS.

Why Carbon and not NSEvent global monitor:
- NSEvent.addGlobalMonitorForEventsMatchingMask_ is a *passive* observer that
  fires AFTER the keystroke is dispatched, so it can be swallowed by the
  focused app or "secure input" mode (terminals, password fields).
- Carbon RegisterEventHotKey installs an OS-level hotkey at the Application
  Event Target; it never collides with focused apps and only needs the
  Accessibility permission (NOT Input Monitoring).
- This is the same path Hammerspoon / Rectangle / Alfred / Raycast use.

The Carbon callback fires on the main AppKit thread because NSApp.run()
drains the same event queue that Carbon dispatches into. Keep handlers
fast — defer heavy work to a thread.
"""

import ctypes
import logging

import AppKit
from ctypes import (
    CFUNCTYPE, POINTER, Structure,
    c_int32, c_uint32, c_uint64, c_void_p,
)

logger = logging.getLogger("voicebot.hotkey")


# ── Carbon API ────────────────────────────────────────────────────────────────
_CARBON_PATH = "/System/Library/Frameworks/Carbon.framework/Carbon"
try:
    _Carbon = ctypes.cdll.LoadLibrary(_CARBON_PATH)
except OSError as e:
    logger.error(f"Failed to load Carbon.framework: {e}")
    _Carbon = None


# Modifier masks (Events.h)
cmdKey     = 1 << 8
shiftKey   = 1 << 9
optionKey  = 1 << 11
controlKey = 1 << 12

# Event class / kind
kEventClassKeyboard     = 0x6B657962  # 'keyb'
kEventHotKeyPressed     = 5
kEventHotKeyReleased    = 6
kEventParamDirectObject = 0x2D2D2D2D  # '----'
typeEventHotKeyID       = 0x686B6964  # 'hkid'

# Status codes
noErr               = 0
eventHotKeyExistsErr = -9878


class _EventHotKeyID(Structure):
    _fields_ = [("signature", c_uint32), ("id", c_uint32)]


class _EventTypeSpec(Structure):
    _fields_ = [("eventClass", c_uint32), ("eventKind", c_uint32)]


# typedef OSStatus (*EventHandlerProcPtr)(EventHandlerCallRef, EventRef, void*)
_EventHandlerProcPtr = CFUNCTYPE(c_int32, c_void_p, c_void_p, c_void_p)


def _setup_carbon_signatures():
    if _Carbon is None:
        return False

    _Carbon.GetApplicationEventTarget.restype = c_void_p
    _Carbon.GetApplicationEventTarget.argtypes = []

    # OSStatus RegisterEventHotKey(UInt32 hotKeyCode, UInt32 hotKeyModifiers,
    #                              EventHotKeyID hotKeyID, EventTargetRef target,
    #                              OptionBits options, EventHotKeyRef *outRef)
    _Carbon.RegisterEventHotKey.restype = c_int32
    _Carbon.RegisterEventHotKey.argtypes = [
        c_uint32, c_uint32, _EventHotKeyID, c_void_p, c_uint32, POINTER(c_void_p),
    ]

    _Carbon.UnregisterEventHotKey.restype = c_int32
    _Carbon.UnregisterEventHotKey.argtypes = [c_void_p]

    # OSStatus InstallEventHandler(EventTargetRef, EventHandlerUPP, ItemCount,
    #                              const EventTypeSpec*, void* userData,
    #                              EventHandlerRef *outRef)
    _Carbon.InstallEventHandler.restype = c_int32
    _Carbon.InstallEventHandler.argtypes = [
        c_void_p, _EventHandlerProcPtr, c_uint32,
        POINTER(_EventTypeSpec), c_void_p, POINTER(c_void_p),
    ]

    _Carbon.RemoveEventHandler.restype = c_int32
    _Carbon.RemoveEventHandler.argtypes = [c_void_p]

    # OSStatus GetEventParameter(EventRef, EventParamName, EventParamType,
    #                            EventParamType *outActualType, ByteCount bufferSize,
    #                            ByteCount *outActualSize, void *outData)
    _Carbon.GetEventParameter.restype = c_int32
    _Carbon.GetEventParameter.argtypes = [
        c_void_p, c_uint32, c_uint32, POINTER(c_uint32),
        c_uint64, POINTER(c_uint64), c_void_p,
    ]
    return True


_CARBON_OK = _setup_carbon_signatures()


# ── Public mappings ───────────────────────────────────────────────────────────

# Cocoa modifier name → Carbon mask. Use this when reading config["hotkey_mods"].
COCOA_TO_CARBON = {
    "cmd":    cmdKey,
    "command": cmdKey,
    "shift":  shiftKey,
    "ctrl":   controlKey,
    "control": controlKey,
    "alt":    optionKey,
    "option": optionKey,
    "opt":    optionKey,
}

# macOS virtual keycodes (HIToolbox/Events.h).
# Covers letters, digits, function keys, arrows, and common control keys.
# Used by both the hotkey registration and the ShortcutField recorder.
VK_MAP = {
    # Letters
    "a": 0,  "b": 11, "c": 8,  "d": 2,  "e": 14,
    "f": 3,  "g": 5,  "h": 4,  "i": 34, "j": 38,
    "k": 40, "l": 37, "m": 46, "n": 45, "o": 31,
    "p": 35, "q": 12, "r": 15, "s": 1,  "t": 17,
    "u": 32, "v": 9,  "w": 13, "x": 7,  "y": 16,
    "z": 6,
    # Digits (top row)
    "0": 29, "1": 18, "2": 19, "3": 20, "4": 21,
    "5": 23, "6": 22, "7": 26, "8": 28, "9": 25,
    # Function keys
    "f1":  122, "f2":  120, "f3":  99,  "f4":  118,
    "f5":  96,  "f6":  97,  "f7":  98,  "f8":  100,
    "f9":  101, "f10": 109, "f11": 103, "f12": 111,
    "f13": 105, "f14": 107, "f15": 113, "f16": 106,
    "f17": 64,  "f18": 79,  "f19": 80,  "f20": 90,
    # Arrows
    "up": 126, "down": 125, "left": 123, "right": 124,
    # Control / whitespace
    "space":  49,
    "tab":    48,
    "return": 36,
    "enter":  36,
    "escape": 53,
    "esc":    53,
    "delete": 51,
    "backspace": 51,
    # Punctuation (commonly used in shortcuts)
    "-": 27, "=": 24, "[": 33, "]": 30, "\\": 42,
    ";": 41, "'": 39, ",": 43, ".": 47, "/": 44,
    "`": 50,
}

# Reverse map: VK → display label (used by ShortcutField for rendering)
_VK_TO_LABEL = {
    49: "Space", 48: "Tab", 36: "↩", 53: "Esc", 51: "⌫",
    126: "↑", 125: "↓", 123: "←", 124: "→",
    122: "F1", 120: "F2", 99: "F3", 118: "F4",
    96: "F5", 97: "F6", 98: "F7", 100: "F8",
    101: "F9", 109: "F10", 103: "F11", 111: "F12",
    105: "F13", 107: "F14", 113: "F15", 106: "F16",
    64: "F17", 79: "F18", 80: "F19", 90: "F20",
}

# Modifier symbols for UI display
MOD_SYMBOLS = {
    "cmd":   "⌘",
    "shift": "⇧",
    "alt":   "⌥",
    "ctrl":  "⌃",
}


def vk_label(vk):
    """Human-readable label for a virtual keycode."""
    if vk in _VK_TO_LABEL:
        return _VK_TO_LABEL[vk]
    # Letters / digits — find by reverse lookup
    for name, code in VK_MAP.items():
        if code == vk and len(name) == 1:
            return name.upper()
    return f"VK{vk}"


def format_shortcut(mods, vk):
    """Format ['cmd','shift'], 25 → '⌘⇧9'.

    `mods` is a list of cocoa modifier names; `vk` is a virtual keycode.
    """
    parts = []
    # Stable visual order: ⌃⌥⇧⌘ (Apple HIG convention)
    order = ["ctrl", "alt", "shift", "cmd"]
    for m in order:
        if m in mods:
            parts.append(MOD_SYMBOLS[m])
    parts.append(vk_label(vk) if vk is not None else "?")
    return "".join(parts)


def cocoa_mods_to_carbon(mods):
    """['cmd', 'shift'] → cmdKey | shiftKey."""
    mask = 0
    for m in mods:
        mask |= COCOA_TO_CARBON.get(m, 0)
    return mask


# ── CarbonHotkey class ────────────────────────────────────────────────────────

class CarbonHotkey:
    """Single global hotkey via Carbon Event Manager.

    Usage:
        hk = CarbonHotkey()
        hk.register(carbon_mods=cmdKey|shiftKey, vk=25, callback=on_press)
        # ... later ...
        hk.unregister()
    """

    _SIGNATURE = 0x564F4943  # 'VOIC'

    def __init__(self):
        self._hotkey_ref = c_void_p()
        self._handler_ref = c_void_p()
        # GC pin — these MUST stay alive while registered, otherwise the
        # Carbon callback invokes freed memory and macOS crashes the app.
        self._handler_proc = None
        self._callback = None
        self._registered = False
        self._available = _CARBON_OK

    @property
    def is_available(self):
        return self._available

    @property
    def is_registered(self):
        return self._registered

    def register(self, carbon_mods, vk, callback):
        """Register a global hotkey. Returns True on success."""
        if not self._available:
            logger.error("Carbon framework unavailable; cannot register hotkey")
            return False

        if self._registered:
            logger.warning("Hotkey already registered — unregister first")
            self.unregister()

        self._callback = callback

        # Build event handler closure (must be a CFUNCTYPE instance)
        def _handler(_call_ref, _event_ref, _user):
            try:
                if self._callback:
                    # Carbon dispatches this handler OUTSIDE AppKit's run-loop
                    # turn, so AppKit window ordering done inline here never
                    # flushes to the WindowServer. Hop onto the main queue so
                    # the callback runs on a real AppKit turn.
                    AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(
                        self._callback
                    )
            except Exception:
                logger.exception("Hotkey callback raised")
            return noErr

        self._handler_proc = _EventHandlerProcPtr(_handler)

        target = _Carbon.GetApplicationEventTarget()
        if not target:
            logger.error("GetApplicationEventTarget returned NULL")
            return False

        # Install event handler for kEventHotKeyPressed
        spec = _EventTypeSpec(kEventClassKeyboard, kEventHotKeyPressed)
        status = _Carbon.InstallEventHandler(
            target, self._handler_proc, 1, ctypes.byref(spec),
            None, ctypes.byref(self._handler_ref),
        )
        if status != noErr:
            logger.error(f"InstallEventHandler failed: status={status}")
            self._handler_proc = None
            return False

        # Register the hotkey itself
        hk_id = _EventHotKeyID(self._SIGNATURE, 1)
        status = _Carbon.RegisterEventHotKey(
            c_uint32(vk), c_uint32(carbon_mods), hk_id, target,
            0, ctypes.byref(self._hotkey_ref),
        )
        if status != noErr:
            if status == eventHotKeyExistsErr:
                logger.error(
                    f"Hotkey conflict (already registered system-wide): "
                    f"vk={vk} mods=0x{carbon_mods:x}"
                )
            else:
                logger.error(
                    f"RegisterEventHotKey failed: status={status} "
                    f"vk={vk} mods=0x{carbon_mods:x}"
                )
            _Carbon.RemoveEventHandler(self._handler_ref)
            self._handler_ref = c_void_p()
            self._handler_proc = None
            return False

        self._registered = True
        logger.info(f"Carbon hotkey registered: vk={vk} mods=0x{carbon_mods:x}")
        return True

    def unregister(self):
        """Tear down current registration. Idempotent."""
        if not self._registered:
            return

        if self._hotkey_ref:
            _Carbon.UnregisterEventHotKey(self._hotkey_ref)
            self._hotkey_ref = c_void_p()

        if self._handler_ref:
            _Carbon.RemoveEventHandler(self._handler_ref)
            self._handler_ref = c_void_p()

        self._handler_proc = None
        self._callback = None
        self._registered = False
        logger.info("Carbon hotkey unregistered")
