"""Tests for BrokenPipeError / ConnectionResetError handling in IPCServer._handle_connection.

Wave 70 log analysis flagged 4 historical occurrences of unhandled BrokenPipeError
at the conn.sendall site in service.py.  These tests verify that the fix (catching
BrokenPipeError, ConnectionResetError, OSError at both sendall sites) is in place and
that _handle_connection never raises when the Swift client disconnects mid-call.
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.service import IPCServer, BackendService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ipc_server() -> IPCServer:
    """Return a minimal IPCServer with a real BackendService backed by stubs."""
    tmp = tempfile.mkdtemp()
    store = StateStore(Path(tmp) / "data")

    class _FakeRecorder:
        is_recording = False
        sample_rate = 16000
        _snapshot_counter = 0
        last_stop_trim_ms = 0
        last_stop_timeout_sec = 3.0

        def start(self):
            self.is_recording = True
            return True

        def stop(self, timeout_sec=3.0, trim_tail_ms=0):
            self.is_recording = False
            return None

        def snapshot_audio(self, max_duration_sec=12.0):
            import numpy as np
            return np.zeros(100, dtype=np.float32), 0.1

    class _FakeTranscriber:
        def transcribe(self, *a, **kw):
            return "ok"

        def transcribe_preview(self, *a, **kw):
            return "preview"

    class _FakeTranslator:
        def translate(self, text, mode, network_mode, **kw):
            from backend.translator import TranslationResult
            return TranslationResult(
                text="", status="not_requested",
                source_lang="", target_lang="",
                mode="off", engine="fake",
            )

    service = BackendService(
        store=store,
        recorder=_FakeRecorder(),
        transcriber=_FakeTranscriber(),
        translator=_FakeTranslator(),
    )
    return IPCServer(socket_path=Path(tmp) / "test.sock", service=service)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class BrokenPipeOnSendallTestCase(unittest.TestCase):
    """_handle_connection must not raise when Swift client disconnects mid-call."""

    def setUp(self):
        self.ipc = _make_ipc_server()

    def _run_handle(self, conn) -> None:
        """Вызывает _handle_connection как это делает serve_forever.

        W1768: production IPCServer (теперь закалённый класс из ipc_server.py)
        перед запуском обработчика занимает слот BoundedSemaphore, а
        _handle_connection освобождает его в finally. Тест-вызовы напрямую
        обязаны сначала занять слот, иначе release() кинет
        "Semaphore released too many times".
        """
        self.ipc._conn_semaphore.acquire(blocking=False)
        self.ipc._handle_connection(conn)

    # ------------------------------------------------------------------
    # 1. BrokenPipeError on the normal-response sendall path
    # ------------------------------------------------------------------

    def test_broken_pipe_on_response_does_not_raise(self) -> None:
        """_handle_connection silently drops BrokenPipeError on conn.sendall."""
        server_sock, client_sock = socket.socketpair()
        # Send a valid request then immediately close the client — server will
        # try to write back to a dead socket.
        req = json.dumps({"id": "t1", "method": "ping", "params": {}}) + "\n"
        client_sock.sendall(req.encode())
        client_sock.close()  # disconnect before server replies

        # Must not raise
        try:
            self._run_handle(server_sock)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self.fail(
                f"_handle_connection raised {type(exc).__name__} instead of "
                f"handling it internally: {exc}"
            )
        finally:
            server_sock.close()

    # ------------------------------------------------------------------
    # 2. ConnectionResetError simulated via socket wrapper
    # ------------------------------------------------------------------

    def test_connection_reset_on_response_does_not_raise(self) -> None:
        """_handle_connection silently drops ConnectionResetError on conn.sendall."""
        server_sock, client_sock = socket.socketpair()
        req = json.dumps({"id": "t2", "method": "ping", "params": {}}) + "\n"
        client_sock.sendall(req.encode())
        client_sock.shutdown(socket.SHUT_RD)  # half-close read end

        # Wrap server_sock in a subclass that overrides sendall to raise
        # ConnectionResetError — socket.sendall is read-only on Python 3.14+.
        class _ResetSocket:
            """Thin delegation wrapper with sendall overridden."""

            def __init__(self, sock: socket.socket) -> None:
                self._sock = sock

            def settimeout(self, t) -> None:
                self._sock.settimeout(t)

            def recv(self, n: int) -> bytes:
                return self._sock.recv(n)

            def sendall(self, data: bytes) -> None:
                raise ConnectionResetError(104, "Connection reset by peer")

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self._sock.close()

        wrapped = _ResetSocket(server_sock)
        try:
            self._run_handle(wrapped)  # type: ignore[arg-type]
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self.fail(
                f"_handle_connection raised {type(exc).__name__} instead of "
                f"handling it internally: {exc}"
            )
        finally:
            client_sock.close()

    # ------------------------------------------------------------------
    # 3. OSError(EPIPE) simulated via socket wrapper
    # ------------------------------------------------------------------

    def test_oserror_epipe_on_response_does_not_raise(self) -> None:
        """_handle_connection silently drops OSError(EPIPE) on conn.sendall."""
        import errno as _errno
        server_sock, client_sock = socket.socketpair()
        req = json.dumps({"id": "t3", "method": "ping", "params": {}}) + "\n"
        client_sock.sendall(req.encode())
        client_sock.close()

        class _EpipeSocket:
            def __init__(self, sock: socket.socket) -> None:
                self._sock = sock

            def settimeout(self, t) -> None:
                self._sock.settimeout(t)

            def recv(self, n: int) -> bytes:
                return self._sock.recv(n)

            def sendall(self, data: bytes) -> None:
                raise OSError(_errno.EPIPE, "Broken pipe")

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self._sock.close()

        wrapped = _EpipeSocket(server_sock)
        try:
            self._run_handle(wrapped)  # type: ignore[arg-type]
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self.fail(
                f"_handle_connection raised {type(exc).__name__}: {exc}"
            )
        # server_sock closed via __exit__ of wrapper

    # ------------------------------------------------------------------
    # 4. BrokenPipeError on the invalid-json-response sendall path
    # ------------------------------------------------------------------

    def test_broken_pipe_on_invalid_json_response_does_not_raise(self) -> None:
        """BrokenPipeError on the invalid_json error-response path is also handled."""
        server_sock, client_sock = socket.socketpair()
        # Send malformed JSON then close immediately
        client_sock.sendall(b"NOT JSON AT ALL\n")
        client_sock.close()

        try:
            self._run_handle(server_sock)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self.fail(
                f"_handle_connection raised {type(exc).__name__} on invalid-json "
                f"path: {exc}"
            )
        finally:
            server_sock.close()

    # ------------------------------------------------------------------
    # 5. Normal flow still works after the fix
    # ------------------------------------------------------------------

    def test_normal_ping_response_still_works(self) -> None:
        """Sanity: _handle_connection still sends a correct response for valid input."""
        server_sock, client_sock = socket.socketpair()
        req = json.dumps({"id": "sanity", "method": "ping", "params": {}}) + "\n"
        client_sock.sendall(req.encode())
        client_sock.shutdown(socket.SHUT_WR)  # EOF — not full close

        self._run_handle(server_sock)

        client_sock.settimeout(1.0)
        buf = b""
        try:
            while b"\n" not in buf:
                chunk = client_sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        except socket.timeout:
            pass

        parsed = json.loads(buf.decode().strip())
        self.assertTrue(parsed.get("ok"), msg=f"Expected ok=True, got: {parsed}")
        self.assertEqual(parsed.get("id"), "sanity")

        server_sock.close()
        client_sock.close()


if __name__ == "__main__":
    unittest.main()
