"""
Focus-aware output mode.

Before typing live text we ask the macOS Accessibility API what UI element is
focused, so dictation only types into real editable fields and never leaks onto
the desktop, into a non-text app, or into a password field.

pyobjc's Accessibility bindings (ApplicationServices / HIServices) are NOT in
this bundle — only Cocoa + Quartz are — so we call the AX C API through ctypes,
the same way permissions.py calls AXIsProcessTrusted. The raw AX calls live in
_AXOps; the activation-retry state machine (`_run_probe`) is pure and
unit-testable with a fake ops layer, since AX itself can't run headless.

Reliability notes:
- AXUIElementCreateSystemWide + kAXFocusedUIElement often returns
  kAXErrorCannotComplete (-25204) even in a trusted process, so we probe the
  FRONTMOST APPLICATION's element first, system-wide as fallback.
- Electron/Chromium apps (e.g. Claude Desktop) return kAXErrorNoValue (-25212)
  for kAXFocusedUIElement until accessibility is *activated* on them. On
  NoValue we set AXManualAccessibility (then AXEnhancedUserInterface) = true on
  the app element, wait briefly, and retry once. Cached per bundle id so it's
  done once per app-session, never per commit.
"""

import ctypes
import logging
import time

logger = logging.getLogger("voicebot.focus")

# Focus decision states.
EDITABLE = "editable"   # a real text field/area — type here
SECURE = "secure"       # password field — NEVER type, regardless of config
SILENT = "silent"       # no focus / not editable / detection failed

_KCFSTRING_ENCODING_UTF8 = 0x08000100

# AXError codes we branch on.
_KAX_SUCCESS = 0
_KAX_ERROR_NO_VALUE = -25212           # Electron before AX activation
_KAX_ERROR_CANNOT_COMPLETE = -25204

# Roles that accept free text (web text inputs also surface as AXTextField).
_EDITABLE_ROLES = {"AXTextField", "AXTextArea", "AXComboBox"}

# Attributes we set to switch on accessibility in Chromium/Electron apps.
_ACTIVATION_ATTRS = ("AXManualAccessibility", "AXEnhancedUserInterface")
_ACTIVATION_WAIT_S = 0.15


def _frontmost_app():
    """(pid, bundle_id) of the frontmost app, or (None, "") on failure.
    Lazily imports AppKit so this module stays importable without pyobjc."""
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None, ""
        return int(app.processIdentifier()), (app.bundleIdentifier() or "")
    except Exception:
        return None, ""


def _run_probe(ops, activated):
    """Pure activation-retry state machine over an AX ops layer.

    `ops` exposes: ready(), is_trusted(), frontmost(), focused(pid) -> (err,
    token|None), activate(pid, attr), classify(token) -> dict, release(token),
    sleep(seconds). `activated` is a per-app-session set of bundle ids that have
    already had accessibility activation sent.
    """
    pid, bundle = ops.frontmost()
    info = {"state": SILENT, "role": "", "subrole": "", "settable": False,
            "err": None, "app": bundle, "trusted": ops.is_trusted(),
            "unprobeable": False}
    if not ops.ready():
        return info

    err, token = ops.focused(pid)

    # Electron/Chromium: no focused element until accessibility is activated.
    if (token is None and err == _KAX_ERROR_NO_VALUE and pid
            and bundle not in activated):
        activated.add(bundle)
        logger.info("focus: activating accessibility for app=%s", bundle)
        for attr in _ACTIVATION_ATTRS:
            ops.activate(pid, attr)
            ops.sleep(_ACTIVATION_WAIT_S)
            err, token = ops.focused(pid)
            if token is not None or err != _KAX_ERROR_NO_VALUE:
                break

    info["err"] = err
    if token is None:
        info["unprobeable"] = err in (_KAX_ERROR_NO_VALUE,
                                      _KAX_ERROR_CANNOT_COMPLETE)
        return info

    try:
        info.update(ops.classify(token))
    finally:
        ops.release(token)
    return info


