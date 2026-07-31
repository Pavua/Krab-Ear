"""RestWatchdog — активный сторож встроенного in-process REST-сервера.

Спека: docs/superpowers/specs/2026-07-31-s3-rest-flip-design.md §Р6.
План: docs/superpowers/plans/2026-07-31-s3-inprocess-rest-enable.md,
задачи 7a/7b.

Задача 7 разделена надвое (аналогично сторожу wake-word, PR #1879— тот же
объём кода слишком велик для одного воркера). **Это 7a** — сам модуль:
тик состояния, HTTP-проба, серия провалов, различение «занят порт» от
«REST мёртв», анти-шторм лечений, код ошибки ``rest.wedged``. **7b**
довооружает этот же файл: терминальную защёлку на время останова backend'а
(``shutting_down``), проводку в ``service.py`` и реальный неблокирующий
``restart()`` в ``rest_inprocess.py``.

Закрывает пробел: до волны S3 REST жил отдельным launchd-юнитом с
``KeepAlive=true`` — система сама поднимала его после любой смерти. После
слияния процессов эта гарантия исчезает: ``InProcessRestServer._serve`` при
падении ``serve_forever`` пишет ``_error`` и логирует — и всё, ни пуша
ErrorBus, ни рестарта. При этом ``get_diagnostics`` не поллится фоново ни в
Python, ни в Swift: различимость состояний в коде есть, наблюдателя — нет.

Направление отказа: WEDGED-состояние деградирует ТОЛЬКО REST, но никогда не
эскалирует в ``BackendSupervisor.forceRestartBackend()`` — тот перезапускает
ВЕСЬ backend через ``kickstart -k`` и теряет активную диктовку/встречу.
Деградация REST предпочтительнее потери записи владельца (сторож это не
делает сам — он просто не умеет эскалировать выше ``rest.wedged``).

Контракт владельца (``owner``, duck-typed, инжектируется конструктором):

    owner.status() -> dict
        - "running": bool — сервер сейчас слушает.
        - "enabled": bool, опционально (default True) — режим in-process
          REST включён вообще. При False сторож не делает ничего — нечего
          сторожить.
        - "tombstone": bool, опционально (default False) — сборка
          REST-приложения упала целиком (``_RestInProcessTombstone``,
          S3/Задача 4). Лечить нечего: ``rest.startup_failed`` уже отправлен
          в service.py, повторная эскалация ``rest.wedged`` была бы дублем
          одного и того же отказа.
        - "ever_served": bool, опционально (default False) — сервер хотя бы
          раз успешно слушал за жизнь текущего процесса («был живой serve»,
          поле добавляется в S3/Задаче 7b внутри ``InProcessRestServer``).
          Без него сторож НИКОГДА не лечит REST, который ни разу не
          поднялся (типично — конфликт порта на старте): это осознанный
          краевой случай, штатный поток включения всегда рестартит backend
          целиком, обходить это по своей инициативе не нужно.
        - "port": int, опционально (default 5005) — используется дефолтной
          HTTP-пробой для построения URL.
    owner.restart() -> bool
        ``stop()`` + ``start()``. True — лечение сработало. В 7a вызывается
        как есть; неблокирующий контракт (см. план, п.6) — ответственность
        7b в ``rest_inprocess.py``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable

logger = logging.getLogger("KrabEar.Backend.RestWatchdog")

# Тик 10с (п.1 задачи): дешёвый локальный опрос status() — читается каждый
# тик независимо от того, наступило ли время сетевой пробы.
_TICK_INTERVAL_SEC_DEFAULT = 10.0
# Проба /health раз в 30с (п.2): реже тика — сетевой запрос дороже локального
# status().
_PROBE_INTERVAL_SEC_DEFAULT = 30.0
_PROBE_TIMEOUT_SEC = 2.0
# Серия, а не одно наблюдение (п.3): N>=2 подряд провала пробы = не менее
# 60с непрерывного нездоровья (проба раз в 30с). Симметрично тридцати-
# секундному порогу staleness у сторожа wake-word (тот же класс защиты от
# одного выброса под нагрузкой — финальная транскрипция встречи держит CPU).
CONSECUTIVE_FAILURES_TO_HEAL = 2
# Анти-шторм (п.8): не более 3 лечений за скользящее окно 600с.
_HEAL_STORM_WINDOW_SEC = 600.0
_HEAL_STORM_MAX = 3


class RestWatchdog:
    """Daemon-тред: проверяет health встроенного REST, лечит через
    ``owner.restart()``, эскалирует ``KrabError('rest.wedged')`` после
    исчерпания анти-шторма лечений."""

    def __init__(
        self,
        *,
        owner: Any,
        probe: Callable[[], bool] | None = None,
        error_bus: Any = None,
        clock: Callable[[], float] = time.monotonic,
        tick_interval_sec: float = _TICK_INTERVAL_SEC_DEFAULT,
        probe_interval_sec: float = _PROBE_INTERVAL_SEC_DEFAULT,
    ) -> None:
        self._owner = owner
        # Проба — тоже duck-typed колбэк: () -> bool, True = сервер жив
        # (любой HTTP-ответ), False = ТОЛЬКО таймаут/ошибка соединения (п.2).
        # Тесты 7a инжектируют фейковую пробу — реальный сетевой запрос
        # заведён по умолчанию в _default_probe ниже.
        self._probe = probe or self._default_probe
        self._error_bus = error_bus
        self._clock = clock
        self._tick_interval_sec = tick_interval_sec
        self._probe_interval_sec = probe_interval_sec

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # S3/Задача 7b, п.7: терминальная защёлка — НЕОБРАТИМАЯ, в отличие от
        # start()/stop() (переиспользуемый lifecycle). single-flight внутри
        # _heal_or_escalate сериализует лечения между собой, но не запрещает
        # тику взять флайт ПОСЛЕ того, как backend начал закрываться: shutdown
        # идёт дальше (TTS и адаптеры уже закрываются), а сторож поднимает
        # REST заново — сервер принимает запросы над полузакрытым backend'ом.
        # Нет метода "отменить" — намеренно: begin_shutdown() зовётся только
        # из финального teardown процесса (_shutdown_backend), возврата назад
        # для живого процесса не бывает.
        self._shutdown_event = threading.Event()

        self._last_probe_ts: float | None = None
        self._consecutive_failures = 0
        self._port_held_externally = False
        # Скользящее окно успешно ВЫДАННЫХ owner.restart() (анти-шторм, п.8).
        self._heal_history: deque[float] = deque(maxlen=32)

    # ------------------------------------------------------------------
    # Lifecycle (start()/stop() — stop() обязателен в BackendService.close(),
    # проводка — задача 7b)
    # ------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="RestWatchdog",
            )
            self._thread.start()
        logger.info(
            "RestWatchdog: запущен (tick=%.1fs, probe=%.1fs)",
            self._tick_interval_sec, self._probe_interval_sec,
        )

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._thread = None
        self._stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
            if thread.is_alive():
                logger.warning("RestWatchdog: тред не завершился за 2.0с")

    def begin_shutdown(self) -> None:
        """Терминальная защёлка (S3/Задача 7b, п.7): лечение запрещено
        НАВСЕГДА, начиная с этого вызова.

        В отличие от ``stop()`` (переиспользуемый lifecycle — можно снова
        ``start()``), это состояние необратимо и переживает даже тред,
        который уже был ВНУТРИ тика в момент вызова: ``check_once()`` и
        ``_heal_or_escalate()`` перепроверяют флаг на своих входах, закрывая
        окно между «backend решил закрываться» и «тик сторожа уже посчитал
        серию провалов и вот-вот позовёт owner.restart()».

        Вызывается ОДИН раз, самым первым шагом ``_shutdown_backend``,
        раньше ``rest_inprocess.begin_shutdown()`` — иначе сторож увидит 503
        на /health уже останавливающегося REST и попытается его «вылечить»
        гонкой с тем же teardown'ом.
        """
        self._shutdown_event.set()

    def _run(self) -> None:
        while not self._stop_event.wait(self._tick_interval_sec):
            try:
                self.check_once()
            except Exception:
                logger.exception("RestWatchdog: тик упал")

    # ------------------------------------------------------------------
    # Один тик (чистая логика — юниты зовут напрямую)
    # ------------------------------------------------------------------

    def check_once(self) -> str | None:
        """Возвращает выполненное действие: "healed" | "escalated" | None."""
        if self._shutdown_event.is_set():
            # Терминальная защёлка (п.7): backend уже начал закрываться —
            # даже локальный owner.status() не зовём, чтобы не тратить тик
            # на то, что заведомо не приведёт ни к чему кроме риска гонки.
            return None

        try:
            status = self._owner.status()
        except Exception:
            logger.exception("RestWatchdog: owner.status() упал")
            return None

        if not bool(status.get("enabled", True)):
            # Режим in-process REST выключен целиком — сторожу нечего
            # сторожить (п.1: тик проверяет running "при включённом режиме").
            self._reset_streak()
            return None

        if bool(status.get("tombstone", False)):
            # п.5: сборка REST-приложения упала — лечить нечего.
            # rest.startup_failed уже отправлен из service.py; повторная
            # эскалация rest.wedged была бы дублем одного и того же отказа.
            self._reset_streak()
            return None

        now = self._clock()
        if self._last_probe_ts is not None and (
            now - self._last_probe_ts < self._probe_interval_sec
        ):
            return None
        self._last_probe_ts = now

        try:
            healthy = bool(self._probe())
        except Exception:
            # Контракт п.2: провалом считаются ТОЛЬКО таймаут и ошибка
            # соединения — probe() обязан сам классифицировать исход по
            # этому правилу. Неопознанное исключение здесь — баг колбэка, а
            # не доказательство смерти REST; логируем и не портим серию.
            logger.exception("RestWatchdog: probe() упал")
            return None

        running = bool(status.get("running", False))

        if healthy:
            # п.4: успешный HTTP-ответ пробы при running=false доказывает,
            # что порт слушает КТО-ТО ДРУГОЙ (легаси-юнит), не наш процесс —
            # отдельное нелечимое состояние, а не смерть REST.
            self._port_held_externally = not running
            self._reset_streak()
            return None

        self._port_held_externally = False
        self._consecutive_failures += 1

        if self._consecutive_failures < CONSECUTIVE_FAILURES_TO_HEAL:
            # п.3: одиночный провал — только предупреждение, лечение не
            # начинается.
            logger.warning(
                "RestWatchdog: проба /health провалилась (%d/%d подряд)",
                self._consecutive_failures, CONSECUTIVE_FAILURES_TO_HEAL,
            )
            return None

        if not bool(status.get("ever_served", False)):
            # Краевой случай, который НЕ надо чинить (см. докстринг модуля):
            # REST, ни разу не слушавший, сторож не поднимает.
            return None

        return self._heal_or_escalate(now)

    # ------------------------------------------------------------------

    def _heal_or_escalate(self, now: float) -> str | None:
        if self._shutdown_event.is_set():
            # Более узкое окно, чем guard в check_once(): защёлка могла
            # взводиться ПОКА этот тик уже шёл (проба /health занимает до
            # _PROBE_TIMEOUT_SEC=2с) — перепроверяем прямо перед решением
            # лечить, а не только на входе в тик.
            return None

        with self._lock:
            while (self._heal_history
                   and now - self._heal_history[0] > _HEAL_STORM_WINDOW_SEC):
                self._heal_history.popleft()
            storm = len(self._heal_history) >= _HEAL_STORM_MAX

        if storm:
            self._escalate()
            return "escalated"

        logger.warning(
            "RestWatchdog: %d подряд провалов пробы — лечение через "
            "owner.restart()", self._consecutive_failures,
        )
        with self._lock:
            self._heal_history.append(now)
        try:
            ok = bool(self._owner.restart())
        except Exception:
            logger.exception("RestWatchdog: owner.restart() упал")
            ok = False

        if not ok:
            self._escalate()
            return "escalated"

        # Успешное лечение сбрасывает серию провалов — следующая проба
        # (через probe_interval_sec) начинает счёт заново. Окно анти-шторма
        # (_heal_history) НЕ трогаем — попытка лечения потрачена независимо
        # от исхода последующей пробы.
        self._consecutive_failures = 0
        return "healed"

    def _reset_streak(self) -> None:
        # Re-arm (п.8): непрерывный здоровый период (хотя бы одна успешная
        # проба) обнуляет и серию провалов, и окно анти-шторма — иначе
        # «исчерпание лечений» означало бы «сдаёмся навсегда» даже после
        # полного восстановления, и REST оставался бы недолечиваемым до
        # ручного вмешательства десять минут по остывающему таймеру.
        self._consecutive_failures = 0
        with self._lock:
            self._heal_history.clear()

    def _escalate(self) -> None:
        self._consecutive_failures = 0
        logger.error(
            "RestWatchdog: лечение исчерпано (%d попыток за %.0fс) — "
            "rest.wedged", _HEAL_STORM_MAX, _HEAL_STORM_WINDOW_SEC,
        )
        if self._error_bus is None:
            return
        try:
            from datetime import datetime, timezone

            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY

            entry = ERROR_REGISTRY.get("rest.wedged", {})
            self._error_bus.push(KrabError(
                severity=entry.get("severity", "error"),
                component="rest",
                code="rest.wedged",
                message_user=entry.get(
                    "user_msg_ru",
                    "Встроенный REST-сервер завис — лечение не помогло.",
                ),
                message_debug=(
                    f"rest watchdog: {_HEAL_STORM_MAX} restart() за "
                    f"{_HEAL_STORM_WINDOW_SEC:.0f}с не восстановили /health"
                ),
                timestamp=datetime.now(timezone.utc),
                context={"heal_attempts": _HEAL_STORM_MAX},
                actionable=False,
                action_id=None,
            ))
        except Exception:
            logger.exception("RestWatchdog: ErrorBus.push упал при эскалации")

    # ------------------------------------------------------------------

    def _default_probe(self) -> bool:
        """GET /health на loopback. True = сервер жив (ЛЮБОЙ HTTP-ответ, в
        т.ч. 429/5xx — /health не защищён require_api_key и делит бакет
        120/мин с остальными loopback-клиентами: Voice Gateway, Swift, сам
        сторож). False — ТОЛЬКО таймаут или ошибка соединения (п.2)."""
        import requests

        try:
            port = int(self._owner.status().get("port", 5005))
        except Exception:
            port = 5005
        try:
            requests.get(
                f"http://127.0.0.1:{port}/health", timeout=_PROBE_TIMEOUT_SEC,
            )
            return True
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            return False

    # ------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """Снапшот для секции ``rest_watchdog`` в ``get_diagnostics``
        (проводка — S3/Задача 7b, по образцу ``wake_word_watchdog``)."""
        with self._lock:
            heal_count = len(self._heal_history)
        return {
            "consecutive_failures": self._consecutive_failures,
            "port_held_externally": self._port_held_externally,
            "heal_attempts_in_window": heal_count,
            "last_probe_ts": self._last_probe_ts,
        }
