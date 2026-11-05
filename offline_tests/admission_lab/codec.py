"""Изолированный тестовый wire codec; не production transport."""

import json
import re
import struct
import uuid

_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z", re.ASCII)
_COMMON = {"v", "op", "request_id", "client_incarnation"}
_FIELDS = {"reserve": _COMMON | {"kind", "executor"},
           "claim": _COMMON | {"grant_id", "executor"}, "status": _COMMON}


def _reject_number(_value):
    raise ValueError("Недопустимое число JSON")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Повторный ключ JSON")
        result[key] = value
    return result


def _name(value):
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise ValueError("Недопустимый идентификатор")


def decode_request(frame: bytes) -> dict:
    """Проверить полный кадр и вернуть допустимый lab request."""
    if not isinstance(frame, bytes) or len(frame) < 4:
        raise ValueError("Неполный заголовок")
    length = struct.unpack("!I", frame[:4])[0]
    if not 1 <= length <= 65536 or len(frame) != length + 4:
        raise ValueError("Недопустимая длина кадра")
    try:
        request = json.loads(frame[4:].decode("utf-8"), object_pairs_hook=_unique_pairs,
                             parse_float=_reject_number, parse_constant=_reject_number)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError("Некорректный JSON") from exc
    if not isinstance(request, dict) or type(request.get("v")) is not int or request["v"] != 0:
        raise ValueError("Недопустимая версия или корень")
    op = request.get("op")
    if not isinstance(op, str) or op not in _FIELDS or set(request) != _FIELDS[op]:
        raise ValueError("Недопустимые операция или поля")
    _name(request["request_id"])
    _name(request["client_incarnation"])
    if op in {"reserve", "claim"}:
        _name(request["executor"])
    if op == "reserve" and request["kind"] not in ("stt", "tts"):
        raise ValueError("Недопустимый kind")
    if op == "claim":
        grant = request["grant_id"]
        try:
            if not isinstance(grant, str) or str(uuid.UUID(grant)) != grant:
                raise ValueError("Неканонический grant")
        except (ValueError, AttributeError) as exc:
            raise ValueError("Недопустимый grant") from exc
    return request
