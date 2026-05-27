"""CalendarLinker — автоматическое связывание транскрипций с событиями Calendar.app.

Использует osascript (AppleScript) для опроса Calendar.app без EventKit.
Не требует дополнительных зависимостей; работает только на macOS.

Дизайн:
- Opt-in: включается через настройку CALENDAR_LINK_ENABLED=True.
- TTL-кэш: osascript не вызывается чаще одного раза за CALENDAR_LINK_CACHE_MIN минут.
- Graceful degradation: при ошибках/отказе в доступе возвращается None.
"""

from __future__ import annotations

import logging
import platform
import subprocess
import time
from datetime import datetime
from typing import Any

logger = logging.getLogger("KrabEar.Backend.CalendarLink")

# AppleScript `as integer` on a date returns seconds since 2001-01-01 00:00:00 UTC
# (the macOS/Cocoa reference epoch), NOT Unix epoch (1970-01-01).
# Offset = datetime(2001,1,1) - datetime(1970,1,1) = 978 307 200 seconds.
_MAC_EPOCH_OFFSET = 978_307_200

_OSASCRIPT_TEMPLATE = """
tell application "Calendar"
    set nowDate to current date
    set resultLines to {}
    repeat with cal in calendars
        try
            repeat with ev in (events of cal whose start date <= nowDate and end date >= nowDate)
                set evTitle to summary of ev
                set evStart to ((start date of ev) as integer)
                set evEnd to ((end date of ev) as integer)
                try
                    set evLoc to location of ev
                    if evLoc is missing value then set evLoc to ""
                on error
                    set evLoc to ""
                end try
                set calName to name of cal
                set resultLines to resultLines & {evTitle & "|||" & evStart & "|||" & evEnd & "|||" & evLoc & "|||" & calName}
            end repeat
        end try
    end repeat
    return resultLines
end tell
"""


def _epoch_to_iso(epoch_sec: int) -> str:
    """Конвертирует Mac timestamp (секунды с 2001-01-01) в ISO 8601 строку.

    AppleScript ``as integer`` на дате возвращает секунды с 2001-01-01 (Mac epoch),
    а не с 1970-01-01 (Unix epoch). Добавляем ``_MAC_EPOCH_OFFSET`` для коррекции.
    """
    try:
        unix_sec = epoch_sec + _MAC_EPOCH_OFFSET
        return datetime.fromtimestamp(unix_sec).isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return ""


def _parse_osascript_output(raw: str) -> list[dict[str, Any]]:
    """Разбирает вывод osascript в список словарей событий."""
    results: list[dict[str, Any]] = []
    if not raw:
        return results
    for line in raw.strip().splitlines():
        line = line.strip().rstrip(",")
        if "|||" not in line:
            continue
        parts = line.split("|||", maxsplit=4)
        if len(parts) < 5:
            continue
        title = parts[0].strip()
        if not title:
            continue
        try:
            start_epoch = int(parts[1].strip())
            end_epoch = int(parts[2].strip())
        except (ValueError, TypeError):
            continue
        location = parts[3].strip()
        calendar_name = parts[4].strip()
        results.append({
            "title": title,
            "start_iso": _epoch_to_iso(start_epoch),
            "end_iso": _epoch_to_iso(end_epoch),
            "location": location,
            "calendar_name": calendar_name,
            "_start_epoch": start_epoch,
        })
    return results


_TCC_DENIAL_MARKERS = (
    "not authorized",
    "not allowed",
    "(-1743)",               # AppleEvent send denied (macOS TCC)
    "errAEEventNotPermitted",
    "isn't running",         # app not yet TCC-prompted / automation blocked
    "doesn't have permission",
)


def _is_tcc_denial(stderr: str) -> bool:
    """Возвращает True, если stderr содержит признак отказа TCC Calendar."""
    s = stderr.lower()
    return any(marker.lower() in s for marker in _TCC_DENIAL_MARKERS)


class CalendarLinker:
    """Связывает транскрипции с активными событиями Calendar.app через osascript."""

    def __init__(self, cache_minutes: int = 5) -> None:
        self._cache_minutes = max(1, int(cache_minutes))
        self._cached_result: dict[str, Any] | None = None
        self._cache_at_time: float = 0.0
        self._cache_window_key: str = ""

    def _window_key(self, at_time: datetime) -> str:
        minute_bucket = (at_time.minute // self._cache_minutes) * self._cache_minutes
        return at_time.strftime(f"%Y-%m-%dT%H:{minute_bucket:02d}")

    def find_active_event(self, at_time: datetime | None = None) -> dict[str, Any] | None:
        """Возвращает наиболее релевантное активное событие Calendar.app или None."""
        if platform.system() != "Darwin":
            logger.debug("CalendarLinker: не macOS, пропускаем")
            return None
        if at_time is None:
            at_time = datetime.now()
        window = self._window_key(at_time)
        now_mono = time.monotonic()
        cache_ttl_sec = self._cache_minutes * 60
        if (
            self._cache_window_key == window
            and (now_mono - self._cache_at_time) < cache_ttl_sec
        ):
            logger.debug("CalendarLinker: кэш hit (window=%s)", window)
            return self._cached_result
        result = self._query_calendar()
        self._cached_result = result
        self._cache_at_time = now_mono
        self._cache_window_key = window
        return result

    def _query_calendar(self) -> dict[str, Any] | None:
        """Выполняет osascript и возвращает наиболее релевантное событие или None."""
        try:
            proc = subprocess.run(
                ["osascript", "-e", _OSASCRIPT_TEMPLATE],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            logger.warning("CalendarLinker: osascript timeout (10s)")
            return None
        except FileNotFoundError:
            logger.debug("CalendarLinker: osascript не найден")
            return None
        except Exception as exc:
            logger.warning("CalendarLinker: ошибка запуска osascript: %s", exc)
            return None
        stderr = proc.stderr or ""
        if _is_tcc_denial(stderr):
            logger.info("CalendarLinker: нет разрешения TCC Calendar")
            return None
        if proc.returncode != 0 and not proc.stdout.strip():
            logger.warning("CalendarLinker: osascript rc=%d", proc.returncode)
            return None
        raw = proc.stdout.strip()
        if not raw:
            logger.debug("CalendarLinker: нет активных событий")
            return None
        events = _parse_osascript_output(raw)
        if not events:
            return None
        best = min(events, key=lambda e: e.get("_start_epoch", 0), default=None)
        if best is None:
            return None
        best.pop("_start_epoch", None)
        logger.info("CalendarLinker: событие «%s»", best.get("title"))
        return best
