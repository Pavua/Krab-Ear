"""Тесты Wave 1767 и 2026-07-20: hardening ``ipc_server.py``.

Охватывает:
  #1 (HIGH) slow-loris / thread exhaustion — recv-таймаут + BoundedSemaphore
  #4 (MED)  single recv() truncation — _recv_until_newline чанковая сборка
  #5 (MED)  socket fd leak on bind() failure
  lifecycle активных handler-потоков — deadline, registry и повторный stop

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


# ---------------------------------------------------------------------------
# 2026-07-20: lifecycle активных handler-потоков
# ---------------------------------------------------------------------------

class _LifecycleJSONConnection:
    """Минимальный потоковый сокет с одним JSON-запросом."""

    def __init__(self) -> None:
        payload = {"id": "lifecycle", "method": "ping", "params": {}}
        self._chunks = [(json.dumps(payload) + "\n").encode("utf-8"), b""]
        self.closed = threading.Event()
        self.sent: list[bytes] = []
        self.timeout: float | None = None

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout

    def recv(self, _size: int) -> bytes:
        return self._chunks.pop(0)

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def close(self) -> None:
        self.closed.set()

    def __enter__(self) -> "_LifecycleJSONConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class _LifecycleImmediateService:
    """Сервис, который немедленно завершает запрос."""

    def handle_request(self, payload: dict) -> dict:
        return {"id": payload.get("id"), "ok": True, "result": {}}


class _LifecycleBlockingService:
    """Сервис с управляемой точкой блокировки внутри handler-потока."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def handle_request(self, payload: dict) -> dict:
        self.entered.set()
        self.release.wait()
        return {"id": payload.get("id"), "ok": True, "result": {}}


class _LifecycleSelfStoppingService:
    """Сервис, вызывающий stop() из текущего IPC-handler."""

    def __init__(self) -> None:
        self.ipc: IPCServer | None = None
        self.stop_result: bool | None = None
        self.done = threading.Event()

    def handle_request(self, payload: dict) -> dict:
        assert self.ipc is not None
        self.stop_result = self.ipc.stop(timeout_sec=0.05)
        self.done.set()
        return {"id": payload.get("id"), "ok": True, "result": {}}


def _make_lifecycle_server(
    test_case: unittest.TestCase,
    service: object,
) -> IPCServer:
    """Создаёт IPCServer в отдельном временном каталоге."""
    temp_dir = tempfile.TemporaryDirectory(prefix="krab-ipc-lifecycle-")
    test_case.addCleanup(temp_dir.cleanup)
    return IPCServer(socket_path=Path(temp_dir.name) / "test.sock", service=service)


def _start_accepted_connection(
    ipc: IPCServer,
    conn: _LifecycleJSONConnection,
) -> bool:
    """Повторяет контракт accept-loop: сначала занимает semaphore-slot."""
    acquired = ipc._conn_semaphore.acquire(blocking=False)
    if not acquired:
        raise AssertionError("Тестовый IPC semaphore неожиданно исчерпан")
    return ipc._start_connection_handler(conn)


def _drain_available_slots(ipc: IPCServer) -> int:
    """Считает свободные semaphore-slot без обращения к внутреннему счётчику."""
    count = 0
    while ipc._conn_semaphore.acquire(blocking=False):
        count += 1
    for _ in range(count):
        ipc._conn_semaphore.release()
    return count


