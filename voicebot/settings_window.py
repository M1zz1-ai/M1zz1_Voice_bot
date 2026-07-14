"""
Нативное окно настроек VoiceBot (PyObjC / AppKit).

Секции:
  ⌨️  Hotkey         — ShortcutField (record-on-click) для назначения комбы
  ⚙️  General        — язык (auto/ru/en/…), max duration, auto-paste, sounds
  🧠  Transcription — выбор модели Whisper + Download-кнопка
"""

import logging

import AppKit
import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSEventMaskKeyDown,
    NSFont,
    NSMakeRect,
    NSPopUpButton,
    NSTextField,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskTitled,
)

# NSSwitch (modern iOS-style toggle) is macOS 10.15+. Fall back to a styled
# checkbox if the runtime doesn't have it.
_HAS_NSSWITCH = hasattr(AppKit, "NSSwitch")

from hotkey import (
    MOD_SYMBOLS,
    VK_MAP,
    format_shortcut,
    vk_label,
)

logger = logging.getLogger("voicebot.settings")

# Reverse map: VK code → canonical key name used in config. Use the SHORTEST
# entry per code so config stays readable (e.g. "esc" not "escape").
_VK_TO_KEYNAME = {}
_NAME_PRIORITY = ("esc", "return", "tab", "space", "delete", "up", "down",
                  "left", "right")
for _name, _code in VK_MAP.items():
    if _code in _VK_TO_KEYNAME:
        # Prefer single-char names over multi-char synonyms (e.g. "9" > "digit9")
        existing = _VK_TO_KEYNAME[_code]
        if len(_name) < len(existing) or _name in _NAME_PRIORITY:
            _VK_TO_KEYNAME[_code] = _name
    else:
        _VK_TO_KEYNAME[_code] = _name


def _cocoa_flags_to_mod_names(flags):
    """NSEvent modifierFlags → list of cocoa modifier names in stable order."""
    NSEventModifierFlagShift   = 1 << 17
    NSEventModifierFlagControl = 1 << 18
    NSEventModifierFlagOption  = 1 << 19
    NSEventModifierFlagCommand = 1 << 20

    mods = []
    if flags & NSEventModifierFlagControl: mods.append("ctrl")
    if flags & NSEventModifierFlagOption:  mods.append("alt")
    if flags & NSEventModifierFlagShift:   mods.append("shift")
    if flags & NSEventModifierFlagCommand: mods.append("cmd")
    return mods


# ── Helper: target-action bridge ──────────────────────────────────────────────

class _ActionTarget(AppKit.NSObject):
    """Bridge to connect NSButton action to a Python callback."""

    def initWithCallback_(self, callback):
        self = objc.super(_ActionTarget, self).init()
        if self is not None:
            self._py_callback = callback
        return self

    def performAction_(self, sender):
        if hasattr(self, "_py_callback") and self._py_callback:
            self._py_callback()


def _make_target(callback):
    return _ActionTarget.alloc().initWithCallback_(callback)


class _WindowHideOnCloseDelegate(AppKit.NSObject):
    """NSWindowDelegate that turns a window close into a hide.

    Combined with setReleasedWhenClosed_(False), this guarantees the
    NSWindow and every attached control survives the title-bar close so
    the next Settings open can reuse them without rebuilding.
    """

    def windowShouldClose_(self, sender):
        sender.orderOut_(None)
        return False


# ── Shortcut recorder ─────────────────────────────────────────────────────────

