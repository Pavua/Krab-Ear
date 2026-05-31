"""HallucinationManager — управление паттернами галлюцинаций Krab Ear.

Объединяет встроенные паттерны (из TextUtils) с пользовательскими.
Пользовательские паттерны сохраняются в {data_dir}/hallucination_patterns.json.

Security note (ReDoS mitigation, Wave 1729)
-------------------------------------------
User-supplied patterns are compiled as regexes and run against arbitrary
transcript text.  An accidental or malicious catastrophic-backtracking pattern
(e.g. ``(a+)+$``, ``(.*a){20}``) can cause the CPython ``re`` engine to hang
for minutes on pathological input, freezing the STT pipeline.

Three-layer defence applied to *custom* patterns only (built-ins are trusted):

1. **Length cap** — pattern strings longer than ``_MAX_PATTERN_LEN`` chars are
   rejected at ``add_pattern()`` time.
2. **Catastrophic-backtracking detector** — ``_reject_catastrophic_pattern()``
   inspects the pattern string for constructs known to produce exponential
   back-tracking (nested/ambiguous quantifiers, e.g. ``(a+)+``,
   ``(?:a|a)+``, ``(a*)*``).  Patterns that match any heuristic are rejected
   with a ``ValueError`` before they are compiled or stored.
3. **Input-length cap at match time** — ``check_text()`` / ``strip_hallucinations()``
   truncate the lowercased text to ``_MAX_MATCH_INPUT_LEN`` characters before
   running any regex.  This bounds the worst-case backtracking depth even if
   an exotic catastrophic pattern slips through heuristic detection.

Built-in patterns (``_BUILTIN_PATTERNS_RAW``) bypass these checks — they are
authored by developers and reviewed at commit time.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Core.HallucinationManager")

# ── ReDoS-mitigation constants ───────────────────────────────────────────────

# Maximum character length allowed for a user-supplied pattern string.
_MAX_PATTERN_LEN: int = 500

# Maximum character length of the text slice passed to re.search at match time.
# Transcripts are typically < 2 KB; 4 KB is generous headroom that still
# eliminates the pathological-input dimension of a catastrophic pattern.
_MAX_MATCH_INPUT_LEN: int = 4096

# Heuristic sub-patterns that signal nested/ambiguous quantifiers causing
# catastrophic backtracking.  The list is not exhaustive — it catches the most
# common classes (e.g. ``(a+)+``, ``(a*)*``, ``(?:a|a)*``, ``(.*a){n}``).
_CATASTROPHIC_RE = re.compile(
    r"""
    # nested quantifier: (X+)+ / (X*)* / (X+)* / (X*)+  where X is not a single literal
    \(  [^)]*  [+*?] \s* \)  \s* [+*?{]
    |
    # repeated alternation that shares a prefix: (?:a|a)+ or (ab|a)+  etc.
    \(  (?: \?:? )?  [^)]+  \|  [^)]+  \)  \s* [+*?{]
    |
    # possessive / atomic not available in stdlib re — but ``{n,}`` applied to
    # a group that already contains a repetition (e.g. ``(.+){5,}``)
    \(  [^)]*  [+*?]  [^)]*  \)  \s*  \{
    """,
    re.VERBOSE,
)


# ── Встроенные паттерны (из core/utils.py _HALLUCINATION_PATTERNS) ───────────
_BUILTIN_PATTERNS_RAW: list[tuple[str, str]] = [
    (r"(?:спасибо за просмотр|спасибо за внимание)[.!?…]*$", "youtube"),
    (r"(?:субтитры сделал [^.!?…]{1,40})[.!?…]*$", "youtube"),
    (r"(?:подписывайтесь на канал)[.!?…]*$", "youtube"),
    (r"(?:до новых встреч)[.!?…]*$", "youtube"),
    (r"(?:продолжение следует)[.!?…]*$", "youtube"),
    (r"(?:to be continued)[.!?…]*$", "youtube"),
    (r"(?:подписывайтесь на наш канал)[.!?…]*$", "youtube"),
    (r"(?:ставьте лайки)[.!?…]*$", "youtube"),
    (r"(?:смотрите в описании)[.!?…]*$", "youtube"),
    (r"(?:поддержите канал)[.!?…]*$", "youtube"),
    (r"(?:приятного просмотра)[.!?…]*$", "youtube"),
    (r"(?:увидимся в следующем видео)[.!?…]*$", "youtube"),
    (r"(?:всем пока)[.!?…]*$", "youtube"),
    (r"(?:спасибо всем за внимание)[.!?…]*$", "youtube"),
    (r"(?:\.\s+)?спасибо\.?\s*$", "youtube"),
]


def _reject_catastrophic_pattern(pattern: str) -> None:
    """Raise ValueError if *pattern* contains a known catastrophic-backtracking construct.

    This is a **heuristic** guard — it catches the most common dangerous forms
    but is not a complete ReDoS prover.  The companion ``_MAX_MATCH_INPUT_LEN``
    cap provides defence-in-depth for any exotic patterns that slip through.

    Args:
        pattern: Raw regex string to inspect.

    Raises:
        ValueError: If the pattern matches a catastrophic-backtracking heuristic.
    """
    if _CATASTROPHIC_RE.search(pattern):
        raise ValueError(
            "Паттерн содержит конструкцию, вызывающую катастрофический откат (ReDoS): "
            f"{pattern!r}. Используйте простые выражения без вложенных квантификаторов."
        )


@dataclass
class HallucinationMatch:
    """Описание найденного совпадения с паттерном галлюцинации."""
    pattern: str
    matched_text: str
    position: int
    category: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HallucinationManager:
    """Управляет встроенными и пользовательскими паттернами галлюцинаций.

    Args:
        data_dir: Путь к директории данных. Если None — только in-memory хранение.
    """

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self._lock = threading.Lock()
        self._data_dir: Path | None = Path(data_dir) if data_dir else None
        self._persist_path: Path | None = (
            self._data_dir / "hallucination_patterns.json" if self._data_dir else None
        )

        # Встроенные паттерны (не изменяются)
        self._builtin: list[dict[str, Any]] = [
            {"pattern": pat, "category": cat, "builtin": True}
            for pat, cat in _BUILTIN_PATTERNS_RAW
        ]

        # Пользовательские паттерны (персистентные)
        self._custom: list[dict[str, Any]] = []
        self._load_custom()

        # Скомпилированные регексы: (pattern_str, compiled_re, category, is_builtin)
        self._compiled: list[tuple[str, re.Pattern, str, bool]] = []
        self._rebuild_compiled()

    # ── Персистентность ─────────────────────────────────────────────────────

    def _load_custom(self) -> None:
        """Загружает пользовательские паттерны из JSON-файла."""
        if self._persist_path is None or not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._custom = [
                    item for item in data
                    if isinstance(item, dict) and "pattern" in item
                ]
                logger.debug("Загружено %d пользовательских паттернов", len(self._custom))
        except Exception as exc:
            logger.warning("Не удалось загрузить hallucination_patterns.json: %s", exc)

    def _save_custom(self) -> None:
        """Сохраняет пользовательские паттерны в JSON-файл."""
        if self._persist_path is None:
            return
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            self._persist_path.write_text(
                json.dumps(self._custom, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Не удалось сохранить hallucination_patterns.json: %s", exc)

    def _rebuild_compiled(self) -> None:
        """Перекомпилирует список регексов из builtin + custom паттернов."""
        compiled = []
        for item in self._builtin:
            try:
                compiled.append((item["pattern"], re.compile(item["pattern"]), item["category"], True))
            except re.error as exc:
                logger.warning("Невалидный встроенный паттерн '%s': %s", item["pattern"], exc)
        for item in self._custom:
            try:
                compiled.append((item["pattern"], re.compile(item["pattern"]), item["category"], False))
            except re.error as exc:
                logger.warning("Невалидный пользовательский паттерн '%s': %s", item["pattern"], exc)
        self._compiled = compiled

    # ── Публичный API ────────────────────────────────────────────────────────

    def add_pattern(self, pattern: str, category: str = "custom") -> dict[str, Any]:
        """Добавляет пользовательский паттерн.

        Args:
            pattern: Строка регулярного выражения.
            category: Категория (по умолчанию "custom").

        Returns:
            dict с полями pattern, category, builtin.

        Raises:
            ValueError: Если паттерн уже существует, не является валидным regex,
                слишком длинный (> ``_MAX_PATTERN_LEN`` символов), или содержит
                конструкцию, вызывающую катастрофический откат (ReDoS).

        Security (Wave 1729 — ReDoS mitigation):
            1. Pattern length is capped at ``_MAX_PATTERN_LEN`` chars.
            2. ``_reject_catastrophic_pattern()`` rejects nested quantifiers and
               ambiguous alternations before the pattern is compiled or stored.
            3. At match time, input text is truncated to ``_MAX_MATCH_INPUT_LEN``
               characters — see ``check_text()`` / ``strip_hallucinations()``.
        """
        pattern = pattern.strip()
        if not pattern:
            raise ValueError("Паттерн не может быть пустым")

        # ── ReDoS mitigation layer 1: pattern length cap ──────────────────────
        if len(pattern) > _MAX_PATTERN_LEN:
            raise ValueError(
                f"Паттерн слишком длинный ({len(pattern)} символов, максимум {_MAX_PATTERN_LEN})"
            )

        # ── ReDoS mitigation layer 2: catastrophic-backtracking heuristic ─────
        _reject_catastrophic_pattern(pattern)

        # Валидация regex (syntax check — after safety guards so we fail fast)
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"Невалидное регулярное выражение: {exc}") from exc

        with self._lock:
            # Проверяем дубликаты (встроенные + пользовательские)
            all_existing = [item["pattern"] for item in self._builtin + self._custom]
            if pattern in all_existing:
                raise ValueError(f"Паттерн уже существует: {pattern!r}")

            entry = {"pattern": pattern, "category": category, "builtin": False}
            self._custom.append(entry)
            self._rebuild_compiled()
            self._save_custom()
            logger.info("Добавлен пользовательский паттерн галлюцинации: %r [%s]", pattern, category)
            return dict(entry)

    def remove_pattern(self, pattern: str) -> bool:
        """Удаляет пользовательский паттерн.

        Args:
            pattern: Строка регулярного выражения для удаления.

        Returns:
            True если паттерн найден и удалён, False если не найден.

        Raises:
            ValueError: При попытке удалить встроенный паттерн.
        """
        pattern = pattern.strip()
        with self._lock:
            # Проверяем встроенные
            builtin_patterns = [item["pattern"] for item in self._builtin]
            if pattern in builtin_patterns:
                raise ValueError(f"Нельзя удалить встроенный паттерн: {pattern!r}")

            original_len = len(self._custom)
            self._custom = [item for item in self._custom if item["pattern"] != pattern]
            if len(self._custom) == original_len:
                return False

            self._rebuild_compiled()
            self._save_custom()
            logger.info("Удалён пользовательский паттерн галлюцинации: %r", pattern)
            return True

    def list_patterns(self) -> list[dict[str, Any]]:
        """Возвращает все паттерны (встроенные + пользовательские).

        Returns:
            Список dict с полями: pattern, category, builtin.
        """
        with self._lock:
            return [dict(item) for item in self._builtin + self._custom]

    def check_text(self, text: str) -> list[HallucinationMatch]:
        """Проверяет текст на совпадения с паттернами галлюцинаций.

        Поиск ведётся по lowercased версии текста для case-insensitive сравнения.

        Security (Wave 1729 — ReDoS mitigation layer 3):
            Input text is truncated to ``_MAX_MATCH_INPUT_LEN`` characters before
            regex matching.  This bounds the worst-case backtracking depth for any
            pattern, providing defence-in-depth alongside the add-time checks.

        Args:
            text: Исходный текст для проверки.

        Returns:
            Список HallucinationMatch для всех найденных совпадений.
        """
        if not text:
            return []

        # ReDoS mitigation layer 3: cap input length before regex execution
        lowered = text.lower()
        if len(lowered) > _MAX_MATCH_INPUT_LEN:
            lowered = lowered[:_MAX_MATCH_INPUT_LEN]

        matches: list[HallucinationMatch] = []

        with self._lock:
            compiled_snapshot = list(self._compiled)

        for pat_str, compiled_re, category, _ in compiled_snapshot:
            m = compiled_re.search(lowered)
            if m:
                matches.append(HallucinationMatch(
                    pattern=pat_str,
                    matched_text=text[m.start():m.end()],
                    position=m.start(),
                    category=category,
                ))

        return matches

    def strip_hallucinations(self, text: str) -> str:
        """Удаляет первое найденное совпадение с паттерном галлюцинации из текста.

        Поведение аналогично TextUtils._strip_hallucinations, но использует
        объединённый список встроенных + пользовательских паттернов.

        Security (Wave 1729 — ReDoS mitigation layer 3):
            Input text is truncated to ``_MAX_MATCH_INPUT_LEN`` characters before
            regex matching.  See ``check_text()`` for rationale.

        Args:
            text: Исходный текст.

        Returns:
            Очищенный текст.
        """
        if not text:
            return text

        # ReDoS mitigation layer 3: cap input length before regex execution
        lowered = text.lower()
        if len(lowered) > _MAX_MATCH_INPUT_LEN:
            lowered = lowered[:_MAX_MATCH_INPUT_LEN]

        with self._lock:
            compiled_snapshot = list(self._compiled)

        for _, compiled_re, _, _ in compiled_snapshot:
            m = compiled_re.search(lowered)
            if not m:
                continue
            if m.start() <= 0:
                return ""
            return text[:m.start()].rstrip(" .,!?:;")

        return text
