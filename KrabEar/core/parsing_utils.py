"""Shared JSON parsing utilities with graceful error handling.

Consolidates the repeated try/except json.loads/json.dumps pattern
used across backend and core modules.
"""
from __future__ import annotations

import json
import logging
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def safe_json_loads(
    data: "str | bytes",
    default: T = None,
    *,
    context: str = "",
) -> "Any | T":
    """Parse JSON with graceful fallback to *default* on error.

    Args:
        data: JSON string or bytes to parse.
        default: Value returned when parsing fails (default ``None``).
        context: Optional label included in the warning log message
            (e.g. file name, IPC method).  Kept empty by default to
            avoid revealing sensitive paths in generic callers.

    Returns:
        Parsed Python object or *default*.

    Examples:
        >>> safe_json_loads('{"a": 1}')
        {'a': 1}
        >>> safe_json_loads("bad json", default={})
        {}
        >>> safe_json_loads(b'[1, 2]')
        [1, 2]
        >>> safe_json_loads("", default=42)
        42
    """
    if not data:
        return default
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        ctx = f" ({context})" if context else ""
        logger.warning("JSON parse failed%s: %s", ctx, exc)
        return default


def safe_json_dumps(obj: Any, default: str = "{}") -> str:
    """Serialize *obj* to JSON string with fallback on serialization error.

    Args:
        obj: Python object to serialize.
        default: String returned when serialization fails (default ``"{}"``).

    Returns:
        JSON string or *default*.

    Examples:
        >>> safe_json_dumps({"a": 1})
        '{"a": 1}'
        >>> safe_json_dumps([1, 2, 3])
        '[1, 2, 3]'
    """
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        logger.warning("JSON serialize failed: %s", exc)
        return default