class _AXOps:
    """ctypes implementation of the AX ops used by _run_probe."""

    def __init__(self):
        self._ok = False
        try:
            self._cf = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreFoundation.framework/"
                "CoreFoundation"
            )
            self._ax = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/ApplicationServices.framework/"
                "ApplicationServices"
            )
            cf, ax = self._cf, self._ax

            cf.CFStringCreateWithCString.restype = ctypes.c_void_p
            cf.CFStringCreateWithCString.argtypes = [
                ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
            cf.CFStringGetCString.restype = ctypes.c_bool
            cf.CFStringGetCString.argtypes = [
                ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
            cf.CFRelease.restype = None
            cf.CFRelease.argtypes = [ctypes.c_void_p]

            ax.AXIsProcessTrusted.restype = ctypes.c_bool
            ax.AXUIElementCreateSystemWide.restype = ctypes.c_void_p
            ax.AXUIElementCreateSystemWide.argtypes = []
            ax.AXUIElementCreateApplication.restype = ctypes.c_void_p
            ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int32]  # pid_t
            ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int  # AXError
            ax.AXUIElementCopyAttributeValue.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p)]
            ax.AXUIElementSetAttributeValue.restype = ctypes.c_int  # AXError
            ax.AXUIElementSetAttributeValue.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            ax.AXUIElementIsAttributeSettable.restype = ctypes.c_int
            ax.AXUIElementIsAttributeSettable.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_bool)]

            # kCFBooleanTrue is a global CFBooleanRef — read its pointer value.
            self._cf_true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")

            self._attr = {
                name: self._make_cfstr(name)
                for name in ("AXFocusedUIElement", "AXRole", "AXSubrole",
                             "AXValue") + _ACTIVATION_ATTRS
            }
            self._ok = all(self._attr.values()) and bool(self._cf_true.value)
        except Exception:
            logger.exception("AX ops init failed")
            self._ok = False

    # ── ops interface ────────────────────────────────────────────────────

    def ready(self):
        return self._ok

    def is_trusted(self):
        try:
            return bool(self._ok and self._ax.AXIsProcessTrusted())
        except Exception:
            return False

    def frontmost(self):
        return _frontmost_app()

    def focused(self, pid):
        """(last_err, focused_ref_or_None). Tries the frontmost app element
        then the system-wide element."""
        candidates = []
        if pid:
            app_el = self._ax.AXUIElementCreateApplication(pid)
            if app_el:
                candidates.append(app_el)
        sysw = self._ax.AXUIElementCreateSystemWide()
        if sysw:
            candidates.append(sysw)

        focused, last_err = None, None
        try:
            for el in candidates:
                err, ref = self._copy_attr(el, "AXFocusedUIElement")
                last_err = err
                if ref is not None:
                    focused = ref
                    break
        finally:
            for el in candidates:
                self._cf.CFRelease(el)
        return last_err, focused

    def activate(self, pid, attr):
        """Set a Chromium/Electron accessibility switch = true on the app."""
        if not pid:
            return
        app_el = self._ax.AXUIElementCreateApplication(pid)
        if not app_el:
            return
        try:
            cfattr = self._attr.get(attr)
            if cfattr:
                self._ax.AXUIElementSetAttributeValue(
                    app_el, cfattr, self._cf_true)
        finally:
            self._cf.CFRelease(app_el)

    def classify(self, focused):
        _, role_ref = self._copy_attr(focused, "AXRole")
        role = self._cfstr_to_py(role_ref) if role_ref else ""
        if role_ref:
            self._cf.CFRelease(role_ref)

        _, sub_ref = self._copy_attr(focused, "AXSubrole")
        subrole = self._cfstr_to_py(sub_ref) if sub_ref else ""
        if sub_ref:
            self._cf.CFRelease(sub_ref)

        settable = ctypes.c_bool(False)
        self._ax.AXUIElementIsAttributeSettable(
            focused, self._attr["AXValue"], ctypes.byref(settable))

        if subrole == "AXSecureTextField":
            state = SECURE
        elif role in _EDITABLE_ROLES or settable.value:
            state = EDITABLE
        else:
            state = SILENT
        return {"state": state, "role": role, "subrole": subrole,
                "settable": bool(settable.value)}

    def release(self, ref):
        self._cf.CFRelease(ref)

    def sleep(self, seconds):
        time.sleep(seconds)

    # ── ctypes helpers ───────────────────────────────────────────────────

    def _make_cfstr(self, s):
        return self._cf.CFStringCreateWithCString(
            None, s.encode("utf-8"), _KCFSTRING_ENCODING_UTF8)

    def _cfstr_to_py(self, ref):
        buf = ctypes.create_string_buffer(512)
        if self._cf.CFStringGetCString(ref, buf, 512, _KCFSTRING_ENCODING_UTF8):
            return buf.value.decode("utf-8", "replace")
        return ""

    def _copy_attr(self, element, attr_name):
        out = ctypes.c_void_p()
        err = self._ax.AXUIElementCopyAttributeValue(
            element, self._attr[attr_name], ctypes.byref(out))
        if err != _KAX_SUCCESS or not out.value:
            return err, None
        return err, out


