"""Настоящий временный Unix-сокет: подпись, bounded framing и один deadline."""

import importlib
import importlib.util
import io
import json
import socket
import tempfile
import threading
import time
import wave
from contextlib import contextmanager
from pathlib import Path

import pytest

from backend.ipc_constants import IPC_MAX_MESSAGE_BYTES
from backend.request_signing import RequestSigner


def _client():
    assert importlib.util.find_spec("backend.call_stt_client"), "call IPC client missing"
    return importlib.import_module("backend.call_stt_client")


def _wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\0\0" * 800)
    return output.getvalue()


def _reply(request, result=None):
    return (json.dumps({
        "id": request["id"], "ok": True,
        "result": result if result is not None else {"status": "ok", "text": "Привет"},
    }, ensure_ascii=False) + "\n").encode()


@contextmanager
def _server(reply):
    # macOS AF_UNIX pathname cap 104 bytes: не используем длинный pytest tmp_path.
    with tempfile.TemporaryDirectory(prefix="call-ipc-", dir="/tmp") as directory:
        path = Path(directory) / "ipc.sock"
        received = []
        errors = []
        stop = threading.Event()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            listener.listen(2)
            listener.settimeout(0.1)

            def serve():
                try:
                    while not stop.is_set():
                        try:
                            connection, _ = listener.accept()
                        except socket.timeout:
                            continue
                        with connection:
                            connection.settimeout(2)
                            frame = bytearray()
                            while not frame.endswith(b"\n"):
                                chunk = connection.recv(65536)
                                if not chunk:
                                    return
                                frame.extend(chunk)
                            request = json.loads(frame)
                            received.append(request)
                            reply(connection, request, stop)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Клиент ушёл по собственному deadline.
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=serve, name="test-call-ipc")
            thread.start()
            try:
                yield path, received
            finally:
                stop.set()
                thread.join(timeout=3)
                assert not thread.is_alive(), "temporary IPC server did not drain"
                assert not errors, errors


def _call(path, *, timeout=2, **kwargs):
    return _client().transcribe_ephemeral_call(
        _wav(), socket_path=path, deadline_monotonic=time.monotonic() + timeout,
        signing_enabled=kwargs.pop("signing_enabled", False),
        signing_secret=kwargs.pop("signing_secret", ""), **kwargs,
    )


@pytest.mark.parametrize("signed", [False, True])
def test_socket_success_and_exact_signing(signed):
    secret = RequestSigner.generate_secret()
    result = {
        "status": "ok", "text": "Привет", "adapter": "gigaam",
        "mode": "v3_e2e_rnnt", "model": "gigaam-v3", "transport": "owner_ipc",
    }

    def reply(connection, request, stop):
        assert request["id"] == request["params"]["request_id"]
        assert request["method"] == "transcribe_ephemeral_call"
        if signed:
            assert RequestSigner().verify_request(
                request["method"], request["params"], request["signature"], secret,
                timestamp=request["timestamp"], nonce=request["nonce"],
            )
        else:
            assert "signature" not in request
        connection.sendall(_reply(request, result))

    with _server(reply) as (path, received):
        assert _call(path, signing_enabled=signed, signing_secret=secret) == result
        assert len(received) == 1


@pytest.mark.parametrize("status", ["busy", "not_ready", "timeout", "privacy_mode", "closing", "error"])
def test_owner_refusal_is_preserved_and_never_retried(status):
    result = {"status": status, "text": "", "reason": "owner_refused"}
    with _server(lambda connection, request, stop: connection.sendall(_reply(request, result))) as (path, received):
        assert _call(path) == result
        assert len(received) == 1


@pytest.mark.parametrize("signing_enabled,signing_secret", [
    (True, ""), (True, "   "), (True, None), (True, 123), ("true", "secret"),
])
def test_missing_signing_secret_fails_before_connect(monkeypatch, signing_enabled, signing_secret):
    client = _client()

    def forbid(*args, **kwargs):
        pytest.fail("invalid signing state opened socket")

    monkeypatch.setattr(socket, "socket", forbid)
    with pytest.raises(client.CallSTTRejectedError):
        _call("/tmp/not-used.sock", signing_enabled=signing_enabled, signing_secret=signing_secret)


@pytest.mark.parametrize("code", ["unauthorized", "rate_limit_exceeded"])
def test_policy_rejection_has_no_unsigned_retry(code):
    def reply(connection, request, stop):
        connection.sendall((json.dumps({
            "id": request["id"], "ok": False,
            "error": {"code": code, "message": "sensitive text not surfaced"},
        }) + "\n").encode())

    with _server(reply) as (path, received):
        client = _client()
        assert hasattr(client, "CallSTTRejectedError"), "authorization refusal requires typed error"
        with pytest.raises(client.CallSTTRejectedError) as error:
            _call(path, signing_enabled=True, signing_secret=RequestSigner.generate_secret())
        assert "sensitive text" not in str(error.value)
        assert len(received) == 1


