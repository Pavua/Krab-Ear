"""Тесты Wave 1768: дедупликация ``IPCServer`` (W746-класс регрессии).

Контекст: ранее ``backend/service.py`` содержал ВТОРОЙ, inline-дубликат
``class IPCServer`` (незакалённый). Production-вход ``main()`` инстанцировал
именно его → HIGH-фикс W1767 #1595 (slow-loris guard, recv-таймаут,
_recv_until_newline reassembly, bind()-fd-leak fix) был МЁРТВЫМ для production.

W1768 удалил inline-дубликат и добавил
``from backend.ipc_server import IPCServer`` в ``service.py``. Эти тесты
гарантируют, что production-путь (``backend.service.IPCServer``, который и
использует ``main()``) — это ЗАКАЛЁННЫЙ класс из ``backend/ipc_server.py``.

Тесты используют fake-сокеты — реальных моделей/сети не трогают.
"""

from __future__ import annotations

import inspect
import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ВАЖНО: импортируем IPCServer ИМЕННО так, как это видит production-вход main()
# — из backend.service. Если дедуп сломается (вернётся inline-дубликат), эти
# импорты подтянут незакалённый класс и тесты упадут.
import backend.service as svc_mod  # noqa: E402
from backend.service import IPCServer as ServiceIPCServer  # noqa: E402
from backend.ipc_server import (  # noqa: E402
    IPC_CONN_RECV_TIMEOUT_SEC,
    IPCServer as ExtractedIPCServer,
)


def _make_service_ipc_server() -> ServiceIPCServer:
    """IPCServer (через путь backend.service) с fake-сервисом — как в main().

    Конструктор вызывается ровно теми kwargs, что и в ``main()``:
    ``IPCServer(socket_path=..., service=...)``.
    """
    data_dir = Path(tempfile.mkdtemp())
    fake_service = MagicMock()
    fake_service.handle_request.return_value = {"id": "x", "ok": True, "result": {}}
    # Совпадение сигнатуры с main(): socket_path= + service=.
    return ServiceIPCServer(socket_path=data_dir / "test.sock", service=fake_service)


class ProductionUsesHardenedClassTestCase(unittest.TestCase):
    """Production-путь обязан использовать закалённый IPCServer."""

    def test_service_ipcserver_is_extracted_hardened_class(self):
        """backend.service.IPCServer IS backend.ipc_server.IPCServer (identity)."""
        self.assertIs(
            ServiceIPCServer,
            ExtractedIPCServer,
            "service.IPCServer должен БЫТЬ закалённым классом из ipc_server.py",
        )
        # И через module-объект (как патчат тесты W1634: svc_mod.IPCServer).
        self.assertIs(svc_mod.IPCServer, ExtractedIPCServer)
        self.assertEqual(svc_mod.IPCServer.__module__, "backend.ipc_server")

    def test_no_inline_duplicate_class_def_in_service_source(self):
        """В исходнике service.py не осталось inline-определения IPCServer-класса.

        Ищем именно синтаксис ОПРЕДЕЛЕНИЯ класса (``class IPCServer:`` /
        ``class IPCServer(``) — упоминания в комментариях/импорте не считаются.
        Это прямой regression-guard на возврат дубликата (companion к
        scripts/audit_duplicate_defs.py).
        """
        import re as _re

        src = inspect.getsource(svc_mod)
        # Совпадает только на реальном определении класса в начале строки.
        match = _re.search(r"^\s*class\s+IPCServer\b", src, _re.MULTILINE)
        self.assertIsNone(
            match,
            "service.py снова содержит inline-определение class IPCServer — дедуп сломан",
        )

    def test_main_instantiates_ipcserver_with_expected_kwargs(self):
        """main() инстанцирует IPCServer(socket_path=..., service=...) и пишет _ipc_server."""
        main_src = inspect.getsource(svc_mod.main)
        self.assertIn(
            "IPCServer(socket_path=socket_path, service=service, ownership=ownership)",
            main_src,
        )
        self.assertIn("service._ipc_server = server", main_src)

    def test_hardened_instance_has_slow_loris_guard(self):
        """Инстанс, созданный как в main(), имеет BoundedSemaphore slow-loris guard."""
        ipc = _make_service_ipc_server()
        self.assertTrue(
            hasattr(ipc, "_conn_semaphore"),
            "Отсутствует _conn_semaphore — slow-loris guard потерян",
        )
        # BoundedSemaphore — тип, экспонирующий acquire/release.
        sem = ipc._conn_semaphore
        self.assertTrue(hasattr(sem, "acquire") and hasattr(sem, "release"))
        # release() сверх лимита у BoundedSemaphore кидает ValueError —
        # подтверждаем, что это именно bounded-семафор, а не обычный Semaphore.
        with self.assertRaises(ValueError):
            sem.release()

    def test_hardened_instance_has_recv_until_newline(self):
        """Инстанс имеет метод _recv_until_newline (reassembly loop W1767 #4)."""
        ipc = _make_service_ipc_server()
        self.assertTrue(
            hasattr(ipc, "_recv_until_newline") and callable(ipc._recv_until_newline),
            "Отсутствует _recv_until_newline — reassembly loop потерян",
        )

    def test_accepted_connection_gets_non_none_timeout(self):
        """_handle_connection ставит ненулевой recv-таймаут на принятый коннект.

        Это ключ slow-loris guard: без таймаута молчащий peer паркует поток
        навсегда. Проверяем, что settimeout() вызывается значением > 0.
        """
        ipc = _make_service_ipc_server()

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        # recv сразу EOF → быстрый выход из обработчика.
        mock_conn.recv.side_effect = [b""]
        # Занимаем слот вручную (как делает serve_forever перед запуском потока).
        ipc._conn_semaphore.acquire(blocking=False)

        ipc._handle_connection(mock_conn)

        mock_conn.settimeout.assert_called_once_with(IPC_CONN_RECV_TIMEOUT_SEC)
        # Таймаут обязан быть положительным (не None и не 0).
        (called_timeout,), _ = mock_conn.settimeout.call_args
        self.assertIsNotNone(called_timeout, "recv-таймаут не должен быть None")
        self.assertGreater(called_timeout, 0, "recv-таймаут должен быть > 0")


