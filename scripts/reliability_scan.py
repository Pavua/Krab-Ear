#!/usr/bin/env python3
"""R1: ежедневный сканер надёжности Krab Ear (stdlib only).

ЗАЧЕМ
-----
Пульт `:8777` показывает прод-процессы, но не «как часто падает диктовка».
Этот скрипт раз в сутки сводит 7 сигналов надёжности из уже существующих
источников (логи, audit-NDJSON, rescue/, forensics/) в JSON-снимок, который
читает `collect_reliability()` пульта. Ретеншен источников короче 7 дней,
поэтому недельное окно собирается из копилки суточных снимков.

🔴 Принцип тот же, что у пульта: отсутствие данных — это `unknown`, а НЕ `ok`.
   Снимок старше 26 ч пульт тоже честно показывает как `unknown`.

PII: в снимки попадают только счётчики и латентности. Содержимое
forensics-трейлов (`own_logs_tail.txt`) и текстов логов НЕ читается в отчёт.

Запуск: `python3 scripts/reliability_scan.py --once` (дефолт) —
пишет `<store>/YYYY-MM-DD.json`, `latest.json`, `summary_7d.json`.
`--json` — только печатает снимок, ничего не пишет (read-only).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- пути по умолчанию -----------------------------------------------------
DATA_DIR_DEFAULT = Path.home() / "Library" / "Application Support" / "KrabEar"
# ops-состояние, не data_dir с пользовательскими данными.
STORE_DIR_DEFAULT = Path.home() / ".local" / "share" / "krab-ear" / "reliability"
REPO_ROOT = Path(__file__).resolve().parents[1]
REST_LOG_DEFAULT = REPO_ROOT / "logs" / "krab-ear-rest.err.log"

WINDOW_HOURS = 24
SUMMARY_DAYS = 7

# --- паттерны (сверено с источниками, 18.09) -------------------------------
_PAT_STT = "Критическая ошибка распознавания"          # core/engine.py
_PAT_GIGAAM = "gigaam-mlx потерял"                     # pipeline/stt_gigaam_mlx.py
_PAT_HANG = "handle_request завис дольше"              # backend/ipc_server.py
_PAT_BRIDGE = "неверный bridge-токен"                  # backend/rest_server.py

# asctime обоих логов — локальное время с МИЛЛИСЕКУНДАМИ: `… 02:05:43,820`.
_LOG_TS_FMT = "%Y-%m-%d %H:%M:%S,%f"
_LOG_TS_LEN = 23

# --- пороги статусов -------------------------------------------------------
STT_WARN, STT_FAIL = 1, 3
GIGAAM_WARN, GIGAAM_FAIL = 1, 3
HANG_WARN, HANG_FAIL = 1, 5
BRIDGE_WARN, BRIDGE_FAIL = 1, 5
RESCUE_WARN, RESCUE_FAIL = 1, 3
UNCLEAN_WARN, UNCLEAN_FAIL = 1, 3
PING_WARN_MS, PING_FAIL_MS = 800.0, 2000.0

FORENSICS_CAVEAT = (
    "retention forensics = 5 каталогов + rate-limit сбора 900с: 7-дневный "
    "счёт обрезается, реальных нечистых смертей могло быть больше"
)


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------
def _existing(paths) -> list:
    return [Path(p) for p in paths if Path(p).is_file()]


def _iter_lines(paths, unreadable: list | None = None):
    for path in _existing(paths):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    yield line
        except OSError:
            # 🔴 fail-closed для монитора: нечитаемый источник — это unknown,
            # НЕ «ноль событий». Иначе «всегда зелёный» при сломанном доступе.
            if unreadable is not None:
                unreadable.append(str(path))
            continue


def _parse_log_ts(line: str) -> datetime | None:
    """Штамп строки лога → aware datetime.

    TZ-ловушка: asctime пишется в ЛОКАЛЬНОЙ зоне машины, поэтому
    `.astimezone()` (без аргумента) привязывает локальную зону — не UTC.
    """
    try:
        naive = datetime.strptime(line[:_LOG_TS_LEN], _LOG_TS_FMT)
    except ValueError:
        return None
    return naive.astimezone()


def _mtime_utc(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _in_window(moment: datetime | None, since: datetime, until: datetime) -> bool:
    return moment is not None and since <= moment <= until


def _count_pattern(logs, pattern: str, since: datetime, until: datetime,
                   unreadable: list | None = None) -> int:
    total = 0
    for line in _iter_lines(logs, unreadable):
        if pattern not in line:
            continue
        if _in_window(_parse_log_ts(line), since, until):
            total += 1
    return total


def _count_status(count: int, warn_at: int, fail_at: int) -> str:
    if count >= fail_at:
        return "fail"
    if count >= warn_at:
        return "warn"
    return "ok"


def _nearest_rank_index(percentile: float, n: int) -> int:
    idx = int(math.ceil(percentile * n)) - 1
    return max(0, min(n - 1, idx))


# ---------------------------------------------------------------------------
# Сканеры сигналов. Контракт: {"records"|"samples": int, "status": str, ...}
# ---------------------------------------------------------------------------
def _scan_log_signal(logs, pattern: str, since: datetime, until: datetime,
                     warn_at: int, fail_at: int) -> dict:
    if not _existing(logs):
        return {"records": 0, "status": "unknown", "reason": "no-source"}
    unreadable: list = []
    records = _count_pattern(logs, pattern, since, until, unreadable)
    if unreadable:
        return {"records": records, "status": "unknown", "reason": "unreadable"}
    return {"records": records, "status": _count_status(records, warn_at, fail_at)}


def scan_stt_critical(logs, since: datetime, until: datetime) -> dict:
    """Критические провалы распознавания. Запись = 1 на диктовку."""
    return _scan_log_signal(logs, _PAT_STT, since, until, STT_WARN, STT_FAIL)


def scan_gigaam_chunk_loss(logs, since: datetime, until: datetime) -> dict:
    """Record-level потери чанков GigaAM (не построчный `вернулся пустым`)."""
    return _scan_log_signal(logs, _PAT_GIGAAM, since, until, GIGAAM_WARN, GIGAAM_FAIL)


def scan_handle_request_hangs(logs, since: datetime, until: datetime) -> dict:
    """IPC-запросы, зависшие дольше backstop-таймаута."""
    return _scan_log_signal(logs, _PAT_HANG, since, until, HANG_WARN, HANG_FAIL)


def scan_bridge_401(logs, since: datetime, until: datetime) -> dict:
    """Неверный bridge-токен (asctime rest-лога — локальное время)."""
    return _scan_log_signal(logs, _PAT_BRIDGE, since, until, BRIDGE_WARN, BRIDGE_FAIL)


def scan_rescue_files(rescue_dir, since: datetime, until: datetime) -> dict:
    """Восстановленные записи по mtime `*.meta.json` (только успешные rescue)."""
    rescue_dir = Path(rescue_dir)
    if not rescue_dir.is_dir():
        return {"records": 0, "status": "unknown", "reason": "no-source"}
    records = 0
    for path in rescue_dir.glob("*.meta.json"):
        if _in_window(_mtime_utc(path), since, until):
            records += 1
    return {"records": records, "status": _count_status(records, RESCUE_WARN, RESCUE_FAIL)}


def scan_unclean_deaths(forensics_dir, since: datetime, until: datetime) -> dict:
    """Нечистые смерти: число каталогов `forensics/*/` по mtime.

    🔴 Ретеншен источника — 5 каталогов, сбор rate-limited (900с), поэтому
    счёт за 7 дней обрезается; это честный caveat снимка, а не ошибка.
    """
    forensics_dir = Path(forensics_dir)
    if not forensics_dir.is_dir():
        return {"records": 0, "status": "unknown", "reason": "no-source",
                "caveat": FORENSICS_CAVEAT}
    records = 0
    for child in forensics_dir.iterdir():
        if child.is_dir() and _in_window(_mtime_utc(child), since, until):
            records += 1
    return {
        "records": records,
        "status": _count_status(records, UNCLEAN_WARN, UNCLEAN_FAIL),
        "caveat": FORENSICS_CAVEAT,
    }


def scan_ping_latency(audit_files, since: datetime, until: datetime) -> dict:
    """p50/p99 `duration_ms` успешных `method=="ping"` из audit-NDJSON.

    Ограничения (в снимок): это время handle_request, НЕ RTT; только
    успешные вызовы. `ts` — ISO-строка с offset (audit_logger.py), не epoch.
    """
    if not _existing(audit_files):
        return {"samples": 0, "status": "unknown", "reason": "no-source"}
    durations = []
    unreadable = []
    for path in _existing(audit_files):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or '"ping"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if entry.get("method") != "ping" or not entry.get("success"):
                        continue
                    try:
                        ts = datetime.fromisoformat(entry.get("ts"))
                    except (TypeError, ValueError):
                        continue
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if not _in_window(ts, since, until):
                        continue
                    value = entry.get("duration_ms")
                    if isinstance(value, (int, float)):
                        durations.append(float(value))
        except OSError:
            unreadable.append(str(path))
            continue
    if unreadable:
        # 🔴 fail-closed: нечитаемый audit-файл — unknown, не «нет данных = ок».
        return {"samples": len(durations), "status": "unknown", "reason": "unreadable",
                "note": "время handle_request, не RTT; только успешные вызовы"}
    if not durations:
        return {"samples": 0, "status": "unknown", "reason": "no-samples",
                "note": "время handle_request, не RTT; только успешные вызовы"}
    durations.sort()
    p50 = durations[_nearest_rank_index(0.50, len(durations))]
    p99 = durations[_nearest_rank_index(0.99, len(durations))]
    status = "fail" if p99 > PING_FAIL_MS else ("warn" if p99 > PING_WARN_MS else "ok")
    return {
        "samples": len(durations),
        "p50": round(p50, 1),
        "p99": round(p99, 1),
        "status": status,
        "note": "время handle_request, не RTT; только успешные вызовы",
    }


def collect_signals(data_dir, rest_log, since: datetime, until: datetime) -> dict:
    data_dir = Path(data_dir)
    logs = [
        data_dir / "backend.log",
        data_dir / "backend.log.1",
        data_dir / "backend.log.2",
        data_dir / "backend.log.3",
    ]
    audits = sorted(data_dir.glob("audit_*.ndjson"))
    return {
        "stt_critical": scan_stt_critical(logs, since, until),
        "gigaam_chunk_loss": scan_gigaam_chunk_loss(logs, since, until),
        "handle_request_hangs": scan_handle_request_hangs(logs, since, until),
        "bridge_401": scan_bridge_401([rest_log], since, until),
        "rescue_files": scan_rescue_files(data_dir / "rescue", since, until),
        "unclean_deaths": scan_unclean_deaths(data_dir / "forensics", since, until),
        "ping_latency": scan_ping_latency(audits, since, until),
    }


def build_snapshot(signals: dict, now: datetime | None = None,
                   window_hours: int = WINDOW_HOURS) -> dict:
    """Общий статус — худший из сигналов; ≥1 unknown не даёт `ok` (даёт warn)."""
    now = now or datetime.now(timezone.utc)
    unknown_sources = sorted(k for k, v in signals.items() if v.get("status") == "unknown")
    statuses = [v.get("status") for v in signals.values()]
    if "fail" in statuses:
        overall = "fail"
    elif "warn" in statuses:
        overall = "warn"
    elif "unknown" in statuses:
        overall = "warn"  # не ok: отсутствие данных — не успех
    else:
        overall = "ok"
    return {
        # ISO с offset, БЕЗ суффикса Z: пульт на python 3.9 не понимает `Z`.
        "generated_ts": now.astimezone().isoformat(),
        "window_hours": window_hours,
        "signals": signals,
        "unknown_sources": unknown_sources,
        "status": overall,
    }


# ---------------------------------------------------------------------------
# Хранилище снимков
# ---------------------------------------------------------------------------
def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def build_summary_7d(store_dir, now: datetime | None = None) -> dict:
    """Агрегат по файлам снимков за 7 суток (пересчёт с нуля, без состояния)."""
    now = now or datetime.now(timezone.utc)
    store_dir = Path(store_dir)
    cutoff = (now.astimezone() - timedelta(days=SUMMARY_DAYS)).date()
    days: list = []
    records: dict = {}
    p50s: list = []
    p99s: list = []
    statuses: list = []
    for path in sorted(store_dir.glob("*.json")):
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.json", path.name)
        if not match:
            continue
        try:
            day = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            continue
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        days.append(match.group(1))
        signals = snapshot.get("signals") or {}
        for name, value in signals.items():
            count = value.get("records")
            if isinstance(count, int):
                records[name] = records.get(name, 0) + count
        ping = signals.get("ping_latency") or {}
        if isinstance(ping.get("p50"), (int, float)):
            p50s.append(float(ping["p50"]))
        if isinstance(ping.get("p99"), (int, float)):
            p99s.append(float(ping["p99"]))
        if snapshot.get("status"):
            statuses.append(snapshot["status"])
    return {
        "generated_ts": now.astimezone().isoformat(),
        "window_days": SUMMARY_DAYS,
        "days": days,
        "days_count": len(days),
        "records": records,
        "ping_p50_median": round(statistics.median(p50s), 1) if p50s else None,
        "ping_p99_median": round(statistics.median(p99s), 1) if p99s else None,
        "statuses": statuses,
    }


def run_once(data_dir, rest_log, store_dir, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=WINDOW_HOURS)
    signals = collect_signals(data_dir, rest_log, since, now)
    snapshot = build_snapshot(signals, now=now)
    store_dir = Path(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    date_str = now.astimezone().strftime("%Y-%m-%d")
    _atomic_write_json(store_dir / f"{date_str}.json", snapshot)
    _atomic_write_json(store_dir / "latest.json", snapshot)
    _atomic_write_json(store_dir / "summary_7d.json", build_summary_7d(store_dir, now=now))
    return snapshot


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true",
                        help="снять снимок один раз и записать (дефолт)")
    parser.add_argument("--json", action="store_true",
                        help="напечатать снимок, ничего не записывать")
    parser.add_argument("--data-dir", default=str(DATA_DIR_DEFAULT))
    parser.add_argument("--rest-log", default=str(REST_LOG_DEFAULT))
    parser.add_argument("--out-dir", default=str(STORE_DIR_DEFAULT))
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=WINDOW_HOURS)
    if args.json:
        signals = collect_signals(args.data_dir, args.rest_log, since, now)
        snapshot = build_snapshot(signals, now=now)
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return 0

    snapshot = run_once(args.data_dir, args.rest_log, args.out_dir, now=now)
    unknown = ", ".join(snapshot["unknown_sources"]) or "нет"
    print(f"Снимок {snapshot['generated_ts']}: {snapshot['status']} "
          f"(unknown: {unknown})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