@pytest.mark.parametrize("kind", [
    "bad_json", "bad_utf8", "missing_newline", "wrong_id", "no_result", "not_object",
    "unknown_status", "invalid_text", "missing_text", "refusal_with_text", "no_ok",
    "truthy_ok", "nan", "float_overflow", "duplicate_id", "extra_response", "oversize", "result_id",
])
def test_malformed_responses_fail_closed(kind):
    def reply(connection, request, stop):
        frame = _reply(request)
        envelope = json.loads(frame)
        if kind == "bad_json":
            frame = b"{\n"
        elif kind == "bad_utf8":
            frame = b"\xff\n"
        elif kind == "missing_newline":
            frame = frame[:-1]
        elif kind == "wrong_id":
            envelope["id"] = "some-other-request"
        elif kind == "no_result":
            del envelope["result"]
        elif kind == "not_object":
            frame = b"[]\n"
        elif kind == "unknown_status":
            envelope["result"]["status"] = "maybe"
        elif kind == "invalid_text":
            envelope["result"]["text"] = 123
        elif kind == "missing_text":
            del envelope["result"]["text"]
        elif kind == "refusal_with_text":
            envelope["result"]["status"] = "privacy_mode"
        elif kind == "no_ok":
            del envelope["ok"]
        elif kind == "truthy_ok":
            envelope["ok"] = 1
        elif kind == "nan":
            envelope["result"]["latency"] = float("nan")
        elif kind == "float_overflow":
            frame = frame.replace(b'"result": {', b'"result": {"latency": 1e9999,')
        elif kind == "duplicate_id":
            frame = b'{"id":"wrong",' + frame[1:]
        elif kind == "extra_response":
            frame += frame
        elif kind == "oversize":
            envelope["result"]["text"] = "x" * IPC_MAX_MESSAGE_BYTES
        elif kind == "result_id":
            envelope["result"]["request_id"] = "other"
        if kind not in {"bad_json", "bad_utf8", "missing_newline", "not_object", "duplicate_id", "extra_response", "float_overflow"}:
            frame = (json.dumps(envelope) + "\n").encode()
        connection.sendall(frame)

    with _server(reply) as (path, received):
        with pytest.raises(_client().CallSTTProtocolError):
            _call(path)
        assert len(received) == 1


def test_slow_chunks_cannot_reset_total_deadline():
    def reply(connection, request, stop):
        frame = _reply(request)
        for chunk in [frame[:10], frame[10:20], frame[20:]]:
            if stop.wait(0.08):
                return
            connection.sendall(chunk)

    with _server(reply) as (path, received):
        started = time.monotonic()
        with pytest.raises(_client().CallSTTTimeoutError):
            _call(path, timeout=0.19)
        assert time.monotonic() - started < 1.5
        assert len(received) == 1


def test_expired_deadline_never_opens_socket(monkeypatch):
    client = _client()
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: pytest.fail("expired deadline connected"))
    with pytest.raises(client.CallSTTTimeoutError):
        _call("/tmp/not-used.sock", timeout=-1)


def test_envelope_overflow_never_opens_socket(monkeypatch):
    client = _client()
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\0\0" * (16000 * 25))
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: pytest.fail("oversize envelope connected"))
    with pytest.raises(client.CallSTTProtocolError):
        client.transcribe_ephemeral_call(
            output.getvalue(), socket_path="/tmp/not-used.sock",
            deadline_monotonic=time.monotonic() + 1,
            signing_enabled=False, signing_secret="",
        )


def test_backpressured_send_uses_same_deadline():
    client = _client()
    # > socket send-buffer, но < полного IPC cap после base64.
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\0\0" * (16000 * 20))
    stop = threading.Event()
    accepted = threading.Event()
    with tempfile.TemporaryDirectory(prefix="call-send-", dir="/tmp") as directory:
        path = Path(directory) / "ipc.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            listener.listen(1)
            listener.settimeout(2)

            def hold_connection():
                with listener.accept()[0] as connection:
                    connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
                    accepted.set()
                    stop.wait(2)

            thread = threading.Thread(target=hold_connection, name="test-call-ipc-send")
            thread.start()
            started = time.monotonic()
            try:
                with pytest.raises(client.CallSTTTimeoutError):
                    client.transcribe_ephemeral_call(
                        output.getvalue(), socket_path=path,
                        deadline_monotonic=started + 0.2,
                        signing_enabled=False, signing_secret="",
                    )
                assert accepted.is_set()
                assert time.monotonic() - started < 1.5
            finally:
                stop.set()
                thread.join(timeout=3)
                assert not thread.is_alive()


def test_connection_failure_is_typed_and_has_no_secret_or_path():
    client = _client()
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        path = Path(directory) / "sensitive-name.sock"
        with pytest.raises(client.CallSTTProtocolError) as error:
            _call(path)
        assert "sensitive-name" not in str(error.value)


@pytest.mark.parametrize("outcome", ["privacy_mode", "unauthorized", "rate_limit_exceeded", "ok"])
@pytest.mark.parametrize("received_at", [102.0, 103.0])
def test_completed_refusal_frame_survives_deadline_but_success_does_not(monkeypatch, outcome, received_at):
    client = _client()
    now = [100.0]
    monkeypatch.setattr(client.time, "monotonic", lambda: now[0])
    original_recv = socket.socket.recv

    def recv(connection, *args, **kwargs):
        chunk = original_recv(connection, *args, **kwargs)
        # Ответ реально получен по AF_UNIX целиком. Дедлайн истёк именно
        # между последним recv и разбором кадра, а не до открытия сокета.
        if chunk.endswith(b"\n") and b'"ok":' in chunk:
            now[0] = received_at
        return chunk

    monkeypatch.setattr(socket.socket, "recv", recv)

    def reply(connection, request, stop):
        if outcome in {"unauthorized", "rate_limit_exceeded"}:
            frame = (json.dumps({
                "id": request["id"], "ok": False,
                "error": {"code": outcome, "message": "refused"},
            }) + "\n").encode()
        else:
            frame = _reply(request, {"status": outcome, "text": "" if outcome == "privacy_mode" else "Привет"})
        connection.sendall(frame)

    with _server(reply) as (path, received):
        if outcome == "privacy_mode":
            assert _call(path, timeout=2) == {"status": "privacy_mode", "text": ""}
        else:
            expected = client.CallSTTTimeoutError if outcome == "ok" else client.CallSTTRejectedError
            with pytest.raises(expected):
                _call(path, timeout=2)
        assert len(received) == 1
        assert now[0] == received_at
