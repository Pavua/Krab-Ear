"""Расширение аббревиатур для вывода STT.

AbbreviationExpander заменяет стандартные сокращения на полные формы в транскрипциях.
Поддерживает русский (ru), английский (en) и испанский (es) языки.
Персистирует пользовательские аббревиатуры в {data_dir}/abbreviations.json.
Контекстно-зависимый: не разворачивает аббревиатуры внутри URL или кода.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.AbbreviationExpander")

# ── URL / code pattern — не разворачивать внутри них ──────────────────────────
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_CODE_SPAN_RE = re.compile(r"`[^`]+`")

# ── Встроенные аббревиатуры ───────────────────────────────────────────────────

# Каждая запись — (pattern, expansion, flags).
# Флаги: "no_after_digit" — не раскрывать, если непосредственно перед стоит цифра.
_BUILTIN_RU: list[tuple[str, str, str]] = [
    # Общие
    ("т.е.", "то есть", ""),
    ("т.к.", "так как", ""),
    ("т.д.", "так далее", ""),
    ("т.п.", "тому подобное", ""),
    ("и т.д.", "и так далее", ""),
    ("и т.п.", "и тому подобное", ""),
    ("напр.", "например", ""),
    ("др.", "другие", ""),
    ("пр.", "прочее", ""),
    ("проч.", "прочее", ""),
    ("св.", "святой", ""),
    ("обл.", "область", ""),
    ("р-н", "район", ""),
    # Контекстно-зависимые
    ("ул.", "улица", ""),
    ("г.", "город", "no_after_digit"),
    ("кв.", "квартира", "no_after_digit"),
    ("пл.", "площадь", "no_after_digit"),
    ("пр-т", "проспект", ""),
    ("д.", "дом", "no_after_digit"),
    # Должности / звания
    ("проф.", "профессор", ""),
    ("акад.", "академик", ""),
    ("гл.", "глава", ""),
    ("зам.", "заместитель", ""),
    ("ред.", "редактор", ""),
    # Единицы
    ("тыс.", "тысяч", ""),
    ("млн.", "миллионов", ""),
    ("млрд.", "миллиардов", ""),
    ("руб.", "рублей", ""),
    ("коп.", "копеек", ""),
]

_BUILTIN_EN: list[tuple[str, str, str]] = [
    ("e.g.", "for example", ""),
    ("i.e.", "that is", ""),
    ("etc.", "et cetera", ""),
    ("vs.", "versus", ""),
    ("approx.", "approximately", ""),
    ("dept.", "department", ""),
    ("est.", "established", ""),
    ("fig.", "figure", ""),
    ("govt.", "government", ""),
    ("info.", "information", ""),
    ("jr.", "junior", ""),
    ("max.", "maximum", ""),
    ("min.", "minimum", ""),
    ("misc.", "miscellaneous", ""),
    ("no.", "number", "no_after_digit"),
    ("orig.", "original", ""),
    ("pkg.", "package", ""),
    ("prof.", "professor", ""),
    ("qty.", "quantity", ""),
    ("ref.", "reference", ""),
    ("rev.", "revision", ""),
    ("sq.", "square", ""),
    ("st.", "street", ""),
    ("vol.", "volume", ""),
]

_BUILTIN_ES: list[tuple[str, str, str]] = [
    ("p.ej.", "por ejemplo", ""),
    ("etc.", "etcétera", ""),
    ("núm.", "número", "no_after_digit"),
    ("pág.", "página", ""),
    ("Sr.", "señor", ""),
    ("Sra.", "señora", ""),
    ("Dr.", "doctor", ""),
    ("Dra.", "doctora", ""),
    ("Av.", "avenida", ""),
    ("Blvd.", "bulevar", ""),
    ("dpto.", "departamento", ""),
    ("aprox.", "aproximadamente", ""),
    ("máx.", "máximo", ""),
    ("mín.", "mínimo", ""),
    ("vol.", "volumen", ""),
    ("cap.", "capítulo", ""),
    ("art.", "artículo", ""),
    ("ref.", "referencia", ""),
]

_BUILTINS: dict[str, list[tuple[str, str, str]]] = {
    "ru": _BUILTIN_RU,
    "en": _BUILTIN_EN,
    "es": _BUILTIN_ES,
}


def _make_pattern(abbr: str) -> re.Pattern:
    """Компилирует regex для точного совпадения аббревиатуры (с учётом пунктуации)."""
    escaped = re.escape(abbr)
    # Граница: пробел/начало строки перед, пробел/конец строки/пунктуация после.
    return re.compile(r"(?<!\w)" + escaped + r"(?=\s|$|[,;:!?»)])", re.IGNORECASE)


class AbbreviationExpander:
    """Разворачивает аббревиатуры в транскрипциях.

    Использование:
        expander = AbbreviationExpander(data_dir=Path("~/.krab_ear_data"))
        result = expander.expand("т.е. это верно, напр. вот так")
        # → "то есть это верно, например вот так"
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir
        # {language: {abbr: {"expansion": str, "flags": str, "builtin": bool}}}
        self._abbrevs: dict[str, dict[str, dict[str, Any]]] = {}
        # Кэш скомпилированных паттернов: {language: [(pattern, expansion, flags, abbr)]}
        self._compiled: dict[str, list[tuple[re.Pattern, str, str, str]]] = {}

        # Загружаем встроенные аббревиатуры
        for lang, entries in _BUILTINS.items():
            self._abbrevs[lang] = {}
            for abbr, expansion, flags in entries:
                self._abbrevs[lang][abbr] = {
                    "expansion": expansion,
                    "flags": flags,
                    "builtin": True,
                }

        # Загружаем пользовательские из файла (перезаписывают встроенные)
        self._load_custom()
        # Компилируем паттерны
        self._rebuild_compiled()

    # ── Публичный API ──────────────────────────────────────────────────────────

    def expand(self, text: str, language: str = "ru") -> str:
        """Разворачивает аббревиатуры в тексте.

        Args:
            text: Исходный текст транскрипции.
            language: Код языка: "ru", "en", "es".

        Returns:
            Текст с раскрытыми аббревиатурами.
        """
        if not text or not text.strip():
            return text

        lang = language.lower()
        if lang not in self._compiled:
            return text

        # Находим защищённые зоны (URL и code spans) — в них не заменяем
        protected: list[tuple[int, int]] = []
        for m in _URL_RE.finditer(text):
            protected.append((m.start(), m.end()))
        for m in _CODE_SPAN_RE.finditer(text):
            protected.append((m.start(), m.end()))

        result = text
        offset = 0  # смещение при замене

        # Собираем все совпадения
        matches: list[tuple[int, int, str, str, str]] = []
        for compiled_re, expansion, flags, abbr in self._compiled[lang]:
            for m in compiled_re.finditer(text):
                start, end = m.start(), m.end()
                # Пропускаем защищённые зоны
                if any(ps <= start < pe for ps, pe in protected):
                    continue
                # Контекстно-зависимые: не раскрывать после цифры
                if "no_after_digit" in flags:
                    before = text[:start].rstrip()
                    if before and before[-1].isdigit():
                        continue
                matches.append((start, end, m.group(0), expansion, abbr))

        # Сортируем по позиции (чтобы применять слева направо) и убираем пересечения
        matches.sort(key=lambda x: x[0])
        non_overlapping: list[tuple[int, int, str, str, str]] = []
        last_end = -1
        for match in matches:
            if match[0] >= last_end:
                non_overlapping.append(match)
                last_end = match[1]

        # Применяем замены с корректировкой смещения
        result = text
        offset = 0
        for start, end, original, expansion, _ in non_overlapping:
            adj_start = start + offset
            adj_end = end + offset
            # Сохраняем регистр первого символа при необходимости
            replacement = self._match_case(original, expansion)
            result = result[:adj_start] + replacement + result[adj_end:]
            offset += len(replacement) - len(original)

        return result

    def add_abbreviation(
        self, abbr: str, expansion: str, language: str = "ru", flags: str = ""
    ) -> None:
        """Добавляет пользовательскую аббревиатуру.

        Args:
            abbr: Аббревиатура (например, "т.н.").
            expansion: Полная форма (например, "так называемый").
            language: Код языка.
            flags: Дополнительные флаги (например, "no_after_digit").
        """
        lang = language.lower()
        if lang not in self._abbrevs:
            self._abbrevs[lang] = {}
        self._abbrevs[lang][abbr] = {
            "expansion": expansion,
            "flags": flags,
            "builtin": False,
        }
        self._rebuild_compiled(lang)
        self._save_custom()
        logger.debug("Добавлена аббревиатура [%s] %r → %r", lang, abbr, expansion)

    def remove_abbreviation(self, abbr: str, language: str = "ru") -> bool:
        """Удаляет аббревиатуру.

        Args:
            abbr: Аббревиатура для удаления.
            language: Код языка.

        Returns:
            True если аббревиатура была удалена, False если не найдена.
        """
        lang = language.lower()
        if lang not in self._abbrevs or abbr not in self._abbrevs[lang]:
            return False
        del self._abbrevs[lang][abbr]
        self._rebuild_compiled(lang)
        self._save_custom()
        logger.debug("Удалена аббревиатура [%s] %r", lang, abbr)
        return True

    def list_abbreviations(self, language: str = "ru") -> list[dict[str, Any]]:
        """Возвращает список аббревиатур для языка.

        Args:
            language: Код языка.

        Returns:
            Список словарей с ключами: abbr, expansion, flags, builtin.
        """
        lang = language.lower()
        if lang not in self._abbrevs:
            return []
        return [
            {
                "abbr": abbr,
                "expansion": entry["expansion"],
                "flags": entry.get("flags", ""),
                "builtin": entry.get("builtin", False),
            }
            for abbr, entry in sorted(self._abbrevs[lang].items())
        ]

    # ── Вспомогательные методы ─────────────────────────────────────────────────

    @staticmethod
    def _match_case(original: str, expansion: str) -> str:
        """Сохраняет регистр первого символа оригинала в замене."""
        if not original or not expansion:
            return expansion
        if original[0].isupper() and expansion[0].islower():
            return expansion[0].upper() + expansion[1:]
        return expansion

    def _rebuild_compiled(self, language: str | None = None) -> None:
        """Перекомпилирует кэш паттернов для одного или всех языков."""
        langs = [language] if language else list(self._abbrevs.keys())
        for lang in langs:
            entries = self._abbrevs.get(lang, {})
            compiled: list[tuple[re.Pattern, str, str, str]] = []
            # Сортируем по длине (длинные сначала, чтобы "и т.д." > "т.д.")
            for abbr in sorted(entries.keys(), key=len, reverse=True):
                entry = entries[abbr]
                try:
                    pattern = _make_pattern(abbr)
                    compiled.append((pattern, entry["expansion"], entry.get("flags", ""), abbr))
                except re.error as exc:
                    logger.warning("Не удалось скомпилировать паттерн для %r: %s", abbr, exc)
            self._compiled[lang] = compiled

    def _load_custom(self) -> None:
        """Загружает пользовательские аббревиатуры из файла."""
        if not self._data_dir:
            return
        path = Path(self._data_dir) / "abbreviations.json"
        if not path.exists():
            return
        try:
            data: dict[str, dict[str, dict]] = json.loads(path.read_text(encoding="utf-8"))
            for lang, entries in data.items():
                if lang not in self._abbrevs:
                    self._abbrevs[lang] = {}
                for abbr, entry in entries.items():
                    # Пользовательские записи перезаписывают встроенные
                    self._abbrevs[lang][abbr] = {
                        "expansion": entry.get("expansion", ""),
                        "flags": entry.get("flags", ""),
                        "builtin": False,
                    }
            logger.debug("Загружены пользовательские аббревиатуры из %s", path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Не удалось загрузить abbreviations.json: %s", exc)

    def _save_custom(self) -> None:
        """Сохраняет пользовательские аббревиатуры в файл."""
        if not self._data_dir:
            return
        path = Path(self._data_dir) / "abbreviations.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            custom: dict[str, dict[str, dict]] = {}
            for lang, entries in self._abbrevs.items():
                custom_entries = {
                    abbr: {"expansion": e["expansion"], "flags": e.get("flags", "")}
                    for abbr, e in entries.items()
                    if not e.get("builtin", False)
                }
                if custom_entries:
                    custom[lang] = custom_entries
            path.write_text(json.dumps(custom, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.debug("Сохранены пользовательские аббревиатуры в %s", path)
        except OSError as exc:
            logger.warning("Не удалось сохранить abbreviations.json: %s", exc)
