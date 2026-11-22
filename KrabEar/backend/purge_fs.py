"""Честное удаление каталогов для privacy purge.

``shutil.rmtree(path, ignore_errors=True)`` проглатывает любые сбои, и purge
писал «удалено» поверх оставшихся данных (для ``shares/`` это открытый текст
пакетов транскрипций). ``rmtree_strict`` удаляет всё, что может, не обрывается
на первой ошибке, а в конце бросает ``OSError``, если что-то осталось.
Вызывающий шаг purge ловит исключение и пишет своё имя в ``secondary_errors``.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any


def rmtree_strict(path: Any) -> None:
    """Удаляет дерево целиком или бросает ``OSError`` с перечнем неудач.

    Уже отсутствующий путь (в том числе исчезнувший по ходу удаления) — не
    ошибка. Ошибки собираются, обход продолжается: purge обязан убрать всё,
    что убирается, а не остановиться на первом сбое.
    """
    target = Path(path)
    failures: list[tuple[str, BaseException]] = []

    def _collect(_func: Any, failed_path: Any, exc: Any) -> None:
        if isinstance(exc, tuple):  # onerror: (type, value, traceback)
            exc = exc[1]
        if isinstance(exc, FileNotFoundError):
            return
        failures.append((str(failed_path), exc))

    if sys.version_info >= (3, 12):
        shutil.rmtree(target, onexc=_collect)
    else:  # pragma: no cover — onerror объявлен устаревшим с 3.12
        shutil.rmtree(target, onerror=_collect)

    if failures or target.exists():
        first = failures[0] if failures else (str(target), "каталог остался")
        raise OSError(
            f"rmtree_strict: не удалось убрать {target} "
            f"({len(failures)} сбоев; первый: {first[0]}: {first[1]})"
        )
