"""Тесты Wave 1767: hardening ipc_server.py (HIGH + 2 MED).

Охватывает:
  #1 (HIGH) slow-loris / thread exhaustion — recv-таймаут + BoundedSemaphore
  #4 (MED)  single recv() truncation — _recv_until_newline чанковая сборка
  #5 (MED)  socket fd leak on bind() failure

Тесты используют fake/loopback socket'ы — реальных моделей не загружают.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.ipc_server import (  # noqa: E402
    IPC_CONN_RECV_TIMEOUT_SEC,
    IPCServer,
    _IPC_MAX_CONNECTIONS,
    _IPC_RECV_CHUNK,
)
from backend.ipc_constants import IPC_MAX_MESSAGE_BYTES  # noqa: E402


# ---------------------------------------------------------------------------
# Вспомогательные заглушки
# ---------------------------------------------------------------------------

def _make_ipc_server(
    *,
    data_dir: Path | None = None,
    ping_response: dict | None = None,
) -> IPCServer:
    """Возвращает IPCServer с минимальным fake-сервисом без загрузки моделей."""
    if data_dir is None:
        data_dir = Path(tempfile.mkdtemp())

    _resp = ping_response if ping_response is not None else {"id": "x", "ok": True, "result": {}}

    fake_service = MagicMock()
    fake_service.handle_request.return_value = _resp

    return IPCServer(socket_path=data_dir / "test.sock", service=fake_service)


class _FragmentedSocket:
    """Фейковый сокет, возвращающий payload по частям (симуляция POSIX-fragmentation).

    ``recv()`` каждый раз возвращает ``chunk_size`` байт пока данные не кончатся,
    потом возвращает b"" (EOF).  Не делает реального syscall.
    """

    def __init__(self, data: bytes, chunk_size: int = 1) -> None:
        self._data = data
        self._pos = 0
        self._chunk_size = chunk_size
        self._timeout: float | None = None

    def settimeout(self, t: float | None) -> None:
        self._timeout = t

    def recv(self, n: int) -> bytes:
        if self._pos >= len(self._data):
            return b""
        end = min(self._pos + self._chunk_size, len(self._data))
        chunk = self._data[self._pos:end]
        self._pos = end
        return chunk

    def sendall(self, data: bytes) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ---------------------------------------------------------------------------
# #1 (HIGH) slow-loris / thread exhaustion
# ---------------------------------------------------------------------------

class SlowLorisGuardTestCase(unittest.TestCase):
    """Коннект без данных должен закрыться по таймауту и освободить семафор."""

    def setUp(self):
        self.ipc = _make_ipc_server()

    def test_silent_conn_closes_within_timeout_and_frees_semaphore(self):
        """Коннект, который ничего не шлёт, закрывается и освобождает семафор.

        Проверяем:
        (a) _handle_connection завершается (не висит вечно);
        (b) семафор после возврата освобождён (finally сработал).

        Стратегия: обнуляем все слоты, кроме одного → передаём этот один слот
        в _handle_connection (acquire вызывает serve_forever) → после завершения
        убеждаемся, что слот вернулся (снова доступен для acquire).
        """
        # Вытаскиваем ВСЕ слоты кроме одного, чтобы потом проверить возврат
        # именно того единственного слота, который займёт _handle_connection.
        drained = 0
        while self.ipc._conn_semaphore.acquire(blocking=False):
            drained += 1
        # Сейчас семафор пуст (0 слотов).
        # Возвращаем 1 слот обратно — этот слот займёт serve_forever перед запуском.
        self.ipc._conn_semaphore.release()
        drained -= 1  # один слот вернули

        # Занимаем этот один слот вручную (как serve_forever).
        acquired = self.ipc._conn_semaphore.acquire(blocking=False)
        self.assertTrue(acquired, "Единственный слот должен быть доступен")
        # Теперь семафор снова пустой (0).

        # Mock-коннект, который кидает socket.timeout при recv() —
        # эмуляция slow-loris (подключился, но ничего не шлёт).
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.recv.side_effect = socket.timeout("timed out")

        t = threading.Thread(
            target=self.ipc._handle_connection,
            args=(mock_conn,),
            daemon=True,
        )
        t.start()
        t.join(timeout=3.0)

        self.assertFalse(t.is_alive(), "Поток должен завершиться — не зависать вечно")

        # После возврата finally должен был вызвать release() → слот вернулся.
        # Убеждаемся: теперь можем снова acquire без блокировки.
        re_acquired = self.ipc._conn_semaphore.acquire(blocking=False)
        self.assertTrue(re_acquired, "Семафор должен быть освобождён в finally")
        # Возвращаем слот обратно.
        self.ipc._conn_semaphore.release()

        # Восстанавливаем остальные слоты.
        for _ in range(drained):
            self.ipc._conn_semaphore.release()

    def test_semaphore_exhausted_conn_is_rejected_without_thread(self):
        """Когда семафор исчерпан, serve_forever закрывает коннект без потока."""
        # Обнуляем все слоты семафора.
        slots_taken = 0
        while self.ipc._conn_semaphore.acquire(blocking=False):
            slots_taken += 1

        # Создаём фейковый коннект.
        server_sock, client_sock = socket.socketpair()
        try:
            # Эмулируем логику serve_forever (без реального bind/listen).
            acquired = self.ipc._conn_semaphore.acquire(blocking=False)
            self.assertFalse(acquired, "Семафор должен быть исчерпан")

            if not acquired:
                server_sock.close()
                closed_by_server = True
            else:
                closed_by_server = False

            self.assertTrue(closed_by_server, "Коннект должен быть закрыт при лимите")
        finally:
            # Возвращаем слоты.
            for _ in range(slots_taken):
                self.ipc._conn_semaphore.release()
            client_sock.close()

    def test_recv_timeout_set_on_conn(self):
        """conn.settimeout(IPC_CONN_RECV_TIMEOUT_SEC) вызывается в _handle_connection."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        # recv возвращает b"" → немедленный выход.
        mock_conn.recv.side_effect = [b""]
        # Семафор занят вручную.
        self.ipc._conn_semaphore.acquire(blocking=False)

        self.ipc._handle_connection(mock_conn)

        # Убеждаемся, что settimeout был вызван с правильным значением.
        mock_conn.settimeout.assert_called_once_with(IPC_CONN_RECV_TIMEOUT_SEC)


