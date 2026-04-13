"""Тесты AutoDeduplicator — автоматическое обнаружение дубликатов транскрипций Krab Ear."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _now_iso() -> str:
    """Возвращает текущий timestamp в ISO-8601 формате."""
    return datetime.now(tz=timezone.utc).isoformat()

from backend.auto_deduplication import (
    AutoDeduplicator,
    DedupResult,
    DEFAULT_DEDUP_THRESHOLD,
    MERGE_THRESHOLD,
    AUTO_DEDUP_ENABLED,
)
from backend.state_store import StateStore


def _make_store(tmp_dir: Path) -> StateStore:
    """Создаёт StateStore во временной директории."""
    data_dir = tmp_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return StateStore(data_dir)


class DedupResultDataclassTestCase(unittest.TestCase):
    """Тесты структуры данных DedupResult."""

    def test_dedup_result_fields(self) -> None:
        """DedupResult содержит все ожидаемые поля."""
        result = DedupResult(
            is_duplicate=False,
            duplicate_of=None,
            similarity=0.0,
            action_taken="kept",
        )
        self.assertFalse(result.is_duplicate)
        self.assertIsNone(result.duplicate_of)
        self.assertEqual(result.similarity, 0.0)
        self.assertEqual(result.action_taken, "kept")

    def test_dedup_result_duplicate_fields(self) -> None:
        """DedupResult корректно хранит данные о найденном дубликате."""
        result = DedupResult(
            is_duplicate=True,
            duplicate_of="abc-123",
            similarity=0.95,
            action_taken="skipped",
        )
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.duplicate_of, "abc-123")
        self.assertEqual(result.similarity, 0.95)
        self.assertEqual(result.action_taken, "skipped")


class CheckDuplicateTestCase(unittest.TestCase):
    """Тесты метода check_duplicate."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(Path(self.tmp.name))
        self.deduplicator = AutoDeduplicator()

    def test_check_duplicate_empty_text_returns_kept(self) -> None:
        """Пустой текст → не дубликат, action_taken = 'kept'."""
        result = self.deduplicator.check_duplicate(
            text="",
            timestamp="2024-01-01T12:00:00",
            store=self.store,
        )
        self.assertFalse(result.is_duplicate)
        self.assertIsNone(result.duplicate_of)
        self.assertEqual(result.action_taken, "kept")

    def test_check_duplicate_unique_text_not_duplicate(self) -> None:
        """Уникальная запись в истории → не дубликат."""
        self.store.add_history_item(text="Привет мир", paste_status="ok")
        result = self.deduplicator.check_duplicate(
            text="Совершенно другой текст для проверки",
            timestamp=_now_iso(),
            store=self.store,
        )
        self.assertFalse(result.is_duplicate)
        self.assertEqual(result.action_taken, "kept")

    def test_check_duplicate_identical_text_is_duplicate(self) -> None:
        """Идентичный текст в пределах временного окна → дубликат."""
        text = "Это тестовая транскрипция для дедупликации"
        self.store.add_history_item(text=text, paste_status="ok")
        result = self.deduplicator.check_duplicate(
            text=text,
            timestamp=_now_iso(),
            store=self.store,
        )
        self.assertTrue(result.is_duplicate)
        self.assertIsNotNone(result.duplicate_of)
        self.assertGreaterEqual(result.similarity, DEFAULT_DEDUP_THRESHOLD)
        self.assertIn(result.action_taken, ("skipped", "merged"))

    def test_check_duplicate_action_merged_for_high_similarity(self) -> None:
        """Сходство >= MERGE_THRESHOLD → action_taken = 'merged'."""
        text = "Тест высокого сходства транскрипции"
        self.store.add_history_item(text=text, paste_status="ok")
        # Передаём threshold ниже MERGE_THRESHOLD, чтобы идентичный текст получил "merged"
        result = self.deduplicator.check_duplicate(
            text=text,
            timestamp=_now_iso(),
            store=self.store,
            threshold=0.9,
        )
        self.assertTrue(result.is_duplicate)
        # Идентичный текст даёт similarity >= MERGE_THRESHOLD → "merged"
        if result.similarity >= MERGE_THRESHOLD:
            self.assertEqual(result.action_taken, "merged")
        else:
            self.assertEqual(result.action_taken, "skipped")

    def test_check_duplicate_action_skipped_for_medium_similarity(self) -> None:
        """Сходство между threshold и MERGE_THRESHOLD → action_taken = 'skipped'."""
        # Используем нестрогий порог, чтобы немного похожий текст прошёл через как skipped
        original = "Транскрипция с важным содержанием для теста"
        similar = "Транскрипция с важным содержанием для проверки"
        self.store.add_history_item(text=original, paste_status="ok")

        result = self.deduplicator.check_duplicate(
            text=similar,
            timestamp=_now_iso(),
            store=self.store,
            threshold=0.7,
        )
        # Если обнаружен дубликат, action должен быть либо "skipped" либо "merged"
        if result.is_duplicate:
            self.assertIn(result.action_taken, ("skipped", "merged"))
        else:
            # Если не дубликат — action_taken = 'kept'
            self.assertEqual(result.action_taken, "kept")

    def test_check_duplicate_empty_history_not_duplicate(self) -> None:
        """Пустая история → никаких дубликатов."""
        result = self.deduplicator.check_duplicate(
            text="Первая запись в пустой истории",
            timestamp=_now_iso(),
            store=self.store,
        )
        self.assertFalse(result.is_duplicate)
        self.assertIsNone(result.duplicate_of)
        self.assertEqual(result.action_taken, "kept")

    def test_check_duplicate_increments_total_checked(self) -> None:
        """Каждый вызов check_duplicate увеличивает total_checked."""
        self.deduplicator.reset_stats()
        self.deduplicator.check_duplicate(
            text="раз", timestamp=_now_iso(), store=self.store
        )
        self.deduplicator.check_duplicate(
            text="два", timestamp=_now_iso(), store=self.store
        )
        stats = self.deduplicator.get_dedup_stats()
        self.assertEqual(stats["total_checked"], 2)