class ShortcutField:
    """Clickable field that records a hotkey combination.

    Click → "Press keys…" → captures next keyDown via local NSEvent monitor
    → renders captured shortcut as symbols (⌘⇧9). ESC cancels recording.

    The widget is built on top of NSButton because we get free click/hover
    behaviour, focus ring, and key-equivalent handling for ESC.
    """

    PROMPT = "Press keys…"

    def __init__(self, parent, x, y, width=220, height=30):
        self._mods = []   # list of cocoa modifier names
        self._vk = None   # virtual keycode
        self._monitor = None
        self._recording = False

        btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, width, height))
        btn.setBezelStyle_(NSBezelStyleRounded)
        btn.setFont_(NSFont.monospacedSystemFontOfSize_weight_(14, 0.0))
        btn.setTitle_("Click to set")
        btn.setToolTip_("Click to set, ESC to cancel")
        parent.addSubview_(btn)
        self._btn = btn

        self._target = _make_target(self._on_click)
        btn.setTarget_(self._target)
        btn.setAction_(objc.selector(self._target.performAction_, signature=b"v@:@"))

    # ── Public API ───────────────────────────────────────────────────────

    def set_value(self, mods, key_name):
        """Initialize from config: mods=['cmd','shift'], key_name='9'."""
        self._mods = list(mods)
        vk = VK_MAP.get(str(key_name).lower())
        self._vk = vk
        self._render()

    def get_value(self):
        """Returns (mods, key_name) ready for config save."""
        if self._vk is None:
            return list(self._mods), ""
        key_name = _VK_TO_KEYNAME.get(self._vk, "")
        return list(self._mods), key_name

    # ── Internal ─────────────────────────────────────────────────────────

    def _render(self):
        if self._vk is None:
            self._btn.setTitle_("Click to set")
        else:
            self._btn.setTitle_(format_shortcut(self._mods, self._vk))

    def _on_click(self):
        if self._recording:
            return
        self._enter_record_mode()

    def _enter_record_mode(self):
        self._recording = True
        self._btn.setTitle_(self.PROMPT)

        # Local NSEvent monitor — fires for events targeted at our app only,
        # so we don't grab global keystrokes. Returning None swallows the
        # event so it doesn't propagate to the focused control.
        def handler(event):
            try:
                vk = event.keyCode()
                flags = int(event.modifierFlags())
                # Escape cancels recording
                if vk == 53:  # Esc
                    self._exit_record_mode()
                    return None

                mods = _cocoa_flags_to_mod_names(flags)
                # Require at least one modifier OR a function key. Plain
                # letter would be a useless hotkey.
                if not mods and not (96 <= vk <= 122 or vk in (49, 48)):
                    return None

                self._mods = mods
                self._vk = vk
                self._exit_record_mode()
            except Exception:
                logger.exception("ShortcutField record handler raised")
                self._exit_record_mode()
            return None  # swallow the event

        self._monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            NSEventMaskKeyDown, handler,
        )

    def _exit_record_mode(self):
        self._recording = False
        if self._monitor is not None:
            try:
                AppKit.NSEvent.removeMonitor_(self._monitor)
            except Exception:
                pass
            self._monitor = None
        self._render()


# ── Settings Window ──────────────────────────────────────────────────────────

WIN_W, WIN_H = 500, 750

# Layout grid — all heights/gaps live here so the column doesn't drift.
PAD          = 24     # left/right window padding (and card inner padding)
TOP_PAD      = 56     # below the (transparent) titlebar before first section
BOTTOM_PAD   = 24     # below the action buttons
TITLE_H      = 22     # section heading line height
TITLE_GAP    = 14     # gap from heading to its card
SECT_GAP     = 26     # gap from end of one card to start of next heading
ROW_H        = 30     # one row of content inside a card
ROW_GAP      = 10     # vertical gap between rows
CARD_INSET_Y = 18     # top/bottom padding inside a card
BTN_H        = 36

# Palette — variant B (approved): deep purple canvas, muted-purple accents.
def _srgb(r, g, b, a=1.0):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)


BG_COLOR     = _srgb(0x14 / 255, 0x10 / 255, 0x1E / 255)  # #14101E window bg
CARD_BG      = _srgb(0x1E / 255, 0x17 / 255, 0x30 / 255)  # #1E1730 card fill
CARD_BORDER  = _srgb(0x8A / 255, 0x6C / 255, 0xB0 / 255, 0.45)  # #8A6CB0
TEXT_COLOR   = _srgb(0xEF / 255, 0xE6 / 255, 0xF7 / 255)  # #EFE6F7 primary
DIM_COLOR    = _srgb(0x9B / 255, 0x8F / 255, 0xB0 / 255)  # #9B8FB0 secondary
ACCENT_COLOR = _srgb(0x8A / 255, 0x6C / 255, 0xB0 / 255)  # #8A6CB0 muted purple
HIGHLIGHT    = _srgb(0xB5 / 255, 0x7B / 255, 0xFF / 255)  # #B57BFF focus/primary
FIELD_BG     = _srgb(0x10 / 255, 0x0C / 255, 0x1A / 255)  # slightly under card


