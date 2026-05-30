"""Расширение аббревиатур для вывода STT.

AbbreviationExpander заменяет стандартные сокращения на полные формы в транскрипциях.
Поддерживает русский (ru), английский (en) и испанский (es) языки.
Персистирует пользовательские аббревиатуры в {data_dir}/abbreviations.json.
Контекстно-зависимый: не разворачивает аббревиатуры внутри URL или кода.

По умолчанию разворачиваются только **однозначные** аббревиатуры.  Аббревиатуры
с несколькими значениями (например ``гл.`` = глава | главный) исключены из
набора по умолчанию во избежание семантической порчи транскрипций.  Для
включения устаревшего поведения передайте ``expand_ambiguous=True`` в
``__init__`` или ``expand()``.

Список неоднозначных RU-аббревиатур (не включаются по умолчанию):
  гл.  — глава vs главный (гл.врач)
  ст.  — старый vs статья vs строка
  п.   — пункт vs поселок vs страница
  с.   — село vs страница vs север
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.AbbreviationExpander")

# ── URL / code pattern — не разворачивать внутри них ──────────────────────────
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_CODE_SPAN_RE = re.compile(r"`[^`]+`")

# ── Встроенные аббревиатуры ───────────────────────────────────────────────────

# Каждая запись — (pattern, expansion, flags).
# Флаги: "no_after_digit" — не раскрывать, если непосредственно перед стоит цифра.

# Однозначные RU-аббревиатуры — включаются по умолчанию.
_BUILTIN_RU_UNAMBIGUOUS: list[tuple[str, str, str]] = [
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
    ("обл.", "область", "no_after_digit"),
    ("р-н", "район", ""),
    # Контекстно-зависимые
    ("ул.", "улица", ""),
    ("г.", "город", "no_after_digit"),
    ("кв.", "квартира", "no_after_digit"),
    ("пл.", "площадь", "no_after_digit"),
    ("пр-т", "проспект", ""),
    # Должности / звания
    ("проф.", "профессор", ""),
    ("акад.", "академик", ""),
    ("зам.", "заместитель", ""),
    # Единицы
    ("тыс.", "тысяч", ""),
    ("млн.", "миллионов", ""),
    ("млрд.", "миллиардов", ""),
    ("руб.", "рублей", ""),
    ("коп.", "копеек", ""),
]

# Неоднозначные RU-аббревиатуры — НЕ включаются по умолчанию.
# Включить opt-in: AbbreviationExpander(expand_ambiguous=True)
# или expand(..., expand_ambiguous=True).
#
# Примеры конфликтов:
#   гл. → глава  vs  гл.врач (главный врач)
#   св. → святой vs  св.  (свежий, свободен в объявлениях)
_BUILTIN_RU_AMBIGUOUS: list[tuple[str, str, str]] = [
    ("гл.", "глава", ""),      # главный (гл.врач, гл.редактор)
    ("св.", "святой", ""),     # свежий / свободен
]

# Для обратной совместимости: полный список (как было до W1081).
_BUILTIN_RU: list[tuple[str, str, str]] = (
    _BUILTIN_RU_UNAMBIGUOUS + _BUILTIN_RU_AMBIGUOUS
)

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

# Defaults per language (однозначные только).
_BUILTINS_UNAMBIGUOUS: dict[str, list[tuple[str, str, str]]] = {
    "ru": _BUILTIN_RU_UNAMBIGUOUS,
    "en": _BUILTIN_EN,   # EN/ES lists have no ambiguous entries yet
    "es": _BUILTIN_ES,
}

# Ambiguous-only per language (для opt-in).
_BUILTINS_AMBIGUOUS_ONLY: dict[str, list[tuple[str, str, str]]] = {
    "ru": _BUILTIN_RU_AMBIGUOUS,
    "en": [],
    "es": [],
}


def _make_pattern(abbr: str) -> re.Pattern:
    """Компилирует regex для точного совпадения аббревиатуры (с учётом пунктуации)."""
    escaped = re.escape(abbr)
    # Граница: пробел/начало строки перед, пробел/конец строки/пунктуация после.
    return re.compile(r"(?<!\w)" + escaped + r"(?=\s|$|[,;:!?»)])", re.IGNORECASE)


class AbbreviationExpander:
    """Разворачивает аббревиатуры в транскрипциях.

    По умолчанию загружаются только **однозначные** встроенные аббревиатуры.
    Неоднозначные (например ``гл.`` = глава | главный врач) пропускаются,
    чтобы не искажать смысл транскрипций.

    Использование:
        expander = AbbreviationExpander(data_dir=Path("~/.krab_ear_data"))
        result = expander.expand("т.е. это верно, напр. вот так")
        # → "то есть это верно, например вот так"

        # Opt-in к полному (legacy) набору:
        expander_full = AbbreviationExpander(expand_ambiguous=True)
        result2 = expander_full.expand("гл. врач")
        # → "глава врач"  (осторожно: семантически некорректно в контексте!)
    """

    def __init__(
        self,
        data_dir: Path | None = None,
        expand_ambiguous: bool = False,
    ) -> None:
        """Инициализирует расширитель аббревиатур.

        Args:
            data_dir: Путь к директории с пользовательскими данными
                      (``abbreviations.json``).  Если ``None`` — только in-memory.
            expand_ambiguous: Если ``True``, включать неоднозначные встроенные
                аббревиатуры (устаревшее поведение до W1081).  По умолчанию
                ``False`` — безопасный режим без семантических искажений.
        """
        self._data_dir = data_dir
        self._expand_ambiguous = expand_ambiguous
        self._lock = threading.RLock()
        # {language: {abbr: {"expansion": str, "flags": str, "builtin": bool,
        #                     "ambiguous": bool}}}
        self._abbrevs: dict[str, dict[str, dict[str, Any]]] = {}
        # Кэш скомпилированных паттернов: {language: [(pattern, expansion, flags, abbr)]}
        self._compiled: dict[str, list[tuple[re.Pattern, str, str, str]]] = {}

        # Загружаем однозначные встроенные аббревиатуры (ambiguous=False всегда).
        for lang, entries in _BUILTINS_UNAMBIGUOUS.items():
            self._abbrevs[lang] = {}
            for abbr, expansion, flags in entries:
                self._abbrevs[lang][abbr] = {
                    "expansion": expansion,
                    "flags": flags,
                    "builtin": True,
                    "ambiguous": False,
                }
        # Добавляем неоднозначные с пометкой ambiguous=True.
        # Они всегда присутствуют в словаре (для list_abbreviations),
        # но expand() пропускает их если allow_ambiguous=False.
        for lang, ambiguous_entries in _BUILTINS_AMBIGUOUS_ONLY.items():
            if lang not in self._abbrevs:
                self._abbrevs[lang] = {}
            for abbr, expansion, flags in ambiguous_entries:
                self._abbrevs[lang][abbr] = {
                    "expansion": expansion,
                    "flags": flags,
                    "builtin": True,
                    "ambiguous": True,
                }

        # Загружаем пользовательские из файла (перезаписывают встроенные)
        self._load_custom()
        # Компилируем паттерны
        self._rebuild_compiled()

    # ── Публичный API ──────────────────────────────────────────────────────────

    def expand(
        self,
        text: str,
        language: str = "ru",
        expand_ambiguous: bool | None = None,
    ) -> str:
        """Разворачивает аббревиатуры в тексте.

        Args:
            text: Исходный текст транскрипции.
            language: Код языка: "ru", "en", "es".
            expand_ambiguous: Переопределить инстанс-уровневый флаг для
                данного вызова.  ``None`` — использовать значение, заданное
                в ``__init__``.

        Returns:
            Текст с раскрытыми аббревиатурами.
        """
        if not text or not text.strip():
            return text

        lang = language.lower()
        with self._lock:
            if lang not in self._compiled:
                return text
            compiled_list = list(self._compiled[lang])

        # Определяем, нужно ли разворачивать неоднозначные аббревиатуры
        allow_ambiguous = (
            self._expand_ambiguous if expand_ambiguous is None else expand_ambiguous
        )

        # Находим защищённые зоны (URL и code spans) — в них не заменяем
        protected: list[tuple[int, int]] = []
        for m in _URL_RE.finditer(text):
            protected.append((m.start(), m.end()))
        for m in _CODE_SPAN_RE.finditer(text):
            protected.append((m.start(), m.end()))

        # Собираем все совпадения
        matches: list[tuple[int, int, str, str, str]] = []
        for compiled_re, expansion, flags, abbr in compiled_list:
            # Пропускаем неоднозначные если opt-in не включён
            entry = self._abbrevs.get(lang, {}).get(abbr, {})
            if entry.get("ambiguous", False) and not allow_ambiguous:
                continue
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
        with self._lock:
            if lang not in self._abbrevs:
                self._abbrevs[lang] = {}
            self._abbrevs[lang][abbr] = {
                "expansion": expansion,
                "flags": flags,
                "builtin": False,
                "ambiguous": False,  # пользовательские записи всегда однозначны
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
        with self._lock:
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
            Список словарей с ключами: abbr, expansion, flags, builtin, ambiguous.
            Неоднозначные аббревиатуры (``ambiguous=True``) не разворачиваются
            по умолчанию — только при ``expand_ambiguous=True``.
        """
        lang = language.lower()
        with self._lock:
            if lang not in self._abbrevs:
                return []
            return [
                {
                    "abbr": abbr,
                    "expansion": entry["expansion"],
                    "flags": entry.get("flags", ""),
                    "builtin": entry.get("builtin", False),
                    "ambiguous": entry.get("ambiguous", False),
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
                    # Пользовательские записи перезаписывают встроенные;
                    # пользовательские записи никогда не помечаются как
                    # ambiguous — пользователь выбрал расширение явно.
                    self._abbrevs[lang][abbr] = {
                        "expansion": entry.get("expansion", ""),
                        "flags": entry.get("flags", ""),
                        "builtin": False,
                        "ambiguous": False,
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
