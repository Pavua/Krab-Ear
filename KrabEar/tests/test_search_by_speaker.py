"""Тесты handle_search_by_speaker в HistoryService."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.history_service import HistoryService
from backend.state_store import StateStore


def _make_store(tmp_dir: str) -> StateStore:
    return StateStore(Path(tmp_dir))


def _add_item(store: StateStore, text: str, diarization: dict | None = None) -> str:
    item = store.add_history_item(text=text, diarization=diarization)
    return item.id


def _diarization_with(*speakers: str) -> dict:
    """Создаёт минимальную структуру диаризации с переданными спикерами."""
    segments = [{"speaker": s, "start": i * 1.0, "end": (i + 1) * 1.0} for i, s in enumerate(speakers)]
    return {"speaker_segments": segments, "speaker_turns": []}


class TestSearchBySpeaker(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = _make_store(self._tmpdir.name)
        self.svc = HistoryService(store=self.store)

    def tearDown(self):
        self._tmpdir.cleanup()

    # ------------------------------------------------------------------
    # 1. Базовый поиск — один спикер
    # ------------------------------------------------------------------
    def test_finds_item_with_matching_speaker(self):
        _add_item(self.store, "Привет мир", _diarization_with("SPEAKER_00"))
        result = self.svc.handle_search_by_speaker({"speaker": "SPEAKER_00"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["text"], "Привет мир")

    # ------------------------------------------------------------------
    # 2. Несколько спикеров в одной записи — ищем каждого
    # ------------------------------------------------------------------
    def test_finds_item_when_multiple_speakers(self):
        _add_item(self.store, "Разговор двух", _diarization_with("SPEAKER_00", "SPEAKER_01"))
        r0 = self.svc.handle_search_by_speaker({"speaker": "SPEAKER_00"})
        r1 = self.svc.handle_search_by_speaker({"speaker": "SPEAKER_01"})
        self.assertEqual(r0["count"], 1)
        self.assertEqual(r1["count"], 1)

    # ------------------------------------------------------------------
    # 3. Записи без диаризации не попадают в результат
    # ------------------------------------------------------------------
    def test_skips_items_without_diarization(self):
        _add_item(self.store, "Без диаризации", diarization=None)
        result = self.svc.handle_search_by_speaker({"speaker": "SPEAKER_00"})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["items"], [])

    # ------------------------------------------------------------------
    # 4. Поиск несуществующего спикера
    # ------------------------------------------------------------------
    def test_no_match_returns_empty(self):
        _add_item(self.store, "Только первый спикер", _diarization_with("SPEAKER_00"))
        result = self.svc.handle_search_by_speaker({"speaker": "SPEAKER_99"})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["items"], [])

    # ------------------------------------------------------------------
    # 5. Отсутствие параметра speaker вызывает ошибку
    # ------------------------------------------------------------------
    def test_missing_speaker_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_search_by_speaker({})

    # ------------------------------------------------------------------
    # 6. Лимит результатов соблюдается
    # ------------------------------------------------------------------
    def test_limit_is_respected(self):
        for i in range(10):
            _add_item(self.store, f"Запись {i}", _diarization_with("SPEAKER_00"))
        result = self.svc.handle_search_by_speaker({"speaker": "SPEAKER_00", "limit": 3})
        self.assertEqual(result["count"], 3)
        self.assertEqual(len(result["items"]), 3)

    # ------------------------------------------------------------------
    # 7. Несколько записей — возвращаются все совпадения (в порядке новых)
    # ------------------------------------------------------------------
    def test_multiple_matching_items(self):
        _add_item(self.store, "Первая", _diarization_with("SPEAKER_01"))
        _add_item(self.store, "Вторая", _diarization_with("SPEAKER_00"))
        _add_item(self.store, "Третья", _diarization_with("SPEAKER_01"))

        result = self.svc.handle_search_by_speaker({"speaker": "SPEAKER_01"})
        self.assertEqual(result["count"], 2)
        texts = [item["text"] for item in result["items"]]
        self.assertIn("Первая", texts)
        self.assertIn("Третья", texts)

    # ------------------------------------------------------------------
    # 8. Пустая история — возвращает пустой список без ошибок
    # ------------------------------------------------------------------
    def test_empty_store_returns_empty(self):
        result = self.svc.handle_search_by_speaker({"speaker": "SPEAKER_00"})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["items"], [])


if __name__ == "__main__":
    unittest.main()
