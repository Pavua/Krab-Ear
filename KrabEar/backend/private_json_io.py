"""Atomic запись приватных JSON-файлов (G3-MEM-05 M05-02).

Заменяет три разных механизма записи (PasteAppMemory, SearchHistoryManager,
MemoryConductor._write_shadow_marker) одним: mkstemp(0600) → write → fsync →
os.replace → fsync(dirfd). Уже созданный файл имеет права 0600; случайный
tmp-файл и каталог получают права, которые мы задаём явно.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger("KrabEar.Backend.PrivateJsonIO")


def write_private_json(path: Path, payload: object) -> bool:
    """Атомарно записывает JSON-документ на диск с приватными правами.

    Возвращает True при подтверждённой успишной записи, False при сбое —
    вызывающая сторона не должна выглядеть успешной без подтверждения.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".private_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        logger.exception("write_private_json: ошибка сохранения %s", path)
        return False
    finally:
        Path(tmp).unlink(missing_ok=True)
    return True
