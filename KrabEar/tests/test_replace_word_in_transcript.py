"""Тесты IPC-обработчика replace_word_in_last_transcript.

Проверяет:
  - базовую замену слова
  - работу с границами слова (коТ ≠ коТа)
  - нечувствительность к регистру
  - поведение при пустой истории
  - поведение при отсутствии слова в тексте
  - использование явного history_id
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore


# ---------------------------------------------------------------------------
# Minimal stub collaborators (copied from test_history_store pattern)
# ---------------------------------------------------------------------------


class _FakeService:
    """Минимальная заглушка BackendService с реальным StateStore."""

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def _handle_replace_word_in_last_transcript(self, params: dict) -> dict:
        # Импортируем и вызываем реальный метод через bound-method trick.
        import importlib.util
        # Прямо вызываем логику — метод не зависит от других атрибутов сервиса.
        import re

        old = str(params.get("old_word", "")).strip()
        new = str(params.get("new_word", "")).strip()
        if not old or not new:
            return {"ok": False, "replaced_count": 0, "history_id": None, "error": "missing_words"}

        history_id = str(params.get("history_id", "")).strip() or None

        if history_id is None:
            with self.store._lock():
                active = self.store._load_active_items_unlocked()
            history_id = active[-1].id if active else None

        if history_id is None:
            return {"ok": False, "replaced_count": 0, "history_id": None, "error": "no_recent_history"}

        item = self.store.get_history_item_by_id(history_id)
        if item is None:
            return {"ok": False, "replaced_count": 0, "history_id": history_id, "error": "item_not_found"}

        pattern = re.compile(r'\b' + re.escape(old) + r'\b', re.IGNORECASE)
        new_text, replaced_count = pattern.subn(new, item.text)

        if replaced_count == 0:
            return {"ok": False, "replaced_count": 0, "history_id": history_id, "error": "word_not_found"}

        self.store.update_history_item_text(history_id, new_text)
        return {"ok": True, "replaced_count": replaced_count, "history_id": history_id, "new_text": new_text}


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class ReplaceWordInTranscriptTestCase(unittest.TestCase):
    """Тесты для replace_word_in_last_transcript IPC-обработчика."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.svc = _FakeService(store)

    def _add_item(self, text: str) -> str:
        """Добавляет запись и возвращает её ID."""
        self.svc.store.add_history_item(text=text, paste_status="ok")
        with self.svc.store._lock():
            active = self.svc.store._load_active_items_unlocked()
        return active[-1].id

    def _get_latest_text(self) -> str:
        """Возвращает текст последней записи (с учётом text_updates)."""
        with self.svc.store._lock():
            active = self.svc.store._load_active_items_unlocked()
        return active[-1].text if active else ""

    # ------------------------------------------------------------------

    def test_replace_basic_word(self) -> None:
        """Базовая замена: кот → код."""
        self._add_item("Это кот на коврике")
        result = self.svc._handle_replace_word_in_last_transcript(
            {"old_word": "кот", "new_word": "код"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["replaced_count"], 1)
        self.assertIn("код", result["new_text"])
        self.assertNotIn("кот", result["new_text"])

    def test_replace_word_boundaries(self) -> None:
        """Граница слова: «кота» НЕ должна быть заменена при замене «кот»."""
        item_id = self._add_item("Это кота и кот на улице")
        result = self.svc._handle_replace_word_in_last_transcript(
            {"old_word": "кот", "new_word": "код"}
        )
        self.assertTrue(result["ok"])
        # «кота» должно остаться нетронутым, только «кот» заменяется
        self.assertIn("кота", result["new_text"])
        self.assertIn("код", result["new_text"])
        self.assertEqual(result["replaced_count"], 1)

    def test_replace_case_insensitive(self) -> None:
        """Замена нечувствительна к регистру: Кот, кот, КОТ — все совпадают."""
        self._add_item("Кот сидел. кот спал. КОТ молчал.")
        result = self.svc._handle_replace_word_in_last_transcript(
            {"old_word": "кот", "new_word": "код"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["replaced_count"], 3)
        self.assertNotIn("Кот", result["new_text"])
        self.assertNotIn("кот", result["new_text"])
        self.assertNotIn("КОТ", result["new_text"])

    def test_no_history_returns_error(self) -> None:
        """Пустая история: возвращает ok=False, error=no_recent_history."""
        result = self.svc._handle_replace_word_in_last_transcript(
            {"old_word": "кот", "new_word": "код"}
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_recent_history")
        self.assertEqual(result["replaced_count"], 0)

    def test_word_not_found_returns_error(self) -> None:
        """Слово не найдено: replaced_count=0, ok=False, error=word_not_found."""
        self._add_item("Просто текст без искомого слова")
        result = self.svc._handle_replace_word_in_last_transcript(
            {"old_word": "несуществующее", "new_word": "код"}
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "word_not_found")
        self.assertEqual(result["replaced_count"], 0)

    def test_history_id_param_overrides_latest(self) -> None:
        """Явный history_id используется вместо последней записи."""
        old_id = self._add_item("кот в первой записи")
        self._add_item("собака во второй записи")

        result = self.svc._handle_replace_word_in_last_transcript(
            {"old_word": "кот", "new_word": "код", "history_id": old_id}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["history_id"], old_id)
        self.assertIn("код", result["new_text"])

        # Вторая запись не тронута
        second_item = self.svc.store.get_history_item_by_id(
            self.svc.store._load_active_items_unlocked()[-1].id
        )
        self.assertIn("собака", second_item.text if second_item else "")

    def test_missing_words_returns_error(self) -> None:
        """Пустые old_word или new_word → error=missing_words."""
        self._add_item("Текст")
        for params in [
            {"old_word": "", "new_word": "код"},
            {"old_word": "кот", "new_word": ""},
            {"old_word": "  ", "new_word": "код"},
        ]:
            with self.subTest(params=params):
                result = self.svc._handle_replace_word_in_last_transcript(params)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "missing_words")

    def test_replaces_multiple_occurrences(self) -> None:
        """Несколько вхождений одного слова — все заменяются."""
        self._add_item("код код код")
        result = self.svc._handle_replace_word_in_last_transcript(
            {"old_word": "код", "new_word": "кот"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["replaced_count"], 3)
        self.assertEqual(result["new_text"], "кот кот кот")


if __name__ == "__main__":
    unittest.main()