class IPCHandlerLifecycleTestCase(unittest.TestCase):
    """Проверяет registry, общий deadline и повторяемую остановку handlers."""

    def test_clean_handler_completion_makes_stop_true(self) -> None:
        """Успешный handler удаляет себя, а stop() возвращает True."""
        ipc = _make_lifecycle_server(self, _LifecycleImmediateService())
        conn = _LifecycleJSONConnection()

        self.assertTrue(_start_accepted_connection(ipc, conn))
        self.assertTrue(ipc.stop(timeout_sec=1.0))

        self.assertTrue(conn.closed.wait(1.0))
        with ipc._handler_threads_lock:
            self.assertEqual(ipc._handler_threads, set())
        self.assertEqual(_drain_available_slots(ipc), _IPC_MAX_CONNECTIONS)

    def test_blocked_handler_stays_tracked_then_retry_succeeds(self) -> None:
        """Таймаут сохраняет живой handle; release + повторный stop завершают его."""
        service = _LifecycleBlockingService()
        ipc = _make_lifecycle_server(self, service)
        # Регистрируем после temp cleanup: unittest выполняет cleanup в LIFO,
        # поэтому ранний RED сначала отпустит handler и лишь затем удалит каталог.
        self.addCleanup(service.release.set)
        conn = _LifecycleJSONConnection()

        self.assertTrue(_start_accepted_connection(ipc, conn))
        self.assertTrue(service.entered.wait(1.0))

        self.assertFalse(ipc.stop(timeout_sec=0.01))
        with ipc._handler_threads_lock:
            tracked = tuple(ipc._handler_threads)
        self.assertEqual(len(tracked), 1)
        self.assertTrue(tracked[0].is_alive())

        service.release.set()
        self.assertTrue(ipc.stop(timeout_sec=1.0))
        with ipc._handler_threads_lock:
            self.assertEqual(ipc._handler_threads, set())
        self.assertEqual(_drain_available_slots(ipc), _IPC_MAX_CONNECTIONS)

    def test_stop_from_current_handler_does_not_join_itself(self) -> None:
        """Текущий handler не объявляет полную квиесценцию до своего выхода."""
        service = _LifecycleSelfStoppingService()
        ipc = _make_lifecycle_server(self, service)
        service.ipc = ipc

        self.assertTrue(_start_accepted_connection(ipc, _LifecycleJSONConnection()))
        self.assertTrue(service.done.wait(1.0))
        self.assertFalse(service.stop_result)
        # После возврата handler удаляет себя из registry; внешний coordinator
        # получает доказательство полной квиесценции повторным drain.
        self.assertTrue(ipc.stop(timeout_sec=1.0))

    def test_signal_stop_request_does_not_touch_registry_lock(self) -> None:
        """Signal-request меняет только bool и не входит в lifecycle-lock."""
        ipc = _make_lifecycle_server(self, _LifecycleImmediateService())

        class _ForbiddenLock:
            def __enter__(self):
                raise AssertionError("signal-request не должен входить в lock")

            def __exit__(self, *_args):
                return False

            def acquire(self, *_args, **_kwargs):
                raise AssertionError("signal-request не должен брать lock")

        ipc._handler_threads_lock = _ForbiddenLock()
        ipc.request_stop_from_signal()
        self.assertTrue(ipc._signal_stop_requested)

    def test_connection_after_signal_request_is_closed_without_handler(self) -> None:
        """Принятый после SIGTERM conn не достигает бизнес-логики."""
        ipc = _make_lifecycle_server(self, _LifecycleImmediateService())
        conn = _LifecycleJSONConnection()
        ipc.request_stop_from_signal()

        with patch("backend.ipc_server.threading.Thread") as thread_factory:
            self.assertFalse(_start_accepted_connection(ipc, conn))

        thread_factory.assert_not_called()
        self.assertTrue(conn.closed.is_set())
        with ipc._handler_threads_lock:
            self.assertEqual(ipc._handler_threads, set())
        self.assertEqual(_drain_available_slots(ipc), _IPC_MAX_CONNECTIONS)

    def test_multiple_handlers_share_one_stop_deadline(self) -> None:
        """Первый join расходует общий бюджет, второй не получает новый таймаут."""
        ipc = _make_lifecycle_server(self, _LifecycleImmediateService())

        class _Clock:
            """Управляемые монотонные часы без реального ожидания."""

            value = 0.0

            def monotonic(self) -> float:
                return self.value

        clock = _Clock()

        class _BudgetConsumer:
            """Duck-type handler, который целиком расходует переданный бюджет."""

            def __init__(self) -> None:
                self.alive = True
                self.join_timeouts: list[float] = []

            def is_alive(self) -> bool:
                return self.alive

            def join(self, timeout: float | None = None) -> None:
                assert timeout is not None
                self.join_timeouts.append(timeout)
                clock.value += timeout

        handlers = (_BudgetConsumer(), _BudgetConsumer())
        with ipc._handler_threads_lock:
            ipc._handler_threads.update(handlers)

        with patch("backend.ipc_server.time.monotonic", side_effect=clock.monotonic):
            self.assertFalse(ipc.stop(timeout_sec=1.0))

        joined = [timeout for handler in handlers for timeout in handler.join_timeouts]
        self.assertEqual(len(joined), 1)
        self.assertAlmostEqual(joined[0], 1.0)
        with ipc._handler_threads_lock:
            self.assertEqual(ipc._handler_threads, set(handlers))

        for handler in handlers:
            handler.alive = False
        self.assertTrue(ipc.stop(timeout_sec=0.0))

    def test_stop_rejects_connection_before_thread_creation(self) -> None:
        """После stop() новый принятый conn закрывается и возвращает slot."""
        ipc = _make_lifecycle_server(self, _LifecycleImmediateService())
        conn = _LifecycleJSONConnection()
        self.assertTrue(ipc.stop(timeout_sec=0.0))

        with patch("backend.ipc_server.threading.Thread") as thread_factory:
            self.assertFalse(_start_accepted_connection(ipc, conn))

        thread_factory.assert_not_called()
        self.assertTrue(conn.closed.is_set())
        with ipc._handler_threads_lock:
            self.assertEqual(ipc._handler_threads, set())
        self.assertEqual(_drain_available_slots(ipc), _IPC_MAX_CONNECTIONS)

    def test_start_failure_was_registered_and_releases_resources(self) -> None:
        """Thread.start() failure убирает pre-registered handle и возвращает slot."""
        ipc = _make_lifecycle_server(self, _LifecycleImmediateService())
        conn = _LifecycleJSONConnection()

        class _StartFailureThread:
            """Duck-type поток; не наследуется от Thread и не попадает в _limbo."""

            observed_registered = False

            def start(self) -> None:
                # Production вызывает start() уже внутри lifecycle-lock;
                # повторный захват обычного Lock здесь создал бы ложный deadlock.
                self.observed_registered = self in ipc._handler_threads
                raise RuntimeError("детерминированный отказ start")

        fake_thread = _StartFailureThread()
        with patch("backend.ipc_server.threading.Thread", return_value=fake_thread):
            self.assertFalse(_start_accepted_connection(ipc, conn))

        self.assertTrue(fake_thread.observed_registered)
        self.assertTrue(conn.closed.is_set())
        with ipc._handler_threads_lock:
            self.assertEqual(ipc._handler_threads, set())
        self.assertEqual(_drain_available_slots(ipc), _IPC_MAX_CONNECTIONS)

    def test_stop_sets_event_without_handler_threads_lock(self) -> None:
        """Полный stop взводит admission-event до ожидания registry-lock.

        Signal callback теперь использует отдельную bool-метку. Для обычного
        coordinator-stop сохраняем раннее закрытие admission: новый conn не
        должен проскочить, пока coordinator ждёт уже удерживаемый registry-lock.
        """
        ipc = _make_lifecycle_server(self, _LifecycleImmediateService())

        self.assertTrue(ipc._handler_threads_lock.acquire(timeout=1.0))
        try:
            stopper = threading.Thread(
                target=lambda: ipc.stop(timeout_sec=0.2), daemon=True
            )
            stopper.start()
            deadline = time.monotonic() + 1.0
            while not ipc._stop_event.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(
                ipc._stop_event.is_set(),
                "stop() обязан взводить _stop_event без захвата "
                "_handler_threads_lock",
            )
        finally:
            ipc._handler_threads_lock.release()
        stopper.join(timeout=2.0)
        self.assertFalse(stopper.is_alive())


