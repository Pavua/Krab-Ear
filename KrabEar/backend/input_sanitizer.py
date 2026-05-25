"""Санитизация входных параметров IPC-запросов Krab Ear.

Защита от: path traversal, чрезмерно длинных строк, управляющих символов,
некорректных числовых диапазонов, слишком больших списков.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# Regex: управляющие символы кроме \t, \n, \r
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Поля, которые содержат пути к файлам/директориям
_PATH_FIELDS = frozenset(
    {
        "path",
        "file_path",
        "audio_path",
        "import_path",
        "export_path",
        "backup_path",
        "output_path",
        "transcript_path",
        "ndjson_path",
    }
)

# Числовые поля с допустимыми диапазонами (min, max, coerce_type)
_NUMERIC_FIELDS: dict[str, tuple[Any, Any, type]] = {
    "page": (0, 10_000, int),
    "page_size": (1, 1000, int),
    "limit": (1, 10_000, int),
    "offset": (0, 10_000_000, int),
    "days": (1, 3650, int),
    "confidence_threshold": (0.0, 1.0, float),
    "duration_seconds": (0.0, 86_400.0, float),
    "max_items": (1, 10_000, int),
    "min_confidence": (0.0, 1.0, float),
    "max_confidence": (0.0, 1.0, float),
}

# Строковые поля с уменьшенным лимитом длины
_SHORT_STRING_FIELDS: dict[str, int] = {
    "method": 256,
    "id": 256,
    "item_id": 512,
    "speaker": 256,
    "lang": 64,
    "source_lang": 64,
    "target_lang": 64,
    "profile": 128,
    "preset": 128,
    "format": 64,
}

# Разрешённые базовые директории (будут дополнены при необходимости)
_DEFAULT_ALLOWED_DIRS = [
    str(Path.home()),
    "/tmp",
    "/var/folders",
]


class InputSanitizer:
    """Санитизирует параметры IPC-запроса перед обработкой."""

    def __init__(self, allowed_dirs: list[str] | None = None) -> None:
        self._allowed_dirs: list[str] = allowed_dirs if allowed_dirs is not None else _DEFAULT_ALLOWED_DIRS

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sanitize_params(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Санитизирует dict параметров запроса.

        Возвращает новый dict с очищенными значениями.
        Бросает ValueError при попытке path traversal.
        """
        result: dict[str, Any] = {}
        for key, value in params.items():
            result[key] = self._sanitize_value(key, value)
        return result

    @staticmethod
    def sanitize_string(s: str, max_length: int = 10_000) -> str:
        """Очищает строку: strip, удаление управляющих символов, обрезка по длине."""
        if not isinstance(s, str):
            s = str(s)
        s = _CONTROL_RE.sub("", s)
        s = s.strip()
        if len(s) > max_length:
            s = s[:max_length]
        return s

    def sanitize_path(self, p: str, allowed_dirs: list[str] | None = None) -> str:
        """Нормализует путь и проверяет, что он находится в разрешённых директориях.

        Бросает ValueError при path traversal или выходе за пределы allowed_dirs.

        Важно: относительные пути НЕ допускаются — они резолвятся через CWD (рабочую
        директорию процесса) и могут проходить проверку allowed_dirs только случайно,
        когда CWD находится внутри home. Все входные пути должны быть абсолютными.
        Тильда (~) разворачивается как исключение.
        """
        if not isinstance(p, str):
            raise ValueError(f"Путь должен быть строкой, получено {type(p).__name__}")
        p = p.strip()
        if not p:
            raise ValueError("Пустой путь не допускается")

        # Запрещаем относительные пути (кроме тильды-сокращений)
        # Относительный путь резолвится через CWD — это непредсказуемо и опасно.
        expanded = os.path.expanduser(p)
        if not os.path.isabs(expanded):
            raise ValueError(
                f"Относительные пути не допускаются: {p!r}. "
                "Используйте абсолютный путь."
            )

        # Резолвим симлинки и нормализуем (убирает ../ и ./)
        resolved = Path(expanded).resolve()

        dirs = allowed_dirs if allowed_dirs is not None else self._allowed_dirs
        allowed_resolved = [Path(os.path.expanduser(d)).resolve() for d in dirs]
        if any(resolved.is_relative_to(a) for a in allowed_resolved):
            return str(resolved)

        raise ValueError(
            f"Path traversal или недопустимый путь: {p!r} разрешается в {resolved}, "
            f"не принадлежит разрешённым директориям: {dirs}"
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _sanitize_value(self, key: str, value: Any) -> Any:
        if value is None:
            return value

        if key in _PATH_FIELDS:
            if isinstance(value, str):
                return self.sanitize_path(value)
            return value

        if key in _NUMERIC_FIELDS:
            mn, mx, typ = _NUMERIC_FIELDS[key]
            try:
                coerced = typ(value)
            except (TypeError, ValueError):
                coerced = mn
            return max(mn, min(mx, coerced))

        if isinstance(value, str):
            max_len = _SHORT_STRING_FIELDS.get(key, 10_000)
            return self.sanitize_string(value, max_length=max_len)

        if isinstance(value, list):
            if len(value) > 1000:
                value = value[:1000]
            return [self._sanitize_value(key, item) for item in value]

        if isinstance(value, dict):
            return {k: self._sanitize_value(k, v) for k, v in value.items()}

        return value
