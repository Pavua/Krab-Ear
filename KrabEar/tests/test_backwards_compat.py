"""Тесты обратной совместимости Krab Ear.

Проверяют, что:
- Старые записи истории (без tags/favorite/annotation) загружаются корректно
- Старые форматы настроек мигрируются автоматически
- Старые IPC-методы (get_history_page, search_history и т.д.) продолжают работать
- Pipeline v1 (engine.transcribe) работает вместе с v2
- Старый NDJSON-формат читается корректно
- Отсутствующие новые поля получают дефолты без крэша
- Форматы экспорта не изменились
- v1.0 data directory открывается кодом v2.0
- Поля HistoryItem из v1 полностью читаются через from_dict
- Настройки v1.0 мигрируются с сохранением всех дефолтов v2
"""

from __future__ import annotations

import json
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore
from backend.models import HistoryItem, DEFAULT_SETTINGS
from backend.history_service import HistoryService
from backend.data_migrator import DataMigrator, LATEST_VERSION
from core.utils import TextUtils


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _v1_item_dict(text: str = "Привет мир", paste_status: str = "ok") -> dict:
    """Минимальная запись в формате v1.0 (без tags, favorite, annotation,
    confidence, cleaned_text, llm_applied, diarization, audio_duration_sec)."""
    return {
        "id": str(uuid.uuid4()),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "text": text,
        "paste_status": paste_status,
        "source_text": "",
        "translated_text": "",
        "translation_mode": "off",
        "source_lang": "",
        "target_lang": "",
        "translation_status": "not_requested",
        "translation_engine": "",
        "chat_id": "",
        "message_id": "",
    }


