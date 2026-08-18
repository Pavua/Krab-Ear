"""Статус приёма событий Sentry: отличает тишину от слепоты.

Живой инцидент 2026-08-13..18 (волна W9): организация выбрала бесплатную квоту
5000 ошибок/месяц, и всё последующее Sentry отбрасывал на приёме
(`rate_limited`). Смок-рутина при этом писала «Sentry quiet (0/0)» и трактовала
отсутствие событий как ЗДОРОВЬЕ — то есть монитор рапортовал ОК ровно там, где
ослеп. Четыре unclean-смерти backend 17-08 и инцидент 12:21 18-08 владелец
не увидел.

Ключевое различение, ради которого написан модуль:
  * IDLE    — событий не было И отказов не было: система правда не падала;
  * BLIND   — приёма нет, а отказы есть: нас не принимают, мы слепы;
  * UNKNOWN — проверить не удалось. НИКОГДА не приравнивать к OK: «не смог
              посмотреть» и «посмотрел, всё чисто» — разные вещи, и подмена
              одного другим и есть механизм тихой слепоты.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Optional, Tuple

logger = logging.getLogger("KrabEar.Backend.SentryQuota")

QUOTA_OK = "ok"
QUOTA_BLIND = "blind"
QUOTA_IDLE = "idle"
QUOTA_UNKNOWN = "unknown"

# Приём формально идёт, но режется подавляющее большинство — практически та же
# слепота: в Sentry долетают единицы из сотен.
_DEGRADED_RATIO = 10


def classify_quota(accepted: Optional[int], rate_limited: Optional[int]) -> str:
    """Классифицировать состояние приёма по счётчикам за окно наблюдения."""
    if accepted is None or rate_limited is None:
        return QUOTA_UNKNOWN
    if accepted <= 0:
        return QUOTA_BLIND if rate_limited > 0 else QUOTA_IDLE
    if rate_limited > accepted * _DEGRADED_RATIO:
        return QUOTA_BLIND
    return QUOTA_OK


def format_quota_line(
    status: str, accepted: Optional[int], rate_limited: Optional[int]
) -> str:
    """Строка для `.remember/smoke-history.log` — состояние словами, не «quiet»."""
    if status == QUOTA_BLIND:
        return (
            f"Sentry СЛЕП: accepted={accepted or 0}/24ч, "
            f"rate_limited={rate_limited or 0} — события отбрасываются на приёме "
            f"(исчерпана квота плана)"
        )
    if status == QUOTA_OK:
        return f"Sentry принимает: {accepted} событий/24ч (отказов {rate_limited or 0})"
    if status == QUOTA_IDLE:
        return "Sentry тихо: за 24ч ни событий, ни отказов — падений не было"
    return "Sentry статус неизвестен — проверить квоту не удалось (это НЕ признак здоровья)"


def fetch_quota_counts(
    token: str, org: str = "po-zm", hours: int = 24, timeout: float = 15.0
) -> Tuple[Optional[int], Optional[int]]:
    """Счётчики (accepted, rate_limited) за окно. При любой беде — (None, None).

    Направление отказа выбрано осознанно: недоступный API даёт UNKNOWN, а не
    оптимистичный ноль — иначе сетевой сбой снова читался бы как «всё тихо».
    """
    stats_period = "24h" if hours <= 24 else "14d"
    url = (
        f"https://de.sentry.io/api/0/organizations/{org}/stats_v2/"
        f"?field=sum(quantity)&groupBy=outcome&statsPeriod={stats_period}&category=error"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        logger.warning("fetch_quota_counts: запрос к Sentry не удался", exc_info=True)
        return None, None

    groups = payload.get("groups")
    if not isinstance(groups, list):
        return None, None

    accepted = rate_limited = 0
    for group in groups:
        outcome = (group.get("by") or {}).get("outcome")
        try:
            quantity = int((group.get("totals") or {}).get("sum(quantity)", 0))
        except (TypeError, ValueError):
            continue
        if outcome == "accepted":
            accepted += quantity
        elif outcome == "rate_limited":
            rate_limited += quantity
    return accepted, rate_limited
