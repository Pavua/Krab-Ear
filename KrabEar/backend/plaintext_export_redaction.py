"""Точечная A5.3 redaction перед log/error/telemetry сериализацией.

Контекст живёт только в стеке запроса. Нельзя применять к успешному IPC-ответу:
capability и receipt должны дойти до доверенного клиента, но не до логов.
"""
from __future__ import annotations

import ast
import json
import re
import traceback

_REDACTED = "[REDACTED]"
_AUTH_KEYS = frozenset({"capability", "receipt", "operation_receipt", "app_session_id"})
# JSON/repr и простые key=value сообщения; quoted values допускают escaping.
_AUTH_TEXT = re.compile(
    r'''(?i)(["']?(?:capability|operation_receipt|receipt|app_session_id)["']?\s*[:=]\s*)'''
    r'''("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,}\]]+)'''
)


def redact_auth(value: object, *, context: object = None) -> object:
    """Удаляет auth values рекурсивно и их повторы в строковых полях.

    Обход ограничен: malformed/cyclic структуры вызывают исключение, которое
    вызывающий telemetry/error boundary обязан обработать fail-closed.
    """
    secrets: set[str] = set()
    auth_texts: set[str] = set()
    remaining = 20000
    remaining_chars = 2_000_000

    def visit(obj: object, depth: int, sensitive: bool = False) -> bool:
        nonlocal remaining, remaining_chars
        remaining -= 1
        if depth > 40 or remaining < 0:
            raise ValueError("redaction structure limit")
        contains_auth = sensitive
        if isinstance(obj, dict):
            for key, item in obj.items():
                child_auth = visit(item, depth + 1, sensitive or str(key).lower() in _AUTH_KEYS)
                contains_auth = contains_auth or child_auth
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                child_auth = visit(item, depth + 1, sensitive)
                contains_auth = contains_auth or child_auth
        elif isinstance(obj, str):
            remaining_chars -= len(obj)
            if remaining_chars < 0:
                raise ValueError("redaction text limit")
            if sensitive and obj:
                secrets.add(obj)
            # request.data и exception.value могут содержать JSON/repr внутри
            # строки. Сначала собираем секреты, затем чистим ВСЕ поля события.
            text = obj.strip()
            if text.startswith(("{", "[", '"', "'")):
                try:
                    decoded = json.loads(text)
                except (ValueError, TypeError):
                    try:
                        decoded = ast.literal_eval(text)
                    except (ValueError, SyntaxError):
                        decoded = None
                if isinstance(decoded, (dict, list, tuple, str)) and decoded != obj:
                    if visit(decoded, depth + 1, sensitive):
                        # Исходная строка может содержать обратимое escaping
                        # ключа/значения. Простая замена decoded token недостаточна.
                        auth_texts.add(obj)
                        contains_auth = True
            for match in _AUTH_TEXT.finditer(obj):
                auth_texts.add(obj)
                contains_auth = True
                raw = match[2]
                if raw[0] in ("'", '"'):
                    # Только quoted scalar, не произвольное выражение.
                    try:
                        decoded = ast.literal_eval(raw)
                    except (ValueError, SyntaxError):
                        raise ValueError("malformed auth label") from None
                    if isinstance(decoded, str) and decoded:
                        secrets.add(decoded)
                elif raw:
                    secrets.add(raw)
        elif sensitive and isinstance(obj, (int, float)) and not isinstance(obj, bool):
            if str(obj):
                secrets.add(str(obj))
        return contains_auth

    visit(context, 0)
    visit(value, 0)
    # Удаляем и повтор самой encoded-строки внутри другого сообщения.
    ordered = sorted(secrets | auth_texts, key=len, reverse=True)

    def clean_text(text: str) -> str:
        if text in auth_texts:
            return _REDACTED
        for secret in ordered:
            text = text.replace(secret, _REDACTED)
        return _AUTH_TEXT.sub(lambda match: match[1] + _REDACTED, text)

    def clean(obj: object) -> object:
        if isinstance(obj, str):
            return clean_text(obj)
        if isinstance(obj, dict):
            return {
                clean_text(str(key)): (
                    _REDACTED if str(key).lower() in _AUTH_KEYS else clean(item)
                )
                for key, item in obj.items()
            }
        if isinstance(obj, (list, tuple)):
            return [clean(item) for item in obj]
        if obj is None or isinstance(obj, (bool, int, float)):
            return obj
        # Не вызываем repr пользовательского объекта в диагностическом пути.
        return _REDACTED

    return clean(value)


def safe_error_metadata(exc: Exception, *, method: str, context: object = None) -> dict:
    """Безопасные признаки ошибки для независимых лимитов Sentry."""
    try:
        return redact_auth({"ipc_method": method, "ipc_error_type": type(exc).__name__}, context=context)
    except Exception:
        return {"ipc_method": "redacted", "ipc_error_type": "redacted"}


def safe_error(exc: Exception, *, context: object = None, with_traceback: bool = False) -> tuple[str, str]:
    """Без raw exception/traceback в LogRecord; сохраняет безопасные детали стека."""
    try:
        details = {
            "message": str(exc),
            "traceback": "".join(traceback.format_exception(exc)) if with_traceback else "",
        }
        sanitized = redact_auth(details, context=context)
        return sanitized["message"], sanitized["traceback"]
    except Exception:
        # Не логируем exc/sanitizer exception: оба могут содержать исходный secret.
        return "Ошибка обработки запроса (диагностика скрыта)", ""


def safe_request_id(payload: dict) -> object:
    """JSON request id отражается только как scalar, также без auth-секретов."""
    value = payload.get("id")
    if value is not None and not isinstance(value, (str, int, float)):
        return None
    try:
        return redact_auth(value, context=payload)
    except Exception:
        return None
