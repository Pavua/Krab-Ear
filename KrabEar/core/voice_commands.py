"""Голосовые команды для диктовки: post-process слой поверх STT-текста.

VoiceCommandProcessor сканирует распознанный текст на «командные» слова
(«запятая», «точка», «новый абзац» и т.д.) и заменяет их символами или
операциями. Работает жадно слева направо с whole-word matching.

Поддерживаемые языки: ru, es, en.

Запуск тестов:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_voice_commands.py -v

W1251 fix: ambiguous single-word triggers removed from default tables.
  - RU: «вопрос» and «точка» removed (common words causing production text damage).
  - EN: «period», «colon», «tab», «dash» removed (medical/UI terms).
  - ES: «coma», «punto», «dos puntos» removed («coma» = medical coma in Spanish).
Use _VOICE_COMMANDS_STRICT_MODE = True (default) to enforce multi-word-only mode.
Set to False (or call set_voice_commands_strict_mode(False) via IPC) to re-enable
single-word triggers for users who prefer them.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

logger = logging.getLogger("KrabEar.VoiceCommands")

# ---------------------------------------------------------------------------
# Strict mode: when True, only unambiguous multi-word commands are active.
# Single-word triggers that collide with common vocabulary are excluded.
# Set to False to restore legacy single-word behaviour (per-user opt-in).
# ---------------------------------------------------------------------------
_VOICE_COMMANDS_STRICT_MODE: bool = True


# ---------------------------------------------------------------------------
# Таблицы команд: (паттерн, тип, значение)
# Тип: "insert" | "capitalize_next" | "uppercase_sent" | "delete_last"
# ---------------------------------------------------------------------------

# Каждая запись: (regex_pattern, action, action_arg)
# Порядок важен — более длинные/специфичные паттерны должны идти ПЕРВЕЕ.

# Multi-word (unambiguous) RU commands — always active.
_RU_COMMANDS_MULTIWORD: list[tuple[str, str, str]] = [
    # Удаление — «удалить последнее <N> X»
    (r"удалить последнее слово", "delete_last", "word"),
    (r"удалить последнее предложение", "delete_last", "sentence"),
    (r"удалить последний абзац", "delete_last", "paragraph"),
    (r"удалить последнее", "delete_last", "word"),  # fallback
    # Регистр
    (r"большая буква", "capitalize_next", ""),
    (r"верхний регистр", "uppercase_sent", ""),
    # Составные знаки препинания (2+ слов — РАНЬШЕ одиночных)
    (r"точка с запятой", "insert", ";"),
    (r"восклицательный знак", "insert", "!"),
    (r"вопросительный знак", "insert", "?"),
    (r"новый абзац", "insert", "\n\n"),
    (r"новая строка", "insert", "\n"),
    # Single-word, low-ambiguity (kept in multi-word table)
    (r"запятая", "insert", ","),
    (r"двоеточие", "insert", ":"),
    (r"восклицание", "insert", "!"),
    (r"табуляция", "insert", "\t"),
    (r"пробел", "insert", " "),
    (r"капс", "uppercase_sent", ""),
]

# Single-word RU triggers that are common words — only active in non-strict mode.
# REMOVED from default: «вопрос» (= "question"), «точка» (= "dot/point"),
# as they cause production text damage (e.g. "это важный вопрос" → "это важный?").
_RU_COMMANDS_SINGLE_WORD_AMBIGUOUS: list[tuple[str, str, str]] = [
    (r"точка", "insert", "."),
    (r"вопрос", "insert", "?"),
    (r"тире", "insert", " — "),
]

# Multi-word (unambiguous) ES commands — always active.
_ES_COMMANDS_MULTIWORD: list[tuple[str, str, str]] = [
    # Удаление
    (r"borrar última palabra", "delete_last", "word"),
    (r"borrar último párrafo", "delete_last", "paragraph"),
    (r"borrar última oración", "delete_last", "sentence"),
    (r"borrar último", "delete_last", "word"),
    # Регистр
    (r"letra mayúscula", "capitalize_next", ""),
    (r"mayúscula", "capitalize_next", ""),
    (r"todo mayúsculas", "uppercase_sent", ""),
    # Составные знаки (раньше одиночных)
    (r"punto y coma", "insert", ";"),
    (r"punto y aparte", "insert", "\n\n"),
    (r"signo de exclamación", "insert", "!"),
    (r"signo de interrogación", "insert", "?"),
    (r"nueva línea", "insert", "\n"),
    # Single-word, low-ambiguity (kept in multi-word table)
    (r"tabulación", "insert", "\t"),
    (r"espacio", "insert", " "),
    (r"guión largo", "insert", " — "),
]

# Single-word ES triggers that are common words — only active in non-strict mode.
# REMOVED from default: «coma» (= "coma" in medical Spanish, e.g. "paciente en coma"),
# «punto» (= "point/dot"), «dos puntos» (two words but common phrase "dos puntos de...").
_ES_COMMANDS_SINGLE_WORD_AMBIGUOUS: list[tuple[str, str, str]] = [
    (r"coma", "insert", ","),
    (r"punto", "insert", "."),
    (r"dos puntos", "insert", ":"),
]

# Multi-word (unambiguous) EN commands — always active.
_EN_COMMANDS_MULTIWORD: list[tuple[str, str, str]] = [
    # Удаление
    (r"delete last word", "delete_last", "word"),
    (r"delete last sentence", "delete_last", "sentence"),
    (r"delete last paragraph", "delete_last", "paragraph"),
    (r"delete last", "delete_last", "word"),
    # Регистр
    (r"capitalize next", "capitalize_next", ""),
    (r"caps lock", "uppercase_sent", ""),
    (r"upper case", "uppercase_sent", ""),
    (r"uppercase", "uppercase_sent", ""),
    # Составные знаки (раньше одиночных)
    (r"semicolon", "insert", ";"),
    (r"new paragraph", "insert", "\n\n"),
    (r"exclamation mark", "insert", "!"),
    (r"exclamation point", "insert", "!"),
    (r"question mark", "insert", "?"),
    (r"new line", "insert", "\n"),
    (r"full stop", "insert", "."),
    (r"em dash", "insert", " — "),
    # Single-word, low-ambiguity (kept in multi-word table)
    (r"comma", "insert", ","),
]

# Single-word EN triggers that are common words — only active in non-strict mode.
# REMOVED from default: «period» (medical/financial term), «colon» (medical term),
# «tab» (UI/keyboard term), «dash» (used in dictation as punctuation or word).
_EN_COMMANDS_SINGLE_WORD_AMBIGUOUS: list[tuple[str, str, str]] = [
    (r"period", "insert", "."),
    (r"colon", "insert", ":"),
    (r"tab", "insert", "\t"),
    (r"dash", "insert", "-"),
]


def _build_command_table(
    multiword: list[tuple[str, str, str]],
    ambiguous: list[tuple[str, str, str]],
    strict: bool,
) -> list[tuple[str, str, str]]:
    """Собирает итоговую таблицу команд с учётом strict mode.

    В strict mode (default): только multiword (unambiguous) команды.
    В non-strict mode: multiword + ambiguous однословные триггеры.
    """
    if strict:
        return list(multiword)
    return list(multiword) + list(ambiguous)


def _make_lang_commands(strict: bool) -> dict[str, list[tuple[str, str, str]]]:
    return {
        "ru": _build_command_table(_RU_COMMANDS_MULTIWORD, _RU_COMMANDS_SINGLE_WORD_AMBIGUOUS, strict),
        "es": _build_command_table(_ES_COMMANDS_MULTIWORD, _ES_COMMANDS_SINGLE_WORD_AMBIGUOUS, strict),
        "en": _build_command_table(_EN_COMMANDS_MULTIWORD, _EN_COMMANDS_SINGLE_WORD_AMBIGUOUS, strict),
    }


# Active command tables (rebuilt when strict mode changes).
_RU_COMMANDS: list[tuple[str, str, str]] = _build_command_table(
    _RU_COMMANDS_MULTIWORD, _RU_COMMANDS_SINGLE_WORD_AMBIGUOUS, _VOICE_COMMANDS_STRICT_MODE
)
_ES_COMMANDS: list[tuple[str, str, str]] = _build_command_table(
    _ES_COMMANDS_MULTIWORD, _ES_COMMANDS_SINGLE_WORD_AMBIGUOUS, _VOICE_COMMANDS_STRICT_MODE
)
_EN_COMMANDS: list[tuple[str, str, str]] = _build_command_table(
    _EN_COMMANDS_MULTIWORD, _EN_COMMANDS_SINGLE_WORD_AMBIGUOUS, _VOICE_COMMANDS_STRICT_MODE
)

_LANG_COMMANDS: dict[str, list[tuple[str, str, str]]] = _make_lang_commands(_VOICE_COMMANDS_STRICT_MODE)

_LANG_COMMANDS: dict[str, list[tuple[str, str, str]]] = {
    "ru": _RU_COMMANDS,
    "es": _ES_COMMANDS,
    "en": _EN_COMMANDS,
}

# Компилированные паттерны: {lang: [(compiled_re, action, arg), ...]}
# Cache is keyed by (lang, strict_mode) to support runtime mode switching.
_COMPILED: dict[tuple[str, bool], list[tuple[re.Pattern[str], str, str]]] = {}


def _build_pattern(raw_pattern: str) -> re.Pattern[str]:
    r"""Оборачивает паттерн в whole-word matching: \b...\b (case-insensitive).

    Если паттерн уже содержит спецсимволы регулярного выражения — оставляем
    как есть (для будущих расширений), иначе экранируем слова через re.escape.
    """
    # Экранируем каждое слово в паттерне, затем соединяем пробелом/\s+
    # Это гарантирует точное совпадение слов, не подстрок
    words = raw_pattern.split()
    escaped = r"\s+".join(re.escape(w) for w in words)
    # Whole-word boundaries: \b перед первым словом и после последнего
    return re.compile(r"\b" + escaped + r"\b", re.IGNORECASE)


def _get_compiled(lang: str, strict: bool = True) -> list[tuple[re.Pattern[str], str, str]]:
    """Возвращает компилированные паттерны для языка (с кэшированием).

    Args:
        lang: язык ("ru", "es", "en").
        strict: если True — только multiword (unambiguous) команды.
    """
    cache_key = (lang, strict)
    if cache_key not in _COMPILED:
        lang_table = _make_lang_commands(strict)
        raw = lang_table.get(lang, [])
        _COMPILED[cache_key] = [(_build_pattern(p), action, arg) for p, action, arg in raw]
    return _COMPILED[cache_key]


# ---------------------------------------------------------------------------
# Операции удаления
# ---------------------------------------------------------------------------

def _delete_last_word(text: str) -> str:
    """Удаляет последнее слово из текста (включая пробел перед ним)."""
    # Ищем последнее слово (непробельная последовательность в конце)
    stripped = text.rstrip()
    if not stripped:
        return ""
    # Убираем последнее слово
    match = re.search(r"\s+\S+$", stripped)
    if match:
        return stripped[: match.start()].rstrip()
    # Единственное слово в тексте
    return ""


def _delete_last_sentence(text: str) -> str:
    """Удаляет последнее предложение (от последней точки/!/?/перевода строки)."""
    stripped = text.rstrip()
    if not stripped:
        return ""
    # Ищем конец предыдущего предложения
    match = re.search(r"[.!?\n](?!.*[.!?\n])", stripped)
    if match:
        return stripped[: match.end()].rstrip()
    # Нет разделителя — удаляем всё
    return ""


def _delete_last_paragraph(text: str) -> str:
    """Удаляет последний абзац (от последнего двойного перевода строки)."""
    stripped = text.rstrip()
    if not stripped:
        return ""
    idx = stripped.rfind("\n\n")
    if idx >= 0:
        return stripped[:idx].rstrip()
    # Нет абзацев — удаляем всё
    return ""


_DELETE_HANDLERS = {
    "word": _delete_last_word,
    "sentence": _delete_last_sentence,
    "paragraph": _delete_last_paragraph,
}


# ---------------------------------------------------------------------------
# Основной процессор
# ---------------------------------------------------------------------------

class VoiceCommandProcessor:
    """Post-process слой: преобразует голосовые команды в тексте в действия.

    Алгоритм: однопроходный жадный слева направо.
    На каждой позиции ищем самый длинный паттерн для данного языка.
    Если найден — применяем action; если нет — копируем символ в вывод.

    Strict mode (default True): only multi-word, unambiguous commands fire.
    Non-strict mode: additionally enables single-word triggers like «вопрос»,
    «точка», «period», «colon», «tab», «coma», «punto» etc.
    Use set_voice_commands_strict_mode(False) to opt-in to legacy behaviour.
    """

    def __init__(
        self,
        settings_get: Optional[Callable[[str, Any], Any]] = None,
    ) -> None:
        """Инициализация.

        Args:
            settings_get: callback (key, default) -> value для runtime toggle'ов.
                         Если None — читаем из DEFAULT_SETTINGS / фолбэк.
        """
        self._settings_get: Callable[[str, Any], Any] = settings_get or (lambda k, d: d)
        # Instance-level strict mode override; None = use module-level default.
        self._strict_mode_override: Optional[bool] = None

    # --- Runtime toggle helpers ---

    def _enabled(self) -> bool:
        return bool(self._settings_get("voice_commands_enabled", True))

    def _allowed_languages(self) -> list[str]:
        val = self._settings_get("voice_commands_languages", ["ru", "es", "en"])
        if isinstance(val, str):
            return [lang.strip() for lang in val.split(",") if lang.strip()]
        return list(val)

    def _strict_mode(self) -> bool:
        """Возвращает текущий strict mode (instance override или module default)."""
        if self._strict_mode_override is not None:
            return self._strict_mode_override
        return bool(self._settings_get("voice_commands_strict_mode", _VOICE_COMMANDS_STRICT_MODE))

    # --- IPC-facing API ---

    def set_voice_commands_strict_mode(self, enabled: bool) -> None:
        """Переключает strict mode для данного экземпляра процессора.

        Когда enabled=True (default): только многословные (unambiguous) команды
        активны. Однословные триггеры-омонимы («вопрос», «точка», «period»,
        «colon», «tab», «coma», «punto») отключены.

        Когда enabled=False: все команды активны (legacy-режим). Рекомендуется
        только для пользователей, которые явно диктуют команды и не говорят
        медицинских/технических терминов.

        Args:
            enabled: True = strict (safe default), False = all triggers active.
        """
        self._strict_mode_override = bool(enabled)
        logger.info(
            "VoiceCommands: strict mode %s",
            "enabled" if enabled else "disabled (all single-word triggers active)",
        )

    # --- Public API ---

    def process(self, text: str, language: str = "ru") -> str:
        """Применяет голосовые команды к тексту.

        Args:
            text: входной текст от STT.
            language: язык текста ("ru", "es", "en" или подстрока, напр. "ru-RU").

        Returns:
            Текст с применёнными командами.
        """
        if not self._enabled():
            logger.debug("VoiceCommands: отключён, пропускаем")
            return text

        # Нормализуем язык: "ru-RU" → "ru"
        lang = (language or "ru").split("-")[0].split("_")[0].lower()

        allowed = self._allowed_languages()
        if lang not in allowed:
            logger.debug("VoiceCommands: язык %s не в разрешённых %s, пропускаем", lang, allowed)
            return text

        strict = self._strict_mode()
        patterns = _get_compiled(lang, strict=strict)
        if not patterns:
            return text

        result = self._apply_commands(text, patterns)
        if result != text:
            logger.debug(
                "VoiceCommands: %d chars → %d chars (lang=%s, strict=%s)",
                len(text), len(result), lang, strict,
            )
        return result

    # --- Internal processing ---

    def _apply_commands(
        self,
        text: str,
        patterns: list[tuple[re.Pattern[str], str, str]],
    ) -> str:
        """Жадный однопроходный парсер команд.

        Модель вставки символа:
        - Перед вставкой: убираем trailing пробел из output (пробел перед командой).
        - После вставки: если в тексте есть продолжение — добавляем один пробел.
          (не добавляем для \n, \n\n, \t — они сами являются разделителями).
        """
        output: list[str] = []
        pos = 0
        length = len(text)

        # Состояние для команд модификации регистра
        capitalize_next = False
        uppercase_next_sentence = False

        while pos < length:
            matched = False

            for pattern, action, arg in patterns:
                m = pattern.match(text, pos)
                if m is None:
                    continue

                # Нашли команду на текущей позиции
                matched = True
                matched_end = m.end()

                if action == "insert":
                    # Убираем trailing пробел из output перед вставкой символа
                    if output and output[-1] == " ":
                        output.pop()
                    output.append(arg)
                    pos = matched_end
                    # Пропускаем пробел(ы) после команды
                    while pos < length and text[pos] == " ":
                        pos += 1
                    # Добавляем пробел после символа только если:
                    # - есть продолжение текста
                    # - последний символ arg не является разделителем (пробел/\n/\t)
                    if pos < length and arg and arg[-1] not in (" ", "\n", "\t"):
                        output.append(" ")

                elif action in ("capitalize_next", "uppercase_sent"):
                    if action == "capitalize_next":
                        capitalize_next = True
                    else:
                        uppercase_next_sentence = True
                    # Убираем trailing пробел из output (пробел перед командой)
                    if output and output[-1] == " ":
                        output.pop()
                    pos = matched_end
                    # Пропускаем пробел(ы) после команды
                    while pos < length and text[pos] == " ":
                        pos += 1
                    # Добавляем один пробел перед следующим словом
                    if pos < length:
                        output.append(" ")

                elif action == "delete_last":
                    handler = _DELETE_HANDLERS.get(arg, _delete_last_word)
                    current = "".join(output)
                    output = [handler(current)]
                    pos = matched_end
                    while pos < length and text[pos] == " ":
                        pos += 1
                    if pos < length:
                        output.append(" ")

                break  # нашли первый подходящий паттерн — применили

            if not matched:
                # Обычный символ — обрабатываем модификаторы регистра
                char = text[pos]
                if capitalize_next and char.isalpha():
                    char = char.upper()
                    capitalize_next = False
                elif uppercase_next_sentence:
                    # Uppercase до конца предложения (до знака препинания)
                    if char.isalpha():
                        char = char.upper()
                    elif char in ".!?\n":
                        uppercase_next_sentence = False
                output.append(char)
                pos += 1

        if capitalize_next:
            logger.info(
                "voice_commands: capitalize_next at end-of-text — no following token"
            )

        return "".join(output)
