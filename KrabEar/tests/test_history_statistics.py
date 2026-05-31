"""Тесты handle_get_history_statistics в HistoryService."""

from __future__ import annotations
from backend.state_store import StateStore
from backend.history_service import HistoryService

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

def _make_service(tmp_dir: Path) -> HistoryService:
    store = StateStore(data_dir=tmp_dir)
    return HistoryService(store=store)


def _add_item(svc: HistoryService, **kwargs) -> dict:
    params = {"paste_status": "ok"}
    params.update(kwargs)
    return svc.handle_add_history_item(params)


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------

class TestHistoryStatisticsEmpty(unittest.TestCase):
    """Статистика для пустой истории."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = _make_service(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_history_returns_zero_totals(self):
        result = self.svc.handle_get_history_statistics({})
        self.assertEqual(result["total_items"], 0)
        self.assertEqual(result["total_duration_sec"], 0.0)
        self.assertEqual(result["total_words"], 0)
        self.assertEqual(result["avg_confidence"], 0.0)
        self.assertEqual(result["languages"], {})
        self.assertIsNone(result["date_range"])
        self.assertEqual(result["items_with_translation"], 0)
        self.assertEqual(result["items_with_diarization"], 0)
        self.assertEqual(result["avg_speakers"], 0.0)
        self.assertEqual(result["top_speakers"], {})
        self.assertEqual(result["daily_counts"], {})


class TestHistoryStatisticsBasic(unittest.TestCase):
    """Базовые агрегаты: total_items, total_words, total_duration_sec."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = _make_service(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_total_items_and_words(self):
        _add_item(self.svc, text="один два три")          # 3 слова
        _add_item(self.svc, text="четыре пять")            # 2 слова
        result = self.svc.handle_get_history_statistics({})
        self.assertEqual(result["total_items"], 2)
        self.assertEqual(result["total_words"], 5)

    def test_total_duration_sec_sums_correctly(self):
        # Добавляем через store напрямую — handle_add_history_item не принимает audio_duration_sec
        store = self.svc.store
        store.add_history_item(
            text="текст А",
            paste_status="ok",
            audio_duration_sec=10.5,
        )
        store.add_history_item(
            text="текст Б",
            paste_status="ok",
            audio_duration_sec=4.5,
        )
        result = self.svc.handle_get_history_statistics({})
        self.assertAlmostEqual(result["total_duration_sec"], 15.0, places=2)

    def test_items_without_duration_are_ignored_in_sum(self):
        _add_item(self.svc, text="без длительности")
        store = self.svc.store
        store.add_history_item(text="с длительностью", paste_status="ok", audio_duration_sec=7.0)
        result = self.svc.handle_get_history_statistics({})
        self.assertEqual(result["total_items"], 2)
        self.assertAlmostEqual(result["total_duration_sec"], 7.0, places=2)


class TestHistoryStatisticsConfidence(unittest.TestCase):
    """avg_confidence: среднее только по записям с заданным confidence."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = _make_service(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _add_with_confidence(self, text: str, confidence: float) -> None:
        """Добавляет запись с confidence напрямую через _append_ndjson."""
        from backend.models import HistoryItem
        item = HistoryItem.create(text=text, paste_status="ok", confidence=confidence)
        with self.svc.store._lock():
            self.svc.store._append_ndjson(self.svc.store.history_path, item.to_dict())

    def test_avg_confidence_computed_correctly(self):
        self._add_with_confidence("раз", 0.8)
        self._add_with_confidence("два", 0.6)
        result = self.svc.handle_get_history_statistics({})
        self.assertAlmostEqual(result["avg_confidence"], 0.7, places=4)

    def test_avg_confidence_zero_when_no_confidence_data(self):
        _add_item(self.svc, text="без confidence")
        result = self.svc.handle_get_history_statistics({})
        self.assertEqual(result["avg_confidence"], 0.0)

    def test_avg_confidence_ignores_none_items(self):
        self._add_with_confidence("с confidence", 0.9)
        _add_item(self.svc, text="без confidence")
        result = self.svc.handle_get_history_statistics({})
        # Среднее только из одного значения 0.9
        self.assertAlmostEqual(result["avg_confidence"], 0.9, places=4)


class TestHistoryStatisticsLanguages(unittest.TestCase):
    """languages: подсчёт по source_lang."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = _make_service(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_language_counts(self):
        store = self.svc.store
        store.add_history_item(text="раз", paste_status="ok", source_lang="ru")
        store.add_history_item(text="dos", paste_status="ok", source_lang="es")
        store.add_history_item(text="три", paste_status="ok", source_lang="ru")
        result = self.svc.handle_get_history_statistics({})
        self.assertEqual(result["languages"].get("ru"), 2)
        self.assertEqual(result["languages"].get("es"), 1)

    def test_items_without_source_lang_not_counted(self):
        _add_item(self.svc, text="без языка")
        result = self.svc.handle_get_history_statistics({})
        self.assertEqual(result["languages"], {})


