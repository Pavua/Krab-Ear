"""Расширитель текстовых сниппетов (voice-triggered text expansions).

Пользователь задаёт пары trigger→expansion:
    "вставь подпись" → "С уважением,\nПавел"
    "мой имейл"     → "pavelr7@gmail.com"

После STT, перед paste, триггерные фразы в транскрипте заменяются на expansions.
Включается настройкой text_snippets_enabled (по умолчанию False).

Особенности безопасности:
- re.escape() на всех триггерах — пользовательский текст НИКОГДА не компилируется
  как raw regex (ReDoS-safe).
- Ограничения: max 200 сниппетов, trigger ≤ 200 символов, expansion ≤ 2000 символов.
- Замена ищет только целые слова/фразы (\\b границы), нечувствительна к регистру.
- Самый длинный совпадающий триггер приоритетен (longest-first — prevents partial shadowing).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, List, Optional

logger = logging.getLogger("KrabEar.Core.TextSnippetExpander")

# Ограничения для защиты от злоупотреблений
_MAX_SNIPPETS = 200
_MAX_TRIGGER_LEN = 200
_MAX_EXPANSION_LEN = 2000


class TextSnippetExpander:
    """Заменяет триггерные фразы в тексте на пользовательские расширения.

    Args:
        settings_get: callback (key, default) -> value для runtime toggle.
                      Если None — toggle читается из default (True/False).
        snippets_provider: callable () -> list[dict] — возвращает текущий список
                           {"trigger": str, "expansion": str}.
                           Вызывается при каждом expand() для актуальности.
    """

    def __init__(
        self,
        settings_get: Optional[Callable[[str, Any], Any]] = None,
        snippets_provider: Optional[Callable[[], List[dict]]] = None,
    ) -> None:
        self._settings_get: Callable[[str, Any], Any] = settings_get or (lambda k, d: d)
        self._snippets_provider: Callable[[], List[dict]] = snippets_provider or (lambda: [])

    # ── Runtime toggle ───────────────────────────────────────────────────────

    def _enabled(self) -> bool:
        return bool(self._settings_get("text_snippets_enabled", False))

    # ── Core logic ───────────────────────────────────────────────────────────

    def expand(self, text: str) -> str:
        """Заменяет триггерные фразы в *text* на соответствующие expansions.

        Возвращает изменённую строку или оригинал если:
        - feature выключена;
        - список сниппетов пуст;
        - ни один триггер не найден в тексте.

        Алгоритм:
        1. Получаем сниппеты от provider (актуальные на момент вызова).
        2. Сортируем по убыванию длины триггера (longest-first) — самый длинный
           совпадает первым, короткий триггер "вставь" не съедает начало "вставь подпись".
        3. Строим список (pattern, replacement), где pattern — \\b-обёрнутый
           re.escape(trigger). Граница слова \\b корректно работает для кириллицы
           в Python 3.7+ при использовании re.UNICODE (флаг по умолчанию).
        4. Однопроходно применяем замены в порядке убывания длины. Используем
           re.subn с count=0 (все вхождения).
        """
        if not self._enabled():
            return text
        if not text:
            return text

        raw_snippets = self._snippets_provider()
        if not raw_snippets:
            return text

        # Нормализуем и фильтруем; longest-first
        snippets = _validated_snippets(raw_snippets)
        snippets.sort(key=lambda s: len(s["trigger"]), reverse=True)

        result = text
        for snippet in snippets:
            trigger = snippet["trigger"]
            expansion = snippet["expansion"]
            # re.escape делает триггер ReDoS-безопасным.
            # \\b — граница слова: не заменяем "email" внутри "emails" итп.
            pattern = r"\b" + re.escape(trigger) + r"\b"
            try:
                result = re.sub(pattern, expansion, result, flags=re.IGNORECASE)
            except re.error as exc:
                # Теоретически не должно случиться с re.escape, но защищаемся.
                logger.warning(
                    "TextSnippetExpander: ошибка regex для триггера %r: %s", trigger, exc
                )
        return result


# ── Helpers ──────────────────────────────────────────────────────────────────

def _validated_snippets(raw: list) -> list:
    """Возвращает список валидных {"trigger", "expansion"} из raw."""
    result = []
    for item in raw[:_MAX_SNIPPETS]:
        if not isinstance(item, dict):
            continue
        trigger = item.get("trigger", "")
        expansion = item.get("expansion", "")
        if (
            isinstance(trigger, str)
            and isinstance(expansion, str)
            and trigger.strip()
            and len(trigger) <= _MAX_TRIGGER_LEN
            and len(expansion) <= _MAX_EXPANSION_LEN
        ):
            result.append({"trigger": trigger.strip(), "expansion": expansion})
    return result
