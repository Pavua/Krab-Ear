"""Bounded REST→IPC replay; один epoch на WS, без outbox и фоновых retry."""
from __future__ import annotations

import json
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Callable

from backend.ipc_constants import IPC_MAX_MESSAGE_BYTES
from backend.request_signing import RequestSigner
from backend.translation_replay_ingress import read_ingress_token


class ReplayTransportError(RuntimeError):
    """Metadata-only transport failure; исходный ответ не отражается."""


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("non-finite JSON")


class TranslationReplayClient:
    def __init__(self, socket_path: Path, data_dir: Path, *, signing_enabled: bool = False,
                 signing_secret: str = "", rpc: Callable | None = None) -> None:
        self._path = Path(socket_path)
        self._token = read_ingress_token(data_dir)
        self._signing_enabled = signing_enabled
        self._signing_secret = signing_secret
        self._rpc = rpc or self._request
        self._anchor: str | None = None
        self._opened: dict | None = None

    def _request(self, method: str, params: dict, deadline: float) -> dict:
        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError("replay deadline")
            return value
        request_id = str(uuid.uuid4())
        request = {"id": request_id, "method": method, "params": params}
        if self._signing_enabled:
            signed = RequestSigner().sign_request(method, params, self._signing_secret)
            request.update(signature=signed.signature, timestamp=signed.timestamp, nonce=signed.nonce)
        wire = (json.dumps(request, ensure_ascii=False, allow_nan=False) + "\n").encode()
        if len(wire) > IPC_MAX_MESSAGE_BYTES:
            raise ReplayTransportError("replay request size")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(remaining())
            conn.connect(os.fspath(self._path))
            conn.settimeout(remaining())
            conn.sendall(wire)
            frame = bytearray()
            while b"\n" not in frame:
                conn.settimeout(remaining())
                chunk = conn.recv(min(65536, IPC_MAX_MESSAGE_BYTES + 1 - len(frame)))
                if not chunk:
                    raise ReplayTransportError("replay incomplete frame")
                frame.extend(chunk)
                if len(frame) > IPC_MAX_MESSAGE_BYTES:
                    raise ReplayTransportError("replay response size")
        if frame.find(b"\n") != len(frame) - 1:
            raise ReplayTransportError("replay trailing frame")
        try:
            response = json.loads(frame.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)
            if not isinstance(response, dict) or response.get("id") != request_id:
                raise ValueError
            if response.get("ok") is not True:
                return {"status": "rejected"}
            result = response.get("result")
            if not isinstance(result, dict):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ReplayTransportError("replay malformed response") from exc
        remaining()
        return result

    def open(self) -> dict:
        """Однократно ДО PCM admission; начальную недоступность нельзя переобозначить."""
        if self._opened is not None:
            return dict(self._opened)
        if self._token is None:
            self._opened = {"status": "unavailable"}
        else:
            try:
                self._opened = self._rpc("translation_replay_begin", {"translation_token": self._token}, time.monotonic() + 1)
                if self._opened.get("status") == "ready":
                    epoch = self._opened.get("epoch")
                    if not isinstance(epoch, str) or not epoch:
                        raise ReplayTransportError("missing epoch")
                    self._anchor = epoch
            except (OSError, ValueError, ReplayTransportError):
                self._opened = {"status": "unavailable"}
        return dict(self._opened)

    def begin_window(self) -> dict:
        if self._anchor is None:
            return dict(self._opened or {"status": "rejected"})
        try:
            return self._rpc("translation_replay_begin", {"translation_token": self._token, "epoch": self._anchor}, time.monotonic() + 1)
        except (OSError, ValueError, ReplayTransportError):
            return {"status": "unknown"}

    def commit_window(self, window: dict, payload: dict) -> dict:
        if window.get("status") != "ready" or not isinstance(window.get("ticket"), str):
            return {"status": "rejected"}
        params = {"translation_token": self._token, "ticket": window["ticket"], "payload": dict(payload)}
        deadline = time.monotonic() + 1
        for _ in range(2):
            try:
                return self._rpc("translation_replay_commit", params, min(deadline, time.monotonic() + .5))
            except (OSError, ValueError, ReplayTransportError):
                pass
        return {"status": "unknown"}

    def replay(self, cursor: str | None = None, limit: int = 200) -> dict:
        return self._rpc("get_event_log", {"cursor_mode": True, "event_type": "live_subs.result",
                                          "cursor": cursor, "limit": limit}, time.monotonic() + 1)
