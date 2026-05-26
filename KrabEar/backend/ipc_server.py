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


class IPCServer:
    """Unix socket сервер, который проксирует запросы в BackendService."""

    def __init__(self, socket_path: Path, service: "BackendService") -> None:
        self.socket_path = socket_path
        self.service = service
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """Останавливает accept loop."""
        self._stop_event.set()

    def serve_forever(self) -> None:
        """Основной цикл обработки входящих подключений."""
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # Wave 58 LOW-2 closure (Wave 47 B2 audit): tighten umask BEFORE bind so the
        # socket is created with owner-only perms from the start. Combined with the
        # explicit `os.chmod()` below this eliminates the TOCTOU window where a
        # concurrent process could open the socket during creation (umask of 0o022
        # would have initial perms 0o755). `listen()` is not called yet, so no
        # accept() can happen even in the theoretical window, but defense-in-depth
        # is cheap here.
        _old_umask = os.umask(0o077)
        try:
            server.bind(str(self.socket_path))
        finally:
            os.umask(_old_umask)
        os.chmod(str(self.socket_path), IPC_SOCKET_PERMISSIONS)
        server.listen(IPC_SOCKET_BACKLOG)
        server.settimeout(IPC_SOCKET_TIMEOUT_SEC)

        logger.info("IPC сервер запущен на %s", self.socket_path)
        try:
            while not self._stop_event.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop_event.is_set():
                        break
                    raise

                # PR #14: thread-per-connection. Без этого длинный STT-запрос
                # блокирует accept-loop и другие IPC-клиенты не могут опрашивать
                # прогресс. daemon=True — потоки умирают вместе с процессом.
                threading.Thread(
                    target=self._handle_connection,
                    args=(conn,),
                    name="ipc-conn",
                    daemon=True,
                ).start()
        finally:
            server.close()
            if self.socket_path.exists():
                self.socket_path.unlink()
            logger.info("IPC сервер остановлен")

    def _handle_connection(self, conn: socket.socket) -> None:
        """Чтение одной JSON-команды и возврат JSON-ответа.

        Выполняется в отдельном потоке на коннект. Socket закрываем здесь же
        через `with conn:` — вызывающая сторона (accept-loop) не trackает.
        """
        with conn:
            try:
                raw = conn.recv(IPC_MAX_MESSAGE_BYTES)
                if not raw:
                    return
                text = raw.decode("utf-8").strip()
                payload = json.loads(text)
                if not isinstance(payload, dict):
                    raise ValueError("payload должен быть JSON-объектом")
            except Exception as exc:
                response = {
                    "id": None,
                    "ok": False,
                    "error": {"code": "invalid_json", "message": str(exc)},
                }
                try:
                    conn.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
                except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                    # Swift client disconnected before response sent — normal during
                    # crash/quit mid-call.  Log at debug, not error.
                    logger.debug(
                        "IPC client disconnected before invalid_json response: %s", exc
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


def default_data_dir() -> Path:
    """Каталог состояния приложения в профиле пользователя."""
    return Path.home() / "Library" / "Application Support" / "KrabEar"


def default_socket_path(data_dir: Path) -> Path:
    """Путь Unix socket внутри того же каталога состояния."""
    return data_dir / "krabear.sock"