class FragmentedMessageViaServicePathTestCase(unittest.TestCase):
    """Регрессия: фрагментированное сообщение собирается (через backend.service)."""

    def test_split_payload_reassembled_via_service_import(self):
        """Запрос в 2 recv-фрагмента успешно обрабатывается production-классом.

        Зеркалит test_handle_connection_with_split_payload из wave1767, но
        инстанс создаётся через ``backend.service.IPCServer`` — доказывая, что
        production-путь несёт reassembly-логику.
        """
        payload_str = json.dumps({"id": "frag", "method": "ping", "params": {}}) + "\n"
        raw = payload_str.encode("utf-8")
        mid = len(raw) // 2
        fragments = [raw[:mid], raw[mid:], b""]  # b"" = EOF

        responses: list[bytes] = []

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

        ipc = _make_service_ipc_server()
        ipc._conn_semaphore.acquire(blocking=False)
        ipc._handle_connection(_SplitSocket())

        self.assertEqual(len(responses), 1, "Должен быть ровно 1 ответ")
        resp = json.loads(responses[0].decode("utf-8"))
        self.assertTrue(resp.get("ok"), f"Ожидали ok=True, получили: {resp}")

    def test_recv_until_newline_reassembles_single_byte_chunks(self):
        """_recv_until_newline (через service-путь) собирает поток по 1 байту."""
        payload = json.dumps({"id": "1", "method": "ping", "params": {}}) + "\n"
        raw = payload.encode("utf-8")

        class _OneByteSocket:
            def __init__(self_):
                self_._pos = 0

            def settimeout(self_, t):
                pass

            def recv(self_, n):
                if self_._pos >= len(raw):
                    return b""
                chunk = raw[self_._pos:self_._pos + 1]
                self_._pos += 1
                return chunk

        ipc = _make_service_ipc_server()
        result = ipc._recv_until_newline(_OneByteSocket())
        parsed = json.loads(result.decode("utf-8"))
        self.assertEqual(parsed["method"], "ping")
        self.assertEqual(parsed["id"], "1")

    def test_silent_conn_closes_and_frees_semaphore_via_service_path(self):
        """slow-loris: молчащий коннект (socket.timeout) завершается + освобождает слот."""
        ipc = _make_service_ipc_server()

        # Опустошаем семафор, оставляя ровно один слот для обработчика.
        drained = 0
        while ipc._conn_semaphore.acquire(blocking=False):
            drained += 1
        ipc._conn_semaphore.release()  # вернули 1 слот
        drained -= 1
        self.assertTrue(ipc._conn_semaphore.acquire(blocking=False))  # заняли его

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.recv.side_effect = socket.timeout("timed out")

        t = threading.Thread(target=ipc._handle_connection, args=(mock_conn,), daemon=True)
        t.start()
        t.join(timeout=3.0)
        self.assertFalse(t.is_alive(), "Поток должен завершиться, а не висеть")

        # finally обязан был вернуть слот.
        re_acquired = ipc._conn_semaphore.acquire(blocking=False)
        self.assertTrue(re_acquired, "Семафор должен быть освобождён в finally")
        ipc._conn_semaphore.release()
        for _ in range(drained):
            ipc._conn_semaphore.release()


if __name__ == "__main__":
    unittest.main()
