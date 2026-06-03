"""HotwordDetector — обнаружение ключевых слов в транскрипциях Krab Ear.

Персистирует список горячих слов в {data_dir}/hotwords.json.
Использует скомпилированные regex для эффективного поиска.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.HotwordDetector")

_CONTEXT_RADIUS = 20  # символов вокруг совпадения


@dataclass
class HotwordMatch:
    word: str
    position: int
    category: str
    context: str  # до 20 символов до и после совпадения


class HotwordDetector:
    """Детектор горячих слов/фраз в транскрибированном тексте."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._data_dir / "hotwords.json"
        self._lock = threading.Lock()
        # {word_lower_or_original: {"word": str, "category": str, "case_sensitive": bool}}
        self._hotwords: dict[str, dict[str, Any]] = {}
        self._patterns: dict[str, re.Pattern] = {}
        self._load()

    # ------------------------------------------------------------------
    # Персистенция
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict) and "word" in entry:
                        self._register(
                            word=entry["word"],
                            category=entry.get("category", "alert"),
                            case_sensitive=bool(entry.get("case_sensitive", False)),
                        )
        except Exception:
            logger.exception("Не удалось загрузить hotwords.json")

    def _save(self) -> None:
        try:
            entries = list(self._hotwords.values())
            self._path.write_text(
                json.dumps(entries, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("Не удалось сохранить hotwords.json")

    # ------------------------------------------------------------------
    # Внутренние
    # ------------------------------------------------------------------

    def _key(self, word: str, case_sensitive: bool) -> str:
        return word if case_sensitive else word.lower()

    def _register(self, word: str, category: str, case_sensitive: bool) -> None:
        key = self._key(word, case_sensitive)
        self._hotwords[key] = {
            "word": word,
            "category": category,
            "case_sensitive": case_sensitive,
        }
        flags = 0 if case_sensitive else re.IGNORECASE
        self._patterns[key] = re.compile(r"\b" + re.escape(word) + r"\b", flags)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def add_hotword(
        self,
        word: str,
        category: str = "alert",
        case_sensitive: bool = False,
    ) -> None:
        """Добавляет горячее слово. Перезаписывает если уже существует."""
        word = word.strip()
        if not word:
            raise ValueError("word не может быть пустым")
        with self._lock:
            self._register(word, category, case_sensitive)
            self._save()
        logger.debug("Hotword добавлен: %r (category=%s)", word, category)

    def remove_hotword(self, word: str) -> bool:
        """Удаляет горячее слово (case-insensitive по умолчанию, затем точное).
        Возвращает True если слово было найдено и удалено."""
        word = word.strip()
        with self._lock:
            # Попробуем найти по нечувствительному ключу сначала
            key_lower = word.lower()
            key_exact = word
            removed = False
            for key in (key_lower, key_exact):
                if key in self._hotwords:
                    del self._hotwords[key]
                    self._patterns.pop(key, None)
                    removed = True
                    break
            if removed:
                self._save()
        return removed

    def get_hotwords(self) -> list[dict[str, Any]]:
        """Возвращает список всех зарегистрированных горячих слов."""
        with self._lock:
            return list(self._hotwords.values())

    def check_text(self, text: str) -> list[HotwordMatch]:
        """Ищет все горячие слова в тексте. Возвращает список совпадений."""
        if not text:
            return []
        matches: list[HotwordMatch] = []
        with self._lock:
            patterns = list(self._patterns.items())
        for key, pattern in patterns:
            entry = self._hotwords.get(key)
            if entry is None:
                continue
            for m in pattern.finditer(text):
                start = m.start()
                end = m.end()
                ctx_start = max(0, start - _CONTEXT_RADIUS)
                ctx_end = min(len(text), end + _CONTEXT_RADIUS)
                context = text[ctx_start:ctx_end]
                matches.append(
                    HotwordMatch(
                        word=entry["word"],
                        position=start,
                        category=entry["category"],
                        context=context,
                    )
                )
        # Сортируем по позиции
        matches.sort(key=lambda x: x.position)
        return matches

    # ------------------------------------------------------------------
    # IPC-обработчики
    # ------------------------------------------------------------------

    def handle_add_hotword(self, params: dict[str, Any]) -> dict[str, Any]:
        word = str(params.get("word", "")).strip()
        if not word:
            return {"ok": False, "error": "word обязателен"}
        category = str(params.get("category", "alert"))
        case_sensitive = bool(params.get("case_sensitive", False))
        self.add_hotword(word, category=category, case_sensitive=case_sensitive)
        return {"ok": True, "word": word, "category": category, "case_sensitive": case_sensitive}

    def handle_remove_hotword(self, params: dict[str, Any]) -> dict[str, Any]:
        word = str(params.get("word", "")).strip()
        if not word:
            return {"ok": False, "error": "word обязателен"}
        removed = self.remove_hotword(word)
        return {"ok": True, "removed": removed}

    def handle_get_hotwords(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "hotwords": self.get_hotwords()}

    def handle_check_hotwords(self, params: dict[str, Any]) -> dict[str, Any]:
        text = str(params.get("text", ""))
        matches = self.check_text(text)
        return {
            "ok": True,
            "matches": [asdict(m) for m in matches],
            "count": len(matches),
        }

    def clear(self) -> None:
        """Очищает все горячие слова из памяти (in-memory purge gap, wave-26).

        Используется при ``purge_all_data`` — disk-файл уже удалён HistoryService;
        этот метод очищает живые in-memory коллекции ``_hotwords`` и ``_patterns``,
        чтобы горячие слова не выжили до перезапуска процесса.

        Потокобезопасно: использует тот же ``self._lock``, что и остальные методы.
        """
        with self._lock:
            self._hotwords.clear()
            self._patterns.clear()
        logger.debug("HotwordDetector.clear(): in-memory hotwords wiped (privacy purge)")
