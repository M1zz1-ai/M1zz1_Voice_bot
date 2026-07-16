# Changelog

All notable changes to VoiceBot are documented here.

## [2.1.3] — 2026-07-16

### Fixed
- **Startup no longer hangs on the microphone probe.** The parent process is
  now 100% PortAudio/CoreAudio-free: `permissions.check_microphone` no longer
  opens an `InputStream` (which could wedge inside `Pa_OpenStream`). Mic TCC
  status is queried via AVFoundation (`AVCaptureDevice.authorizationStatus…`)
  and the consent prompt is triggered asynchronously — the menu bar comes up
  first and mic status resolves in the background, surfacing "denied" in the
  status item. Audio capture happens only in the recorder's child process.

## [2.1.2] — 2026-07-16

### Fixed
- **Restart menu item now actually restarts.** It previously only quit. It now
  spawns a detached relauncher (`sh -c 'sleep 1; open -b …'`, new session) that
  fires after the old process exits, then quits.
- **Overlay animation surviving sleep/wake and display changes.** The overlay
  panel was created once for the original screen and reused forever, going
  stale after wake or a display reconfiguration. It now subscribes to
  `NSWorkspaceDidWakeNotification` and
  `NSApplicationDidChangeScreenParametersNotification` and drops the panel
  (recreating it fresh on next show, or in place if visible mid-recording).

### Added
- **Overlay telemetry** — one INFO line per recording on hide:
  `overlay session: <n> state changes, avg tick fps=<n>`, to catch a starved
  animation tick timer.

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

## 2.1.4 — 2026-07-17
- No functional changes: TCC-persistence verification release (stable signing identity test).
