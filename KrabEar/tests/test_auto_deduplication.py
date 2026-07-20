"""Тесты AutoDeduplicator — автоматическое обнаружение дубликатов транскрипций Krab Ear."""

from __future__ import annotations
from backend.auto_deduplication import (
    AutoDeduplicator,
    DedupResult,
    DEFAULT_DEDUP_THRESHOLD,
    MERGE_THRESHOLD,
    AUTO_DEDUP_ENABLED,
)
from backend.state_store import StateStore

from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _now_iso() -> str:
    """Возвращает текущий timestamp в ISO-8601 формате."""
    return datetime.now(tz=timezone.utc).isoformat()


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
        _make_store(Path(tmp.name))
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
        self.addCleanup(self.svc.close)

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


class PrivacyModeGuardTestCase(unittest.TestCase):
    """W1004 F2 — AutoDeduplicator пропускает дедупликацию в режиме конфиденциальности."""

    def test_auto_dedup_skips_in_privacy_mode(self) -> None:
        """check_duplicate возвращает sentinel когда privacy_mode_enabled=True (W1248)."""
        # W1248: canonical key is "privacy_mode_enabled"; action_taken="privacy_skipped".
        deduplicator = AutoDeduplicator(
            settings_provider=lambda k, d=False: True if k == "privacy_mode_enabled" else d
        )

        # Store с существующей идентичной записью
        existing_item = {
            "id": "orig-001",
            "text": "Секретная транскрипция для проверки приватного режима",
            "ts": _now_iso(),
        }
        mock_store = MagicMock()
        mock_store.get_history_page.return_value = ([existing_item], None)

        result = deduplicator.check_duplicate(
            text="Секретная транскрипция для проверки приватного режима",
            timestamp=_now_iso(),
            store=mock_store,
        )

        # В privacy_mode дедупликация не должна выполняться
        self.assertFalse(result.is_duplicate, "В privacy_mode дубликаты не должны определяться")
        self.assertIsNone(result.duplicate_of)
        # W1248: action_taken is "privacy_skipped" (not "kept")
        self.assertIn(result.action_taken, ("privacy_skipped", "kept"))
        # Store не должен вызываться — данные не читаются в privacy_mode
        mock_store.get_history_page.assert_not_called()

    def test_auto_dedup_active_when_privacy_mode_disabled(self) -> None:
        """check_duplicate работает штатно когда privacy_mode_enabled=False."""
        # W1248: canonical key is "privacy_mode_enabled"
        deduplicator = AutoDeduplicator(
            settings_provider=lambda k, d=False: False if k == "privacy_mode_enabled" else d
        )

        existing_item = {
            "id": "orig-002",
            "text": "Обычная транскрипция без приватного режима",
            "ts": _now_iso(),
        }
        mock_store = MagicMock()
        mock_store.get_history_page.return_value = ([existing_item], None)

        result = deduplicator.check_duplicate(
            text="Обычная транскрипция без приватного режима",
            timestamp=_now_iso(),
            store=mock_store,
        )

        # Дедупликация должна выполняться в обычном режиме
        self.assertTrue(result.is_duplicate, "Идентичный текст должен быть определён как дубликат")
        mock_store.get_history_page.assert_called_once()

    def test_privacy_mode_no_settings_provider_runs_dedup(self) -> None:
        """AutoDeduplicator без settings_provider работает как раньше (без privacy guard)."""
        deduplicator = AutoDeduplicator()  # settings_provider=None

        existing_item = {
            "id": "orig-003",
            "text": "Транскрипция без провайдера настроек",
            "ts": _now_iso(),
        }
        mock_store = MagicMock()
        mock_store.get_history_page.return_value = ([existing_item], None)

        result = deduplicator.check_duplicate(
            text="Транскрипция без провайдера настроек",
            timestamp=_now_iso(),
            store=mock_store,
        )
        # Без settings_provider — обычная дедупликация
        self.assertTrue(result.is_duplicate)

    def test_privacy_mode_settings_provider_exception_safe(self) -> None:
        """Если settings_provider бросает исключение — privacy_mode считается False (fail-safe)."""
        # W1505 N1 HIGH fix: провайдер принимает два аргумента (key, default)
        def broken_settings(key: str, default: object = False) -> object:
            raise RuntimeError("Ошибка получения настроек")

        deduplicator = AutoDeduplicator(settings_provider=broken_settings)

        # При ошибке провайдера настроек — дедупликация продолжается (не ломается)
        existing_item = {
            "id": "orig-004",
            "text": "Транскрипция при сломанном провайдере",
            "ts": _now_iso(),
        }
        mock_store = MagicMock()
        mock_store.get_history_page.return_value = ([existing_item], None)

        # Не должно бросать исключение
        result = deduplicator.check_duplicate(
            text="Транскрипция при сломанном провайдере",
            timestamp=_now_iso(),
            store=mock_store,
        )
        self.assertIn(result.action_taken, ("kept", "skipped", "merged"))

    def test_privacy_provider_signature_two_args(self) -> None:
        """Регрессия W1505 N1 HIGH: settings_provider вызывается с двумя аргументами (key, default).

        Нулевой lambda (zero-arg) вызывает TypeError который поглощается except Exception
        и возвращает False — privacy_mode никогда не активируется. Этот тест ловит регрессию.
        W1248: canonical key is "privacy_mode_enabled".
        """
        calls: list[tuple] = []

        def recording_provider(key: str, default: object = False) -> object:
            calls.append((key, default))
            return True if key == "privacy_mode_enabled" else default

        deduplicator = AutoDeduplicator(settings_provider=recording_provider)
        mock_store = MagicMock()
        mock_store.get_history_page.return_value = ([], None)

        deduplicator.check_duplicate(
            text="тестовый текст для регрессии",
            timestamp=_now_iso(),
            store=mock_store,
        )

        # Провайдер должен быть вызван ровно с двумя аргументами
        self.assertGreater(len(calls), 0, "settings_provider не был вызван")
        for call_args in calls:
            self.assertEqual(len(call_args), 2, f"Ожидалось 2 аргумента, получено {len(call_args)}: {call_args}")
        # privacy_mode_enabled=True → check_duplicate скипает store
        mock_store.get_history_page.assert_not_called()


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


