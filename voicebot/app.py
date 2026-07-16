#!/usr/bin/env python3
"""
VoiceBot — macOS menu bar app for voice-to-text via local mlx-whisper.

Hotkey (configurable, default Cmd+Shift+9):
  - Press once → start recording (animated icon)
  - Press again → stop, transcribe, paste result

Modules:
  config           — JSON config in ~/.voicebot/config.json
  recorder         — sounddevice capture
  whisper_engine   — MLX Whisper model lifecycle
  transcriber      — thin adapter to whisper_engine
  hotkey           — Carbon RegisterEventHotKey wrapper
  paster           — NSPasteboard + Cmd+V
  animations       — menu-bar icon frames
  sounds           — system sound effects
  permissions      — Microphone + Accessibility checks
"""

import logging
import os
import subprocess
import threading

import AppKit
import rumps

from animations import AnimationGenerator
from config import Config
from hotkey import (
    CarbonHotkey,
    VK_MAP,
    cocoa_mods_to_carbon,
    format_shortcut,
)
from focus import FocusProber, OutputGate
from live_transcribe import LiveTranscriber
from overlay import Overlay
from paster import TextPaster
from permissions import check_accessibility, check_microphone, show_alert
from recorder import AudioRecorder
from settings_window import SettingsWindow
from sounds import SoundPlayer
from transcriber import Transcriber
from whisper_engine import WhisperEngine

logger = logging.getLogger("voicebot.app")

