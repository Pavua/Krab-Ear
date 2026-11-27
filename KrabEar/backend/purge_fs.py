"""Честное удаление каталогов для privacy purge.

``shutil.rmtree(path, ignore_errors=True)`` проглатывает любые сбои, и purge
писал «удалено» поверх оставшихся данных (для ``shares/`` это открытый текст
пакетов транскрипций). ``rmtree_strict`` удаляет всё, что может, не обрывается
на первой ошибке, а в конце бросает ``OSError``, если что-то осталось.
Вызывающий шаг purge ловит исключение и пишет своё имя в ``secondary_errors``.

``purge_flat_dir_contents`` — для плоских каталогов (``rescue/``, ``temp_uploads/``),
которые сами должны остаться: содержимое удаляется через fd каталога, без pathlib.
"""

from __future__ import annotations

import os
import shutil
import stat
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


_FLAT_DIR_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def purge_flat_dir_contents(path: Any) -> tuple[int, list[str]]:
    """Очищает плоский каталог, оставляя сам каталог. Возвращает ``(удалено, проблемы)``.

    G3-P03-06 (ревью #2320): ``Path.is_dir()``/``is_symlink()`` на Python 3.14
    отвечают False на ``PermissionError`` — недоступный каталог читался как
    «отсутствует», и purge рапортовал успех. Здесь каталог открывается один раз
    (``O_DIRECTORY|O_NOFOLLOW``), обход и удаление идут относительно его fd:
    подмена пути симлинком после открытия уже не перенаправит удаление.

    - Каталога нет (ENOENT) — ``(0, [])``, это не ошибка.
    - Симлинк на месте каталога, не каталог, любая другая ошибка открытия —
      ``OSError`` наружу; цель симлинка не трогается.
    - Обычный файл удаляется; симлинк на файл — снимается только ссылка.
    - Подкаталог и прочие объекты не трогаются и попадают в «проблемы»:
      каталог плоский, вложенность — неожиданное состояние.
    """
    try:
        dfd = os.open(path, _FLAT_DIR_FLAGS)
    except FileNotFoundError:
        return 0, []
    deleted = 0
    problems: list[str] = []
    try:
        if not stat.S_ISDIR(os.fstat(dfd).st_mode):
            raise NotADirectoryError(f"purge_flat_dir_contents: не каталог: {path}")
        for name in os.listdir(dfd):
            try:
                mode = os.stat(name, dir_fd=dfd, follow_symlinks=False).st_mode
            except FileNotFoundError:
                continue
            except OSError as exc:
                problems.append(f"{name}: {exc}")
                continue
            if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                problems.append(f"{name}: не обычный файл, оставлен")
                continue
            try:
                os.unlink(name, dir_fd=dfd)
            except FileNotFoundError:
                continue
            except OSError as exc:
                problems.append(f"{name}: {exc}")
                continue
            deleted += 1
    finally:
        os.close(dfd)
    return deleted, problems