class FocusProber:
    """Reports whether the currently focused element is an editable text target.
    Isolated behind this class so OutputGate is testable with a fake prober."""

    def __init__(self):
        self._ops = None
        self._activated = set()  # bundle ids we've sent AX activation to

    def _ensure(self):
        if self._ops is None:
            self._ops = _AXOps()
        return self._ops

    def probe_info(self):
        """Full diagnostic dict (state/role/subrole/settable/err/app/trusted/
        unprobeable). Never raises."""
        try:
            return _run_probe(self._ensure(), self._activated)
        except Exception:
            logger.exception("FocusProber.probe_info failed")
            return {"state": SILENT, "role": "", "subrole": "",
                    "settable": False, "err": None, "app": "",
                    "trusted": False, "unprobeable": True}

    def probe(self):
        """Just the decision state (for OutputGate)."""
        return self.probe_info()["state"]

    def prewarm(self):
        """Probe once so Electron accessibility activation is sent at recording
        start (cached), giving the app time to expose its tree before the first
        commit. Result is discarded."""
        self.probe_info()

    def is_trusted(self):
        return self._ensure().is_trusted()


def _describe(info):
    role = info.get("role") or ""
    who = f"role={role}" if role else f"err={info.get('err')}"
    return f"{who}, app={info.get('app', '')}"


class OutputGate:
    """Per-recording decision: type live text, or stay silent (clipboard-only).

    Sticky: once a recording falls back to silent — focus lost, a non-editable
    target, a password field, or a failed probe — it stays silent for the rest
    of that recording. That way, if focus returns mid-dictation we do NOT dump
    the backlog into the cursor; the full text is recovered from the clipboard
    instead. Password (secure) fields are never typed into, even when smart
    typing is disabled. When smart typing is off, anything else is typed
    (legacy behaviour).

    Logs one INFO line per decision at recording start and on any mode change.
    """

    def __init__(self, prober, smart_typing=True):
        self._prober = prober
        self._smart = smart_typing
        self._silent = False
        self._last_logged = None

    def reset(self, smart_typing=None):
        """Begin a new recording. Clears the sticky-silent latch and, in smart
        mode, prewarms the prober so Electron activation is sent early."""
        self._silent = False
        self._last_logged = None
        if smart_typing is not None:
            self._smart = smart_typing
        if self._smart and hasattr(self._prober, "prewarm"):
            try:
                self._prober.prewarm()
            except Exception:
                logger.exception("prober prewarm failed")

    @property
    def is_silent(self):
        return self._silent

    def allow_typing(self):
        """True if the caller may type into the focused element right now.
        Re-probes focus each call and latches silent on any fallback."""
        if self._silent:
            return False
        info = self._probe_info()
        state = info["state"]
        self._maybe_log(state, info)
        if state == SECURE:
            self._silent = True
            return False
        if not self._smart:
            return True  # legacy: type anywhere except a secure field
        if state == EDITABLE:
            return True
        self._silent = True  # SILENT / failed probe → sticky silent
        return False

    def _probe_info(self):
        info = self._prober.probe_info()
        if isinstance(info, str):  # tolerate a bare-state prober
            info = {"state": info, "role": "", "err": None, "app": ""}
        return info

    def _maybe_log(self, state, info):
        if state == self._last_logged:
            return
        self._last_logged = state
        if state == SILENT and info.get("unprobeable"):
            logger.info("gate: silent-unprobeable (app=%s)", info.get("app", ""))
        else:
            logger.info("output gate: %s (%s)", state, _describe(info))