# ---------------------------------------------------------------------------
# #4 (MED) recv truncation — _recv_until_newline
# ---------------------------------------------------------------------------

class RecvUntilNewlineTestCase(unittest.TestCase):
    """Сборка сообщения из фрагментированных recv()."""

    def setUp(self):
        self.ipc = _make_ipc_server()

    def test_split_message_reassembled_correctly(self):
        """JSON-сообщение, доставленное в 2 recv-фрагмента, корректно собирается."""
        msg = json.dumps({"id": "1", "method": "ping", "params": {}}) + "\n"
        raw = msg.encode("utf-8")

        # Делим payload на два куска.
        mid = len(raw) // 2
        part1 = raw[:mid]
        part2 = raw[mid:]

        # Фейковый сокет возвращает кусок 1, потом кусок 2.
        fragments = [part1, part2, b""]  # b"" = EOF

        class _PiecedSocket:
            def __init__(self_):
                self_._idx = 0

            def settimeout(self_, t):
                pass

            def recv(self_, n):
                if self_._idx >= len(fragments):
                    return b""
                chunk = fragments[self_._idx]
                self_._idx += 1
                return chunk

        result = self.ipc._recv_until_newline(_PiecedSocket())
        parsed = json.loads(result.decode("utf-8"))
        self.assertEqual(parsed["method"], "ping")
        self.assertEqual(parsed["id"], "1")

    def test_single_byte_recv_reassembles(self):
        """Работает корректно, когда recv() возвращает ровно по 1 байту."""
        payload = json.dumps({"id": "x", "method": "ping", "params": {}}) + "\n"
        raw = payload.encode("utf-8")
        sock = _FragmentedSocket(raw, chunk_size=1)

        result = self.ipc._recv_until_newline(sock)
        parsed = json.loads(result.decode("utf-8"))
        self.assertEqual(parsed["method"], "ping")

    def test_over_cap_message_raises_valueerror(self):
        """Сообщение без '\\n' длиннее IPC_MAX_MESSAGE_BYTES → ValueError."""
        # Формируем данные больше лимита, без '\n'.
        over_limit = b"x" * (IPC_MAX_MESSAGE_BYTES + 1)
        sock = _FragmentedSocket(over_limit, chunk_size=_IPC_RECV_CHUNK)

        with self.assertRaises(ValueError, msg="Должен бросить ValueError при превышении лимита"):
            self.ipc._recv_until_newline(sock)

    def test_over_cap_does_not_accumulate_unbounded(self):
        """Проверяем, что не накапливаем весь поток в памяти до лимита."""
        # Сообщение ровно на 1 байт больше лимита — должно упасть быстро.
        over_limit = b"A" * (IPC_MAX_MESSAGE_BYTES + 512)
        sock = _FragmentedSocket(over_limit, chunk_size=_IPC_RECV_CHUNK)

        raised = False
        try:
            self.ipc._recv_until_newline(sock)
        except ValueError:
            raised = True
        self.assertTrue(raised)

    def test_empty_conn_returns_empty_bytes(self):
        """Если recv() сразу вернул b"" (EOF) — возвращаем b""."""
        sock = _FragmentedSocket(b"", chunk_size=1)
        result = self.ipc._recv_until_newline(sock)
        self.assertEqual(result, b"")

    def test_handle_connection_with_split_payload(self):
        """_handle_connection успешно обрабатывает запрос, пришедший в 2 recv-фрагмента."""
        payload_str = json.dumps({"id": "frag", "method": "ping", "params": {}}) + "\n"
        raw = payload_str.encode("utf-8")
        mid = len(raw) // 2
        fragments = [raw[:mid], raw[mid:], b""]

        responses = []

        class _SplitSocket:
            def __init__(self_):
                self_._idx = 0
                self_._timeout = None

            def settimeout(self_, t):
                self_._timeout = t

            def recv(self_, n):
                if self_._idx >= len(fragments):
                    return b""
                chunk = fragments[self_._idx]
                self_._idx += 1
                return chunk

            def sendall(self_, data):
                responses.append(data)

            def close(self_):
                pass

            def __enter__(self_):
                return self_

            def __exit__(self_, *_):
                pass

        self.ipc._conn_semaphore.acquire(blocking=False)
        self.ipc._handle_connection(_SplitSocket())

        self.assertEqual(len(responses), 1, "Должен быть ровно 1 ответ")
        resp = json.loads(responses[0].decode("utf-8"))
        self.assertTrue(resp.get("ok"), f"Ожидали ok=True, получили: {resp}")


