"""Tests for IPC socket-level failure modes.

Covers edge cases that silently hit users in production but had zero test
coverage (D2 tech debt). Uses socket.socketpair() for unit-isolated pairs —
no real Unix socket path required. The "server" side is simulated inline;
the "client" side exercises the recv/decode path that callers rely on.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _send_raw(server_sock: socket.socket, data: bytes) -> None:
    """Send raw bytes from the server side of a socket pair."""
    server_sock.sendall(data)


def _client_recv_response(client_sock: socket.socket, bufsize: int = 65536) -> str:
    """Read newline-terminated response from client side; accumulates chunks."""
    buf = b""
    while b"\n" not in buf:
        chunk = client_sock.recv(bufsize)
        if not chunk:
            break
        buf += chunk
    return buf.decode("utf-8").strip()


def _client_recv_response_timeout(
    client_sock: socket.socket,
    timeout: float = 0.5,
) -> str | None:
    """Try to read a response; return None if timeout expires before data."""
    client_sock.settimeout(timeout)
    try:
        buf = b""
        while b"\n" not in buf:
            chunk = client_sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        return buf.decode("utf-8").strip() if buf else None
    except socket.timeout:
        return None
    finally:
        client_sock.settimeout(None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class IPCSocketErrorTestCase(unittest.TestCase):
    """Socket-level error handling — server-side faults vs client behaviour."""

    # ------------------------------------------------------------------
    # 1. Malformed JSON from server
    # ------------------------------------------------------------------

    def test_malformed_json_response_raises_specific_error(self) -> None:
        """Client receiving truncated/corrupt JSON must raise JSONDecodeError."""
        server_sock, client_sock = socket.socketpair()
        with server_sock, client_sock:
            # Server sends syntactically invalid JSON
            _send_raw(server_sock, b'{"ok": true, "result": {BROKEN\n')
            server_sock.shutdown(socket.SHUT_WR)

            raw = _client_recv_response(client_sock)
            with self.assertRaises(json.JSONDecodeError):
                json.loads(raw)

    # ------------------------------------------------------------------
    # 2. Truncated response reassembled from partial recv calls
    # ------------------------------------------------------------------

    def test_truncated_response_partial_recv_completes(self) -> None:
        """Client must accumulate chunks until newline even if server sends in parts."""
        server_sock, client_sock = socket.socketpair()
        with server_sock, client_sock:
            payload = json.dumps({"ok": True, "id": "x1", "result": {"status": "ok"}})
            # Split into two sends with a tiny gap — simulates slow handler flush
            half = len(payload) // 2

            def _send_in_parts() -> None:
                server_sock.sendall(payload[:half].encode())
                time.sleep(0.05)
                server_sock.sendall((payload[half:] + "\n").encode())

            t = threading.Thread(target=_send_in_parts, daemon=True)
            t.start()

            raw = _client_recv_response(client_sock)
            t.join(timeout=2)

            parsed = json.loads(raw)
            self.assertTrue(parsed.get("ok"))
            self.assertEqual(parsed.get("id"), "x1")

    # ------------------------------------------------------------------
    # 3. Socket timeout while waiting for server response
    # ------------------------------------------------------------------

    def test_socket_timeout_propagates(self) -> None:
        """When server never responds, recv with timeout raises socket.timeout."""
        server_sock, client_sock = socket.socketpair()
        with server_sock, client_sock:
            # Server stays silent — simulates a hung handler
            client_sock.settimeout(0.15)
            with self.assertRaises(socket.timeout):
                client_sock.recv(4096)

    # ------------------------------------------------------------------
    # 4. Broken pipe when server closes mid-write
    # ------------------------------------------------------------------

    def test_broken_pipe_during_send(self) -> None:
        """Sending to a closed socket raises BrokenPipeError (or OSError/EPIPE)."""
        server_sock, client_sock = socket.socketpair()
        # Close client immediately — server now writes to dead connection
        client_sock.close()

        large_payload = (b"x" * 65536) + b"\n"
        with self.assertRaises((BrokenPipeError, OSError, ConnectionResetError)):
            # May need multiple writes before the kernel buffer fills up and
            # returns EPIPE; loop until the error surfaces
            for _ in range(100):
                server_sock.sendall(large_payload)

        server_sock.close()

    # ------------------------------------------------------------------
    # 5. Empty / newline-only response
    # ------------------------------------------------------------------

    def test_empty_response_handled(self) -> None:
        """Client receiving only a bare newline gets an empty string, not a crash."""
        server_sock, client_sock = socket.socketpair()
        with server_sock, client_sock:
            _send_raw(server_sock, b"\n")
            server_sock.shutdown(socket.SHUT_WR)

            raw = _client_recv_response(client_sock)
            # Empty string — caller must guard against json.loads("")
            self.assertEqual(raw, "")
            with self.assertRaises((json.JSONDecodeError, ValueError)):
                json.loads(raw) if raw else (_ for _ in ()).throw(ValueError("empty"))

    # ------------------------------------------------------------------
    # 6. Large response requires multiple recv calls (>8 KB)
    # ------------------------------------------------------------------

    def test_large_response_multiple_recv_calls(self) -> None:
        """A response larger than a single recv buffer must be assembled correctly."""
        server_sock, client_sock = socket.socketpair()
        with server_sock, client_sock:
            # Build a valid JSON response that exceeds 8 KB
            big_text = "а" * 5000  # Cyrillic — 2 bytes each in UTF-8 → ~10 KB
            response = json.dumps(
                {"ok": True, "id": "big1", "result": {"text": big_text}},
                ensure_ascii=False,
            )
            encoded = (response + "\n").encode("utf-8")
            self.assertGreater(len(encoded), 8192, "Payload must exceed 8 KB")

            def _send() -> None:
                server_sock.sendall(encoded)

            t = threading.Thread(target=_send, daemon=True)
            t.start()

            # Client reads with small bufsize to force multiple recv calls
            buf = b""
            client_sock.settimeout(2.0)
            while b"\n" not in buf:
                chunk = client_sock.recv(1024)  # intentionally small
                if not chunk:
                    break
                buf += chunk

            t.join(timeout=2)
            parsed = json.loads(buf.decode("utf-8").strip())
            self.assertTrue(parsed.get("ok"))
            self.assertEqual(parsed["result"]["text"], big_text)

    # ------------------------------------------------------------------
    # 7. Concurrent calls use separate socket pairs (isolation)
    # ------------------------------------------------------------------

    def test_concurrent_calls_serialize_or_isolate(self) -> None:
        """Multiple callers using separate socket pairs receive correct responses."""
        NUM_WORKERS = 8
        errors: list[str] = []

        def _worker(idx: int) -> None:
            server_sock, client_sock = socket.socketpair()
            try:
                response = {"ok": True, "id": str(idx), "result": {"n": idx}}
                _send_raw(server_sock, (json.dumps(response) + "\n").encode())
                server_sock.shutdown(socket.SHUT_WR)

                raw = _client_recv_response(client_sock)
                parsed = json.loads(raw)
                if parsed.get("id") != str(idx):
                    errors.append(f"worker {idx}: got id={parsed.get('id')}")
                if parsed["result"]["n"] != idx:
                    errors.append(f"worker {idx}: got n={parsed['result']['n']}")
            except Exception as exc:
                errors.append(f"worker {idx}: {exc}")
            finally:
                server_sock.close()
                client_sock.close()

        threads = [
            threading.Thread(target=_worker, args=(i,), daemon=True)
            for i in range(NUM_WORKERS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], msg="\n".join(errors))


# ---------------------------------------------------------------------------
# Bonus: IPCServer._handle_connection unit tests
# ---------------------------------------------------------------------------

class IPCHandleConnectionTestCase(unittest.TestCase):
    """Tests for IPCServer._handle_connection using socketpair."""

    def _make_server_and_pair(self):
        """Return (IPCServer instance, server_sock, client_sock)."""
        import tempfile
        from backend.service import IPCServer, BackendService
        from backend.state_store import StateStore

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
        ipc = IPCServer(socket_path=Path(tmp) / "test.sock", service=service)
        server_sock, client_sock = socket.socketpair()
        return ipc, server_sock, client_sock

    def test_handle_connection_invalid_json_returns_error(self) -> None:
        """_handle_connection returns invalid_json error for malformed input."""
        ipc, server_sock, client_sock = self._make_server_and_pair()
        # client_sock is what the "caller" owns; server_sock is the accepted conn
        client_sock.sendall(b"THIS IS NOT JSON\n")
        client_sock.shutdown(socket.SHUT_WR)

        # Run _handle_connection synchronously — it reads from server_sock
        ipc._handle_connection(server_sock)

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
        self.assertFalse(parsed.get("ok"))
        self.assertEqual(parsed["error"]["code"], "invalid_json")
        client_sock.close()

    def test_handle_connection_valid_ping_returns_ok(self) -> None:
        """_handle_connection for a valid 'ping' request returns ok=True."""
        ipc, server_sock, client_sock = self._make_server_and_pair()

        req = json.dumps({"id": "ping1", "method": "ping", "params": {}}) + "\n"
        client_sock.sendall(req.encode())
        client_sock.shutdown(socket.SHUT_WR)

        ipc._handle_connection(server_sock)

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
        self.assertTrue(parsed.get("ok"))
        self.assertEqual(parsed.get("id"), "ping1")
        client_sock.close()


if __name__ == "__main__":
    unittest.main()
