"""Строгий разбор булевых значений из настроек и сохранённых записей.

``bool("false")`` равно True: строка из settings.json или повреждённой записи
истории читалась как включённый флаг (KE05C-26, KE05D-20). ``parse_bool`` понимает
настоящие bool, 0/1 и однозначные строки, а всё остальное возвращает как ``default`` —
вызывающий сам выбирает безопасную сторону (fail-closed для защитных флагов).
"""

from __future__ import annotations

import math
from typing import Any

_TRUE = frozenset({"true", "1", "yes", "on", "y", "t"})
_FALSE = frozenset({"false", "0", "no", "off", "n", "f", ""})


def parse_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return default
        if value == 1:
            return True
        if value == 0:
            return False
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    return default
