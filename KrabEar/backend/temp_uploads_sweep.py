"""Уборка осиротевших сырых REST-загрузок из ``temp_uploads/``.

REST кладёт загруженное аудио в ``temp_uploads/`` и удаляет в ``finally``. Но процесс может
умереть между записью и удалением (fail-fast после таймаута, SIGKILL, перезапуск launchd,
ENOSPC), и файл с сырой речью владельца остаётся навсегда: на живом профиле 27.11.2026 нашлись
файлы с июля. ``purge_all_data`` их стирает только по явной команде, поэтому нужна
самостоятельная уборка по возрасту.

Правила безопасности:
- удаляются только обычные файлы (``lstat`` + ``S_ISREG``): симлинки, каталоги и спецфайлы
  не трогаются и не разыменовываются;
- сам каталог-симлинк не обходится вообще (fail-closed);
- свежие файлы (запрос ещё может идти) не удаляются: порог по ``mtime`` с большим запасом.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("KrabEar.TempUploadsSweep")

# Запрос с загрузкой живёт секунды-минуты; час — с большим запасом над самым долгим decode.
DEFAULT_MAX_AGE_SEC = 3600.0
# Не чаще раза в 10 минут при вызове «на каждый запрос».
DEFAULT_MIN_INTERVAL_SEC = 600.0


@dataclass(frozen=True)
class SweepResult:
    deleted: int = 0
    failed: int = 0
    skipped: int = 0


def sweep_stale_uploads(
    directory: Path | str,
    max_age_sec: float = DEFAULT_MAX_AGE_SEC,
    now: float | None = None,
) -> SweepResult:
    """Удалить обычные файлы старше ``max_age_sec`` секунд. Ошибки не пробрасываются."""
    root = Path(directory)
    current = time.time() if now is None else now
    deleted = failed = skipped = 0
    try:
        root_st = os.lstat(root)
        if not stat.S_ISDIR(root_st.st_mode):
            # Симлинк на каталог или не каталог: не обходим (fail-closed).
            logger.warning("temp_uploads sweep: %s не обычный каталог, пропуск", root)
            return SweepResult(0, 0, 1)
        with os.scandir(root) as it:
            entries = list(it)
    except FileNotFoundError:
        return SweepResult()
    except OSError:
        logger.warning("temp_uploads sweep: каталог недоступен", exc_info=True)
        return SweepResult(0, 1, 0)

    for entry in entries:
        try:
            st = os.lstat(entry.path)
        except FileNotFoundError:
            # Исчез между scandir и lstat (штатный finally): цель достигнута.
            continue
        except OSError:
            failed += 1
            continue
        if not stat.S_ISREG(st.st_mode):
            skipped += 1
            continue
        if current - st.st_mtime < max_age_sec:
            skipped += 1
            continue
        try:
            os.unlink(entry.path)
            deleted += 1
        except FileNotFoundError:
            # Параллельно удалил штатный finally: цель достигнута.
            continue
        except OSError:
            logger.warning("temp_uploads sweep: не удалось удалить %s", entry.name, exc_info=True)
            failed += 1
    if deleted:
        logger.info("temp_uploads sweep: удалено %d осиротевших загрузок", deleted)
    return SweepResult(deleted, failed, skipped)


_throttle_lock = threading.Lock()
_last_run: dict[str, float] = {}


def maybe_sweep_stale_uploads(
    directory: Path | str,
    min_interval_sec: float = DEFAULT_MIN_INTERVAL_SEC,
    max_age_sec: float = DEFAULT_MAX_AGE_SEC,
    now: float | None = None,
) -> SweepResult | None:
    """Уборка не чаще ``min_interval_sec`` на каталог; ``None`` — пропущено по троттлингу."""
    key = str(directory)
    current = time.monotonic() if now is None else now
    with _throttle_lock:
        previous = _last_run.get(key)
        if previous is not None and current - previous < min_interval_sec:
            return None
        _last_run[key] = current
    return sweep_stale_uploads(directory, max_age_sec=max_age_sec)
