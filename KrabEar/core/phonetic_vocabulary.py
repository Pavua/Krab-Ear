"""Фонетический словарь пользователя (phonetic correction vocabulary).

Пользователь задаёт пары вариант→каноническое написание (many-to-one):
    ["пашел", "павэл"] → "Павел"
    ["дэмо", "демма"]  → "демо"

После STT, перед paste, каждый вариант в транскрипте заменяется на
каноническое написание. Включается настройкой phonetic_vocab_enabled
(по умолчанию False).

Особенности безопасности:
- re.escape() на всех вариантах — пользовательский текст НИКОГДА не
  компилируется как raw regex (ReDoS-safe).
- Ограничения: max 200 записей, вариант ≤ 200 символов, каноническое ≤ 200 символов.
- Замена ищет только целые слова (\\b границы), нечувствительна к регистру.
- Самый длинный совпадающий вариант приоритетен (longest-first — prevents
  partial shadowing).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, List, Optional

logger = logging.getLogger("KrabEar.Core.PhoneticVocabulary")

# Ограничения для защиты от злоупотреблений
_MAX_ENTRIES = 200
_MAX_VARIANT_LEN = 200
_MAX_CANONICAL_LEN = 200


class PhoneticVocabulary:
    """Заменяет фонетические варианты в тексте на канонические написания.

    Args:
        settings_get: callback (key, default) -> value для runtime toggle.
                      Если None — toggle читается из default (False).
        entries_provider: callable () -> list[dict] — возвращает текущий список
                          {"canonical": str, "variants": [str, ...]}.
                          Вызывается при каждом correct() для актуальности.
    """

    def __init__(
        self,
        settings_get: Optional[Callable[[str, Any], Any]] = None,
        entries_provider: Optional[Callable[[], List[dict]]] = None,
    ) -> None:
        self._settings_get: Callable[[str, Any], Any] = settings_get or (lambda k, d: d)
        self._entries_provider: Callable[[], List[dict]] = entries_provider or (lambda: [])

    # ── Runtime toggle ───────────────────────────────────────────────────────

    def _enabled(self) -> bool:
        return bool(self._settings_get("phonetic_vocab_enabled", False))

    # ── Core logic ───────────────────────────────────────────────────────────

    def correct(self, text: str) -> str:
        """Заменяет фонетические варианты в *text* на канонические написания.

        Возвращает изменённую строку или оригинал если:
        - feature выключена;
        - список записей пуст;
        - ни один вариант не найден в тексте.

        Алгоритм:
        1. Получаем записи от provider (актуальные на момент вызова).
        2. Раскладываем каждую запись в плоский список (variant, canonical),
           сортируем по убыванию длины варианта (longest-first) — длинный вариант
           "пашела" не съедается коротким "пашел".
        3. Строим список (pattern, replacement), где pattern — \\b-обёрнутый
           re.escape(variant). Граница слова \\b корректно работает для кириллицы
           в Python 3.7+ при использовании re.UNICODE (флаг по умолчанию).
        4. Однопроходно применяем замены в порядке убывания длины.
        """
        if not self._enabled():
            return text
        if not text:
            return text

        raw_entries = self._entries_provider()
        if not raw_entries:
            return text

        # Раскладываем в плоские пары (variant, canonical), longest-first
        pairs = _build_pairs(raw_entries)
        if not pairs:
            return text

        result = text
        for variant, canonical in pairs:
            # re.escape делает вариант ReDoS-безопасным.
            # \\b — граница слова: не заменяем "Павел" внутри "Павелов" итп.
            pattern = r"\b" + re.escape(variant) + r"\b"
            try:
                result = re.sub(pattern, canonical, result, flags=re.IGNORECASE)
            except re.error as exc:
                # Теоретически не должно случиться с re.escape, но защищаемся.
                logger.warning(
                    "PhoneticVocabulary: ошибка regex для варианта %r: %s", variant, exc
                )
        return result


# ── Helpers ──────────────────────────────────────────────────────────────────

def _build_pairs(raw: list) -> list:
    """Строит плоский список [(variant, canonical), ...] из raw записей.

    Отфильтровывает невалидные записи. Сортирует по убыванию длины варианта.
    """
    pairs: list = []
    for item in raw[:_MAX_ENTRIES]:
        if not isinstance(item, dict):
            continue
        canonical = item.get("canonical", "")
        variants = item.get("variants", [])
        if (
            not isinstance(canonical, str)
            or not canonical.strip()
            or len(canonical) > _MAX_CANONICAL_LEN
        ):
            continue
        if not isinstance(variants, list):
            continue
        canonical_clean = canonical.strip()
        for v in variants:
            if (
                isinstance(v, str)
                and v.strip()
                and len(v) <= _MAX_VARIANT_LEN
            ):
                pairs.append((v.strip(), canonical_clean))
    # Longest variant first to prevent partial shadowing
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs
