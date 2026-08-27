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
from collections import deque
from typing import Any, Callable

logger = logging.getLogger("KrabEar.Backend.WakeWordWatchdog")

_STALE_SEC_MIN = 10.0
_STALE_SEC_MAX = 120.0
_STALE_SEC_DEFAULT = 30.0
_CHECK_INTERVAL_SEC_DEFAULT = 5.0
# Анти-голодание (ревью Task 4): частые легитимные паузы (диктовки)
# сбрасывают эпизод раньше второй stale-проверки — без окна поверх эпизодов
# сломанный навсегда поток лечился бы вечно, не эскалируя. THREAD_HUNG-танцы
# тоже учитываются в окне (каждый стоит stop-join и зомби-тред).
_HEAL_STORM_WINDOW_SEC = 600.0
_HEAL_STORM_MAX = 3
# Сколько подряд выходов цикла по голоданию стрима (без единого живого кадра
# между ними) считаем неизлечимым клином. Таймлайн при поллере 10с: выходы на
# ~t+3/+16/+29с → эскалация за 30-40с, сопоставимо со stale_sec=30 для обычной
# staleness. 🔴 Критерий ИМЕННО счётчик, а не собственный таймер: два критерия
# и два писателя wedged разъехались бы при первой же правке констант.
_MAX_STARVE_EXITS = 3


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
        is_recording: Callable[[], bool] | None = None,
        is_worker_hung: Callable[[], bool] | None = None,
        settings_get: Callable[[str, Any], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        check_interval_sec: float = _CHECK_INTERVAL_SEC_DEFAULT,
    ) -> None:
        self._adapter = adapter
        self._coordinator = reinit_coordinator
        self._error_bus = error_bus
        # Живой инцидент 2026-07-16: meeting-запись не снимает wake-word
        # слушатель (хоткейный путь снимает через WakeWordPoller) — адаптер
        # легитимно голодает по чанкам всю запись. Без этого чека staleness-
        # шторм доводил до wedged-эскалации и kickstart'а backend'а ПОСРЕДИ
        # финальной транскрипции встречи.
        self._is_recording = is_recording
        # W8: «worker рекордера жив, но записи нет» — состояние, в котором
        # wake_word_start отвергается гейтом W7, сессия не создаётся, и без
        # этого сигнала watchdog считал бы тишину легитимной паузой.
        self._is_worker_hung = is_worker_hung
        self._settings_get: Callable[[str, Any], Any] = settings_get or (lambda _k, d: d)
        self._clock = clock
        self._check_interval_sec = check_interval_sec

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._heal_attempted_this_episode = False
        self._escalated_this_episode = False
        # Скользящее окно успешно ВЫДАННЫХ heal'ов поверх эпизодов (Fix C):
        # DEFERRED/BUSY-ретраи сюда не попадают.
        self._heal_history: deque[float] = deque(maxlen=32)
        # Момент первого наблюдения dead-session аномалии (Fix D):
        # model выставлен, но тред не жив — сигнатура упавшего restore.
        self._anomaly_since: float | None = None

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _enabled(self) -> bool:
        try:
            return bool(self._settings_get("wake_word_watchdog_enabled", True))
        except Exception:
            return True

    def _worker_hung_active(self) -> bool:
        """Заблокирован ли старт живым зависшим worker'ом рекордера."""
        if self._is_worker_hung is None:
            return False
        try:
            return bool(self._is_worker_hung())
        except Exception:
            # fail-safe в сторону МОЛЧАНИЯ: ложная эскалация = kickstart
            # backend'а посреди работы владельца, это дороже пропуска.
            logger.exception("WakeWordWatchdog: is_worker_hung() упал")
            return False

    def _recording_active(self) -> bool:
        if self._is_recording is None:
            return False
        try:
            return bool(self._is_recording())
        except Exception:
            # fail-open в сторону РАБОТАЮЩЕГО watchdog'а: ошибочный пропуск
            # лечения хуже лишнего deferred-танца (координатор сам
            # перепроверяет is_recording перед Pa_Terminate).
            logger.exception("WakeWordWatchdog: is_recording() упал")
            return False

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
            if thread.is_alive():
                logger.warning(
                    "WakeWordWatchdog: тред не завершился за 2.0с (вероятно, "
                    "тик внутри reinit-танца) — daemon доработает текущий тик "
                    "и выйдет по stop_event"
                )

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

        if self._recording_active():
            # Активная запись = легитимная пауза источника чанков: ни heal,
            # ни эскалация; эпизод/аномалия сбрасываются (симметрия чистой
            # паузе ниже). После записи адаптер либо оживёт сам, либо watchdog
            # снова возьмёт его в работу со свежим эпизодом.
            with self._lock:
                self._anomaly_since = None
            self._reset_episode()
            return None

        try:
            running = bool(self._adapter.is_running())
            model = self._adapter.active_model()
        except Exception:
            logger.exception("WakeWordWatchdog: опрос адаптера упал")
            return None
        session_active = running and model is not None

        if not session_active:
            if model is not None and not running:
                # Мёртвая сессия: слушатель ДОЛЖЕН жить (model выставлен —
                # чистый stop() его зануляет), но треда нет. Сигнатура
                # упавшего restore / умершего цикла. Эпизод НЕ сбрасываем —
                # иначе полностью мёртвый слушатель маскировался бы под паузу
                # (worst case ревью Task 4).
                return self._handle_dead_session()
            if self._worker_hung_active() and not self._recording_active():
                # W8 (2026-08-18): сессии нет НЕ потому, что владелец диктует, а
                # потому что wake_word_start отвергается гейтом W7 — worker
                # рекордера физически жив после stop()-таймаута. Само это не
                # рассосётся (класс 08-07 держит PortAudio часами), а Swift
                # ретраит вечно, не жгя бюджет. Без этой ветки эпизод
                # сбрасывался каждый тик, wedged был недостижим, и подсистема
                # молча простаивала — ровно то, против чего 2026-08-09 вводился
                # DEFERRED_WORKER_HUNG.
                return self._handle_blocked_start()
            # Голодание стрима: сессии нет не из-за паузы, а потому что
            # PortAudio-стрим не отдавал кадры и цикл вышел сам (guarded read,
            # спека 2026-08-23). Без этой ветки причина попадала бы в «чистую
            # паузу» ниже, эпизод сбрасывался бы каждый тик, и wedged был бы
            # недостижим — подсистема молчала бы вечно.
            if not self._recording_active():
                try:
                    hb_starve = self._adapter.heartbeat() or {}
                except Exception:
                    hb_starve = {}
                if hb_starve.get("starvation_active"):
                    return self._handle_starved_stream(hb_starve)

            # Чистая пауза (recording/conversation/TTS/privacy): Swift снял
            # слушатель через stop(). Эпизод и аномалия сбрасываются.
            with self._lock:
                self._anomaly_since = None
            self._reset_episode()
            return None

        with self._lock:
            self._anomaly_since = None

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
            # Анти-шторм (Fix C): >= _HEAL_STORM_MAX выданных heal'ов за
            # окно — поток сломан навсегда, а частые легитимные паузы
            # сбрасывают эпизод раньше эскалации; эскалируем поверх эпизодов.
            with self._lock:
                while (self._heal_history
                       and now - self._heal_history[0] > _HEAL_STORM_WINDOW_SEC):
                    self._heal_history.popleft()
                storm = len(self._heal_history) >= _HEAL_STORM_MAX
            if storm:
                self._escalate(staleness, "heal_storm")
                return "escalated"

            from backend.audio_reinit import ReinitOutcome

            logger.warning(
                "WakeWordWatchdog: heartbeat stale %.1fs (порог %.1fs) — "
                "мягкое лечение через координатор",
                staleness, stale_sec,
            )
            outcome = self._coordinator.reinit_with_wake_word_restore()
            if outcome in (ReinitOutcome.DEFERRED_RECORDING, ReinitOutcome.BUSY):
                return None  # попытка отложена, не потрачена
            if outcome in (ReinitOutcome.THREAD_HUNG, ReinitOutcome.DEFERRED_WORKER_HUNG):
                # THREAD_HUNG-танец тоже стоил 3с stop-join и оставил
                # зомби-тред — анти-шторм обязан видеть и такие танцы
                # (Fable-гейт волны, Finding 1b), иначе цикл
                # respawn→hang→dance не капится никогда.
                # DEFERRED_WORKER_HUNG (ревью 2026-08-09, F1): координатор
                # даже не дошёл до adapter.stop() — рекордерский worker-тред
                # уже заклинил (is_worker_thread_alive=True, is_recording
                # лжёт False после stop()-таймаута). В отличие от настоящей
                # диктовки (DEFERRED_RECORDING, которая разрешится сама),
                # этот случай не разрешится без внешнего рестарта — трактуем
                # как ПОТРАЧЕННУЮ попытку тем же путём, что THREAD_HUNG,
                # иначе wedged:true недостижим и эскалация к Swift-агенту
                # (единственному, кто умеет kickstart -k backend) не наступит
                # никогда — тихий бессрочный простой wake-word подсистемы.
                with self._lock:
                    self._heal_history.append(now)
                self._escalate(staleness, str(getattr(outcome, "value", outcome)))
                return "escalated"
            with self._lock:
                self._heal_attempted_this_episode = True
                self._heal_history.append(now)
            return "healed"

        self._escalate(staleness, "stale_after_reinit")
        return "escalated"

    # ------------------------------------------------------------------

    def _handle_blocked_start(self) -> str | None:
        """Старт слушателя заблокирован живым зависшим worker'ом рекордера.

        Таймер тот же, что у dead-session: даём `stale_sec` на естественный
        выход worker'а (гейт самоочищается по `is_worker_thread_alive`), затем
        эскалируем. Мягкое лечение не пробуем: координатор сам откажется —
        `Pa_Terminate` под живым стримом рекордера это crash-класс.
        """
        now = self._clock()
        with self._lock:
            if self._escalated_this_episode:
                return None
            if self._anomaly_since is None:
                self._anomaly_since = now
                return None
            elapsed = now - self._anomaly_since
        if elapsed < self._stale_sec():
            return None
        self._escalate(elapsed, "blocked_start_worker_hung")
        return "escalated"

    def _handle_starved_stream(self, heartbeat: dict[str, Any]) -> str | None:
        """Стрим не отдаёт кадры: ведём аномалию и эскалируем ПО СЧЁТЧИКУ.

        Мягкое лечение здесь не пробуем: перезапуск сессии — домен
        поллер-self-heal'а, он и так ретраит каждые ~10 с. Наша задача —
        не дать причине раствориться в «чистой паузе» и довести неизлечимый
        случай до wedged, где его подхватит агент.

        🔴 Порог — число подряд-выходов без живого кадра, а не время: адаптер
        уже считает его сам, а второй (временной) критерий сделал бы двух
        писателей одного side-effect с разъезжающимися порогами.
        """
        now = self._clock()
        streak = 0
        try:
            streak = int(heartbeat.get("consecutive_starve_exits") or 0)
        except (TypeError, ValueError):
            streak = 0

        with self._lock:
            if self._escalated_this_episode:
                return None
            if self._anomaly_since is None:
                self._anomaly_since = now
            elapsed = now - self._anomaly_since

        if streak < _MAX_STARVE_EXITS:
            return None
        self._escalate(elapsed, f"starved_stream:{streak}")
        return "escalated"

    def _handle_dead_session(self) -> str | None:
        """Сессия должна жить, но треда нет: даём поллер-self-heal'у
        stale_sec на оживление, затем эскалируем (heal бессмыслен —
        координаторский restore сам только что не смог поднять сессию)."""
        now = self._clock()
        with self._lock:
            if self._escalated_this_episode:
                return None
            if self._anomaly_since is None:
                self._anomaly_since = now
                return None
            elapsed = now - self._anomaly_since
        if elapsed < self._stale_sec():
            return None
        self._escalate(elapsed, "dead_session")
        return "escalated"

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
                    "user_msg_ru", "Wake word завис — требуется перезапуск Krab Ear…",
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
