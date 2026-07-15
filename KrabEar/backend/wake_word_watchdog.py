"""WakeWordWatchdog — активный сторож независимого wake-word аудио-потока
(спека docs/superpowers/specs/2026-07-15-wake-word-watchdog-design.md §4.3).

Закрывает подтверждённый пробел покрытия: AudioSelfHealer триггерится только
пустыми ДИКТОВКАМИ, а заклинивший wake-word поток (тред жив, CoreAudio не
отдаёт кадры — живой инцидент 2026-07-13, Sentry KRAB-EAR-BACKEND-1J) для него
невидим, wake_word_status.running при этом врёт true (голый thread.is_alive()).

Семантика эпизода: «эпизод» — непрерывный интервал staleness внутри одной
сессии слушателя. Закрывается ТОЛЬКО реальным свежим чанком (не свежим
listen_started_ts! — иначе после heal новая сессия закрывала бы эпизод своим
grace-окном и watchdog зациклился бы heal'ом, никогда не эскалируя),
неактивной сессией или рестартом процесса.

Направление отказа — fail-safe: ложный staleness стоит один лишний цикл
stop/reinit/start (~1-2с тишины микрофона) один раз на эпизод; исключение в
тике ловится и логируется, тред живёт.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger("KrabEar.Backend.WakeWordWatchdog")

_STALE_SEC_MIN = 10.0
_STALE_SEC_MAX = 120.0
_STALE_SEC_DEFAULT = 30.0
_CHECK_INTERVAL_SEC_DEFAULT = 5.0


class WakeWordWatchdog:
    """Таймер-тред: проверяет heartbeat слушателя, лечит через координатор,
    эскалирует wedged-флагом + ErrorBus.

    Все коллабораторы инжектятся (duck-typed) — тестируется фейками без
    sounddevice/реального адаптера:
      adapter: is_running(), active_model(), heartbeat(), set_wedged(), is_wedged()
      reinit_coordinator: reinit_with_wake_word_restore() -> ReinitOutcome
      error_bus: push(KrabError) | None
      settings_get: (key, default) -> Any
      clock: () -> float (monotonic)
    """

    def __init__(
        self,
        *,
        adapter: Any,
        reinit_coordinator: Any,
        error_bus: Any = None,
        settings_get: Callable[[str, Any], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        check_interval_sec: float = _CHECK_INTERVAL_SEC_DEFAULT,
    ) -> None:
        self._adapter = adapter
        self._coordinator = reinit_coordinator
        self._error_bus = error_bus
        self._settings_get: Callable[[str, Any], Any] = settings_get or (lambda _k, d: d)
        self._clock = clock
        self._check_interval_sec = check_interval_sec

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._heal_attempted_this_episode = False
        self._escalated_this_episode = False

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _enabled(self) -> bool:
        try:
            return bool(self._settings_get("wake_word_watchdog_enabled", True))
        except Exception:
            return True

    def _stale_sec(self) -> float:
        try:
            value = float(self._settings_get("wake_word_stale_sec", _STALE_SEC_DEFAULT))
        except (TypeError, ValueError):
            value = _STALE_SEC_DEFAULT
        return max(_STALE_SEC_MIN, min(_STALE_SEC_MAX, value))

    # ------------------------------------------------------------------
    # Lifecycle (start()/stop() — stop() обязателен в BackendService.close())
    # ------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="WakeWordWatchdog",
            )
            self._thread.start()
        logger.info(
            "WakeWordWatchdog: запущен (interval=%.1fs)", self._check_interval_sec,
        )

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._thread = None
        self._stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop_event.wait(self._check_interval_sec):
            try:
                self.check_once()
            except Exception:
                logger.exception("WakeWordWatchdog: тик упал")

    # ------------------------------------------------------------------
    # Один тик (чистая логика — юниты зовут напрямую)
    # ------------------------------------------------------------------

    def check_once(self) -> str | None:
        """Возвращает выполненное действие: "healed" | "escalated" | None."""
        if not self._enabled():
            return None

        try:
            session_active = bool(self._adapter.is_running()) and (
                self._adapter.active_model() is not None
            )
        except Exception:
            logger.exception("WakeWordWatchdog: опрос адаптера упал")
            return None

        if not session_active:
            # Легитимные паузы (recording/conversation/TTS/privacy) выглядят
            # именно так — Swift шлёт wake_word_stop. Эпизод сбрасывается.
            self._reset_episode()
            return None

        hb = self._adapter.heartbeat()
        started = hb.get("listen_started_ts")
        last = hb.get("last_chunk_ts")
        if started is None:
            return None  # тред спавнут, но не вошёл в цикл — свежий

        now = self._clock()
        stale_sec = self._stale_sec()

        # Эпизод закрывает ТОЛЬКО реальный свежий чанк (см. докстринг модуля).
        if last is not None and (now - last) < stale_sec:
            self._close_episode_fresh()
            return None

        staleness = now - max(started, last or 0.0)
        if staleness < stale_sec:
            return None  # grace-окно прогрева: не алармим и не закрываем эпизод

        with self._lock:
            heal_tried = self._heal_attempted_this_episode
            escalated = self._escalated_this_episode
        if escalated:
            return None

        if not heal_tried:
            from backend.audio_reinit import ReinitOutcome

            logger.warning(
                "WakeWordWatchdog: heartbeat stale %.1fs (порог %.1fs) — "
                "мягкое лечение через координатор",
                staleness, stale_sec,
            )
            outcome = self._coordinator.reinit_with_wake_word_restore()
            if outcome in (ReinitOutcome.DEFERRED_RECORDING, ReinitOutcome.BUSY):
                return None  # попытка отложена, не потрачена
            if outcome == ReinitOutcome.THREAD_HUNG:
                self._escalate(staleness, str(getattr(outcome, "value", outcome)))
                return "escalated"
            with self._lock:
                self._heal_attempted_this_episode = True
            return "healed"

        self._escalate(staleness, "stale_after_reinit")
        return "escalated"

    # ------------------------------------------------------------------

    def _reset_episode(self) -> None:
        with self._lock:
            self._heal_attempted_this_episode = False
            self._escalated_this_episode = False

    def _close_episode_fresh(self) -> None:
        self._reset_episode()
        try:
            if self._adapter.is_wedged():
                logger.info("WakeWordWatchdog: heartbeat ожил — снимаю wedged")
                self._adapter.set_wedged(False)
        except Exception:
            logger.exception("WakeWordWatchdog: сброс wedged упал")

    def _escalate(self, staleness: float, reason: str) -> None:
        with self._lock:
            self._heal_attempted_this_episode = True
            self._escalated_this_episode = True
        logger.error(
            "WakeWordWatchdog: мягкое лечение невозможно/не помогло (%s, "
            "staleness=%.1fs) — wedged:true, лечение на стороне агента",
            reason, staleness,
        )
        try:
            self._adapter.set_wedged(True)
        except Exception:
            logger.exception("WakeWordWatchdog: set_wedged упал")
        if self._error_bus is None:
            return
        try:
            from datetime import datetime, timezone

            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY

            entry = ERROR_REGISTRY.get("audio.wakeword_wedged", {})
            self._error_bus.push(KrabError(
                severity=entry.get("severity", "error"),
                component="audio",
                code="audio.wakeword_wedged",
                message_user=entry.get(
                    "user_msg_ru", "Wake word завис — перезапускаю Krab Ear…",
                ),
                message_debug=(
                    f"wake-word heartbeat stale {staleness:.1f}s, reason={reason}"
                ),
                timestamp=datetime.now(timezone.utc),
                context={"staleness_sec": round(staleness, 1), "reason": reason},
                actionable=False,
                action_id=None,
            ))
        except Exception:
            logger.exception("WakeWordWatchdog: ErrorBus.push упал при эскалации")

    # ------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """Снапшот для get_diagnostics."""
        try:
            session_active = bool(self._adapter.is_running()) and (
                self._adapter.active_model() is not None
            )
        except Exception:
            session_active = False
        staleness: float | None = None
        try:
            hb = self._adapter.heartbeat()
            started = hb.get("listen_started_ts")
            last = hb.get("last_chunk_ts")
            if started is not None:
                staleness = self._clock() - max(started, last or 0.0)
        except Exception:
            pass
        wedged = False
        try:
            wedged = bool(self._adapter.is_wedged())
        except Exception:
            pass
        with self._lock:
            heal_attempted = self._heal_attempted_this_episode
        return {
            "enabled": self._enabled(),
            "session_active": session_active,
            "staleness_sec": round(staleness, 3) if staleness is not None else None,
            "heal_attempted_this_episode": heal_attempted,
            "wedged": wedged,
        }
