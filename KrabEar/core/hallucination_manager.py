"""HallucinationManager — управление паттернами галлюцинаций Krab Ear.

Объединяет встроенные паттерны (из TextUtils) с пользовательскими.
Пользовательские паттерны сохраняются в {data_dir}/hallucination_patterns.json.
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
            ValueError: Если паттерн уже существует или не является валидным regex.
        """
        pattern = pattern.strip()
        if not pattern:
            raise ValueError("Паттерн не может быть пустым")

        # Валидация regex
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

        Args:
            text: Исходный текст для проверки.

        Returns:
            Список HallucinationMatch для всех найденных совпадений.
        """
        if not text:
            return []

        lowered = text.lower()
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

        Args:
            text: Исходный текст.

        Returns:
            Очищенный текст.
        """
        if not text:
            return text

        lowered = text.lower()

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
