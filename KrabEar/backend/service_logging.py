"""Logging configuration for the Krab Ear backend service.

Extracted from backend/service.py (W797 phase 1).
Provides configure_logging(), JsonFormatter, and _STANDARD_LOG_ATTRS.
"""

from __future__ import annotations

import json as _json
import logging
import sys
from pathlib import Path

from core.config import settings


# Standard LogRecord attributes that must NOT be included in JSON "extra" output.
_STANDARD_LOG_ATTRS: frozenset[str] = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
})


class JsonFormatter(logging.Formatter):
    """Форматтер, сериализующий записи лога в JSON.

    Любой ключ из ``extra={}`` (не входящий в стандартные атрибуты LogRecord)
    автоматически включается в итоговый JSON-объект.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Merge extra= fields — any attribute not in the standard set
        extra = {
            k: v for k, v in record.__dict__.items()
            if k not in _STANDARD_LOG_ATTRS
        }
        if extra:
            log_entry.update(extra)
        # Append exception info if present
        if record.exc_info:
            log_entry["exc"] = self.formatException(record.exc_info)
        return _json.dumps(log_entry, default=str)


def configure_logging(data_dir: Path) -> None:
    """Настраивает логирование backend в файл и stdout."""
    from logging.handlers import RotatingFileHandler as _RotatingFileHandler  # noqa: PLC0415
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "backend.log"

    if settings.LOG_FORMAT == "json":
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        _RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,  # 5 MB — wave687 log rotation
            backupCount=3,
            encoding="utf-8",
        ),
    ]
    for h in handlers:
        h.setFormatter(formatter)

    logging.basicConfig(level=logging.INFO, handlers=handlers)