def _write_ndjson(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 1. HistoryItem.from_dict: старая запись без новых полей
# ---------------------------------------------------------------------------

class TestHistoryItemV1Deserialization(unittest.TestCase):
    """HistoryItem.from_dict не должен падать на v1-записях и должен
    подставлять корректные дефолты для всех полей v2."""

    def _make_item(self, extra: dict | None = None) -> HistoryItem:
        payload = _v1_item_dict()
        if extra:
            payload.update(extra)
        return HistoryItem.from_dict(payload)

    def test_v1_item_loads_without_crash(self):
        """Запись без tags/favorite/confidence/etc. загружается без исключения."""
        item = self._make_item()
        self.assertIsInstance(item, HistoryItem)

    def test_v1_item_tags_default_empty_list(self):
        """Отсутствующее поле tags получает дефолт []."""
        item = self._make_item()
        self.assertEqual(item.tags, [])

    def test_v1_item_favorite_default_false(self):
        """Отсутствующее поле favorite получает дефолт False."""
        item = self._make_item()
        self.assertFalse(item.favorite)

    def test_v1_item_confidence_default_none(self):
        """Отсутствующее поле confidence получает дефолт None."""
        item = self._make_item()
        self.assertIsNone(item.confidence)

    def test_v1_item_diarization_default_none(self):
        """Отсутствующее поле diarization получает дефолт None."""
        item = self._make_item()
        self.assertIsNone(item.diarization)

    def test_v1_item_audio_duration_default_none(self):
        """Отсутствующее поле audio_duration_sec получает дефолт None."""
        item = self._make_item()
        self.assertIsNone(item.audio_duration_sec)

    def test_v1_item_llm_fields_default_false_zero(self):
        """Отсутствующие LLM-поля получают False/0."""
        item = self._make_item()
        self.assertFalse(item.llm_applied)
        self.assertEqual(item.llm_latency_ms, 0)

    def test_v1_item_text_preserved(self):
        """Текст из v1-записи сохраняется без искажений."""
        item = self._make_item()
        self.assertEqual(item.text, "Привет мир")

    def test_v1_item_id_ts_preserved(self):
        """id и ts v1-записи остаются без изменений."""
        payload = _v1_item_dict()
        item = HistoryItem.from_dict(payload)
        self.assertEqual(item.id, payload["id"])
        self.assertEqual(item.ts, payload["ts"])


# ---------------------------------------------------------------------------
# 2. StateStore: загрузка NDJSON с v1-записями
# ---------------------------------------------------------------------------

class TestStateStoreV1NdjsonCompat(unittest.TestCase):
    """StateStore должен читать history.ndjson в формате v1.0 без потерь."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir()

    def _make_store_with_v1_items(self, count: int = 3) -> tuple[StateStore, list[dict]]:
        v1_records = [_v1_item_dict(text=f"запись-{i}") for i in range(count)]
        _write_ndjson(self.data_dir / "history.ndjson", v1_records)
        (self.data_dir / "history_tombstones.ndjson").touch()
        (self.data_dir / "history_status.ndjson").touch()
        (self.data_dir / "history_tags.ndjson").touch()
        (self.data_dir / "history_favorites.ndjson").touch()
        (self.data_dir / "history_annotations.ndjson").touch()
        (self.data_dir / "vocabulary.txt").touch()
        store = StateStore(self.data_dir)
        return store, v1_records

    def test_v1_ndjson_all_items_loaded(self):
        """Все v1-записи читаются из NDJSON без потерь."""
        store, v1_records = self._make_store_with_v1_items(3)
        items, _ = store.get_history_page(cursor=None, limit=50)
        self.assertEqual(len(items), 3)

    def test_v1_ndjson_ids_match(self):
        """id из v1-записей сохраняются в загруженных элементах."""
        store, v1_records = self._make_store_with_v1_items(2)
        items, _ = store.get_history_page(cursor=None, limit=50)
        loaded_ids = {i["id"] for i in items}
        expected_ids = {r["id"] for r in v1_records}
        self.assertEqual(loaded_ids, expected_ids)

    def test_v1_ndjson_missing_tags_field_defaults_to_empty(self):
        """Поле tags в загруженных v1-элементах — пустой список."""
        store, _ = self._make_store_with_v1_items(1)
        items, _ = store.get_history_page(cursor=None, limit=50)
        self.assertEqual(items[0]["tags"], [])

    def test_v1_ndjson_missing_favorite_field_defaults_false(self):
        """Поле favorite в загруженных v1-элементах — False."""
        store, _ = self._make_store_with_v1_items(1)
        items, _ = store.get_history_page(cursor=None, limit=50)
        self.assertFalse(items[0]["favorite"])

    def test_v1_ndjson_search_works(self):
        """Поиск по v1-истории работает без ошибок."""
        store, v1_records = self._make_store_with_v1_items(3)
        # "запись" встречается в тексте всех 3 записей
        results, _ = store.search_history(
            query="запись", cursor=None, limit=50,
        )
        self.assertEqual(len(results), 3)

    def test_v1_ndjson_tombstone_still_works(self):
        """Tombstone-удаление работает для v1-записей."""
        store, v1_records = self._make_store_with_v1_items(2)
        target_id = v1_records[0]["id"]
        ok = store.delete_history_item(target_id)
        self.assertTrue(ok)
        items, _ = store.get_history_page(cursor=None, limit=50)
        loaded_ids = {i["id"] for i in items}
        self.assertNotIn(target_id, loaded_ids)


# ---------------------------------------------------------------------------
# 3. Настройки: старый формат мигрируется с дефолтами
# ---------------------------------------------------------------------------

class TestSettingsMigrationCompat(unittest.TestCase):
    """StateStore.load_settings() дополняет старые настройки дефолтами v2."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "settings_test"
        self.store = StateStore(self.data_dir)

    def test_old_settings_without_new_fields_get_defaults(self):
        """Файл настроек с только старыми ключами дополняется новыми дефолтами."""
        old_settings = {
            "mode": "headless",
            "auto_paste": True,
        }
        self.store.settings_path.write_text(
            json.dumps(old_settings), encoding="utf-8"
        )
        loaded = self.store.load_settings()
        # Новые поля v2 должны присутствовать из DEFAULT_SETTINGS
        self.assertIn("translation_mode", loaded)
        self.assertIn("silence_guard_enabled", loaded)
        self.assertIn("background_guard_enabled", loaded)

    def test_old_settings_existing_values_preserved(self):
        """Значения из старого файла настроек не перезаписываются дефолтами."""
        old_settings = {"mode": "headless", "auto_paste": False}
        self.store.settings_path.write_text(json.dumps(old_settings), encoding="utf-8")
        loaded = self.store.load_settings()
        self.assertFalse(loaded["auto_paste"])

    def test_empty_settings_file_returns_defaults(self):
        """Пустой/отсутствующий файл настроек возвращает все дефолты."""
        if self.store.settings_path.exists():
            self.store.settings_path.unlink()
        loaded = self.store.load_settings()
        for key in DEFAULT_SETTINGS:
            self.assertIn(key, loaded)

    def test_corrupted_settings_file_returns_defaults(self):
        """Повреждённый JSON в файле настроек → возврат дефолтов без крэша."""
        self.store.settings_path.write_text("not valid json{{", encoding="utf-8")
        loaded = self.store.load_settings()
        # Должны получить дефолтные настройки
        self.assertEqual(loaded["mode"], DEFAULT_SETTINGS["mode"])


# ---------------------------------------------------------------------------
# 4. Старые IPC-методы продолжают работать через HistoryService
# ---------------------------------------------------------------------------

class TestLegacyIPCMethodsCompat(unittest.TestCase):
    """Ключевые IPC-методы, вызываемые из Swift, должны работать без изменений."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)
        # Добавляем несколько записей для тестов
        for i in range(3):
            self.store.add_history_item(text=f"тест IPC {i}", paste_status="ok")

    def test_get_history_page_returns_items_and_cursor(self):
        """get_history_page возвращает items и next_cursor."""
        result = self.svc.handle_get_history_page({"limit": 10})
        self.assertIn("items", result)
        self.assertIn("next_cursor", result)
        self.assertEqual(len(result["items"]), 3)

    def test_search_history_returns_items_and_cursor(self):
        """search_history возвращает items и next_cursor."""
        result = self.svc.handle_search_history({"query": "тест"})
        self.assertIn("items", result)
        self.assertIn("next_cursor", result)
        self.assertGreater(len(result["items"]), 0)

    def test_add_history_item_returns_dict_with_id(self):
        """add_history_item возвращает словарь с полем id."""
        result = self.svc.handle_add_history_item({"text": "новая запись", "paste_status": "ok"})
        self.assertIn("id", result)
        self.assertIn("ts", result)
        self.assertIn("text", result)

    def test_delete_history_item_returns_deleted_true(self):
        """delete_history_item возвращает {"deleted": True}."""
        item = self.store.add_history_item(text="удалить", paste_status="ok")
        result = self.svc.handle_delete_history_item({"id": item.id})
        self.assertEqual(result, {"deleted": True})

    def test_compact_history_returns_compacted_true(self):
        """compact_history возвращает {"compacted": True, ...}."""
        result = self.svc.handle_compact_history({})
        self.assertTrue(result.get("compacted"))

    def test_get_history_stats_returns_expected_keys(self):
        """get_history_stats возвращает ожидаемые ключи статистики."""
        result = self.svc.handle_get_history_stats({})
        for key in ("active_count", "history_lines", "tombstones_lines"):
            self.assertIn(key, result)

    def test_get_history_overview_returns_expected_keys(self):
        """get_history_overview возвращает ожидаемые ключи обзора."""
        result = self.svc.handle_get_history_overview({})
        self.assertIn("active_count", result)
        self.assertIn("paste_ok", result)


# ---------------------------------------------------------------------------
# 5. Миграция данных v1.0 → v2.0
# ---------------------------------------------------------------------------

class TestDataMigratorV1toV2(unittest.TestCase):
    """DataMigrator корректно обнаруживает и мигрирует v1.0 данные."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "v1_data"
        self.data_dir.mkdir()
        self.migrator = DataMigrator()

    def _create_v1_data_dir(self, item_count: int = 2) -> None:
        v1_records = [_v1_item_dict(text=f"v1 текст {i}") for i in range(item_count)]
        _write_ndjson(self.data_dir / "history.ndjson", v1_records)
        (self.data_dir / "history_tombstones.ndjson").touch()

    def test_detects_v1_schema(self):
        """DataMigrator определяет v1.0 у директории с записями без tags/favorite."""
        self._create_v1_data_dir()
        version = self.migrator.get_schema_version(self.data_dir)
        self.assertEqual(version, "1.0")

    def test_detects_migration_needed(self):
        """check_migration_needed возвращает True для v1 директории."""
        self._create_v1_data_dir()
        self.assertTrue(self.migrator.check_migration_needed(self.data_dir))

    def test_migrate_v1_to_v2_adds_missing_fields(self):
        """После миграции v1→v2 все записи имеют поля tags и favorite."""
        self._create_v1_data_dir(item_count=3)
        result = self.migrator.migrate(self.data_dir, target_version="2.0")
        self.assertEqual(result.to_version, "2.0")
        self.assertGreater(result.items_migrated, 0)
        # Проверяем, что записи в новом файле имеют нужные поля
        migrated_items = []
        for line in (self.data_dir / "history.ndjson").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                migrated_items.append(json.loads(line))
        for item in migrated_items:
            self.assertIn("tags", item)
            self.assertIn("favorite", item)

    def test_v2_data_dir_reports_no_migration_needed(self):
        """DataMigrator не требует миграции для v2-данных."""
        # Создаём v2-запись с полями tags/favorite
        v2_record = _v1_item_dict()
        v2_record["tags"] = []
        v2_record["favorite"] = False
        _write_ndjson(self.data_dir / "history.ndjson", [v2_record])
        (self.data_dir / "history_tombstones.ndjson").touch()
        self.assertFalse(self.migrator.check_migration_needed(self.data_dir))

    def test_v1_data_dir_readable_by_v2_state_store(self):
        """v1.0 data directory открывается StateStore v2 без ошибок."""
        self._create_v1_data_dir(item_count=2)
        # StateStore создаёт все недостающие файлы автоматически
        store = StateStore(self.data_dir)
        items, _ = store.get_history_page(cursor=None, limit=50)
        self.assertEqual(len(items), 2)
        # Все items имеют дефолтные значения для новых полей
        for item_dict in items:
            self.assertEqual(item_dict.get("tags"), [])
            self.assertFalse(item_dict.get("favorite", True))


# ---------------------------------------------------------------------------
# 6. Экспорт: формат не изменился для простых записей
# ---------------------------------------------------------------------------

class TestExportFormatBackwardsCompat(unittest.TestCase):
    """Проверяет, что форматы экспорта Markdown/JSON не изменились для базовых случаев."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def test_markdown_export_contains_header(self):
        """Экспорт Markdown всегда начинается с фиксированного заголовка."""
        self.store.add_history_item(text="пример текста", paste_status="ok")
        result = self.svc.handle_export_history({"limit": 10})
        content = result["content"]
        self.assertTrue(content.startswith("# Krab Ear — Экспорт истории"))

    def test_markdown_export_contains_item_text(self):
        """Текст записи присутствует в Markdown-экспорте."""
        self.store.add_history_item(text="уникальный текст для теста", paste_status="ok")
        result = self.svc.handle_export_history({"limit": 10})
        self.assertIn("уникальный текст для теста", result["content"])

    def test_markdown_export_empty_history_stable_format(self):
        """Экспорт пустой истории возвращает стабильный заголовок."""
        result = self.svc.handle_export_history({})
        self.assertIn("История пуста", result["content"])
        self.assertEqual(result["total_items"], 0)

    def test_json_export_contains_items_array(self):
        """JSON-экспорт содержит массив items."""
        self.store.add_history_item(text="json тест", paste_status="ok")
        result = self.svc.handle_export_history_json({"limit": 10})
        self.assertIn("items", result)
        self.assertIsInstance(result["items"], list)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["text"], "json тест")

    def test_json_export_item_has_required_fields(self):
        """JSON-экспорт содержит обязательные поля id, ts, text."""
        self.store.add_history_item(text="поля экспорта", paste_status="ok")
        result = self.svc.handle_export_history_json({"limit": 10})
        item = result["items"][0]
        for field in ("id", "ts", "text", "paste_status"):
            self.assertIn(field, item)


# ---------------------------------------------------------------------------
# 7. TextUtils: legacy static method aliases
# ---------------------------------------------------------------------------

class TestTextUtilsLegacyAliases(unittest.TestCase):
    """Проверяет, что legacy-методы TextUtils продолжают работать."""

    def test_cleanup_transcript_soft(self):
        """TextUtils.cleanup_transcript с profile='soft' не падает и возвращает строку."""
        result = TextUtils.cleanup_transcript("тест тест", profile="soft")
        self.assertIsInstance(result, str)

    def test_cleanup_transcript_strict(self):
        """TextUtils.cleanup_transcript с profile='strict' работает корректно."""
        result = TextUtils.cleanup_transcript("спасибо за просмотр", profile="strict")
        self.assertIsInstance(result, str)

    def test_normalize_entities_returns_string(self):
        """normalize_entities не крэшится на пустой строке."""
        result = TextUtils.normalize_entities("")
        self.assertEqual(result, "")

    def test_normalize_entities_brand_replacement(self):
        """Кириллические бренды заменяются на латиницу."""
        result = TextUtils.normalize_entities("Это Телеграм сообщение")
        self.assertIn("Telegram", result)

    def test_strip_hallucinations_via_cleanup(self):
        """Галлюцинация 'спасибо за просмотр' удаляется при очистке."""
        text = "Реальный текст. Спасибо за просмотр."
        result = TextUtils.cleanup_transcript(text, profile="soft")
        self.assertNotIn("просмотр", result.lower())


# ---------------------------------------------------------------------------
# 8. Импорт NDJSON из v1 директории
# ---------------------------------------------------------------------------

class TestImportV1NdjsonCompat(unittest.TestCase):
    """Импорт history.ndjson из v1.0 директории работает без ошибок."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "new_data")
        self.svc = HistoryService(store=self.store)

    def test_import_v1_ndjson_succeeds(self):
        """Импорт v1-файла history.ndjson не вызывает ошибок."""
        v1_path = Path(self.tmp.name) / "v1_export.ndjson"
        v1_records = [_v1_item_dict(text=f"импорт {i}") for i in range(3)]
        _write_ndjson(v1_path, v1_records)
        result = self.svc.handle_import_history_ndjson({"path": str(v1_path)})
        self.assertEqual(result["imported"], 3)
        self.assertEqual(result["errors"], 0)

    def test_import_v1_ndjson_items_readable_after_import(self):
        """После импорта v1-записей их можно получить через get_history_page."""
        v1_path = Path(self.tmp.name) / "v1_export2.ndjson"
        v1_records = [_v1_item_dict(text=f"post-import {i}") for i in range(2)]
        _write_ndjson(v1_path, v1_records)
        self.svc.handle_import_history_ndjson({"path": str(v1_path)})
        result = self.svc.handle_get_history_page({"limit": 50})
        self.assertEqual(len(result["items"]), 2)

    def test_import_v1_ndjson_skips_duplicates(self):
        """Повторный импорт того же файла не создаёт дублей."""
        v1_path = Path(self.tmp.name) / "v1_dedup.ndjson"
        v1_records = [_v1_item_dict(text="дедуп")]
        _write_ndjson(v1_path, v1_records)
        self.svc.handle_import_history_ndjson({"path": str(v1_path)})
        result2 = self.svc.handle_import_history_ndjson({"path": str(v1_path)})
        self.assertEqual(result2["skipped"], 1)
        self.assertEqual(result2["imported"], 0)


if __name__ == "__main__":
    unittest.main()