# Whisper model registry — mirror of whisper_engine.MODELS keys + display info.
# Keep here to avoid import-time MLX dependency just for label rendering.
MODEL_NAMES = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]
MODEL_INFO = {
    "tiny":           "tiny  ~75 MB  ~32× realtime",
    "base":           "base  ~140 MB  ~16× realtime",
    "small":          "small  ~470 MB  ~6× realtime",
    "medium":         "medium  ~1.5 GB  ~2× realtime",
    "large-v3":       "large-v3  ~3.1 GB  ~1× realtime",
    "large-v3-turbo": "large-v3-turbo  ~800 MB  ~8× realtime",
}

LANGUAGES = ["auto", "ru", "en", "uk", "de", "fr", "es", "ja", "zh"]


class SettingsWindow:
    """Native macOS dark-themed settings window."""

    def __init__(self, config, on_save=None):
        self.config = config
        self.on_save = on_save
        self.window = None
        self._controls = {}
        self._targets = []  # prevent GC of NSObject bridges
        self._shortcut = None

    def show(self):
        # Build the NSWindow exactly once and reuse it on every open.
        # Recreating it (the previous behaviour after Cancel/Save called
        # close()) churned PyObjC bridges in self._targets and reliably
        # crashed the app on the second open.
        try:
            if self.window is None:
                self._build()
            self._load_values()
            self.window.center()
            self.window.makeKeyAndOrderFront_(None)
            AppKit.NSApp.activateIgnoringOtherApps_(True)
        except Exception:
            # We *must* see what's going wrong if the silent ObjC crash
            # ever surfaces as a Python exception instead.
            logger.exception("SettingsWindow.show() failed")

    # ── Build UI ──────────────────────────────────────────────────────────

    def _build(self):
        # Edit menu so copy/paste work in any text fields.
        app = AppKit.NSApplication.sharedApplication()
        if not app.mainMenu():
            main_menu = AppKit.NSMenu.alloc().init()
            app.setMainMenu_(main_menu)
            edit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Edit", None, "")
            edit_menu = AppKit.NSMenu.alloc().initWithTitle_("Edit")
            edit_menu.addItemWithTitle_action_keyEquivalent_("Undo", "undo:", "z")
            edit_menu.addItemWithTitle_action_keyEquivalent_("Redo", "redo:", "Z")
            edit_menu.addItem_(AppKit.NSMenuItem.separatorItem())
            edit_menu.addItemWithTitle_action_keyEquivalent_("Cut", "cut:", "x")
            edit_menu.addItemWithTitle_action_keyEquivalent_("Copy", "copy:", "c")
            edit_menu.addItemWithTitle_action_keyEquivalent_("Paste", "paste:", "v")
            edit_menu.addItemWithTitle_action_keyEquivalent_("Select All", "selectAll:", "a")
            edit_item.setSubmenu_(edit_menu)
            main_menu.addItem_(edit_item)

        # Window — transparent titlebar lets the dark canvas extend all the
        # way to the corners; traffic lights float on top of it.
        frame = NSMakeRect(0, 0, WIN_W, WIN_H)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                 NSWindowStyleMaskFullSizeContentView)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False,
        )
        self.window.setTitle_("VoiceBot — Settings")
        self.window.setBackgroundColor_(BG_COLOR)
        self.window.setLevel_(3)
        self.window.setReleasedWhenClosed_(False)
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.setTitleVisibility_(1)  # NSWindowTitleHidden
        self._delegate = _WindowHideOnCloseDelegate.alloc().init()
        self.window.setDelegate_(self._delegate)

        content = self.window.contentView()
        y = WIN_H - TOP_PAD  # working cursor: top edge of next element

        # ── Hotkey ────────────────────────────────────────────────────────
        y = self._section_title(content, "Hotkey", y)
        hk_h = CARD_INSET_Y * 2 + 36
        card, y = self._card(content, y, hk_h)
        inner = hk_h - CARD_INSET_Y - 26
        self._label(card, "Dictation", PAD, inner + 6)
        self._shortcut = ShortcutField(
            card, x=PAD + 110, y=inner, width=260, height=32,
        )
        self._controls["shortcut"] = self._shortcut

        # ── General ───────────────────────────────────────────────────────
        y = self._section_title(content, "General", y)
        rows = 5
        gen_h = CARD_INSET_Y * 2 + rows * ROW_H + (rows - 1) * ROW_GAP
        card, y = self._card(content, y, gen_h)
        cy = gen_h - CARD_INSET_Y - ROW_H  # bottom of top row

        self._label(card, "Language", PAD, cy + 6)
        self._controls["language"] = self._popup(
            card, PAD + 130, cy + 2, 120, LANGUAGES,
        )
        cy -= ROW_H + ROW_GAP

        self._label(card, "Max duration (sec)", PAD, cy + 6)
        self._controls["max_duration"] = self._text_field(
            card, PAD + 220, cy + 2, 70,
        )
        cy -= ROW_H + ROW_GAP

        self._label(card, "Unload model after idle (min, 0=never)", PAD, cy + 6)
        self._controls["idle_unload"] = self._text_field(
            card, PAD + 300, cy + 2, 60,
        )
        cy -= ROW_H + ROW_GAP

        self._controls["auto_paste"] = self._toggle(
            card, "Auto-paste text", PAD, cy + 2,
        )
        self._controls["sounds_enabled"] = self._toggle(
            card, "Sounds", PAD + 250, cy + 2,
        )
        cy -= ROW_H + ROW_GAP

        self._controls["live_mode"] = self._toggle(
            card, "Live transcription", PAD, cy + 2,
        )
        self._controls["start_at_login"] = self._toggle(
            card, "Start at login", PAD + 250, cy + 2,
        )

        # ── Transcription ────────────────────────────────────────────────
        y = self._section_title(content, "Transcription", y)
        rows = 3
        tr_h = CARD_INSET_Y * 2 + rows * ROW_H + (rows - 1) * ROW_GAP
        card, y = self._card(content, y, tr_h)
        cy = tr_h - CARD_INSET_Y - ROW_H

        self._label(card, "Model", PAD, cy + 6)
        self._controls["whisper_model"] = self._popup(
            card, PAD + 130, cy + 2, 220, MODEL_NAMES,
        )
        t_model = _make_target(self._on_model_changed)
        self._targets.append(t_model)
        self._controls["whisper_model"].setTarget_(t_model)
        self._controls["whisper_model"].setAction_(
            objc.selector(t_model.performAction_, signature=b"v@:@"))
        cy -= ROW_H + ROW_GAP

        self._controls["model_info"] = self._label(
            card, MODEL_INFO["large-v3-turbo"], PAD, cy + 6,
        )
        cy -= ROW_H + ROW_GAP

        dl_btn = self._button(
            card, "⬇  Download now", PAD + 130, cy + 1, 220, ROW_H + 2,
        )
        t_dl = _make_target(self._do_download_model)
        self._targets.append(t_dl)
        dl_btn.setTarget_(t_dl)
        dl_btn.setAction_(objc.selector(t_dl.performAction_, signature=b"v@:@"))
        self._controls["dl_button"] = dl_btn

        # ── Action buttons ───────────────────────────────────────────────
        save_btn = self._button(
            content, "Save", WIN_W - PAD - 110, BOTTOM_PAD, 110, BTN_H,
        )
        t_save = _make_target(self._do_save)
        self._targets.append(t_save)
        save_btn.setTarget_(t_save)
        save_btn.setAction_(objc.selector(t_save.performAction_, signature=b"v@:@"))
        save_btn.setKeyEquivalent_("\r")
        try:
            save_btn.setBezelColor_(HIGHLIGHT)
        except Exception:
            pass

        cancel_btn = self._button(
            content, "Cancel", WIN_W - PAD - 230, BOTTOM_PAD, 110, BTN_H,
        )
        t_cancel = _make_target(self._do_cancel)
        self._targets.append(t_cancel)
        cancel_btn.setTarget_(t_cancel)
        cancel_btn.setAction_(objc.selector(t_cancel.performAction_, signature=b"v@:@"))
        cancel_btn.setKeyEquivalent_("\x1b")

        # View Logs — link-style button on the left, opens the log in Console.
        logs_btn = self._button(
            content, "View Logs", PAD, BOTTOM_PAD, 110, BTN_H,
        )
        try:
            logs_btn.setBezelStyle_(15)  # NSBezelStyleInline (subtle/link-ish)
        except Exception:
            pass
        t_logs = _make_target(self._open_logs)
        self._targets.append(t_logs)
        logs_btn.setTarget_(t_logs)
        logs_btn.setAction_(objc.selector(t_logs.performAction_, signature=b"v@:@"))

    # ── Load / Save ───────────────────────────────────────────────────────

    def _load_values(self):
        cfg = self.config

        # Shortcut field
        self._shortcut.set_value(cfg["hotkey_mods"], cfg["hotkey_key"])

        # General
        lang = cfg["language"]
        idx = LANGUAGES.index(lang) if lang in LANGUAGES else 0
        self._controls["language"].selectItemAtIndex_(idx)

        self._controls["max_duration"].setStringValue_(
            str(cfg["max_duration_seconds"])
        )
        self._controls["idle_unload"].setStringValue_(
            str(cfg.get("model_idle_unload_minutes", 15))
        )
        self._controls["auto_paste"].setState_(
            NSControlStateValueOn if cfg["auto_paste"] else NSControlStateValueOff
        )
        self._controls["sounds_enabled"].setState_(
            NSControlStateValueOn if cfg["sounds_enabled"]
            else NSControlStateValueOff
        )
        self._controls["live_mode"].setState_(
            NSControlStateValueOn if cfg.get("live_mode", True)
            else NSControlStateValueOff
        )

        import autostart
        self._controls["start_at_login"].setState_(
            NSControlStateValueOn if autostart.is_enabled()
            else NSControlStateValueOff
        )

        # Whisper model
        model = cfg.get("whisper_model", "large-v3-turbo")
        idx = MODEL_NAMES.index(model) if model in MODEL_NAMES \
              else MODEL_NAMES.index("large-v3-turbo")
        self._controls["whisper_model"].selectItemAtIndex_(idx)
        self._on_model_changed()

    def _do_save(self):
        cfg = self.config

        # Shortcut
        mods, key_name = self._shortcut.get_value()
        if not key_name:
            # User didn't set anything — keep the existing value.
            mods = cfg["hotkey_mods"]
            key_name = cfg["hotkey_key"]

        # Numeric fields
        try:
            max_dur = int(str(self._controls["max_duration"].stringValue()))
        except ValueError:
            max_dur = 300

        try:
            idle_unload = max(0, int(
                str(self._controls["idle_unload"].stringValue())
            ))
        except ValueError:
            idle_unload = 15

        cfg.update({
            "hotkey_mods": mods,
            "hotkey_key": key_name,
            "language": str(self._controls["language"].titleOfSelectedItem()),
            "max_duration_seconds": max_dur,
            "model_idle_unload_minutes": idle_unload,
            "auto_paste": self._controls["auto_paste"].state()
                          == NSControlStateValueOn,
            "sounds_enabled": self._controls["sounds_enabled"].state()
                              == NSControlStateValueOn,
            "live_mode": self._controls["live_mode"].state()
                         == NSControlStateValueOn,
            "whisper_model": str(self._controls["whisper_model"].titleOfSelectedItem()),
        })
        cfg.save()

        # Start-at-login is a filesystem side effect (LaunchAgent), not a
        # config.json key — apply it directly from the checkbox state.
        import autostart
        autostart.set_enabled(
            self._controls["start_at_login"].state() == NSControlStateValueOn
        )

        logger.info("Settings saved")
        # orderOut_ hides without releasing the window, so the next show()
        # can reuse the same NSWindow instance and its target/control graph.
        self.window.orderOut_(None)

        if self.on_save:
            self.on_save()

    def _do_cancel(self):
        self.window.orderOut_(None)

    def _open_logs(self):
        """Open the log file in Console (moved here from the menu bar)."""
        import os
        import subprocess
        log_file = os.path.join(self.config.config_dir, "logs", "voicebot.log")
        subprocess.run(["open", "-a", "Console", log_file], check=False)

    def _do_download_model(self):
        """User clicked 'Download now' — save first so engine sees new model,
        then on_save() triggers WhisperEngine.switch_model which kicks off
        the download."""
        self._do_save()

    def _on_model_changed(self):
        name = str(self._controls["whisper_model"].titleOfSelectedItem())
        self._controls["model_info"].setStringValue_(
            MODEL_INFO.get(name, name)
        )

    # ── UI primitives ─────────────────────────────────────────────────────

    def _section_title(self, parent, text, y_top):
        """Place section header with its TOP at `y_top`.

        Returns the y-coord at which the following card's top edge should sit
        (= y_top shifted down by title height + title-to-card gap).
        """
        label_y = y_top - TITLE_H  # NSMakeRect uses bottom-left origin
        bullet = NSView.alloc().initWithFrame_(
            NSMakeRect(PAD, label_y + (TITLE_H - 6) // 2, 6, 6),
        )
        bullet.setWantsLayer_(True)
        bullet.layer().setBackgroundColor_(HIGHLIGHT.CGColor())
        bullet.layer().setCornerRadius_(0)  # small purple square, not a dot
        parent.addSubview_(bullet)

        label = NSTextField.labelWithString_(text)
        label.setFrame_(
            NSMakeRect(PAD + 16, label_y, WIN_W - 2 * PAD - 16, TITLE_H),
        )
        label.setFont_(NSFont.systemFontOfSize_weight_(14, 0.4))  # semibold
        label.setTextColor_(TEXT_COLOR)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        parent.addSubview_(label)
        return y_top - TITLE_H - TITLE_GAP

    def _card(self, parent, y_top, height):
        """Flat card with TOP at `y_top`: a slightly-lighter #1E1730 fill with a
        muted-purple hairline border (variant B). Plain NSView so the fill is
        the exact approved colour rather than a translucent material.

        Returns (card, y_for_next_section_title).
        """
        rect = NSMakeRect(PAD, y_top - height, WIN_W - 2 * PAD, height)
        card = NSView.alloc().initWithFrame_(rect)
        card.setWantsLayer_(True)
        layer = card.layer()
        layer.setBackgroundColor_(CARD_BG.CGColor())
        layer.setCornerRadius_(14)
        layer.setMasksToBounds_(True)
        layer.setBorderWidth_(1)
        layer.setBorderColor_(CARD_BORDER.CGColor())
        parent.addSubview_(card)
        return card, y_top - height - SECT_GAP

    def _toggle(self, parent, title, x, y):
        """NSSwitch (iOS-style toggle) with a label to its right.

        Falls back to a NSButton switch on older OS versions. Returns the
        switch so callers can read/write its state() exactly like before.
        """
        if _HAS_NSSWITCH:
            sw = AppKit.NSSwitch.alloc().initWithFrame_(
                NSMakeRect(x, y, 40, 22),
            )
        else:
            sw = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, 40, 22))
            sw.setButtonType_(3)  # NSSwitchButton
            sw.setTitle_("")
        parent.addSubview_(sw)

        label = NSTextField.labelWithString_(title)
        label.setFrame_(NSMakeRect(x + 50, y + 1, 240, 22))
        label.setFont_(NSFont.systemFontOfSize_(13))
        label.setTextColor_(TEXT_COLOR)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        parent.addSubview_(label)
        return sw

    def _label(self, parent, text, x, y):
        label = NSTextField.labelWithString_(text)
        label.setFrame_(NSMakeRect(x, y, 300, 20))
        label.setFont_(NSFont.systemFontOfSize_(13))
        label.setTextColor_(DIM_COLOR)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        parent.addSubview_(label)
        return label

    def _text_field(self, parent, x, y, width, editable=True):
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, width, 24))
        field.setFont_(NSFont.monospacedSystemFontOfSize_weight_(13, 0.0))
        field.setTextColor_(TEXT_COLOR)
        field.setBackgroundColor_(FIELD_BG)
        field.setBezeled_(True)
        field.setBezelStyle_(0)
        field.setEditable_(editable)
        field.setSelectable_(True)
        field.setFocusRingType_(1)
        parent.addSubview_(field)
        return field

    def _checkbox(self, parent, title, x, y):
        btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, 200, 22))
        btn.setButtonType_(3)  # NSSwitchButton
        btn.setTitle_(title)
        btn.setFont_(NSFont.systemFontOfSize_(12))
        parent.addSubview_(btn)
        return btn

    def _popup(self, parent, x, y, width, items):
        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(x, y, width, 26), False,
        )
        popup.setFont_(NSFont.systemFontOfSize_(12))
        for item in items:
            popup.addItemWithTitle_(item)
        parent.addSubview_(popup)
        return popup

    def _button(self, parent, title, x, y, w, h):
        btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        btn.setTitle_(title)
        btn.setBezelStyle_(NSBezelStyleRounded)
        btn.setFont_(NSFont.systemFontOfSize_(13))
        parent.addSubview_(btn)
        return btn