class TestHistoryStatisticsTranslation(unittest.TestCase):
    """items_with_translation: только записи с translation_status == 'ok'."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = _make_service(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_items_with_translation_count(self):
        store = self.svc.store
        store.add_history_item(
            text="текст",
            paste_status="ok",
            translated_text="text",
            translation_status="ok",
        )
        store.add_history_item(
            text="другой текст",
            paste_status="ok",
            translated_text="",
            translation_status="not_requested",
        )
        result = self.svc.handle_get_history_statistics({})
        self.assertEqual(result["items_with_translation"], 1)


class TestHistoryStatisticsDiarization(unittest.TestCase):
    """items_with_diarization, avg_speakers, top_speakers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = _make_service(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _add_diarized(self, speakers: list[str]) -> None:
        turns = [{"speaker": s, "text": f"реплика {s}", "start": i * 2.0, "end": i * 2.0 + 1.5}
                 for i, s in enumerate(speakers)]
        diar = {"enabled": True, "speaker_turns": turns}
        self.svc.store.add_history_item(
            text=" ".join(f"реплика {s}" for s in speakers),
            paste_status="ok",
            diarization=diar,
        )

    def test_diarization_count_and_avg_speakers(self):
        self._add_diarized(["SPEAKER_00", "SPEAKER_01"])         # 2 спикера
        self._add_diarized(["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"])  # 3 спикера
        result = self.svc.handle_get_history_statistics({})
        self.assertEqual(result["items_with_diarization"], 2)
        self.assertAlmostEqual(result["avg_speakers"], 2.5, places=2)

    def test_single_speaker_diar_not_counted(self):
        """Диаризация с одним спикером не засчитывается."""
        turns = [{"speaker": "SPEAKER_00", "text": "моно", "start": 0.0, "end": 2.0}]
        diar = {"enabled": True, "speaker_turns": turns}
        self.svc.store.add_history_item(text="моно", paste_status="ok", diarization=diar)
        result = self.svc.handle_get_history_statistics({})
        self.assertEqual(result["items_with_diarization"], 0)
        self.assertEqual(result["avg_speakers"], 0.0)

    def test_top_speakers_populated(self):
        self._add_diarized(["SPEAKER_00", "SPEAKER_01"])
        self._add_diarized(["SPEAKER_00", "SPEAKER_02"])
        result = self.svc.handle_get_history_statistics({})
        top = result["top_speakers"]
        # SPEAKER_00 встречается в 2 записях
        self.assertEqual(top.get("SPEAKER_00"), 2)
        self.assertIn("SPEAKER_01", top)
        self.assertIn("SPEAKER_02", top)


class TestHistoryStatisticsDateRange(unittest.TestCase):
    """date_range и daily_counts."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = _make_service(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_date_range_reflects_min_max_dates(self):
        # Добавляем записи и проверяем, что date_range не None и from <= to
        _add_item(self.svc, text="запись А")
        _add_item(self.svc, text="запись Б")
        result = self.svc.handle_get_history_statistics({})
        dr = result["date_range"]
        self.assertIsNotNone(dr)
        self.assertLessEqual(dr["from"], dr["to"])

    def test_daily_counts_includes_today(self):
        from datetime import date
        _add_item(self.svc, text="сегодняшняя запись")
        result = self.svc.handle_get_history_statistics({})
        from datetime import datetime, timezone  # noqa: E402
        today_str = datetime.now(timezone.utc).date().isoformat()  # UTC matches stored UTC timestamps
        # Запись добавлена сегодня, должна попасть в daily_counts
        self.assertIn(today_str, result["daily_counts"])
        self.assertGreaterEqual(result["daily_counts"][today_str], 1)

    def test_result_has_all_required_keys(self):
        """Результат всегда содержит все обязательные ключи."""
        _add_item(self.svc, text="тест")
        result = self.svc.handle_get_history_statistics({})
        required_keys = [
            "total_items", "total_duration_sec", "total_words", "avg_confidence",
            "languages", "date_range", "items_with_translation",
            "items_with_diarization", "avg_speakers", "top_speakers", "daily_counts",
        ]
        for key in required_keys:
            self.assertIn(key, result, f"Ключ '{key}' отсутствует в результате")


if __name__ == "__main__":
    unittest.main()
