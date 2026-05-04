"""Постоянное хранилище пользовательского словаря STT (vocabulary).

Формат файла: {"words": ["word1", "word2"], "updated_at": "ISO8601"}

Особенности:
- Сохранение/загрузка через атомарный tmp→replace.
- Merge с программными добавлениями (без дублей).
- Graceful обработка пустого или повреждённого файла.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from core.parsing_utils import safe_json_loads

logger = logging.getLogger("KrabEar.Backend.VocabularyStore")

_VOCABULARY_FILENAME = "vocabulary.json"


class VocabularyStore:
    """JSON-хранилище пользовательского словаря для подсказок Whisper.

    Args:
        data_dir: директория, где хранится vocabulary.json.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.path = data_dir / _VOCABULARY_FILENAME
        data_dir.mkdir(parents=True, exist_ok=True)
        # Phase B.2 — error_bus late-injection

    def _push_error(self, code: str, message_debug: str, severity: str | None = None) -> None:
        """Push KrabError to attached ErrorBus if available. Late-injected attribute."""
        error_bus = getattr(self, "_error_bus", None)
        if error_bus is None:
            return
        try:
            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY
            entry = ERROR_REGISTRY.get(code, {})
            err = KrabError(
                severity=severity or entry.get("severity", "warn"),
                component="vocabulary",
                code=code,
                message_user=entry.get("user_msg_ru", "Ошибка словаря"),
                message_debug=message_debug,
                timestamp=datetime.now(timezone.utc),
                context={"path": str(self.path)},
                actionable=entry.get("actionable", False),
                action_id=entry.get("action_id"),
            )
            error_bus.push(err)
        except Exception:
            logger.exception("error_bus.push failed for code=%s", code)

    # ── Public API ──────────────────────────────────────────────────────────

    def load(self) -> List[str]:
        """Загружает словарь из файла.

        Возвращает пустой список при отсутствии или повреждении файла.
        """
        if not self.path.exists():
            return []

        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("vocabulary.json повреждён, возвращаем пустой список: %s", exc)
            # Phase B.2: vocabulary.load_fail — file read error
            self._push_error(
                "vocabulary.load_fail",
                f"OSError reading vocabulary.json: {exc}",
                severity="warn",
            )
            return []
        payload = safe_json_loads(raw, default=None, context="vocabulary.json")
        if payload is None:
            logger.warning("vocabulary.json повреждён, возвращаем пустой список")
            # Phase B.2: vocabulary.load_fail — corrupted JSON
            self._push_error(
                "vocabulary.load_fail",
                "vocabulary.json parse error (invalid JSON) — STT will run without vocabulary bias",
                severity="warn",
            )
            return []

        if not isinstance(payload, dict):
            logger.warning("vocabulary.json: неожиданный формат (не dict), возвращаем пустой список")
            return []

        words = payload.get("words", [])
        if not isinstance(words, list):
            logger.warning("vocabulary.json: поле 'words' не список, возвращаем пустой список")
            return []

        return [w for w in words if isinstance(w, str) and w.strip()]

    def save(self, words: List[str]) -> None:
        """Сохраняет словарь атомарно (tmp→replace).

        Дубли удаляются, слова сортируются для детерминированности.
        """
        unique = sorted({w.strip() for w in words if isinstance(w, str) and w.strip()})
        payload = {
            "words": unique,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        tmp_path = self.path.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(self.path)
        except OSError as exc:
            logger.error("Ошибка сохранения vocabulary.json: %s", exc)
            raise

    def merge(self, extra: List[str]) -> List[str]:
        """Объединяет загруженный словарь с дополнительными словами.

        Возвращает объединённый список без дублей (не сохраняет на диск).
        """
        base = set(self.load())
        base.update(w.strip() for w in extra if isinstance(w, str) and w.strip())
        return sorted(base)

    def add_words(self, new_words: List[str]) -> List[str]:
        """Добавляет слова к существующему словарю и сохраняет на диск.

        Возвращает итоговый список.
        """
        current = set(self.load())
        current.update(w.strip() for w in new_words if isinstance(w, str) and w.strip())
        merged = sorted(current)
        self.save(merged)
        return merged

    def remove_words(self, words_to_remove: List[str]) -> List[str]:
        """Удаляет слова из словаря и сохраняет на диск.

        Возвращает итоговый список.
        """
        remove_set = {w.strip() for w in words_to_remove if isinstance(w, str) and w.strip()}
        current = [w for w in self.load() if w not in remove_set]
        self.save(current)
        return current
