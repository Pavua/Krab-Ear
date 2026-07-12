"""AudioSelfHealer — passive, counter-driven self-heal for a wedged audio stack.

Root cause (prod incident 2026-07-12): the long-lived backend process
(launchd ai.krab.ear.backend, python service.py) ended up with a wedged
sounddevice/PortAudio stack — streams opened without raising any exception,
but every read returned all-zero frames, and the input-device list went
stale (1 device instead of 3). No exception anywhere to hang error handling
off; the only symptom observable from inside the process is that every
completed recording comes back empty ("Аудио пустое, попробуйте ещё раз",
wake word silent). The incident was invisible for 9 days (last successful
transcription 3 июля) and only a full process restart cleared it
(test_microphone: rms 0.0 -> 0.078). Likely trigger: a PortAudioError -9986
storm on 07-07/07-08 (37k errors in the parent's backend log).

Design (MVP — one shape only, do not add alternate strategies):

  The trigger is PASSIVE. This class never opens a microphone stream or
  polls anything on a timer; it only counts outcomes that
  ``RecordingCoreService.handle_stop_recording`` already classifies for
  other reasons — an RMS-below-threshold silence-guard trip, or an empty
  transcript at nonzero duration. ``audio_selfheal_empty_threshold``
  (default 3) consecutive empty outcomes trigger a soft self-heal: stop the
  wake-word listener (if one is running), reinitialize PortAudio in-process
  (``sd._terminate()`` / ``sd._initialize()``), restart the wake-word
  listener with its previous model/threshold. A single non-empty result at
  any point resets the streak — the pipeline has proven itself healthy.

  If the streak reaches threshold again right after a reinit attempt (i.e.
  the very next recording is *also* empty — the soft fix did not help),
  this escalates loudly via ErrorBus (``audio.stack_wedged``) instead of
  reinit-looping forever, and resets its own state so it can try the whole
  cycle again rather than going silent for the rest of the process
  lifetime. It never restarts the backend process itself — that stays a
  human call (or BackendSupervisor/HealthMonitor on the Swift side, which
  already own process-level restarts).

Every collaborator is injected so this class is fully unit-testable with
plain fakes — no real audio, no real sounddevice, no real
OpenWakeWordAdapter, no BackendService.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

logger = logging.getLogger("KrabEar.Backend.AudioSelfHeal")

_THRESHOLD_MIN = 2
_THRESHOLD_MAX = 10
_THRESHOLD_DEFAULT = 3
_WAKE_WORD_THRESHOLD_DEFAULT = 0.5


class AudioSelfHealer:
    """Counts consecutive empty recordings and drives a passive self-heal.

    Parameters
    ----------
    reinit_audio_backend:
        Zero-arg callable that reinitializes the audio backend (production:
        ``sd._terminate(); sd._initialize()``). Only ever called when
        ``is_recording()`` reports idle.
    is_recording:
        Zero-arg callable returning whether a recording is currently active.
        Consulted at the moment the empty-streak crosses the threshold so a
        reinit attempt never interrupts live capture; if recording is
        active the attempt is deferred (not dropped) — the next empty
        result re-evaluates it.
    wake_word_adapter:
        Duck-typed collaborator exposing ``is_running() -> bool``,
        ``active_model() -> str | None``, ``active_threshold() -> float | None``,
        ``stop() -> None`` and ``start(model_name, on_detected, threshold=...)``
        (see ``backend.openwakeword_adapter.OpenWakeWordAdapter``). Optional
        — pass ``None`` when wake-word wiring is not available.
    error_bus:
        Duck-typed collaborator exposing ``push(KrabError) -> bool`` (see
        ``backend.error_bus.ErrorBus``). Optional — escalation is skipped
        (logged only) when absent.
    settings_get:
        ``(key, default) -> Any`` runtime settings reader, same shape as
        ``BackendService._get_runtime_setting``. Defaults to always
        returning *default* (i.e. self-heal enabled with threshold=3) when
        not provided.
    """

    def __init__(
        self,
        *,
        reinit_audio_backend: Callable[[], None],
        is_recording: Callable[[], bool],
        wake_word_adapter: Any = None,
        error_bus: Any = None,
        settings_get: Callable[[str, Any], Any] | None = None,
    ) -> None:
        self._reinit_audio_backend = reinit_audio_backend
        self._is_recording = is_recording
        self._wake_word_adapter = wake_word_adapter
        self._error_bus = error_bus
        self._settings_get: Callable[[str, Any], Any] = settings_get or (lambda _k, d: d)

        self._lock = threading.Lock()
        self._empty_streak = 0
        self._reinit_attempted_since_last_success = False

    # ------------------------------------------------------------------
    # Settings (runtime-overridable — see core/config.py DEFAULT_SETTINGS
    # and backend/settings_validator.py _BOOL_FIELDS/_RANGE_FIELDS)
    # ------------------------------------------------------------------

    def _enabled(self) -> bool:
        try:
            return bool(self._settings_get("audio_selfheal_enabled", True))
        except Exception:
            return True

    def _threshold(self) -> int:
        try:
            value = int(self._settings_get("audio_selfheal_empty_threshold", _THRESHOLD_DEFAULT))
        except (TypeError, ValueError):
            value = _THRESHOLD_DEFAULT
        return max(_THRESHOLD_MIN, min(_THRESHOLD_MAX, value))

    # ------------------------------------------------------------------
    # Public API — called from RecordingCoreService.handle_stop_recording
    # ------------------------------------------------------------------

    def record_success(self) -> None:
        """A recording just produced real, non-empty transcribed audio.

        Resets the empty streak and clears the "reinit already attempted"
        flag — the audio pipeline has just proven itself healthy again.
        """
        with self._lock:
            self._empty_streak = 0
            self._reinit_attempted_since_last_success = False

    def record_empty_result(self) -> None:
        """A completed recording ended up empty: an RMS-below-threshold
        silence-guard trip, or an empty transcript at nonzero duration.

        Passive — never opens/reads a real audio stream itself, only
        inspects an in-memory counter. No-ops entirely (no state mutation)
        when ``audio_selfheal_enabled`` is False.
        """
        if not self._enabled():
            return

        action: str | None = None
        with self._lock:
            self._empty_streak += 1
            threshold = self._threshold()
            if self._empty_streak < threshold:
                return
            if self._reinit_attempted_since_last_success:
                action = "escalate"
            elif self._is_recording():
                # A new recording started in the gap between the empty result
                # completing and us evaluating the streak here. Never reinit
                # while audio is actively flowing. Streak is left as-is (still
                # >= threshold) so the very next empty result re-evaluates —
                # the attempt is deferred, not dropped.
                logger.info(
                    "AudioSelfHealer: streak=%d >= %d, но идёт активная запись — "
                    "reinit отложен",
                    self._empty_streak, threshold,
                )
                return
            else:
                action = "reinit"
                self._reinit_attempted_since_last_success = True

        if action == "reinit":
            self._perform_reinit()
        elif action == "escalate":
            self._escalate()
            with self._lock:
                self._empty_streak = 0
                self._reinit_attempted_since_last_success = False

    # ------------------------------------------------------------------
    # Internal — soft self-heal (reinit) and loud escalation
    # ------------------------------------------------------------------

    def _perform_reinit(self) -> None:
        logger.warning(
            "AudioSelfHealer: %d пустых записей подряд — переинициализация "
            "аудио-стека (PortAudio)",
            self._empty_streak,
        )
        saved_model: str | None = None
        saved_threshold: float | None = None
        wake_word_was_running = False

        if self._wake_word_adapter is not None:
            try:
                wake_word_was_running = bool(self._wake_word_adapter.is_running())
            except Exception:
                logger.exception("AudioSelfHealer: wake_word_adapter.is_running() упал")
                wake_word_was_running = False
            if wake_word_was_running:
                try:
                    saved_model = self._wake_word_adapter.active_model()
                    get_threshold = getattr(self._wake_word_adapter, "active_threshold", None)
                    saved_threshold = get_threshold() if callable(get_threshold) else None
                except Exception:
                    logger.exception(
                        "AudioSelfHealer: не удалось прочитать состояние wake word перед reinit"
                    )
                try:
                    self._wake_word_adapter.stop()
                except Exception:
                    logger.exception("AudioSelfHealer: wake_word_adapter.stop() перед reinit упал")

        try:
            self._reinit_audio_backend()
        except Exception:
            logger.exception("AudioSelfHealer: reinit_audio_backend завершился с исключением")

        if self._wake_word_adapter is not None and wake_word_was_running and saved_model:
            try:
                self._wake_word_adapter.start(
                    saved_model,
                    self._on_wake_word_detected_after_reinit,
                    threshold=(
                        saved_threshold
                        if saved_threshold is not None
                        else _WAKE_WORD_THRESHOLD_DEFAULT
                    ),
                )
            except Exception:
                logger.exception(
                    "AudioSelfHealer: не удалось перезапустить wake word после reinit"
                )

    def _on_wake_word_detected_after_reinit(self, model_name: str, score: float) -> None:
        """Default on_detected callback for the post-reinit wake-word restart.

        Detection propagation to the Swift agent happens via
        ``OpenWakeWordAdapter._record_detection()`` (called unconditionally
        inside the listener loop before this callback runs), so this only
        needs to log.
        """
        logger.info(
            "AudioSelfHealer: wake word обнаружен после reinit (model=%r, score=%.3f)",
            model_name, score,
        )

    def _escalate(self) -> None:
        logger.error(
            "AudioSelfHealer: аудио-стек всё ещё пуст после переинициализации — эскалация"
        )
        if self._error_bus is None:
            return
        try:
            from datetime import datetime, timezone

            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY

            entry = ERROR_REGISTRY.get("audio.stack_wedged", {})
            self._error_bus.push(KrabError(
                severity=entry.get("severity", "error"),
                component="audio",
                code="audio.stack_wedged",
                message_user=entry.get(
                    "user_msg_ru", "Аудио-стек завис — перезапустите Krab Ear",
                ),
                message_debug=(
                    "Пустые записи повторились после переинициализации PortAudio "
                    f"(streak={self._empty_streak})"
                ),
                timestamp=datetime.now(timezone.utc),
                context={"empty_streak": self._empty_streak},
                actionable=False,
                action_id=None,
            ))
        except Exception:
            logger.exception("AudioSelfHealer: ErrorBus.push упал при эскалации")
