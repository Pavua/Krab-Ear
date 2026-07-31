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
from typing import Any, Callable

logger = logging.getLogger("KrabEar.Backend.RestInProcess")

SHUTDOWN_JOIN_TIMEOUT_SEC = 5.0
# Сколько ждать фактического входа треда в serve_forever перед shutdown().
# Меньше join-таймаута: это ожидание СТАРТА цикла, а не его завершения.
SERVE_ENTER_TIMEOUT_SEC = 2.0


class InProcessRestServer:
    """Владелец WSGI-сервера, поднятого в daemon-треде текущего процесса."""

    def __init__(
        self,
        app: Any,
        settings: Any,
        enabled: bool,
        error_push: Callable[[str, str], None] | None = None,
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

        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._error: str | None = None
        self._lock = threading.Lock()
        # Выставляется тредом ПЕРЕД входом в serve_forever — барьер для stop().
        self._serving = threading.Event()

    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Поднимает сервер. True если слушает; False при выключенном
        рубильнике или сбое биндинга. НИКОГДА не бросает."""
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
        """Идемпотентный останов: shutdown() + join треда."""
        with self._lock:
            server, thread = self._server, self._thread
            self._server, self._thread = None, None

        if server is not None:
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