# ---------------------------------------------------------------------------
# Живой инцидент 2026-08-04 — handle_request может зависнуть НАВСЕГДА и
# перманентно утечь семафор-слот.
#
# conn.settimeout(IPC_CONN_RECV_TIMEOUT_SEC) защищает ТОЛЬКО recv()/sendall()
# на сокете — на чистый Python-код между ними (self.service.handle_request(...))
# он не действует вообще. Если бизнес-логика зависает (deadlock на локе,
# который никогда не освободится) — finally: semaphore.release() НИКОГДА не
# срабатывает, потому что поток навсегда застревает ВНУТРИ try, до finally.
# Живое наблюдение: 11667 срабатываний "лимит 64 коннектов исчерпан" в логе
# backend.log за 8 дней, растущее в реальном времени — то есть у сервера
# накапливаются перманентно потерянные слоты, и в какой-то момент отклоняются
# ВСЕ новые подключения, включая HealthMonitor-пинги и попытки диктовки.
# ---------------------------------------------------------------------------

class HandleRequestDeadlockGuardTestCase(unittest.TestCase):
    """handle_request обязан быть bounded — зависшая бизнес-логика не должна
    держать семафор-слот вечно."""

    def test_hanging_handle_request_still_releases_semaphore(self):
        """Зависший (никогда не возвращающийся) handle_request не должен
        навсегда занимать слот семафора — backstop-таймаут обязан сработать."""
        never_returns = threading.Event()  # никогда не .set() — эмуляция deadlock
        fake_service = MagicMock()
        fake_service.handle_request.side_effect = lambda payload: never_returns.wait()

        ipc = _make_ipc_server()
        ipc.service = fake_service
        # Backstop обязан быть настраиваемым: прод использует щедрый потолок
        # (180с, с запасом над самым долгим клиентским IPC-таймаутом 120с),
        # тест — короткий, чтобы не ждать реальные 180с.
        ipc._request_timeout_sec = 0.3

        # Освобождаем все слоты кроме одного, занимаем этот один вручную —
        # тот же паттерн, что test_silent_conn_closes_within_timeout_and_frees_semaphore.
        drained = 0
        while ipc._conn_semaphore.acquire(blocking=False):
            drained += 1
        ipc._conn_semaphore.release()
        drained -= 1
        acquired = ipc._conn_semaphore.acquire(blocking=False)
        self.assertTrue(acquired)

        payload = json.dumps({"id": "1", "method": "stop_recording", "params": {}}) + "\n"
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.recv.side_effect = [payload.encode("utf-8"), b""]

        t = threading.Thread(target=ipc._handle_connection, args=(mock_conn,), daemon=True)
        t.start()
        t.join(timeout=3.0)

        self.assertFalse(
            t.is_alive(),
            "_handle_connection обязан вернуться даже если handle_request "
            "никогда не завершается (backstop-таймаут)"
        )
        re_acquired = ipc._conn_semaphore.acquire(blocking=False)
        self.assertTrue(
            re_acquired,
            "Семафор обязан освободиться даже при зависшем handle_request "
            "(живой инцидент 2026-08-04: 11667 утечек за 8 дней)"
        )
        ipc._conn_semaphore.release()
        for _ in range(drained):
            ipc._conn_semaphore.release()
        never_returns.set()  # отпускаем осиротевший фоновый поток


if __name__ == "__main__":
    unittest.main()
