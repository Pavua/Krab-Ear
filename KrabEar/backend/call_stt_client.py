"""Один ограниченный IPC-вызов уже существующего владельца GigaAM.

Настройки подписи и путь сокета передаёт REST из авторитетной конфигурации.
Здесь нет fallback, retry, чтения секретов, запуска моделей или записи истории.
"""

from __future__ import annotations

import json
import math
import os
import socket
import time
import uuid
from typing import Any

from backend.call_stt_wire import CallSTTValidationError, build_call_request
from backend.ipc_constants import IPC_MAX_MESSAGE_BYTES

_STATUSES = frozenset({"ok", "busy", "not_ready", "timeout", "privacy_mode", "closing", "error"})


class CallSTTProtocolError(RuntimeError):
    """Некорректный IPC-контракт или ошибка транспорта без исходного payload."""


class CallSTTTimeoutError(CallSTTProtocolError, TimeoutError):
    """Истёк общий бюджет запроса, в том числе до открытия соединения."""


class CallSTTRejectedError(CallSTTProtocolError):
    """Owner отклонил авторизацию; REST обязан запретить любой fallback."""


def _remaining(deadline_monotonic: float) -> float:
    if type(deadline_monotonic) not in (float, int) or not math.isfinite(deadline_monotonic):
        raise CallSTTProtocolError("Некорректный deadline IPC")
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise CallSTTTimeoutError("Истёк deadline IPC")
    return remaining


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Повтор JSON-ключа")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Недопустимая JSON-константа")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Нечисловое значение JSON")
    return result


def _decode_response(frame: bytes, request_id: str) -> dict[str, Any]:
    try:
        envelope = json.loads(
            frame.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant, parse_float=_finite_float,
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CallSTTProtocolError("Некорректный JSON-ответ IPC") from exc
    if not isinstance(envelope, dict) or envelope.get("id") != request_id:
        raise CallSTTProtocolError("IPC ответ не соответствует запросу")
    error = envelope.get("error")
    if envelope.get("ok") is False and isinstance(error, dict) and error.get("code") == "unauthorized":
        raise CallSTTRejectedError("Владелец отклонил авторизацию IPC")
    if envelope.get("ok") is not True or "error" in envelope:
        raise CallSTTProtocolError("Владелец отклонил IPC-запрос")
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise CallSTTProtocolError("Некорректный result IPC")
    status = result.get("status")
    text = result.get("text")
    if not isinstance(status, str) or status not in _STATUSES or not isinstance(text, str):
        raise CallSTTProtocolError("Некорректный исход телефонного STT")
    if status != "ok" and text:
        raise CallSTTProtocolError("Отказ STT содержит текст")
    if "request_id" in result and result["request_id"] != request_id:
        raise CallSTTProtocolError("STT result не соответствует запросу")
    return result


def transcribe_ephemeral_call(
    wav_bytes: bytes,
    *,
    socket_path: str | os.PathLike[str],
    deadline_monotonic: float,
    signing_enabled: bool,
    signing_secret: str,
) -> dict[str, Any]:
    """Возвращает проверенный owner result; все фазы расходуют один deadline."""
    if type(signing_enabled) is not bool or (
        signing_enabled and (not isinstance(signing_secret, str) or not signing_secret.strip())
    ):
        raise CallSTTRejectedError("Некорректная конфигурация авторизации IPC")
    _remaining(deadline_monotonic)
    request_id = str(uuid.uuid4())
    try:
        request = build_call_request(
            wav_bytes, request_id=request_id, deadline_monotonic=deadline_monotonic,
            signing_enabled=signing_enabled, signing_secret=signing_secret,
        )
    except CallSTTValidationError as exc:
        raise CallSTTProtocolError("Недопустимый телефонный IPC-запрос") from exc
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(_remaining(deadline_monotonic))
            connection.connect(os.fspath(socket_path))
            connection.settimeout(_remaining(deadline_monotonic))
            connection.sendall(request)
            frame = bytearray()
            while True:
                connection.settimeout(_remaining(deadline_monotonic))
                chunk = connection.recv(min(65536, IPC_MAX_MESSAGE_BYTES - len(frame) + 1))
                if not chunk:
                    raise CallSTTProtocolError("IPC ответ оборван до newline")
                frame.extend(chunk)
                if len(frame) > IPC_MAX_MESSAGE_BYTES:
                    raise CallSTTProtocolError("IPC ответ превышает лимит")
                newline = frame.find(b"\n")
                if newline >= 0:
                    if newline != len(frame) - 1:
                        raise CallSTTProtocolError("Лишние данные после IPC ответа")
                    break
    except CallSTTProtocolError:
        raise
    except TimeoutError as exc:
        raise CallSTTTimeoutError("Истёк deadline IPC") from exc
    except (OSError, TypeError, ValueError) as exc:
        raise CallSTTProtocolError("Недоступен IPC транспорт") from exc
    # Полный кадр уже в памяти и ограничен IPC cap. Полученный запрет
    # privacy/авторизации нельзя превращать в retryable timeout: декодер
    # сначала сохранит отказ, и лишь обычный результат проверит дедлайн.
    result = _decode_response(bytes(frame), request_id)
    if result["status"] == "privacy_mode":
        return result
    _remaining(deadline_monotonic)
    return result
