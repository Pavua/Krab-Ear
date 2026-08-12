"""Dirty-marker + сбор форензики после НЕкорректного завершения (R1 Task 6).

Постфактум форензика слепа: unified log ротируется, и без свежего снимка
``log show``/``launchctl print`` источник рестарта не восстановить (см.
спека §4.3). Решение — детерминированный dirty-marker вместо эвристик по
mtime:

- ``write_alive_marker()`` пишет ``<data_dir>/runtime_alive.marker`` на
  старте текущей жизни процесса;
- ``GracefulShutdownHandler._persist`` удаляет маркер СРАЗУ ПОСЛЕ успешной
  записи ``shutdown_info.json`` — то есть только при доказанном graceful
  завершении;
- маркер, доживший до следующего старта, детерминированно означает, что
  предыдущая жизнь процесса умерла БЕЗ graceful shutdown (SIGKILL/OOM/crash)
  — ``check_and_collect`` собирает в этом случае свежую форензику, пока
  unified log её ещё помнит.

Весь сбор best-effort и fail-open: ошибка любого отдельного шага (или всего
сбора целиком) НИКОГДА не поднимается наружу и не блокирует старт backend —
вызывается из фонового треда ``startup-recovery`` (см. ``service.py``).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.ShutdownForensics")

# Имя dirty-маркера — единственный источник правды об этом имени;
# GracefulShutdownHandler импортирует эту константу, чтобы не заводить
# второй литерал того же значения (класс sibling-drift из CLAUDE.md).
_MARKER = "runtime_alive.marker"

_SHUTDOWN_INFO_FILE = "shutdown_info.json"
_FORENSICS_DIR = "forensics"
_MAX_RETAINED_DIRS = 5
# Rate-limit самого СБОРА (мини-волна 2026-08-11): launchd (KeepAlive=true,
# ThrottleInterval=5) в crash-loop'е поднимает процесс до ~720 раз/час, и
# каждый подъём гонял полный сбор — два subprocess-вызова с таймаутами до
# 30с + новый каталог. Значение симметрично UNCLEAN_RESTART_REPORT_MIN_GAP_SEC
# (service.py, лимит ОТПРАВКИ события) — но это НЕЗАВИСИМЫЙ лимит: экономия
# на сборе не глушит отправку (вердикт "unclean_rate_limited" остаётся
# unclean для _report_unclean_restart). Источник правды о последнем сборе —
# mtime новейшего подкаталога forensics/, без отдельного state-файла.
_COLLECT_MIN_GAP_SEC = 900.0
_LOG_TAIL_LINES = 300
# Окно смерти не знаем точнее — берём фиксированный хвост перед моментом
# сбора; свежий хвост ценнее точного окна (спека §4.3 п.2).
_LOG_WINDOW = timedelta(minutes=10)
_LAUNCHD_UNIT = "ai.krab.ear.backend"


def write_alive_marker(data_dir: "str | os.PathLike[str]") -> None:
    """Пометить текущую жизнь процесса как живую.

    Fail-open: ошибка диска здесь НЕ должна ронять старт backend — один
    WARN, следующий старт просто не увидит UNCLEAN по этой конкретной жизни
    (недостающая форензика лучше упавшего старта).
    """
    try:
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        marker_path = data_dir / _MARKER
        marker_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "started_at_iso": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        logger.warning(
            "shutdown_forensics: write_alive_marker провалился — UNCLEAN-детекция "
            "для этой жизни процесса недоступна", exc_info=True,
        )


def check_and_collect(
    data_dir: "str | os.PathLike[str]",
    log_dirs: "list[Path] | None" = None,
    timeout_sec: float = 30.0,
) -> str:
    """Проверить прошлую жизнь процесса и собрать форензику при UNCLEAN.

    :param data_dir: каталог состояния backend (тот же, что у StateStore).
    :param log_dirs: пути к собственным лог-файлам backend (stdout/stderr
        launchd-юнита); отсутствующие пути молча пропускаются.
    :param timeout_sec: таймаут КАЖДОГО subprocess-вызова (``log show``
        бывает медленным на большом unified log).
    :returns: ``"first_run"`` — ни маркера, ни ``shutdown_info.json`` (первый
        старт в этом data_dir); ``"clean"`` — маркера нет, прошлая жизнь
        завершилась через ``_persist``; ``"unclean_collected"`` — маркер был,
        форензика собрана (best-effort, отдельные шаги могли не удаться);
        ``"unclean_rate_limited"`` — маркер был (смерть UNCLEAN — сигнал НЕ
        теряется), но дорогой сбор пропущен: свежая форензика уже собрана
        менее ``_COLLECT_MIN_GAP_SEC`` назад (crash-loop-защита);
        ``"unclean_collect_failed"`` — сам сбор упал катастрофически.

    Никогда не бросает исключений; вызывается из фонового треда
    ``startup-recovery`` и не блокирует старт IPC.
    """
    try:
        data_dir = Path(data_dir)
        marker_path = data_dir / _MARKER
        info_path = data_dir / _SHUTDOWN_INFO_FILE

        if not marker_path.exists():
            return "clean" if info_path.exists() else "first_run"

        # Crash-loop-защита: недавний уже собранный каталог означает, что
        # unified log/launchctl-состояние этой серии смертей уже снято —
        # повторный сбор каждые ~5с не добавляет информации, но жжёт CPU
        # (subprocess-таймауты) и диск. Fail-open: ЛЮБАЯ ошибка чтения
        # mtime → собираем (недостающая экономия лучше недостающей
        # форензики).
        try:
            last_collect_ts = _latest_forensics_mtime(data_dir / _FORENSICS_DIR)
        except Exception:
            logger.warning(
                "shutdown_forensics: не удалось прочитать mtime последнего "
                "сбора — rate-limit пропущен, собираем", exc_info=True,
            )
            last_collect_ts = None
        if last_collect_ts is not None:
            age_sec = datetime.now(timezone.utc).timestamp() - last_collect_ts
            # Отрицательный age (mtime из будущего — скачок NTP) трактуем
            # как «недавно»: та же защита от вечного подавления УЖЕ есть у
            # лимита отправки (service.py) — здесь пропуск сбора на одну
            # серию безопасен, отправка события живёт своим лимитом.
            if age_sec < _COLLECT_MIN_GAP_SEC:
                try:
                    marker_path.unlink(missing_ok=True)
                except Exception:
                    logger.warning(
                        "shutdown_forensics: не удалось удалить маркер в "
                        "rate-limited ветке", exc_info=True,
                    )
                logger.warning(
                    "shutdown_forensics: UNCLEAN-смерть, но форензика уже "
                    "собиралась %.0fс назад (< %.0fс) — сбор пропущен "
                    "(crash-loop-защита)", age_sec, _COLLECT_MIN_GAP_SEC,
                )
                return "unclean_rate_limited"

        return _collect_forensics(
            data_dir=data_dir,
            marker_path=marker_path,
            info_path=info_path,
            log_dirs=list(log_dirs or []),
            timeout_sec=timeout_sec,
        )
    except Exception:
        logger.exception("shutdown_forensics: check_and_collect провалился целиком")
        return "unclean_collect_failed"


def _collect_forensics(
    *,
    data_dir: Path,
    marker_path: Path,
    info_path: Path,
    log_dirs: "list[Path]",
    timeout_sec: float,
) -> str:
    """Собрать форензику под UNCLEAN-веткой. Катастрофа здесь = сбор упал целиком.

    Отдельные шаги (log show / launchctl print / хвосты логов / копии
    контекста) ловят СВОИ ошибки внутри себя и никогда не поднимают их
    сюда — они best-effort по отдельности. Эта функция ловит только то, что
    вышло за пределы отдельных шагов (например саму директорию форензики не
    удалось создать) — тогда сбор признаётся провалившимся целиком.
    """
    try:
        stale_marker: dict[str, Any] = {}
        try:
            stale_marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning(
                "shutdown_forensics: маркер повреждён — сбор продолжается без "
                "его контекста", exc_info=True,
            )

        ts_dir_name = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        out_dir = data_dir / _FORENSICS_DIR / ts_dir_name
        out_dir.mkdir(parents=True, exist_ok=True)

        _collect_log_show(out_dir, timeout_sec)
        _collect_launchctl_print(out_dir, timeout_sec)
        _collect_own_logs_tail(out_dir, log_dirs)
        _collect_context_copies(out_dir, info_path, stale_marker)

        try:
            marker_path.unlink(missing_ok=True)
        except Exception:
            logger.warning(
                "shutdown_forensics: не удалось удалить маркер после сбора "
                "форензики", exc_info=True,
            )

        _enforce_retention(data_dir / _FORENSICS_DIR)

        logger.warning(
            "shutdown_forensics: предыдущая жизнь процесса завершилась "
            "НЕкорректно (SIGKILL/crash/OOM) — форензика собрана в %s",
            out_dir,
        )
        return "unclean_collected"
    except Exception:
        logger.exception("shutdown_forensics: сбор форензики провалился целиком")
        return "unclean_collect_failed"


def _latest_forensics_mtime(forensics_root: Path) -> "float | None":
    """Epoch-mtime самого свежего подкаталога ``forensics/``; None — сборов
    ещё не было (каталога нет / он пуст). Единственный потребитель —
    crash-loop rate-limit в ``check_and_collect``; отдельного state-файла
    о «последнем сборе» намеренно нет — mtime каталога и есть факт сбора."""
    if not forensics_root.is_dir():
        return None
    mtimes = [
        entry.stat().st_mtime
        for entry in forensics_root.iterdir()
        if entry.is_dir()
    ]
    return max(mtimes) if mtimes else None


def _log_show_start_arg() -> str:
    start = datetime.now() - _LOG_WINDOW
    return start.strftime("%Y-%m-%d %H:%M:%S")


def _write_best_effort(path: Path, content: str) -> None:
    try:
        path.write_text(content, encoding="utf-8")
    except Exception:
        logger.debug("shutdown_forensics: не удалось записать %s", path, exc_info=True)


def _collect_log_show(out_dir: Path, timeout_sec: float) -> None:
    """Свежий хвост unified log за окно перед сбором (launchd/jetsam-сообщения).

    macOS-only. На Linux/CI (или при отсутствующей команде) — файл с текстом
    ошибки вместо содержимого; статус всей сборки от этого не меняется
    (спека §4.3 п.3).
    """
    out_path = out_dir / "log_show.txt"
    try:
        result = subprocess.run(
            [
                "log", "show",
                "--start", _log_show_start_arg(),
                "--predicate", 'eventMessage CONTAINS[c] "krab"',
                "--style", "compact",
            ],
            capture_output=True, text=True, timeout=timeout_sec,
        )
        _write_best_effort(
            out_path,
            (result.stdout or "") + "\n--- stderr ---\n" + (result.stderr or ""),
        )
    except Exception as exc:
        _write_best_effort(out_path, f"log show провалился: {exc!r}")


def _collect_launchctl_print(out_dir: Path, timeout_sec: float) -> None:
    """Снимок launchd-юнита на момент сбора (crash/spawn-failed диагностика)."""
    out_path = out_dir / "launchctl_print.txt"
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{_LAUNCHD_UNIT}"],
            capture_output=True, text=True, timeout=timeout_sec,
        )
        _write_best_effort(
            out_path,
            (result.stdout or "") + "\n--- stderr ---\n" + (result.stderr or ""),
        )
    except Exception as exc:
        _write_best_effort(out_path, f"launchctl print провалился: {exc!r}")


def _collect_own_logs_tail(out_dir: Path, log_dirs: "list[Path]") -> None:
    """Хвост собственных stdout/stderr логов backend (последние N строк каждого)."""
    out_path = out_dir / "own_logs_tail.txt"
    chunks: list[str] = []
    for log_path in log_dirs:
        log_path = Path(log_path)
        try:
            if not log_path.exists():
                continue
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = lines[-_LOG_TAIL_LINES:]
            chunks.append(f"=== {log_path} (последние {len(tail)} строк) ===")
            chunks.extend(tail)
        except Exception:
            chunks.append(f"=== {log_path}: ошибка чтения ===")
            logger.debug("shutdown_forensics: ошибка чтения лога %s", log_path, exc_info=True)
    _write_best_effort(out_path, "\n".join(chunks))


def _collect_context_copies(out_dir: Path, info_path: Path, stale_marker: dict[str, Any]) -> None:
    """Копии контекста прошлой жизни — сам маркер и последний shutdown_info.json."""
    try:
        (out_dir / "stale_marker.json").write_text(
            json.dumps(stale_marker, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except Exception:
        logger.debug("shutdown_forensics: не удалось сохранить stale_marker.json", exc_info=True)
    try:
        if info_path.exists():
            shutil.copyfile(info_path, out_dir / "prev_shutdown_info.json")
    except Exception:
        logger.debug(
            "shutdown_forensics: не удалось скопировать prev_shutdown_info.json", exc_info=True,
        )


def _enforce_retention(forensics_root: Path) -> None:
    """Оставить только `_MAX_RETAINED_DIRS` новейших каталогов форензики."""
    try:
        if not forensics_root.is_dir():
            return
        dirs = sorted(
            (d for d in forensics_root.iterdir() if d.is_dir()),
            key=lambda d: d.name,
        )
        excess = len(dirs) - _MAX_RETAINED_DIRS
        for stale_dir in dirs[: max(excess, 0)]:
            shutil.rmtree(stale_dir, ignore_errors=True)
    except Exception:
        logger.debug("shutdown_forensics: retention enforcement провалился", exc_info=True)