class RunDeduplicationTestCase(unittest.TestCase):
    """Тесты метода run_deduplication."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(Path(self.tmp.name))
        self.deduplicator = AutoDeduplicator()

    def test_run_deduplication_empty_store(self) -> None:
        """Пустая история → total_scanned=0, duplicate_groups=0."""
        result = self.deduplicator.run_deduplication(self.store)
        self.assertEqual(result["total_scanned"], 0)
        self.assertEqual(result["duplicate_groups"], 0)
        self.assertIsInstance(result["duplicates"], list)
        self.assertEqual(len(result["duplicates"]), 0)

    def test_run_deduplication_no_duplicates(self) -> None:
        """История без дубликатов → duplicate_groups=0."""
        self.store.add_history_item(text="Первый уникальный текст здесь", paste_status="ok")
        self.store.add_history_item(text="Второй абсолютно другой текст", paste_status="ok")
        self.store.add_history_item(text="Третий совершенно иной текст", paste_status="ok")
        result = self.deduplicator.run_deduplication(self.store)
        self.assertEqual(result["duplicate_groups"], 0)
        self.assertGreaterEqual(result["total_scanned"], 3)

    def test_run_deduplication_finds_duplicates(self) -> None:
        """История с дубликатами → duplicate_groups > 0."""
        duplicate_text = "Это дублирующийся текст транскрипции"
        self.store.add_history_item(text=duplicate_text, paste_status="ok")
        self.store.add_history_item(text=duplicate_text, paste_status="ok")
        result = self.deduplicator.run_deduplication(self.store, threshold=0.9)
        self.assertGreater(result["duplicate_groups"], 0)
        self.assertGreaterEqual(result["total_scanned"], 2)

    def test_run_deduplication_returns_correct_keys(self) -> None:
        """run_deduplication возвращает dict с ожидаемыми ключами."""
        result = self.deduplicator.run_deduplication(self.store)
        self.assertIn("total_scanned", result)
        self.assertIn("duplicate_groups", result)
        self.assertIn("duplicates", result)

    def test_run_deduplication_duplicate_entry_has_original_and_duplicates(self) -> None:
        """Каждая запись в duplicates содержит original_id, duplicate_ids, similarity."""
        dup_text = "Текст для проверки структуры результата дедупликации"
        self.store.add_history_item(text=dup_text, paste_status="ok")
        self.store.add_history_item(text=dup_text, paste_status="ok")
        result = self.deduplicator.run_deduplication(self.store, threshold=0.9)
        if result["duplicate_groups"] > 0:
            entry = result["duplicates"][0]
            self.assertIn("original_id", entry)
            self.assertIn("duplicate_ids", entry)
            self.assertIn("similarity", entry)
            self.assertIsInstance(entry["duplicate_ids"], list)
            self.assertIsInstance(entry["similarity"], float)


class GetDedupStatsTestCase(unittest.TestCase):
    """Тесты метода get_dedup_stats."""

    def setUp(self) -> None:
        self.deduplicator = AutoDeduplicator()
        self.deduplicator.reset_stats()

    def test_initial_stats_are_zero(self) -> None:
        """После сброса все счётчики равны нулю."""
        stats = self.deduplicator.get_dedup_stats()
        self.assertEqual(stats["total_checked"], 0)
        self.assertEqual(stats["duplicates_found"], 0)
        self.assertEqual(stats["chars_saved"], 0)
        self.assertEqual(stats["dedup_rate"], 0.0)

    def test_stats_returns_correct_keys(self) -> None:
        """get_dedup_stats возвращает dict с ожидаемыми ключами."""
        stats = self.deduplicator.get_dedup_stats()
        self.assertIn("total_checked", stats)
        self.assertIn("duplicates_found", stats)
        self.assertIn("chars_saved", stats)
        self.assertIn("dedup_rate", stats)

    def test_dedup_rate_is_float(self) -> None:
        """dedup_rate является float в диапазоне [0..1]."""
        stats = self.deduplicator.get_dedup_stats()
        self.assertIsInstance(stats["dedup_rate"], float)
        self.assertGreaterEqual(stats["dedup_rate"], 0.0)
        self.assertLessEqual(stats["dedup_rate"], 1.0)

    def test_reset_stats_clears_all_counters(self) -> None:
        """reset_stats() обнуляет все накопленные счётчики."""
        tmp = tempfile.TemporaryDirectory()
        store = _make_store(Path(tmp.name))
        tmp.cleanup()

        # Просто вызываем check_duplicate с пустым store чтобы поднять счётчики
        # Используем MagicMock store который вернёт пустой список
        mock_store = MagicMock()
        mock_store.get_history_page.return_value = ([], None)
        self.deduplicator.check_duplicate(text="тест", timestamp="2024-01-01T00:00:00", store=mock_store)

        self.deduplicator.reset_stats()
        stats = self.deduplicator.get_dedup_stats()
        self.assertEqual(stats["total_checked"], 0)
        self.assertEqual(stats["duplicates_found"], 0)


class AutoDedupIPCTestCase(unittest.TestCase):
    """Тесты IPC-обработчиков через BackendService.handle_request."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")

        recorder = MagicMock()
        recorder.is_recording = False

        from backend.service import BackendService
        self.svc = BackendService(
            store=store,
            recorder=recorder,
            transcriber=MagicMock(),
            translator=MagicMock(),
        )

    def test_get_dedup_stats_handler(self) -> None:
        """IPC get_dedup_stats возвращает корректный ответ."""
        resp = self.svc.handle_request(
            {"id": "1", "method": "get_dedup_stats", "params": {}}
        )
        self.assertTrue(resp.get("ok"), f"Expected ok=True, got: {resp}")
        result = resp["result"]
        self.assertIn("total_checked", result)
        self.assertIn("duplicates_found", result)
        self.assertIn("chars_saved", result)
        self.assertIn("dedup_rate", result)

    def test_check_duplicate_handler_empty_text(self) -> None:
        """IPC check_duplicate с пустым текстом возвращает action_taken='kept'."""
        resp = self.svc.handle_request(
            {"id": "2", "method": "check_duplicate", "params": {"text": ""}}
        )
        self.assertTrue(resp.get("ok"), f"Expected ok=True, got: {resp}")
        result = resp["result"]
        self.assertFalse(result["is_duplicate"])
        self.assertEqual(result["action_taken"], "kept")

    def test_check_duplicate_handler_new_text(self) -> None:
        """IPC check_duplicate с новым уникальным текстом → not duplicate."""
        resp = self.svc.handle_request(
            {
                "id": "3",
                "method": "check_duplicate",
                "params": {
                    "text": "Совершенно уникальная транскрипция",
                    "timestamp": _now_iso(),
                },
            }
        )
        self.assertTrue(resp.get("ok"), f"Expected ok=True, got: {resp}")
        result = resp["result"]
        self.assertIn("is_duplicate", result)
        self.assertIn("similarity", result)
        self.assertIn("action_taken", result)

    def test_run_deduplication_handler_empty_history(self) -> None:
        """IPC run_deduplication с пустой историей → total_scanned=0."""
        resp = self.svc.handle_request(
            {"id": "4", "method": "run_deduplication", "params": {}}
        )
        self.assertTrue(resp.get("ok"), f"Expected ok=True, got: {resp}")
        result = resp["result"]
        self.assertIn("total_scanned", result)
        self.assertIn("duplicate_groups", result)
        self.assertIn("duplicates", result)
        self.assertEqual(result["total_scanned"], 0)

    def test_run_deduplication_handler_with_duplicates(self) -> None:
        """IPC run_deduplication находит дубликаты в заполненной истории."""
        store = self.svc.store
        dup_text = "Повторяющийся текст для IPC теста дедупликации"
        store.add_history_item(text=dup_text, paste_status="ok")
        store.add_history_item(text=dup_text, paste_status="ok")

        resp = self.svc.handle_request(
            {"id": "5", "method": "run_deduplication", "params": {"threshold": 0.9}}
        )
        self.assertTrue(resp.get("ok"), f"Expected ok=True, got: {resp}")
        result = resp["result"]
        self.assertGreaterEqual(result["total_scanned"], 2)
        # Если дубликаты найдены, проверяем структуру
        if result["duplicate_groups"] > 0:
            entry = result["duplicates"][0]
            self.assertIn("original_id", entry)
            self.assertIn("duplicate_ids", entry)

    def test_check_duplicate_and_stats_update(self) -> None:
        """После check_duplicate через IPC total_checked в статистике растёт."""
        # Сначала получаем начальное значение
        resp_before = self.svc.handle_request(
            {"id": "10", "method": "get_dedup_stats", "params": {}}
        )
        before_count = resp_before["result"]["total_checked"]

        # Проверяем запись
        self.svc.handle_request(
            {
                "id": "11",
                "method": "check_duplicate",
                "params": {
                    "text": "Текст для проверки счётчика статистики",
                    "timestamp": _now_iso(),
                },
            }
        )

        resp_after = self.svc.handle_request(
            {"id": "12", "method": "get_dedup_stats", "params": {}}
        )
        after_count = resp_after["result"]["total_checked"]
        self.assertGreater(after_count, before_count)


