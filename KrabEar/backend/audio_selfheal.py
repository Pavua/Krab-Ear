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
  (default 3) consecutive empty outcomes trigger a soft self-heal by
  delegating to ``AudioReinitCoordinator.reinit_with_wake_word_restore()``
  (see ``backend.audio_reinit``) — the coordinator owns the full dance
  (stop the wake-word listener if running, reinitialize PortAudio
  in-process, restore the listener with its previous model/threshold) plus
  the is_recording-guard and the single-flight lock. A single non-empty
  result at any point resets the streak — the pipeline has proven itself
  healthy.

  A ``DEFERRED_RECORDING``/``BUSY`` outcome from the coordinator means the
  attempt was deferred, not spent: the streak is left as-is (still >=
  threshold) so the very next empty result re-evaluates and retries it. Any
  other outcome (``OK``/``THREAD_HUNG``/``FAILED``) counts as a spent
  attempt. If the streak reaches threshold again right after a spent
  attempt (i.e. the very next recording is *also* empty — the soft fix did
  not help), this escalates loudly via ErrorBus (``audio.stack_wedged``)
  instead of reinit-looping forever, and resets its own state so it can
  try the whole cycle again rather than going silent for the rest of the
  process lifetime. It never restarts the backend process itself — that
  stays a human call (or BackendSupervisor/HealthMonitor on the Swift
  side, which already own process-level restarts).

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


class AudioSelfHealer:
    """Counts consecutive empty recordings and drives a passive self-heal.

    Parameters
    ----------
    reinit_coordinator:
        ``backend.audio_reinit.AudioReinitCoordinator`` (or a duck-typed
        equivalent) exposing ``reinit_with_wake_word_restore() ->
        ReinitOutcome``. Owns the whole reinit dance — stop/restore the
        wake-word listener, the is_recording-guard, PortAudio reinit, and
        the single-flight lock. This class never talks to sounddevice or
        OpenWakeWordAdapter directly.
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
        reinit_coordinator: Any,
        error_bus: Any = None,
        settings_get: Callable[[str, Any], Any] | None = None,
    ) -> None:
        self._reinit_coordinator = reinit_coordinator
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
        when ``audio_selfheal_enabled`` is False. When the empty-streak
        crosses threshold, delegates to
        ``AudioReinitCoordinator.reinit_with_wake_word_restore()``; a
        ``DEFERRED_RECORDING``/``BUSY`` outcome means the attempt is
        deferred (not spent) — the next empty result re-evaluates it.
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
            else:
                action = "reinit"

        if action == "reinit":
            from backend.audio_reinit import ReinitOutcome

            outcome = self._reinit_coordinator.reinit_with_wake_word_restore()
            if outcome in (ReinitOutcome.DEFERRED_RECORDING, ReinitOutcome.BUSY):
                # Попытка отложена, не потрачена — следующий пустой результат
                # переоценит streak (семантика прежнего is_recording-defer).
                logger.info(
                    "AudioSelfHealer: reinit отложен координатором (%s)",
                    getattr(outcome, "value", outcome),
                )
                return
            with self._lock:
                self._reinit_attempted_since_last_success = True
        elif action == "escalate":
            self._escalate()
            with self._lock:
                self._empty_streak = 0
                self._reinit_attempted_since_last_success = False

    # ------------------------------------------------------------------
    # Internal — loud escalation
    # ------------------------------------------------------------------

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
