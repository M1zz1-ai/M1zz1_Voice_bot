# Changelog

All notable changes to VoiceBot are documented here.

## [2.1.1] — 2026-07-16

### Fixed
- **Smart typing in Electron/Chromium apps (e.g. Claude Desktop).** These apps
  return `kAXErrorNoValue` for the focused element until accessibility is
  activated on them. The focus prober now sets `AXManualAccessibility`
  (falling back to `AXEnhancedUserInterface`) on the app and retries — cached
  once per app-session — and prewarms this at recording start so the tree is
  exposed before the first commit. Unprobeable apps stay silent and log a
  distinct `gate: silent-unprobeable (app=…)` line.

## [2.1.0] — 2026-07-16

### Added
- **Context-aware output mode (opt-in).** Enable "Smart typing" in Settings to
  probe the focused UI element via the Accessibility API before each live
  commit and only type into real editable text fields. Non-editable / no-focus
  / password fields fall back to silent mode (text is collected internally and
  left on the clipboard for ⌘V), sticky per recording. Password (secure) fields
  are never typed into, regardless of the setting. Default is **off** (legacy
  always-type behaviour).
- **Always-on clipboard recovery.** Every recording now leaves the full
  transcription on the clipboard for ⌘V, in both typed and silent modes.
- **Silent-mode completion cue** — success sound/flash plus a brief
  "📋 In clipboard — ⌘V" status hint.
- **`--probe-focus` self-test** and per-decision gate logging
  (`output gate: <state> (role=…, app=…)`) for diagnosing focus classification.
- **Persistent code-signing.** `build_app.sh` signs with a local self-signed
  identity ("VoiceBot Dev Signing") when present so Microphone/Accessibility
  grants survive rebuilds; ad-hoc fallback with a loud warning otherwise.
- **Versioning.** Single `config.VERSION` constant injected into the bundle;
  version logged at startup.

### Fixed
- Focus probe now queries the frontmost application's focused element (the
  system-wide element returns `kAXErrorCannotComplete` even when trusted) and
  handles AX error codes explicitly.

## [2.0.0] — 2026-07-16

### Fixed
- **CoreAudio stop deadlock** — audio capture moved out of process; the parent
  never touches CoreAudio, and a stuck capture child is SIGKILLed on stop so a
  wedge can never freeze the UI.
- **Background-thread UI crashes (SIGTRAP)** — all AppKit/rumps mutations are
  marshalled onto the main thread.
- **Live-mode lag / vanishing overlay squares** — sliding-window transcription
  bounds per-poll inference to ~2s regardless of dictation length; finalize
  reconciles only the tail window without duplicating text.