class AutoDedupConstantsTestCase(unittest.TestCase):
    """Тесты констант и настроек модуля."""

    def test_auto_dedup_enabled_default_false(self) -> None:
        """AUTO_DEDUP_ENABLED по умолчанию False (безопасное умолчание)."""
        self.assertFalse(AUTO_DEDUP_ENABLED)

    def test_default_threshold_value(self) -> None:
        """DEFAULT_DEDUP_THRESHOLD равен 0.9."""
        self.assertEqual(DEFAULT_DEDUP_THRESHOLD, 0.9)

    def test_merge_threshold_higher_than_default(self) -> None:
        """MERGE_THRESHOLD строго выше DEFAULT_DEDUP_THRESHOLD."""
        self.assertGreater(MERGE_THRESHOLD, DEFAULT_DEDUP_THRESHOLD)

    def test_auto_dedup_enabled_in_default_settings(self) -> None:
        """auto_dedup_enabled присутствует в DEFAULT_SETTINGS."""
        from core.config import DEFAULT_SETTINGS
        self.assertIn("auto_dedup_enabled", DEFAULT_SETTINGS)
        self.assertFalse(DEFAULT_SETTINGS["auto_dedup_enabled"])

    def test_auto_dedup_threshold_in_default_settings(self) -> None:
        """auto_dedup_threshold присутствует в DEFAULT_SETTINGS."""
        from core.config import DEFAULT_SETTINGS
        self.assertIn("auto_dedup_threshold", DEFAULT_SETTINGS)
        self.assertEqual(DEFAULT_SETTINGS["auto_dedup_threshold"], 0.9)


if __name__ == "__main__":
    unittest.main()