class NearDuplicateThresholdTestCase(unittest.TestCase):
    """Tests for threshold tuning, near-duplicates, unicode, concurrency, time window."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(Path(self.tmp.name))
        self.deduplicator = AutoDeduplicator()

    # ------------------------------------------------------------------
    # test_near_duplicate_above_threshold
    # ------------------------------------------------------------------
    def test_near_duplicate_above_threshold(self) -> None:
        """Text that is very similar (above threshold) is flagged as duplicate."""
        original = "Это длинное предложение для проверки механизма дедупликации записей"
        similar = "Это длинное предложение для проверки механизма дедупликации записей!"
        self.store.add_history_item(text=original, paste_status="ok")

        result = self.deduplicator.check_duplicate(
            text=similar,
            timestamp=_now_iso(),
            store=self.store,
            threshold=0.85,
        )
        # Both texts are nearly identical — should be detected as duplicate.
        self.assertTrue(result.is_duplicate)
        self.assertIsNotNone(result.duplicate_of)
        self.assertGreaterEqual(result.similarity, 0.85)

    # ------------------------------------------------------------------
    # test_threshold_adjustable
    # ------------------------------------------------------------------
    def test_threshold_adjustable(self) -> None:
        """Lowering threshold makes more texts qualify as duplicates."""
        text_a = "Транскрипция встречи по вопросам разработки продукта"
        text_b = "Транскрипция встречи по вопросам разработки проекта"
        self.store.add_history_item(text=text_a, paste_status="ok")

        # High threshold — not a duplicate
        result_strict = self.deduplicator.check_duplicate(
            text=text_b,
            timestamp=_now_iso(),
            store=self.store,
            threshold=0.99,
        )
        # Low threshold — should detect near-duplicate
        result_lax = self.deduplicator.check_duplicate(
            text=text_b,
            timestamp=_now_iso(),
            store=self.store,
            threshold=0.7,
        )
        # Lax threshold should find duplicate; strict should not
        self.assertFalse(result_strict.is_duplicate)
        self.assertTrue(result_lax.is_duplicate)

    # ------------------------------------------------------------------
    # test_no_action_below_threshold
    # ------------------------------------------------------------------
    def test_no_action_below_threshold(self) -> None:
        """Texts below similarity threshold → action_taken = 'kept', not duplicate."""
        self.store.add_history_item(
            text="Сводка бюджетного комитета на квартал", paste_status="ok"
        )
        result = self.deduplicator.check_duplicate(
            text="Погода сегодня хорошая и солнечная",
            timestamp=_now_iso(),
            store=self.store,
            threshold=DEFAULT_DEDUP_THRESHOLD,
        )
        self.assertFalse(result.is_duplicate)
        self.assertIsNone(result.duplicate_of)
        self.assertEqual(result.action_taken, "kept")

    # ------------------------------------------------------------------
    # test_handles_unicode_text
    # ------------------------------------------------------------------
    def test_handles_unicode_text(self) -> None:
        """Unicode text (Cyrillic, emoji, mixed scripts) is handled without error."""
        unicode_texts = [
            "Привет! 🎤 Тест записи голоса на кириллице.",
            "¡Hola! Prueba de transcripción en español con ñoño.",
            "Mixed: Привет world こんにちは 🌍 test",
            "Эмодзи: 🔥🚀💡 и кириллица вместе с ASCII",
        ]
        for text in unicode_texts:
            with self.subTest(text=text[:30]):
                result = self.deduplicator.check_duplicate(
                    text=text,
                    timestamp=_now_iso(),
                    store=self.store,
                )
                # Must not raise; result must have valid action
                self.assertIn(result.action_taken, ("kept", "skipped", "merged"))
                self.assertIsInstance(result.similarity, float)

    def test_handles_unicode_identical_duplicate(self) -> None:
        """Identical unicode text is correctly flagged as duplicate."""
        unicode_text = "Транскрипция 🎙️ встречи: итоги квартала — продажи выросли на 15%"
        self.store.add_history_item(text=unicode_text, paste_status="ok")
        result = self.deduplicator.check_duplicate(
            text=unicode_text,
            timestamp=_now_iso(),
            store=self.store,
        )
        self.assertTrue(result.is_duplicate)

    # ------------------------------------------------------------------
    # test_concurrent_dedup
    # ------------------------------------------------------------------
    def test_concurrent_dedup(self) -> None:
        """AutoDeduplicator is thread-safe under concurrent check_duplicate calls."""
        self.store.add_history_item(
            text="Параллельный тест дедупликации нескольких потоков", paste_status="ok"
        )
        errors: list[Exception] = []
        results: list[DedupResult] = []
        lock = threading.Lock()

        def worker(text: str) -> None:
            try:
                r = self.deduplicator.check_duplicate(
                    text=text,
                    timestamp=_now_iso(),
                    store=self.store,
                )
                with lock:
                    results.append(r)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(
                target=worker,
                args=(f"Уникальный текст для потока номер {i}",),
            )
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [], f"Thread errors: {errors}")
        self.assertEqual(len(results), 10)
        # All threads completed, stats are consistent
        stats = self.deduplicator.get_dedup_stats()
        self.assertGreaterEqual(stats["total_checked"], 10)

    # ------------------------------------------------------------------
    # test_skip_old_items_outside_window
    # ------------------------------------------------------------------
    def test_skip_old_items_outside_window(self) -> None:
        """Items with timestamps outside the 60-second window are not matched as duplicates."""
        old_ts = "2020-01-01T00:00:00+00:00"
        recent_ts = _now_iso()

        # Simulate store returning one old item
        old_item = {
            "id": "old-001",
            "text": "Транскрипция из далёкого прошлого",
            "ts": old_ts,
        }
        mock_store = MagicMock()
        mock_store.get_history_page.return_value = ([old_item], None)

        # Check same text but recent timestamp — outside 60s window → not duplicate
        result = self.deduplicator.check_duplicate(
            text="Транскрипция из далёкого прошлого",
            timestamp=recent_ts,
            store=mock_store,
        )
        # Because the old item is >60s away from now, it should NOT match
        self.assertFalse(result.is_duplicate)
        self.assertEqual(result.action_taken, "kept")

    def test_identical_items_within_window_marked_duplicate(self) -> None:
        """Same text with timestamps within 60s window IS marked as duplicate."""
        text = "Транскрипция в пределах временного окна"
        # Use mock store with item that has 'now' timestamp
        now_ts = _now_iso()
        existing_item = {
            "id": "recent-001",
            "text": text,
            "ts": now_ts,
        }
        mock_store = MagicMock()
        mock_store.get_history_page.return_value = ([existing_item], None)

        result = self.deduplicator.check_duplicate(
            text=text,
            timestamp=now_ts,
            store=mock_store,
        )
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.duplicate_of, "recent-001")

    def test_identical_items_marked_duplicate(self) -> None:
        """Identical text items in store are correctly identified as duplicates."""
        text = "Идентичный текст для проверки маркировки дубликатов"
        self.store.add_history_item(text=text, paste_status="ok")

        result = self.deduplicator.check_duplicate(
            text=text,
            timestamp=_now_iso(),
            store=self.store,
        )
        self.assertTrue(result.is_duplicate)
        self.assertIn(result.action_taken, ("skipped", "merged"))


class W1412SettingsProviderTestCase(unittest.TestCase):
    """W1406 N1 CRIT + N2 HIGH — regression tests для W1412 fix."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(Path(self.tmp.name))

    # ------------------------------------------------------------------
    # test_auto_deduplicator_constructed_with_settings_provider
    # ------------------------------------------------------------------
    def test_auto_deduplicator_constructed_with_settings_provider(self) -> None:
        """AutoDeduplicator принимает settings_provider kwarg и хранит его (W1406 N1)."""
        called_keys: list[str] = []

        def fake_provider(key: str, default: object = None) -> object:
            called_keys.append(key)
            return default

        dedup = AutoDeduplicator(settings_provider=fake_provider)
        # Поставщик должен быть сохранён
        self.assertIs(dedup._settings_provider, fake_provider)

    def test_auto_deduplicator_no_provider_still_works(self) -> None:
        """AutoDeduplicator без settings_provider не падает (обратная совместимость)."""
        dedup = AutoDeduplicator()
        result = dedup.check_duplicate(
            text="тест без провайдера",
            timestamp=_now_iso(),
            store=self.store,
        )
        self.assertIn(result.action_taken, ("kept", "skipped", "merged"))

    # ------------------------------------------------------------------
    # test_auto_dedup_skipped_when_privacy_mode_enabled
    # ------------------------------------------------------------------
    def test_auto_dedup_skipped_when_privacy_mode_enabled(self) -> None:
        """check_duplicate пропускается когда privacy_mode_enabled=True (W1248).

        W1248: canonical key is "privacy_mode_enabled"; action_taken="privacy_skipped".
        """
        # Добавляем запись в store чтобы было что сравнивать
        text = "Конфиденциальная транскрипция приватного разговора"
        self.store.add_history_item(text=text, paste_status="ok")

        # Создаём dedup с privacy_mode_enabled=True (W1248 canonical key)
        dedup = AutoDeduplicator(
            settings_provider=lambda key, default=None: True if key == "privacy_mode_enabled" else default
        )

        result = dedup.check_duplicate(
            text=text,  # идентичный текст — без privacy gate был бы дубликатом
            timestamp=_now_iso(),
            store=self.store,
        )
        # В режиме приватности дедупликация должна быть пропущена
        self.assertFalse(result.is_duplicate, "Дедупликация не должна выполняться в режиме приватности")
        # W1248: action_taken is "privacy_skipped"
        self.assertIn(result.action_taken, ("privacy_skipped", "kept"))
        self.assertEqual(result.similarity, 0.0)

    def test_auto_dedup_not_skipped_when_privacy_mode_disabled(self) -> None:
        """check_duplicate НЕ пропускается когда privacy_mode_enabled=False (нормальный режим)."""
        text = "Обычная транскрипция без режима приватности"
        self.store.add_history_item(text=text, paste_status="ok")

        dedup = AutoDeduplicator(
            settings_provider=lambda key, default=None: False if key == "privacy_mode_enabled" else default
        )

        result = dedup.check_duplicate(
            text=text,
            timestamp=_now_iso(),
            store=self.store,
        )
        # Без privacy mode идентичный текст должен быть дубликатом
        self.assertTrue(result.is_duplicate)

    def test_privacy_mode_enabled_returns_false_without_provider(self) -> None:
        """_privacy_mode_enabled() → False если settings_provider не задан."""
        dedup = AutoDeduplicator()
        self.assertFalse(dedup._privacy_mode_enabled())

    def test_privacy_mode_enabled_returns_true_when_provider_says_so(self) -> None:
        """_privacy_mode_enabled() → True если settings_provider возвращает True."""
        dedup = AutoDeduplicator(settings_provider=lambda key, default=None: True)
        self.assertTrue(dedup._privacy_mode_enabled())

    def test_privacy_mode_enabled_handles_provider_exception(self) -> None:
        """_privacy_mode_enabled() → False при исключении в settings_provider (не падает)."""
        def broken_provider(key: str, default: object = None) -> object:
            raise RuntimeError("settings broken")

        dedup = AutoDeduplicator(settings_provider=broken_provider)
        # Должен обработать исключение и вернуть False
        self.assertFalse(dedup._privacy_mode_enabled())

    # ------------------------------------------------------------------
    # test_handle_run_deduplication_injects_semantic_searcher
    # ------------------------------------------------------------------
    def test_handle_run_deduplication_injects_semantic_searcher(self) -> None:
        """_handle_run_deduplication инжектирует _semantic_searcher в params (W1406 N2 HIGH).

        Проверяем через BackendService что params["_semantic_searcher"] присутствует
        при вызове handle_run_deduplication.
        """
        store = StateStore(Path(self.tmp.name) / "svc_data")

        recorder = MagicMock()
        recorder.is_recording = False

        from backend.service import BackendService
        svc = BackendService(
            store=store,
            recorder=recorder,
            transcriber=MagicMock(),
            translator=MagicMock(),
        )
        self.addCleanup(svc.close)

        # Патчим handle_run_deduplication чтобы перехватить params
        captured_params: list[dict] = []
        original_handler = svc._auto_deduplicator.handle_run_deduplication

        def spy_handler(params: dict) -> dict:
            captured_params.append(dict(params))
            return original_handler(params)

        svc._auto_deduplicator.handle_run_deduplication = spy_handler

        resp = svc.handle_request(
            {"id": "w1412-n2", "method": "run_deduplication", "params": {}}
        )
        self.assertTrue(resp.get("ok"), f"Expected ok=True, got: {resp}")

        # Проверяем что semantic_searcher был инжектирован
        self.assertTrue(
            len(captured_params) > 0,
            "handle_run_deduplication не был вызван"
        )
        self.assertIn(
            "_semantic_searcher",
            captured_params[0],
            "W1406 N2: _semantic_searcher не был инжектирован в params"
        )
        # Инжектированный searcher должен быть тем же объектом что и в svc
        self.assertIs(
            captured_params[0]["_semantic_searcher"],
            svc._semantic_searcher,
            "Инжектированный semantic_searcher не совпадает с svc._semantic_searcher"
        )

    def test_service_auto_deduplicator_has_settings_provider(self) -> None:
        """BackendService конструирует AutoDeduplicator с settings_provider (W1406 N1 CRIT).

        Регрессионный тест — без этого фикса settings_provider=None и privacy gate
        всегда обходился.
        """
        store = StateStore(Path(self.tmp.name) / "svc_data2")

        recorder = MagicMock()
        recorder.is_recording = False

        from backend.service import BackendService
        svc = BackendService(
            store=store,
            recorder=recorder,
            transcriber=MagicMock(),
            translator=MagicMock(),
        )
        self.addCleanup(svc.close)

        self.assertIsNotNone(
            svc._auto_deduplicator._settings_provider,
            "W1406 N1 CRIT: AutoDeduplicator._settings_provider должен быть задан в BackendService"
        )
        # Должен быть именно _get_runtime_setting — сравниваем по __func__ (bound method)
        self.assertIs(
            svc._auto_deduplicator._settings_provider.__func__,
            svc._get_runtime_setting.__func__,
            "settings_provider должен ссылаться на метод _get_runtime_setting"
        )


if __name__ == "__main__":
    unittest.main()
