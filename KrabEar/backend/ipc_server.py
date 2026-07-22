"""Unix-socket IPC server и вспомогательные функции для путей.

Выделено из ``backend/service.py`` в рамках W797 phase 2 (W813).
``BackendService`` остаётся в ``service.py``; ``IPCServer`` отвечает
только за приём подключений и маршрутизацию запросов.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from backend.ipc_constants import (
    IPC_MAX_MESSAGE_BYTES,
    IPC_SOCKET_BACKLOG,
    IPC_SOCKET_PERMISSIONS,
    IPC_SOCKET_TIMEOUT_SEC,
)

if TYPE_CHECKING:
    from backend.service import BackendService

logger = logging.getLogger("KrabEar.Backend.Service")

# W1767 #1 (HIGH): slow-loris / thread exhaustion guard.
# Принятый коннект без этого таймаута паркует handler-поток навсегда.
IPC_CONN_RECV_TIMEOUT_SEC: float = 30.0

# W1767 #1 (HIGH): максимальное число одновременных handler-потоков.
# При исчерпании новый коннект немедленно закрывается (structured warning).
_IPC_MAX_CONNECTIONS: int = 64

# Размер чанка для сборки потокового сообщения (W1767 #4 MED).
_IPC_RECV_CHUNK: int = 4096


class IPCServer:
    """Unix socket сервер, который проксирует запросы в BackendService."""

    def __init__(self, socket_path: Path, service: "BackendService") -> None:
        self.socket_path = socket_path
        self.service = service
        self._stop_event = threading.Event()
        # Python вызывает signal callback между произвольными bytecode main-
        # потока. Поэтому callback меняет только эту простую метку и не входит
        # в Lock/Event; полный drain выполняется позже из обычного finally.
        self._signal_stop_requested = False
        # W1767 #1 (HIGH): semaphore ограничивает число параллельных коннектов.
        self._conn_semaphore = threading.BoundedSemaphore(_IPC_MAX_CONNECTIONS)
        # Registry нужен не для статистики: shutdown обязан дождаться handlers,
        # иначе service может закрыть STT-ресурсы под выполняющимся запросом.
        self._handler_threads: set[threading.Thread] = set()
        self._handler_threads_lock = threading.Lock()

    def request_stop_from_signal(self) -> None:
        """Попросить accept-loop завершиться без lock, I/O и логирования."""
        self._signal_stop_requested = True

    def stop(self, timeout_sec: float = 1.5) -> bool:
        """Закрывает допуск новых соединений и ждёт активные handlers.

        ``timeout_sec`` — общий бюджет на все потоки, а не таймаут каждого
        ``join()``. Живые после deadline handles остаются в registry, поэтому
        повторный вызов может дождаться их после разблокировки.

        Вызов из самого handler закрывает admission и дренирует остальные
        потоки, но возвращает ``False``: текущий handler ещё не квиесцирован.

        :returns: ``True`` только после завершения всех handler-потоков.
        """
        timeout = max(0.0, float(timeout_sec))
        deadline = time.monotonic() + timeout
        current = threading.current_thread()
        current_is_handler = False

        # Admission-event ставим до registry-lock: пока coordinator ждёт lock,
        # поздний conn уже будет отвергнут внутри _start_connection_handler.
        # Signal callback сюда больше не входит — он меняет отдельную bool-метку.
        self._stop_event.set()

        while True:
            with self._handler_threads_lock:
                current_is_handler = current in self._handler_threads
                handlers: list[threading.Thread] = []
                for handler in tuple(self._handler_threads):
                    if handler is current:
                        continue
                    if handler.is_alive():
                        handlers.append(handler)
                    else:
                        # Нормальный handler удаляет себя в finally; эта ветка
                        # страхует завершение между snapshot и проверкой.
                        self._handler_threads.discard(handler)

            if not handlers:
                return not current_is_handler

            for handler in handlers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                handler.join(timeout=remaining)

            with self._handler_threads_lock:
                alive = [
                    handler
                    for handler in self._handler_threads
                    if handler is not current and handler.is_alive()
                ]
                for handler in tuple(self._handler_threads):
                    if handler is not current and not handler.is_alive():
                        self._handler_threads.discard(handler)

            if not alive:
                return not current_is_handler
            if time.monotonic() >= deadline:
                logger.warning(
                    "IPC: %d handler-потоков не завершились за %.2fс",
                    len(alive),
                    timeout,
                    extra={"alive_handlers": len(alive)},
                )
                return False

    def serve_forever(self) -> None:
        """Основной цикл обработки входящих подключений."""
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()

        # W1767 #5 (MED): сокет создаём ВНУТРИ try/finally — bind() failure
        # (EADDRINUSE, нет прав) больше не утечёт файловый дескриптор.
        # Ранее `server = socket.socket(...)` стоял ДО первого try, поэтому
        # OSError при bind() оставляла fd открытым.
        # Wave 58 LOW-2: umask сужаем ДО bind(), чтобы сокет сразу создавался
        # с owner-only правами (umask 0o022 → нач. perms 0o755 → race window).
        _old_umask = os.umask(0o077)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            try:
                server.bind(str(self.socket_path))
            finally:
                # Восстанавливаем umask сразу после bind — не задерживаем.
                os.umask(_old_umask)
            os.chmod(str(self.socket_path), IPC_SOCKET_PERMISSIONS)
            server.listen(IPC_SOCKET_BACKLOG)
            server.settimeout(IPC_SOCKET_TIMEOUT_SEC)

            logger.info("IPC сервер запущен на %s", self.socket_path)
            while not (
                self._stop_event.is_set() or self._signal_stop_requested
            ):
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if (
                        self._stop_event.is_set()
                        or self._signal_stop_requested
                    ):
                        break
                    raise

                # Сигнал мог прийти, пока accept() ждал соединение. Такой conn
                # уже нельзя передавать бизнес-логике перед shutdown-барьером.
                if self._signal_stop_requested:
                    conn.close()
                    break

                # W1767 #1 (HIGH): проверяем лимит коннектов.
                # BoundedSemaphore.acquire(blocking=False) — не блокирует;
                # при лимите закрываем коннект без запуска потока.
                if not self._conn_semaphore.acquire(blocking=False):
                    logger.warning(
                        "IPC: лимит %d коннектов исчерпан — новый коннект отклонён",
                        _IPC_MAX_CONNECTIONS,
                        extra={"conn_limit": _IPC_MAX_CONNECTIONS},
                    )
                    conn.close()
                    continue

                self._start_connection_handler(conn)
        finally:
            server.close()
            if self.socket_path.exists():
                self.socket_path.unlink()
            logger.info("IPC сервер остановлен")

    def _start_connection_handler(self, conn: socket.socket) -> bool:
        """Регистрирует и запускает handler для уже занятого semaphore-slot.

        Регистрация происходит до ``start()`` под lifecycle-lock. При shutdown
        или ошибке запуска метод сам закрывает conn и возвращает semaphore-slot.
        """
        should_cleanup = False
        handler: threading.Thread | None = None
        with self._handler_threads_lock:
            if self._stop_event.is_set() or self._signal_stop_requested:
                should_cleanup = True
            else:
                try:
                    handler = threading.Thread(
                        target=self._handle_connection,
                        args=(conn,),
                        name="ipc-conn",
                        daemon=True,
                    )
                    self._handler_threads.add(handler)
                    handler.start()
                except Exception:
                    if handler is not None:
                        self._handler_threads.discard(handler)
                    logger.exception("IPC: не удалось запустить handler-поток")
                    should_cleanup = True
                else:
                    return True

        if should_cleanup:
            try:
                conn.close()
            except OSError as exc:
                logger.debug("IPC: ошибка закрытия незапущенного conn: %s", exc)
            finally:
                self._conn_semaphore.release()
        return False

    def _handle_connection(self, conn: socket.socket) -> None:
        """Чтение одной JSON-команды и возврат JSON-ответа.

        Выполняется в отдельном потоке на коннект. Socket закрываем здесь же
        через `with conn:` — вызывающая сторона (accept-loop) не trackает.

        W1767 #1: семафор освобождается в finally-блоке независимо от исхода.
        W1767 #4: recv заменён на цикл до '\\n' с защитой от memory-DoS.
        """
        try:
            # W1767 #1 (HIGH): принятый коннект получает явный recv-таймаут.
            # Без этого peer, который подключился но молчит, навсегда паркует
            # данный handler-поток; semaphore slot не будет освобождён.
            conn.settimeout(IPC_CONN_RECV_TIMEOUT_SEC)

            with conn:
                try:
                    # W1767 #4 (MED): читаем до '\n' накопительным циклом.
                    # POSIX stream НЕ гарантирует целое сообщение за один recv().
                    # Большие payload (live_subs_ingest ~42 KB, transcribe_paths)
                    # могут прийти фрагментами → json.loads кидал бы JSONDecodeError.
                    raw = self._recv_until_newline(conn)
                    if not raw:
                        return
                    text = raw.decode("utf-8").strip()
                    payload = json.loads(text)
                    if not isinstance(payload, dict):
                        raise ValueError("payload должен быть JSON-объектом")
                except socket.timeout:
                    # W1767 #1 (HIGH): slow-loris guard — тихо закрываем коннект.
                    logger.debug(
                        "IPC: коннект закрыт по recv-таймауту (%ss) — slow-loris guard",
                        IPC_CONN_RECV_TIMEOUT_SEC,
                    )
                    return
                except Exception as exc:
                    response = {
                        "id": None,
                        "ok": False,
                        "error": {"code": "invalid_json", "message": str(exc)},
                    }
                    try:
                        conn.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
                    except (BrokenPipeError, ConnectionResetError, OSError) as send_exc:
                        # Swift client disconnected before response sent — normal during
                        # crash/quit mid-call.  Log at debug, not error.
                        logger.debug(
                            "IPC client disconnected before invalid_json response: %s", send_exc
                        )
                    except Exception:
                        logger.exception("Ошибка отправки invalid_json-ответа")
                    return

                try:
                    response = self.service.handle_request(payload)
                except Exception as exc:
                    logger.exception("Непойманная ошибка в handle_request")
                    response = {
                        "id": payload.get("id"),
                        "ok": False,
                        "error": {"code": "internal_error", "message": str(exc)},
                    }
                try:
                    conn.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
                except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                    # Swift client disconnected before response sent — common when the
                    # agent crashes or quits mid-call.  Log at debug, not error.
                    logger.debug(
                        "IPC client disconnected before response: %s", exc
                    )
                except Exception:
                    logger.exception("Ошибка отправки ответа клиенту")
        finally:
            # W1767 #1 (HIGH): освобождаем семафор в любом случае,
            # в том числе при socket.timeout (slow-loris guard).
            try:
                self._conn_semaphore.release()
            finally:
                # Удаляем именно текущий поток: handle может завершиться в любой
                # точке и должен исчезнуть из registry даже при ошибке release().
                with self._handler_threads_lock:
                    self._handler_threads.discard(threading.current_thread())

    # ------------------------------------------------------------------
    # W1767 #4 (MED): вспомогательный метод сборки потокового сообщения
    # ------------------------------------------------------------------

    def _recv_until_newline(self, conn: socket.socket) -> bytes:
        """Читает байты из коннекта до появления символа '\\n'.

        Собирает чанки по ``_IPC_RECV_CHUNK`` байт; прерывается как только
        буфер содержит '\\n'.  Возвращает содержимое ДО первого '\\n'.

        Защита от memory-DoS: если накоплено > ``IPC_MAX_MESSAGE_BYTES`` байт
        до символа '\\n' — закрывает коннект и бросает ``ValueError``.
        Это предотвращает ситуацию, когда злоумышленник шлёт гигантский поток
        без новой строки.

        :raises ValueError: превышен лимит IPC_MAX_MESSAGE_BYTES.
        :raises socket.timeout: recv истёк (slow-loris guard).
        """
        buf = b""
        while True:
            chunk = conn.recv(_IPC_RECV_CHUNK)
            if not chunk:
                # Пир закрыл коннект без сообщения.
                return b""
            buf += chunk
            if b"\n" in buf:
                # Берём только первое сообщение (до первой '\n').
                line, _ = buf.split(b"\n", 1)
                return line
            if len(buf) > IPC_MAX_MESSAGE_BYTES:
                # W1767 #4 (MED): memory-DoS guard — сброс коннекта.
                logger.warning(
                    "IPC: сообщение превысило лимит %d байт — коннект закрыт",
                    IPC_MAX_MESSAGE_BYTES,
                    extra={"limit_bytes": IPC_MAX_MESSAGE_BYTES},
                )
                raise ValueError(
                    f"IPC message exceeded {IPC_MAX_MESSAGE_BYTES} bytes without newline"
                )


def default_data_dir() -> Path:
    """Каталог состояния приложения в профиле пользователя."""
    return Path.home() / "Library" / "Application Support" / "KrabEar"


def default_socket_path(data_dir: Path) -> Path:
    """Путь Unix socket внутри того же каталога состояния."""
    return data_dir / "krabear.sock"
