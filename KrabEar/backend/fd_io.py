"""Общие примитивы записи через файловый дескриптор."""

from __future__ import annotations

import os


def write_all(fd: int, data: bytes) -> None:
    """Дописывает ``data`` в ``fd`` до конца.

    ``os.write`` вправе записать меньше запрошенного (ENOSPC, сигнал); одиночный вызов молча теряет
    хвост, и усечённый секрет/лиз публикуется через rename/link как успешный. Нулевое продвижение —
    ошибка (иначе цикл бесконечен), вызывающий не должен публиковать файл.
    """
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("os.write не продвигается (short write)")
        view = view[written:]
