"""Приватная запись REST-загрузок в ``temp_uploads/``.

Сырое аудио владельца не должно читаться другими пользователями машины: каталог создаётся
с правами 0700, файлы — 0600 атомарно (``O_CREAT|O_EXCL`` с режимом сразу, без окна
umask-0644). Каталог открывается с ``O_NOFOLLOW`` и файл создаётся относительно его fd
(``dir_fd``), поэтому подмена каталога/файла симлинком отвергается, а не разыменовывается.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.PrivateUpload")

DIR_MODE = 0o700
FILE_MODE = 0o600


def ensure_private_dir(path: Path) -> None:
    """Создаёт каталог (0700) и приводит права существующего к 0700.

    Симлинк вместо каталога — ``OSError`` (fail-closed): не следуем по нему и не меняем права
    чужой цели. Сбой ``fchmod`` на нашем каталоге логируется: файлы всё равно создаются 0600.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(path, DIR_MODE)
    except FileExistsError:
        pass
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)  # симлинк → ELOOP, не каталог → ENOTDIR
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise NotADirectoryError(str(path))
        try:
            os.fchmod(fd, DIR_MODE)
        except OSError as exc:
            logger.warning("temp_uploads: не удалось выставить 0700: %s", exc.strerror or exc)
    finally:
        os.close(fd)


def save_private(source: Any, path: Path) -> None:
    """Пишет ``source`` в ``path`` с правами 0600; существующий файл/симлинк — ``OSError``.

    ``source`` — ``bytes`` либо объект с ``save(fileobj)`` (werkzeug ``FileStorage``).
    При сбое записи частичный файл остаётся вызывающему (его ``finally`` удаляет путь).
    """
    path = Path(path)
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    dfd = os.open(path.parent, dir_flags)
    try:
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        ffd = os.open(path.name, file_flags, FILE_MODE, dir_fd=dfd)
    finally:
        os.close(dfd)
    try:
        os.fchmod(ffd, FILE_MODE)
    except OSError:
        os.close(ffd)
        raise
    with os.fdopen(ffd, "wb") as out:
        if isinstance(source, (bytes, bytearray, memoryview)):
            out.write(source)
        else:
            source.save(out)
