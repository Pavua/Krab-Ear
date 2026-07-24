"""HallucinationManager — управление паттернами галлюцинаций Krab Ear.

Объединяет встроенные паттерны (из TextUtils) с пользовательскими.
Пользовательские паттерны сохраняются в {data_dir}/hallucination_patterns.json.

Security note (ReDoS mitigation, Wave 1729 + Wave 1730 bypass fix)
-------------------------------------------------------------------
User-supplied patterns are compiled as regexes and run against arbitrary
transcript text.  An accidental or malicious catastrophic-backtracking pattern
(e.g. ``(a+)+$``, ``(.*a){20}``) can cause the CPython ``re`` engine to hang
for minutes on pathological input, freezing the STT pipeline.

Three-layer defence applied to *custom* patterns only (built-ins are trusted):

1. **Length cap** — pattern strings longer than ``_MAX_PATTERN_LEN`` chars are
   rejected at ``add_pattern()`` time.
2. **Structural nested-quantifier scanner** — ``_reject_catastrophic_pattern()``
   performs a paren-balanced structural scan of the pattern to detect a
   quantifier (``+``, ``*``, ``{n,}``) applied to a group (capturing, non-
   capturing ``(?:...)``, or other inline-flag groups) whose body itself
   contains a quantifier — at ANY nesting depth.  This correctly rejects all
   known bypass variants including ``((?:a)+)+``, ``(?:(a+))+``, deeply
   nested forms, and the classic ``(.*a){n}`` repeated-group form.

   Wave 1729's original flat-regex heuristic was blind to non-capturing
   groups: ``((?:a)+)+$`` and ``(?:(a+))+$`` slipped past it because the
   flat regex only scanned for ``(...)+`` where ``[^)]`` could not cross group
   boundaries.  The structural scan operates directly on the parse-tree shape
   and is immune to this class of bypass.

3. **Input-length cap at match time** — ``check_text()`` / ``strip_hallucinations()``
   truncate the lowercased text to ``_MAX_MATCH_INPUT_LEN`` characters before
   running any regex.  This bounds the worst-case backtracking depth even if
   an exotic catastrophic pattern slips through heuristic detection.  NOTE:
   this cap does NOT protect against the bypass variants (they hang on input
   as short as 26–27 chars), so the structural scan is the primary defence.

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

# Импортируем общий ReDoS-safe хелпер (Wave 1735).
# Используем его вместо дублирования логики — hallucination_manager остаётся
# основным consumer пользовательских паттернов, а core.safe_regex предоставляет
# переиспользуемую защиту для любых других мест в кодовой базе.
from core.safe_regex import compile_safe as _compile_safe, run_with_timeout as _run_with_timeout  # noqa: E402

logger = logging.getLogger("KrabEar.Core.HallucinationManager")

# ── ReDoS-mitigation constants ───────────────────────────────────────────────

# Maximum character length allowed for a user-supplied pattern string.
_MAX_PATTERN_LEN: int = 500

# Maximum character length of the text slice passed to re.search at match time.
# Transcripts are typically < 2 KB; 4 KB is generous headroom that still
# eliminates the pathological-input dimension of a catastrophic pattern.
# NOTE: this cap does NOT protect against nested-quantifier bypass patterns
# that hang on inputs as short as 26–27 chars — the structural scan is the
# primary defence for those.
_MAX_MATCH_INPUT_LEN: int = 4096

# Quantifier characters that follow a group and make it catastrophic.
_QUANTIFIER_CHARS = frozenset("+*{")

# Regex flag prefix characters that immediately follow "(?".
# (?:...) (?i:...) (?im:...) (?=...) (?!...) (?<=...) (?<!...) — all open a group.
_GROUP_FLAG_CHARS = frozenset(":imsxu=<!>")


def _body_is_repeatable(pattern: str, start: int, end: int) -> bool:
    """Return True if the group body ``pattern[start:end]`` can match strings of
    variable length due to internal quantification, making the outer group
    dangerous to quantify.

    A body is considered "repeatable" (catastrophic-backtracking-prone when
    the outer group is also quantified) if it contains:

    1. A bare quantifier (``+``, ``*``, ``{``) at top-level depth 0 —
       e.g. body ``a+`` has ``+`` at depth 0.
    2. A sub-group that is itself followed by a quantifier at depth 0 —
       e.g. body ``(a)+`` has ``(a)`` followed by ``+`` after its closing paren.
    3. A sub-group whose own body is repeatable (recursive) —
       e.g. body ``(a+)`` contains sub-group ``(a+)`` whose body ``a+`` is
       repeatable.  This is the ``(?:(a+))+`` bypass case: the outer NC group
       wraps a capturing group with an internal quantifier, which can still
       match variable-length strings and cause catastrophic backtracking.

    Args:
        pattern: Full pattern string.
        start: Index of the first character inside the group body.
        end: Exclusive end index (the closing ``)``'s position).

    Returns:
        True if the body is repeatable in any of the three senses above.
    """
    depth = 0
    in_char_class = False
    i = start
    while i < end:
        ch = pattern[i]
        if in_char_class:
            if ch == "\\" and i + 1 < end:
                i += 2
                continue
            if ch == "]":
                in_char_class = False
        else:
            if ch == "\\" and i + 1 < end:
                i += 2
                continue
            if ch == "[":
                in_char_class = True
            elif ch == "(":
                depth += 1
                if depth == 1:
                    # Find the matching ')' for this sub-group.
                    sub_start = i
                    sub_depth = 1
                    k = i + 1
                    sub_in_class = False
                    while k < end and sub_depth > 0:
                        c = pattern[k]
                        if sub_in_class:
                            if c == "\\" and k + 1 < end:
                                k += 2
                                continue
                            if c == "]":
                                sub_in_class = False
                        else:
                            if c == "\\" and k + 1 < end:
                                k += 2
                                continue
                            if c == "[":
                                sub_in_class = True
                            elif c == "(":
                                sub_depth += 1
                            elif c == ")":
                                sub_depth -= 1
                        k += 1
                    if sub_depth != 0:
                        # Unbalanced — skip ahead.
                        i += 1
                        continue
                    # sub_close = k - 1 (index of closing ')')
                    sub_close = k - 1
                    # Determine sub-group body start (skip inline-flag prefix).
                    sub_body_start = sub_start + 1
                    if sub_body_start < sub_close and pattern[sub_body_start] == "?":
                        sk = sub_body_start + 1
                        while sk < sub_close and pattern[sk] in _GROUP_FLAG_CHARS:
                            sk += 1
                        sub_body_start = sk
                    # Case 2: sub-group is followed by a quantifier in the body.
                    after_sub = k  # first char after sub ')'
                    if after_sub < end and pattern[after_sub] in _QUANTIFIER_CHARS:
                        return True
                    # Case 3: sub-group body is itself repeatable (recursive check).
                    if _body_is_repeatable(pattern, sub_body_start, sub_close):
                        return True
                    # Skip to after the sub-group close (outer loop will continue).
                    i = sub_close  # will be incremented at bottom of loop
            elif ch == ")":
                if depth > 0:
                    depth -= 1
            elif depth == 0 and ch in _QUANTIFIER_CHARS:
                # Case 1: bare quantifier at top level of this body.
                return True
        i += 1
    return False


def _group_body_has_quantifier(pattern: str, start: int, end: int) -> bool:
    """Thin wrapper kept for API compatibility — delegates to ``_body_is_repeatable``."""
    return _body_is_repeatable(pattern, start, end)


def _scan_for_nested_quantifiers(pattern: str) -> bool:
    """Perform a paren-balanced structural scan for nested-quantifier patterns.

    Detects any group (capturing, non-capturing ``(?:...)``, inline-flag
    variants, lookarounds) whose *body* contains a quantifier AND which is
    itself followed by a quantifier.  This catches the full class of
    catastrophic-backtracking structures including:

    - ``(a+)+``          — classic nested plus
    - ``(a*)*``          — nested star
    - ``((?:a)+)+``      — non-capturing inner group (Wave 1729 bypass)
    - ``(?:(a+))+``      — non-capturing outer group (Wave 1729 bypass)
    - ``((?:[a-z])+)+``  — char-class in non-capturing group (Wave 1729 bypass)
    - ``((?:a)+){5,}``   — curly quantifier on NC-wrapped repeated group
    - ``((a+)+)``        — deeply nested

    Args:
        pattern: Raw regex string to inspect.

    Returns:
        True if a catastrophic nested-quantifier structure is found.
    """
    length = len(pattern)
    i = 0
    in_char_class = False

    while i < length:
        ch = pattern[i]

        if in_char_class:
            if ch == "\\" and i + 1 < length:
                i += 2
                continue
            if ch == "]":
                in_char_class = False
            i += 1
            continue

        if ch == "\\" and i + 1 < length:
            i += 2
            continue

        if ch == "[":
            in_char_class = True
            i += 1
            continue

        if ch != "(":
            i += 1
            continue

        # Found a group opening — locate its matching closing paren.
        group_start = i
        depth = 1
        j = i + 1
        inner_in_class = False
        while j < length and depth > 0:
            c = pattern[j]
            if inner_in_class:
                if c == "\\" and j + 1 < length:
                    j += 2
                    continue
                if c == "]":
                    inner_in_class = False
            else:
                if c == "\\" and j + 1 < length:
                    j += 2
                    continue
                if c == "[":
                    inner_in_class = True
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
            j += 1

        if depth != 0:
            # Unbalanced parens — let the compiler catch it; skip.
            i += 1
            continue

        # j is now the index *after* the closing ")".
        close_paren_idx = j - 1  # index of ")"
        body_start = group_start + 1

        # Skip inline-flag / non-capturing prefix: (?...) starts with "(?".
        if body_start < close_paren_idx and pattern[body_start] == "?":
            k = body_start + 1
            while k < close_paren_idx and pattern[k] in _GROUP_FLAG_CHARS:
                k += 1
            # k is now the first char of the real body (may still be empty).
            body_start = k

        # Check if this group is followed by a quantifier.
        after = j  # first char after ")"
        # Skip optional whitespace that re.VERBOSE would eat — but since user
        # patterns are vanilla patterns, we don't expect VERBOSE; skip anyway.
        while after < length and pattern[after] == " ":
            after += 1

        if after < length and pattern[after] in _QUANTIFIER_CHARS:
            # Group is quantified — check if its body contains a quantifier too.
            if _group_body_has_quantifier(pattern, body_start, close_paren_idx):
                return True

        # Also recurse into the group body for deeply nested structures.
        # The outer while-loop will naturally scan all positions including
        # group interiors since we advance i by 1 each iteration.
        i += 1

    return False


def _reject_catastrophic_pattern(pattern: str) -> None:
    """Raise ValueError if *pattern* contains a known catastrophic-backtracking construct.

    Uses a paren-balanced structural scan (``_scan_for_nested_quantifiers``)
    that correctly handles non-capturing groups (``(?:...)``), inline-flag
    groups, and arbitrary nesting depth.  The previous flat-regex heuristic
    (Wave 1729) was bypassed by ``((?:a)+)+$`` and ``(?:(a+))+$`` because
    ``[^)]`` cannot cross group boundaries — the structural scan eliminates
    that blind spot.

    The companion ``_MAX_MATCH_INPUT_LEN`` cap provides defence-in-depth for
    any exotic patterns that slip through, but does NOT protect against the
    bypass class (which hangs on ~26-char inputs — well below 4096).

    Args:
        pattern: Raw regex string to inspect.

    Raises:
        ValueError: If the pattern contains a nested-quantifier structure
            that causes catastrophic backtracking.
    """
    if _scan_for_nested_quantifiers(pattern):
        raise ValueError(
            "Паттерн содержит конструкцию, вызывающую катастрофический откат (ReDoS): "
            f"{pattern!r}. Используйте простые выражения без вложенных квантификаторов."
        )


# ── Встроенные паттерны (из core/utils.py _HALLUCINATION_PATTERNS) ───────────
_BUILTIN_PATTERNS_RAW: list[tuple[str, str]] = [
    (r"(?:спасибо за просмотр|спасибо за внимание)[.!?…]*$", "youtube"),
    # W1894: см. пояснение в core/utils.py — списки обязаны совпадать (есть тест паритета).
    (r"(?:субтитры (?:сделал|сделала|создал|создала|создавал|создавала|делал|делала) [^.!?…]{1,40})[.!?…]*$", "youtube"),
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
        from core.atomic_io import atomic_write_text
        if self._persist_path is None:
            return
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                self._persist_path,
                json.dumps(self._custom, ensure_ascii=False, indent=2)
            )
        except Exception as exc:
            logger.warning("Не удалось сохранить hallucination_patterns.json: %s", exc)

    def _rebuild_compiled(self) -> None:
        """Перекомпилирует список регексов из builtin + custom паттернов."""
        compiled = []
        for item in self._builtin:
            # Встроенные паттерны доверенные — компилируем напрямую.
            try:
                compiled.append((item["pattern"], re.compile(item["pattern"]), item["category"], True))
            except re.error as exc:
                logger.warning("Невалидный встроенный паттерн '%s': %s", item["pattern"], exc)
        for item in self._custom:
            # Пользовательские паттерны — через _compile_safe (ReDoS-защита, Wave 1735).
            try:
                compiled.append((
                    item["pattern"],
                    _compile_safe(item["pattern"], max_pattern_len=_MAX_PATTERN_LEN),
                    item["category"],
                    False,
                ))
            except (re.error, ValueError) as exc:
                logger.warning(
                    "Пользовательский паттерн отклонён при перекомпиляции: %s",
                    exc,
                    extra={"pattern_len": len(item["pattern"])},
                )
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

        # ── ReDoS mitigation layers 1+2 (Wave 1729/1730/1735): делегируем в
        #    core.safe_regex.compile_safe — длина + структурный сканер +
        #    синтаксическая проверка в одном вызове.
        try:
            _compile_safe(pattern, max_pattern_len=_MAX_PATTERN_LEN)
        except ValueError:
            # Пробрасываем ValueError напрямую (уже содержит описание причины).
            raise
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

        for pat_str, compiled_re, category, is_builtin in compiled_snapshot:
            # Встроенные паттерны доверенные — прямой вызов без таймаута.
            # Пользовательские — через run_with_timeout (Wave 1735).
            if is_builtin:
                m = compiled_re.search(lowered)
            else:
                m = _run_with_timeout(compiled_re, lowered, timeout_sec=1.0)
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

        for _, compiled_re, _, is_builtin in compiled_snapshot:
            # Встроенные — прямой вызов; пользовательские — с таймаутом (Wave 1735).
            if is_builtin:
                m = compiled_re.search(lowered)
            else:
                m = _run_with_timeout(compiled_re, lowered, timeout_sec=1.0)
            if not m:
                continue
            if m.start() <= 0:
                return ""
            return text[:m.start()].rstrip(" .,!?:;")

        return text