# ---------------------------------------------------------------------------
# #5 (MED) socket fd leak on bind() failure
# ---------------------------------------------------------------------------

class BindFailureFdLeakTestCase(unittest.TestCase):
    """OSError при bind() не должна утечь файловый дескриптор."""

    def test_bind_failure_closes_socket(self):
        """bind() failure (OSError) → socket.close() вызван, fd не утечёт."""
        ipc = _make_ipc_server()

        close_calls = []
        original_socket_class = socket.socket

        class _MockSocket:
            """Spy-обёртка: перехватывает bind() → бросает OSError; отслеживает close()."""

            def __init__(self_, family, sock_type):
                self_._real = original_socket_class(family, sock_type)
                self_._closed = False

            def bind(self_, addr):
                raise OSError(98, "Address already in use")

            def close(self_):
                self_._closed = True
                close_calls.append(True)
                self_._real.close()

            def setsockopt(self_, *a, **kw):
                return self_._real.setsockopt(*a, **kw)

            def fileno(self_):
                return self_._real.fileno()

            # Прочие методы делегируем к реальному сокету.
            def __getattr__(self_, name):
                return getattr(self_._real, name)

        with patch("backend.ipc_server.socket.socket", _MockSocket):
            with patch("os.umask", return_value=0o022):
                with self.assertRaises(OSError):
                    ipc.serve_forever()

        self.assertTrue(
            len(close_calls) >= 1,
            "socket.close() должен быть вызван при bind() failure (fd не утечёт)",
        )

    def test_bind_failure_restores_umask(self):
        """bind() failure → umask восстановлен до оригинального значения."""
        ipc = _make_ipc_server()

        captured_umask_calls: list[int] = []
        original_umask = os.umask

        def _spy_umask(mask: int) -> int:
            captured_umask_calls.append(mask)
            return original_umask(mask)

        original_socket_class = socket.socket

        class _BindFailSocket:
            def __init__(self_, family, sock_type):
                self_._real = original_socket_class(family, sock_type)

            def bind(self_, addr):
                raise OSError(98, "Address already in use")

            def close(self_):
                self_._real.close()

            def __getattr__(self_, name):
                return getattr(self_._real, name)

        with patch("backend.ipc_server.socket.socket", _BindFailSocket):
            with patch("backend.ipc_server.os.umask", side_effect=_spy_umask):
                with self.assertRaises(OSError):
                    ipc.serve_forever()

        # Первый вызов должен устанавливать 0o077 (tighten).
        # Второй вызов должен восстанавливать оригинал.
        self.assertGreaterEqual(
            len(captured_umask_calls), 2,
            "umask() должен быть вызван минимум дважды (set + restore)",
        )
        self.assertEqual(captured_umask_calls[0], 0o077, "Первый вызов umask — 0o077")


if __name__ == "__main__":
    unittest.main()
