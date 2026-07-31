"""InProcessRestServer — REST-сервер внутри backend-процесса (волна M2).

Спека: docs/superpowers/specs/2026-07-16-m-series-rest-merge-design.md §4.2.

Зачем не app.run(): у Flask'а нет чистого останова — процесс завершается
вместе с сервером. make_server() отдаёт объект с .shutdown(), который можно
позвать из GracefulShutdownHandler и дождаться выхода треда.

Направление отказа — fail-open: любой сбой старта (занятый порт, ошибка
биндинга) НЕ роняет backend. Диктовка важнее веб-сервера, а порт может
держать ещё не выгруженный легаси-агент ai.krab.ear.rest.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from flask import g, jsonify, request

logger = logging.getLogger("KrabEar.Backend.RestInProcess")

SHUTDOWN_JOIN_TIMEOUT_SEC = 5.0
# Сколько ждать фактического входа треда в serve_forever перед shutdown().
# Меньше join-таймаута: это ожидание СТАРТА цикла, а не его завершения.
SERVE_ENTER_TIMEOUT_SEC = 2.0

# S3/Задача 5: посчитанный бюджет дренажа активных REST-запросов в stop().
# ExitTimeOut=15 (ai.krab.ear.backend.plist.template) минус уже занятые
# _IPC_DRAIN_BUDGET_SEC=8.0 (service.py) и SHUTDOWN_JOIN_TIMEOUT_SEC=5.0 выше
# оставляют 2.0с остатка. Флаг shutting_down взводится РАНЬШЕ этого бюджета —
# в _shutdown_backend, до IPCServer.stop() — поэтому 8с IPC-дренажа уже
# работают ОДНОВРЕМЕННО как окно дренажа REST; этот бюджет лишь добирает
# остаток для запросов, не успевших завершиться за то же окно, а не ждёт их
# с нуля. Тест арифметики — test_rest_inprocess_drain_budget_S3_task5.py.
REST_DRAIN_BUDGET_SEC = 2.0

# Опрос счётчика активных запросов, а не join(): werkzeug-тред обслуживает
# keep-alive СОЕДИНЕНИЕ и живёт дольше одного ЗАПРОСА — join() такого треда
# пере-ждал бы. Сравни с IPCServer.stop() (ipc_server.py:65-129), который
# ждёт именно треды-handler'ы, а не запросы.
_DRAIN_POLL_INTERVAL_SEC = 0.05

# Долгоживущие стримы НЕ считаются в реестре активных запросов — иначе
# дренаж всегда выгорал бы весь бюджет на один подключённый клиент. Их ТРИ:
# SSE GET /v1/events, WS /v1/stream и WS /ws/events (rest_server.py:2402,
# хендлер ws_events на 2125) — последний легко пропустить, он не про STT.
_STREAMING_PATHS = frozenset({"/v1/events", "/v1/stream", "/ws/events"})


class InProcessRestServer:
    """Владелец WSGI-сервера, поднятого в daemon-треде текущего процесса."""

    def __init__(
        self,
        app: Any,
        settings: Any,
        enabled: bool,
        error_push: Callable[[str, str], None] | None = None,
        sentinel_push: Callable[[], int] | None = None,
    ) -> None:
        # S3/Задача 4: рубильник приходит ПАРАМЕТРОМ, а не читается отсюда
        # сами. Раньше конструктор сам заглядывал в
        # settings.REST_IN_PROCESS_ENABLED — рубильник получался двухголовым:
        # владелец включает настройкой (rest_in_process_enabled в
        # settings.json), service.py конструирует сервер, а он тут же молча
        # возвращает False, потому что читает статический pydantic-дефолт, а
        # не runtime-значение. Владелец процесса (service.py) — единственный,
        # кто знает актуальное значение; сюда оно приходит уже вычисленным.
        self._app = app
        self._enabled = bool(enabled)
        self._port = int(getattr(settings, "REST_SERVER_PORT", 5005))
        self._error_push = error_push
        # S3/Задача 5: колбэк рассылки sentinel'а SSE/WS-подписчикам EventBus
        # (см. stop()). Опционален — тестовые/встраиваемые вызовы могут не
        # передавать его, тогда этот шаг просто пропускается.
        self._sentinel_push = sentinel_push

        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._error: str | None = None
        self._lock = threading.Lock()
        # Выставляется тредом ПЕРЕД входом в serve_forever — барьер для stop().
        self._serving = threading.Event()

        # S3/Задача 5: гейт останова запросов + реестр активных (не-стриминговых)
        # запросов. См. _install_drain_hooks() и владение флагом в docstring stop().
        self._shutting_down = threading.Event()
        self._active_requests = 0
        self._active_lock = threading.Lock()
        self._install_drain_hooks()

    # ------------------------------------------------------------------

    def _install_drain_hooks(self) -> None:
        """Регистрирует 503-гейт и реестр активных запросов на self._app.

        Не-Flask WSGI-объект (тестовый транспортный стаб) не имеет
        before_request/teardown_request — тихо пропускаем: хуки регистрируются
        ОДИН раз здесь, в конструкторе, а не заново на каждом start(), поэтому
        переживают повторные start()/stop() одного и того же экземпляра.
        """
        before_request = getattr(self._app, "before_request", None)
        teardown_request = getattr(self._app, "teardown_request", None)
        if not callable(before_request) or not callable(teardown_request):
            return

        def _gate_or_count():
            if self._shutting_down.is_set():
                return jsonify({"error": "shutting_down"}), 503
            if request.path not in _STREAMING_PATHS:
                # Помечаем в g, а не по пути повторно в teardown: флаг
                # shutting_down мог измениться МЕЖДУ before_request и
                # teardown_request этого же запроса — decrement обязан
                # зеркалить именно то, что реально было засчитано.
                g.rest_inprocess_counted = True
                with self._active_lock:
                    self._active_requests += 1
            return None

        def _uncount(_exc: BaseException | None) -> None:
            if getattr(g, "rest_inprocess_counted", False):
                with self._active_lock:
                    self._active_requests = max(0, self._active_requests - 1)

        before_request(_gate_or_count)
        teardown_request(_uncount)

    def begin_shutdown(self) -> None:
        """Взводит флаг ``shutting_down`` БЕЗ полного stop() — только допуск.

        Вызывается из ``_shutdown_backend`` РАНЬШЕ ``IPCServer.stop()``
        (S3/Задача 5, п.5): пока идут 8с IPC-дренажа, before_request уже
        отдаёт 503 новым REST-запросам, и это же окно параллельно служит
        дренажом REST вместо последовательного ожидания после него. Полный
        stop() (сентинел + дренаж реестра + shutdown()/server_close())
        всё равно происходит позже, внутри service.close().

        Идемпотентен (``threading.Event.set()``): повторный вызов, в т.ч.
        изнутри stop() ниже, не имеет дополнительного эффекта.
        """
        self._shutting_down.set()

    def _drain_active_requests(self, timeout: float) -> bool:
        """Ждёт опустошения реестра активных (не-стриминговых) запросов.

        Бюджет — ОБЩИЙ deadline на все запросы разом, а не таймаут ожидания
        каждого (семантика IPCServer.stop(), ipc_server.py:65-129). Механику
        оттуда копировать нельзя: там ждут join() тредов-handler'ов, тут —
        поллинг счётчика запросов, потому что werkzeug-тред обслуживает
        keep-alive СОЕДИНЕНИЕ и живёт дольше одного ЗАПРОСА.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._active_lock:
                remaining = self._active_requests
            if remaining <= 0:
                return True
            now = time.monotonic()
            if now >= deadline:
                logger.warning(
                    "InProcessRestServer: %d активных запросов не завершились за %.1fс",
                    remaining, timeout,
                )
                return False
            time.sleep(min(_DRAIN_POLL_INTERVAL_SEC, deadline - now))

    def start(self) -> bool:
        """Поднимает сервер. True если слушает; False при выключенном
        рубильнике или сбое биндинга. НИКОГДА не бросает."""
        # S3/Задача 5: сброс ОБЯЗАТЕЛЕН и БЕЗУСЛОВЕН — start() вызывается
        # повторно из лечения сторожа (restart() = stop() + start(), задача 7).
        # Без сброса REST навсегда отвечает 503 на всё, включая /health, после
        # первого же лечения — а сторож по контракту принимает любой
        # HTTP-ответ за доказательство жизни и больше никогда не лечит.
        # Терминальность (запрет вновь открыть допуск при ФИНАЛЬНОМ shutdown)
        # обеспечивается снаружи: после начала останова процесса start() уже
        # никто не позовёт.
        self._shutting_down.clear()
        if not self._enabled:
            logger.info("InProcessRestServer: выключен рубильником")
            return False

        with self._lock:
            if self._server is not None:
                return True
            try:
                from werkzeug.serving import make_server

                self._server = make_server(
                    "127.0.0.1", self._port, self._app, threaded=True
                )
            except (OSError, SystemExit) as exc:
                # EADDRINUSE — самый вероятный: легаси rest-агент ещё жив.
                # werkzeug's BaseWSGIServer.__init__ сам ловит OSError при
                # неудачном bind, печатает причину в stderr и зовёт
                # sys.exit(1) — наружу долетает SystemExit, а не OSError.
                # Ловим оба, иначе fail-open не срабатывает и сбой бинда
                # тихо утекает как необработанный SystemExit из потока.
                self._error = f"{type(exc).__name__}: {exc}"
                self._server = None
                logger.error(
                    "InProcessRestServer: порт %s занят — работаем БЕЗ "
                    "встроенного REST (%s)", self._port, self._error,
                )
                self._push_error(
                    "rest.port_conflict",
                    f"127.0.0.1:{self._port} занят: {self._error}",
                )
                return False
            except Exception as exc:
                self._error = f"{type(exc).__name__}: {exc}"
                self._server = None
                logger.exception("InProcessRestServer: не удалось создать сервер")
                self._push_error("rest.port_conflict", self._error)
                return False

            self._serving.clear()
            self._thread = threading.Thread(
                target=self._serve,
                args=(self._server,),
                name="rest-inprocess",
                daemon=True,
            )
            self._thread.start()
            # Сбрасываем ошибку прошлой попытки ПОД тем же локом, что и
            # остальные мутации состояния. Вне лока здесь возникало окно, в
            # котором конкурентный status() (поллинг диагностики) видел уже
            # поднятый сервер вместе со стухшим текстом ошибки от предыдущего
            # конфликта порта — реальный сценарий, потому что повторный start()
            # после выгрузки легаси-агента задуман как штатный путь.
            self._error = None

        logger.info("InProcessRestServer: слушает 127.0.0.1:%s", self._port)
        return True

    def _serve(self, server: Any) -> None:
        # Сервер приходит АРГУМЕНТОМ, а не читается из self._server: поле
        # мутирует stop() под локом, и при гонке тред увидел бы None, вышел
        # бы не входя в serve_forever(), а ждущий stop() повис бы навсегда
        # (см. барьер _serving ниже).
        self._serving.set()
        try:
            server.serve_forever()
        except Exception:
            # Тред-граница: необработанное исключение здесь тихо убило бы
            # REST и оставило status() врать про running=True.
            logger.exception("InProcessRestServer: serve_forever упал")
            with self._lock:
                self._error = "serve_forever crashed"
                self._server = None

    def stop(self, timeout: float = SHUTDOWN_JOIN_TIMEOUT_SEC) -> None:
        """Идемпотентный останов: сентинел → дренаж реестра → shutdown() + join.

        Владение флагом ``shutting_down``: stop() ВСЕГДА взводит его первым
        же действием через begin_shutdown() — вызывается и из финального
        shutdown backend'а (обычно ПОСЛЕ отдельного раннего begin_shutdown()
        из _shutdown_backend — тут это лишь идемпотентный повтор), и напрямую
        из лечения сторожа (S3/Задача 7, restart() = stop() + start()). Без
        этого взвода здесь допуск оставался бы открыт всё время дренажа, и
        реестр активных запросов никогда бы не опустел.

        Терминальность (запрет вновь открыть допуск) обеспечивается СНАРУЖИ —
        взводом в _shutdown_backend и защёлкой сторожа, а не самим stop():
        start() безусловно сбрасывает флаг, поэтому именно вызывающий решает,
        будет ли за этим stop() ещё один start().
        """
        self.begin_shutdown()

        with self._lock:
            server, thread = self._server, self._thread
            self._server, self._thread = None, None

        if server is not None:
            # Сентинел ПЕРЕД дренажем реестра (не после) — SSE/WS-подписчики
            # (/v1/events, /ws/events) закрываются сами, не дожидаясь
            # poll-таймаута до 15с; сами они в реестр не входят (см.
            # _STREAMING_PATHS), поэтому дренаж их и не ждёт, и не закрывает.
            if self._sentinel_push is not None:
                try:
                    self._sentinel_push()
                except Exception:
                    logger.exception("InProcessRestServer: sentinel_push бросил")

            self._drain_active_requests(REST_DRAIN_BUDGET_SEC)

            # shutdown() ждёт событие, которое выставляет ТОЛЬКО finally внутри
            # serve_forever(), причём ждёт БЕЗ таймаута. Позвать его до входа
            # треда в цикл = повиснуть навсегда (параметр timeout ниже
            # ограничивает лишь join, до него управление не дойдёт).
            # Поэтому сначала барьер: дождаться фактического входа в цикл.
            if self._serving.wait(timeout=SERVE_ENTER_TIMEOUT_SEC):
                try:
                    server.shutdown()
                except Exception:
                    logger.exception("InProcessRestServer: shutdown() бросил")
            else:
                # Тред не вошёл в цикл за отведённое время. shutdown() тут
                # запрещён; закрытие слушающего сокета ниже само выведет тред,
                # если он всё-таки войдёт позже.
                logger.warning(
                    "InProcessRestServer: тред не вошёл в serve_forever за %.1fс "
                    "— пропускаем shutdown(), закрываем сокет",
                    SERVE_ENTER_TIMEOUT_SEC,
                )
            try:
                server.server_close()
            except Exception:
                logger.exception("InProcessRestServer: server_close() бросил")

        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning(
                    "InProcessRestServer: тред не вышел за %.1fс", timeout
                )

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._server is not None and (
                self._thread is not None and self._thread.is_alive()
            )
            return {
                "enabled": self._enabled,
                "running": bool(running),
                "port": self._port,
                "error": self._error,
            }

    # ------------------------------------------------------------------

    def _push_error(self, code: str, detail: str) -> None:
        if self._error_push is None:
            return
        try:
            self._error_push(code, detail)
        except Exception:
            logger.exception("InProcessRestServer: error_push бросил")