# ── Assets ────────────────────────────────────────────────────────────────────
import sys as _sys
_BASE_DIR = getattr(_sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(_BASE_DIR, "assets")
ICON_IDLE = os.path.join(ASSETS_DIR, "icon_idle.png")

# Status-bar hint shown after a silent-mode dictation (text is clipboard-only).
_CLIP_HINT = "📋 In clipboard — ⌘V"


class VoiceBot(rumps.App):

    def __init__(self, cfg: Config):
        # quit_button=None: we supply our own single "Quit" that also unloads
        # the LaunchAgent, so rumps must not add its default Quit item too.
        super().__init__("", quit_button=None)
        self.cfg = cfg

        self.icon = ICON_IDLE
        self.title = ""

        # Menu items — plain text, no emoji.
        self._status_item = rumps.MenuItem("Idle")
        self._last_text_item = rumps.MenuItem("Last transcription",
                                              callback=self._copy_last)

        self.menu = [
            self._status_item,
            self._last_text_item,
            None,
            rumps.MenuItem("Settings...", callback=self._open_settings),
            None,
            rumps.MenuItem("Restart", callback=self._restart),
            rumps.MenuItem("Quit", callback=self._quit_forever),
        ]

        # ── Whisper engine ───────────────────────────────────────────────
        lang = cfg["language"]
        self._engine = WhisperEngine(
            model_name=cfg["whisper_model"],
            language=None if lang == "auto" else lang,
            on_state_change=self._on_engine_state,
            idle_unload_minutes=cfg.get("model_idle_unload_minutes", 15),
        )
        # Kick off model download/load in background — app stays responsive.
        self._engine.ensure_loaded(blocking=False)
        self._transcriber = Transcriber(engine=self._engine, language=lang)

        # ── Audio ────────────────────────────────────────────────────────
        self._recorder = self._new_recorder()

        # ── UX modules ───────────────────────────────────────────────────
        self._settings_window = SettingsWindow(
            config=cfg,
            on_save=self._on_settings_saved,
        )
        self._paster = TextPaster(auto_paste=cfg["auto_paste"])
        self._sounds = SoundPlayer(enabled=cfg["sounds_enabled"])
        self._anims = AnimationGenerator()
        self._overlay = Overlay()

        # Context-aware output. The gate probes the focused UI element (AX) on
        # the worker/live thread and decides type-vs-silent per commit, sticky
        # per recording. Reset at every recording start in _start_worker.
        self._focus_prober = FocusProber()
        self._output_gate = OutputGate(
            self._focus_prober, smart_typing=cfg.get("smart_typing", True),
        )

        # Live (streaming) transcription. Always instantiated; started only
        # when cfg["live_mode"] is true at hotkey-press time.
        self._live = LiveTranscriber(
            recorder=self._recorder,
            engine=self._engine,
            paster=self._paster,
            sample_rate=cfg["sample_rate"],
            poll_seconds=cfg.get("live_poll_seconds", 1.5),
            stability_runs=cfg.get("live_stability_runs", 2),
            gate=self._output_gate,
        )

        # Animation state
        self._current_frames = []
        self._frame_idx = 0
        self._anim_timer = rumps.Timer(self._tick_anim, 1.0 / cfg["anim_fps"])
        self._animating = False

        # ── Hotkey ───────────────────────────────────────────────────────
        self._hotkey = CarbonHotkey()
        self._install_hotkey()

        self._refresh_status_label()
        # Resolve mic TCC without opening a stream (never blocks; the parent is
        # CoreAudio-free). Undetermined → async prompt; denied → status text.
        self._resolve_microphone()
        logger.info("VoiceBot ready")

    def _resolve_microphone(self):
        try:
            from permissions import (
                microphone_status,
                request_microphone_access,
            )
        except Exception:
            logger.exception("permissions import failed")
            return

        denied_title = "⚠️ Microphone denied — System Settings › Privacy"
        status = microphone_status()
        logger.info(f"Microphone authorization: {status}")
        if status == "denied":
            self._on_main(
                lambda: setattr(self._status_item, "title", denied_title))
        elif status == "undetermined":
            def done(granted):
                logger.info("Microphone access %s",
                            "granted" if granted else "denied")
                if granted:
                    self._on_main(self._refresh_status_label)
                else:
                    self._on_main(
                        lambda: setattr(self._status_item, "title",
                                        denied_title))
            request_microphone_access(done)

    # ── Hotkey management ──────────────────────────────────────────────────

    def _install_hotkey(self):
        """Resolve cfg → Carbon mask + vk, then register."""
        mods = self.cfg["hotkey_mods"]
        key = str(self.cfg["hotkey_key"]).lower()
        vk = VK_MAP.get(key)
        if vk is None:
            logger.error(
                f"Unknown hotkey key {key!r} — supported: see hotkey.VK_MAP"
            )
            return False

        carbon_mods = cocoa_mods_to_carbon(mods)
        ok = self._hotkey.register(carbon_mods, vk, self._toggle_recording)
        if ok:
            logger.info(
                f"Hotkey installed: {format_shortcut(mods, vk)} "
                f"(vk={vk}, mods=0x{carbon_mods:x})"
            )
        else:
            logger.error(
                "Hotkey registration failed — check Accessibility permission "
                "or pick a different combo (conflicts with another app)"
            )
        return ok

    def _current_hotkey_label(self):
        vk = VK_MAP.get(str(self.cfg["hotkey_key"]).lower())
        return format_shortcut(self.cfg["hotkey_mods"], vk)

    # ── Recording toggle ───────────────────────────────────────────────────

    def _new_recorder(self):
        """Construct a fresh AudioRecorder (own lock, own stream). Called at
        startup and whenever the previous instance is retired as dead."""
        return AudioRecorder(
            sample_rate=self.cfg["sample_rate"],
            max_duration=self.cfg["max_duration_seconds"],
            on_auto_stop=self._on_auto_stop,
        )

    def _replace_recorder(self):
        """Retire the current recorder and swap in a fresh one. A recorder
        whose stop() wedged inside CoreAudio holds its lock forever; we never
        touch that instance again."""
        self._recorder.mark_dead()
        self._recorder = self._new_recorder()
        self._live._recorder = self._recorder
        logger.info("Replaced AudioRecorder with a fresh instance")

    def _toggle_recording(self):
        # Hotkey callback on the MAIN thread — only read state and spawn a
        # worker. NEVER call into PortAudio here (start OR stop): CoreAudio can
        # deadlock and would freeze the menu bar + hotkey forever.
        logger.info(f"Hotkey triggered. is_recording={self._recorder.is_recording}")
        if not self._recorder.is_recording:
            threading.Thread(target=self._start_worker, daemon=True).start()
        else:
            self._stop_and_send()

    def _start_worker(self):
        """Background start path — never runs on the main thread (see
        _toggle_recording). Swaps in a fresh recorder if the current one is a
        zombie from a timed-out stop."""
        if self._recorder.is_dead:
            self._replace_recorder()

        if not self._recorder.start():
            # A failed start can mean a wedged previous stop still holds the
            # old lock (start() times out on it) or a genuinely unavailable
            # mic. Swap in a clean recorder and try exactly once more.
            self._replace_recorder()
            if not self._recorder.start():
                self._sounds.play("error")
                self._set_state("error", "Microphone unavailable")
                return

        # Fresh output-mode decision per recording (clears sticky-silent),
        # picking up the current smart_typing setting.
        self._output_gate.reset(self.cfg.get("smart_typing", True))

        # Lazy-load weights into MLX while the user speaks. By the time they
        # stop, the ModelHolder cache is hot and transcribe() skips the ~3 s
        # cold weight-load that used to happen on first dictation.
        self._engine.kickoff_warmup()
        if self.cfg.get("live_mode"):
            self._live.start()
        self._sounds.play("start")
        self._set_state("recording")

    def _stop_and_send(self):
        # Runs on the MAIN thread (the hotkey callback is marshalled onto
        # mainQueue). Calling recorder.stop()/live.stop() here can deadlock
        # inside CoreAudio — an AB-BA lock between the main thread's
        # AudioOutputUnitStop and the audio IO thread's startStopCallback —
        # freezing the menu bar and hotkey forever. So the main thread only
        # snapshots state and hands the whole stop→transcribe path to a worker.
        live_was_on = self.cfg.get("live_mode") and self._recorder.is_recording
        pre_stop_audio = self._recorder.snapshot()
        threading.Thread(
            target=self._stop_worker, args=(live_was_on, pre_stop_audio),
            daemon=True,
        ).start()

    def _stop_worker(self, live_was_on, pre_stop_audio):
        """Background stop path — never runs on the main thread (see
        _stop_and_send). Stops live polling and the recorder, then
        transcribes."""
        if live_was_on:
            self._live.stop()

        audio = self._stop_recorder_guarded(pre_stop_audio)
        self._sounds.play("stop")

        if audio is None:
            self._set_state("idle")
            return

        if live_was_on:
            self._finalize_live(audio)
        else:
            self._transcribe_and_paste(audio)

    def _stop_recorder_guarded(self, pre_stop_audio, timeout=10.0):
        """Call recorder.stop() with a hard timeout. PortAudio can deadlock
        inside FinishStoppingStream; if stop() hasn't returned within
        `timeout`, log and fall back to the pre-stop snapshot (audio frames
        live in Python lists and stay readable) so we still transcribe what was
        captured."""
        result = {}

        def _run():
            result["audio"] = self._recorder.stop()

        worker = threading.Thread(target=_run, daemon=True, name="recorder-stop")
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            logger.error(
                "recorder.stop() did not return within %.0fs — likely a "
                "CoreAudio deadlock; retiring this recorder and continuing "
                "with the pre-stop snapshot",
                timeout,
            )
            # The stuck worker holds this recorder's lock forever; retire the
            # instance so the next start() builds a fresh one (never blocks on
            # the zombie lock).
            self._replace_recorder()
            return pre_stop_audio
        return result.get("audio")

    def _on_auto_stop(self, audio):
        """Called from recorder when max duration reached."""
        live_was_on = self.cfg.get("live_mode")
        if live_was_on:
            self._live.stop()
        self._sounds.play("stop")
        target = self._finalize_live if live_was_on else self._transcribe_and_paste
        threading.Thread(target=target, args=(audio,), daemon=True).start()

    # ── Transcription ──────────────────────────────────────────────────────

    def _transcribe_and_paste(self, audio):
        self._set_state("processing")

        text = self._transcriber.transcribe(audio, self.cfg["sample_rate"])

        if text:
            # Context-aware: type into the focused field only if it's editable;
            # otherwise stay silent. Either way the full text lands on the
            # clipboard (paste() sets it; copy() sets it without ⌘V).
            silent = not self._output_gate.allow_typing()
            if silent:
                self._paster.copy(text)
            else:
                self._paster.paste(text)
            self._on_main(
                lambda: setattr(self._last_text_item, "title",
                                f"Last: {text[:40]}...")
            )
            self._sounds.play("success")
            self._overlay.set_state("success")
            self._play_success_anim(hint=_CLIP_HINT if silent else None)
        else:
            self._sounds.play("error")
            self._set_state("error", "Transcription error")

    def _finalize_live(self, audio):
        """Live-mode end-of-recording flush: re-transcribe the full buffer
        and paste anything the streaming loop didn't commit."""
        self._set_state("processing")
        terminal = False
        try:
            self._live.finalize(audio)
            full = self._live.committed
            if full:
                # Always leave the full transcription on the clipboard for ⌘V
                # (recovery path in typed mode, primary path in silent mode).
                self._paster.copy(full)
                silent = self._live.output_silent
                self._on_main(
                    lambda: setattr(self._last_text_item, "title",
                                    f"Last: {full[:40]}...")
                )
                self._sounds.play("success")
                self._overlay.set_state("success")
                self._play_success_anim(hint=_CLIP_HINT if silent else None)
            else:
                self._sounds.play("error")
                self._set_state("error", "Empty transcription")
            terminal = True
        except Exception:
            logger.exception("live finalize failed")
            self._sounds.play("error")
            self._set_state("error", "Live finalize error")
            terminal = True
        finally:
            # Never leave the app pinned in "processing": if we somehow fell
            # through without a terminal state, force back to idle.
            if not terminal:
                logger.error("live finalize: no terminal state reached — idle")
                self._set_state("idle")

    def _play_success_anim(self, hint=None):
        """Success flash. If `hint` is given (silent output mode), show it in
        the status item for ~4s after the flash so the user knows the text is
        on the clipboard for ⌘V, then settle to idle."""
        success_frames = self._anims.get_frames("success")
        if not success_frames:
            self._success_to_idle(hint)
            return

        # Timer start + frame/icon mutation must happen on the main thread.
        def start_frames():
            self._stop_anim()
            self._current_frames = success_frames
            self._frame_idx = 0
            self._anim_timer.start()
            self._animating = True

        self._on_main(start_frames)

        def finish():
            import time
            time.sleep(len(success_frames) / max(self.cfg["anim_fps"], 1))
            self._success_to_idle(hint)

        threading.Thread(target=finish, daemon=True).start()

    def _success_to_idle(self, hint):
        if not hint:
            self._set_state("idle")
            return

        # Silent mode: settle the icon/overlay but keep a "clipboard" hint in
        # the status item for ~4s (marshalled on the main thread), then idle.
        def show_hint():
            self._stop_anim()
            self.icon = ICON_IDLE
            self.title = ""
            self._overlay.hide()
            self._status_item.title = hint

        self._on_main(show_hint)

        def to_idle():
            import time
            time.sleep(4.0)
            self._set_state("idle")

        threading.Thread(target=to_idle, daemon=True).start()

    # ── Engine state callback ──────────────────────────────────────────────

    def _on_engine_state(self, state):
        """Called by WhisperEngine on a worker thread.

        Marshals UI updates onto the main thread (rumps/AppKit requires it).
        """
        def update():
            s = state["state"]
            if s == "downloading":
                pct = int(state.get("progress", 0) * 100)
                self._status_item.title = (
                    f"⏬ Downloading {state['model']}… {pct}%"
                )
            elif s == "loading":
                self._status_item.title = f"⏳ Loading {state['model']}…"
            elif s == "ready":
                self._set_state("idle")
            elif s == "error":
                detail = state.get("detail", "Model failed")
                self._status_item.title = f"⚠ {detail[:60]}"

        self._on_main(update)

    # ── Animation ──────────────────────────────────────────────────────────

    def _tick_anim(self, _timer):
        if not self._current_frames:
            return
        self._frame_idx = (self._frame_idx + 1) % len(self._current_frames)
        self.icon = self._current_frames[self._frame_idx]

    def _start_anim(self, anim_type):
        frames = self._anims.get_frames(anim_type)
        if frames and not self._animating:
            self._current_frames = frames
            self._frame_idx = 0
            self.icon = frames[0]
            self._anim_timer.start()
            self._animating = True

    def _stop_anim(self):
        if self._animating:
            self._anim_timer.stop()
            self._animating = False
            self._current_frames = []

    # ── UI State ───────────────────────────────────────────────────────────

    def _refresh_status_label(self):
        if self._engine.is_ready:
            self._status_item.title = (
                f"Idle — {self._current_hotkey_label()} "
                f"({self.cfg['whisper_model']})"
            )
        else:
            self._status_item.title = f"Loading {self.cfg['whisper_model']}…"

    def _on_main(self, fn):
        """Run `fn` on the main thread's run loop.

        AppKit/rumps UI mutation from a background thread can SIGTRAP while the
        status menu is open (setTitle → menu resize → NSWindow setFrame). Every
        UI touch-point funnels through here so callers may run on any worker.
        """
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(fn)

    def _set_state(self, state, detail=""):
        """Apply a UI state. Self-marshalling: AppKit/rumps mutation must run
        on the main thread, so this is safe to call from any worker thread."""
        def apply():
            hotkey_label = self._current_hotkey_label()
            model = self.cfg["whisper_model"]

            labels = {
                "idle":       f"Idle — {hotkey_label} ({model})",
                "recording":  f"🎙 Recording... ({hotkey_label} to stop)",
                "processing": f"⏳ Processing… ({model})",
                "error":      f"⚠️ {detail}",
            }
            self._status_item.title = labels.get(state, "")

            if state == "idle":
                self._stop_anim()
                self.icon = ICON_IDLE
                self.title = ""
                self._overlay.hide()
            elif state == "recording":
                self._start_anim("recording")
                self._overlay.show()
            elif state == "processing":
                self._stop_anim()
                self._start_anim("processing")
                self._overlay.set_state("processing")
            elif state == "error":
                self._stop_anim()
                self._overlay.set_state("error")
                error_frames = self._anims.get_frames("error")
                if error_frames:
                    self._current_frames = error_frames
                    self._frame_idx = 0
                    self._anim_timer.start()
                    self._animating = True

                self.title = "⚠️"

                def clear_error():
                    import time
                    time.sleep(4.0)
                    self._set_state("idle")

                threading.Thread(target=clear_error, daemon=True).start()

        self._on_main(apply)

    # ── Menu callbacks ─────────────────────────────────────────────────────

    def _copy_last(self, _):
        if self._paster.last_text:
            self._paster.paste(self._paster.last_text)

    def _open_settings(self, _):
        try:
            logger.info("Settings button clicked")
            self._settings_window.show()
        except Exception:
            logger.exception("Error opening settings")

    def _on_settings_saved(self):
        """Hot-reload everything that changed — no restart required."""
        logger.info("Settings saved — applying live")

        # Hotkey
        self._hotkey.unregister()
        self._install_hotkey()

        # Model
        if self._engine.model_name != self.cfg["whisper_model"]:
            self._engine.switch_model(self.cfg["whisper_model"])

        # Language
        new_lang = self.cfg["language"]
        self._transcriber.language = new_lang
        self._engine.language = None if new_lang == "auto" else new_lang

        # Misc
        self._paster.auto_paste = self.cfg["auto_paste"]
        self._sounds.enabled = self.cfg["sounds_enabled"]
        self._engine.set_idle_unload_minutes(
            self.cfg.get("model_idle_unload_minutes", 15)
        )

        self._refresh_status_label()

    def _open_logs(self, _):
        log_file = os.path.join(self.cfg.config_dir, "logs", "voicebot.log")
        subprocess.run(["open", "-a", "Console", log_file], check=False)

    def _restart(self, _):
        # Spawn a detached relauncher that fires AFTER we exit, then quit.
        # (`open -b` on a live instance would only focus it, not respawn.)
        import relaunch
        try:
            self._hotkey.unregister()
        except Exception:
            pass
        relaunch.relaunch_detached()
        rumps.quit_application()

    def _quit_forever(self, _):
        # Unload the login agent for this session (leaves the plist so
        # start-at-login still fires next login).
        import autostart
        autostart.unload_session()
        try:
            self._hotkey.unregister()
            self._engine.shutdown()
        except Exception:
            pass
        rumps.quit_application()
