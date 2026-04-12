"""Тесты для handle_export_history_json в HistoryService.

Покрывает:
1. Базовый экспорт — структура payload (export_info + entries)
2. Пустая история — entries=[], total_entries=0
3. Фильтр date-range (from_ts / to_ts)
4. pretty=False — компактный JSON
5. Метаданные перевода в записи
6. Метаданные диаризации в записи
7. Аннотации попадают в поле annotation
8. save_to_file создаёт файл с корректным JSON
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore
from backend.history_service import HistoryService


def _make_store(tmp_dir: Path) -> StateStore:
    return StateStore(data_dir=tmp_dir)


def _make_svc(store: StateStore) -> HistoryService:
    return HistoryService(store=store)


class TestExportHistoryJsonStructure(unittest.TestCase):
    """Проверяет базовую структуру JSON-экспорта."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmp.name))
        self.svc = _make_svc(self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # 1. Базовый экспорт — корневые ключи payload
    # ------------------------------------------------------------------

    def test_basic_export_has_required_keys(self) -> None:
        self.store.add_history_item(text="Тест экспорта")
        result = self.svc.handle_export_history_json({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 1)
        self.assertGreater(result["chars"], 0)
        self.assertIsNone(result["path"])

        data = json.loads(
            # Re-generate to inspect payload structure
            json.dumps(
                self._get_payload()
            )
        )
        self.assertIn("export_info", data)
        self.assertIn("entries", data)
        info = data["export_info"]
        self.assertEqual(info["version"], "2.0")
        self.assertIn("exported_at", info)
        self.assertIn("total_entries", info)
        self.assertIn("filters", info)

    def _get_payload(self) -> dict:
        """Вспомогательный метод: возвращает распарсенный payload."""
        result = self.svc.handle_export_history_json({"pretty": False})
        # We need the raw json content — re-generate via direct call
        import io, json as _json
        import subprocess
        # Re-use internal method flow: call handle and inspect via save_to_file
        tmp_dir = Path(self._tmp.name)
        result2 = self.svc.handle_export_history_json({"save_to_file": True, "pretty": True})
        if result2["path"]:
            return _json.loads(Path(result2["path"]).read_text())
        return {}

    # ------------------------------------------------------------------
    # 2. Пустая история
    # ------------------------------------------------------------------

    def test_empty_history_exports_zero_entries(self) -> None:
        result = self.svc.handle_export_history_json({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 0)


class TestExportHistoryJsonEntries(unittest.TestCase):
    """Проверяет содержимое entries в JSON-экспорте."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmp.name))
        self.svc = _make_svc(self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _export_parsed(self, params: dict | None = None) -> dict:
        """Вспомогательный: сохраняет в файл и возвращает распарсенный payload."""
        p = {"save_to_file": True, "pretty": True}
        if params:
            p.update(params)
        result = self.svc.handle_export_history_json(p)
        self.assertIsNotNone(result["path"])
        return json.loads(Path(result["path"]).read_text())

    # ------------------------------------------------------------------
    # 3. Поля записи — базовые метаданные
    # ------------------------------------------------------------------

    def test_entry_fields_present(self) -> None:
        import json as _j
        # Добавляем запись напрямую через NDJSON с полными метаданными
        entry_line = _j.dumps({
            "type": "item",
            "id": "fields-test-001",
            "ts": "2026-04-12T10:00:00+00:00",
            "text": "Привет мир",
            "paste_status": "ok",
            "source_lang": "ru",
            "confidence": 0.91,
            "audio_duration_sec": 3.5,
            "tags": [],
            "favorite": False,
        })
        with open(self.store.history_path, "a") as f:
            f.write(entry_line + "\n")

        data = self._export_parsed()
        entry = next(e for e in data["entries"] if e["id"] == "fields-test-001")

        required_keys = {"id", "timestamp", "text", "language", "confidence",
                         "duration_sec", "paste_status", "translation",
                         "diarization", "tags", "favorite", "annotation"}
        for key in required_keys:
            self.assertIn(key, entry, f"Отсутствует ключ: {key}")

        self.assertEqual(entry["text"], "Привет мир")
        self.assertEqual(entry["language"], "ru")
        self.assertAlmostEqual(entry["confidence"], 0.91, places=2)
        self.assertAlmostEqual(entry["duration_sec"], 3.5, places=1)
        self.assertEqual(entry["paste_status"], "ok")

    # ------------------------------------------------------------------
    # 4. pretty=False — компактный JSON (нет переносов строк)
    # ------------------------------------------------------------------

    def test_compact_json_when_pretty_false(self) -> None:
        self.store.add_history_item(text="Компактный экспорт")
        result = self.svc.handle_export_history_json({"pretty": False, "save_to_file": True})
        json_text = Path(result["path"]).read_text()
        # Компактный JSON не должен начинаться с пробела после '{'
        self.assertNotIn("\n  ", json_text[:50])

    # ------------------------------------------------------------------
    # 5. Перевод попадает в блок translation
    # ------------------------------------------------------------------

    def test_translation_block_populated(self) -> None:
        self.store.add_history_item(
            text="Hola mundo",
            translated_text="Привет мир",
            translation_mode="ru",
            translation_status="ok",
            translation_engine="hf_marian",
            source_lang="es",
            target_lang="ru",
        )
        data = self._export_parsed()
        entry = data["entries"][0]
        tr = entry["translation"]
        self.assertIsNotNone(tr)
        self.assertEqual(tr["text"], "Привет мир")
        self.assertEqual(tr["engine"], "hf_marian")
        self.assertEqual(tr["status"], "ok")

    # ------------------------------------------------------------------
    # 6. Диаризация попадает в блок diarization
    # ------------------------------------------------------------------

    def test_diarization_block_populated(self) -> None:
        diar_data = {
            "enabled": True,
            "speaker_turns": [
                {"speaker": "SPEAKER_00", "text": "Добрый день", "start": 0.0, "end": 2.1},
                {"speaker": "SPEAKER_01", "text": "Здравствуйте", "start": 2.5, "end": 4.0},
            ],
        }
        item = self.store.add_history_item(text="Разговор двух спикеров")
        # Обновляем диаризацию напрямую через store
        with self.store._lock():
            active = self.store._load_active_items_unlocked()
        for it in active:
            if it.id == item.id:
                it.diarization = diar_data
                break
        # Добавляем новую запись с диаризацией
        item2 = self.store.add_history_item(text="Встреча")
        # Проверяем формат diarization_block с mock-данными через прямой вызов
        # (StateStore хранит diarization при сохранении через add_history_item с diarization param)
        # Используем обходной путь: создадим запись через метод _append_ndjson напрямую
        import json as _j
        entry_line = _j.dumps({
            "type": "item",
            "id": "test-diar-001",
            "ts": "2026-04-12T10:00:00+00:00",
            "text": "Диалог",
            "paste_status": "ok",
            "diarization": diar_data,
            "tags": [],
            "favorite": False,
        })
        with open(self.store.history_path, "a") as f:
            f.write(entry_line + "\n")

        data = self._export_parsed()
        diar_entry = next((e for e in data["entries"] if e["id"] == "test-diar-001"), None)
        self.assertIsNotNone(diar_entry)
        diar = diar_entry["diarization"]
        self.assertIsNotNone(diar)
        self.assertTrue(diar["enabled"])
        self.assertEqual(diar["speakers"], 2)
        self.assertEqual(len(diar["segments"]), 2)

    # ------------------------------------------------------------------
    # 7. Аннотации попадают в поле annotation
    # ------------------------------------------------------------------

    def test_annotation_included_in_export(self) -> None:
        item = self.store.add_history_item(text="Запись с заметкой")
        self.store.set_annotation(item.id, "Важная встреча по проекту")

        data = self._export_parsed()
        entry = next(e for e in data["entries"] if e["id"] == item.id)
        self.assertEqual(entry["annotation"], "Важная встреча по проекту")

    # ------------------------------------------------------------------
    # 8. save_to_file создаёт валидный JSON-файл
    # ------------------------------------------------------------------

    def test_save_to_file_creates_valid_json(self) -> None:
        self.store.add_history_item(text="Файловый экспорт")
        result = self.svc.handle_export_history_json({"save_to_file": True})
        self.assertIsNotNone(result["path"])
        path = Path(result["path"])
        self.assertTrue(path.exists())
        self.assertTrue(path.suffix == ".json")
        # Файл должен содержать валидный JSON
        parsed = json.loads(path.read_text())
        self.assertIn("export_info", parsed)
        self.assertEqual(parsed["export_info"]["total_entries"], 1)

    # ------------------------------------------------------------------
    # 9. total_entries в export_info совпадает с len(entries)
    # ------------------------------------------------------------------

    def test_total_entries_matches_entries_length(self) -> None:
        for i in range(5):
            self.store.add_history_item(text=f"Запись {i}")
        data = self._export_parsed()
        self.assertEqual(data["export_info"]["total_entries"], len(data["entries"]))
        self.assertEqual(len(data["entries"]), 5)

    # ------------------------------------------------------------------
    # 10. Фильтр по date-range ограничивает результаты
    # ------------------------------------------------------------------

    def test_date_range_filter_limits_results(self) -> None:
        import json as _j
        # Добавляем запись с ts в прошлом
        old_entry = _j.dumps({
            "type": "item",
            "id": "old-entry-001",
            "ts": "2020-01-01T00:00:00+00:00",
            "text": "Старая запись",
            "paste_status": "ok",
            "tags": [],
            "favorite": False,
        })
        new_entry = _j.dumps({
            "type": "item",
            "id": "new-entry-001",
            "ts": "2026-04-12T12:00:00+00:00",
            "text": "Новая запись",
            "paste_status": "ok",
            "tags": [],
            "favorite": False,
        })
        with open(self.store.history_path, "a") as f:
            f.write(old_entry + "\n")
            f.write(new_entry + "\n")

        # Экспортируем только 2026-й год
        result = self.svc.handle_export_history_json({
            "from_ts": "2026-01-01T00:00:00+00:00",
            "to_ts": "2026-12-31T23:59:59+00:00",
            "save_to_file": True,
        })
        data = json.loads(Path(result["path"]).read_text())
        ids = [e["id"] for e in data["entries"]]
        self.assertIn("new-entry-001", ids)
        self.assertNotIn("old-entry-001", ids)


class TestExportHistoryJsonIPCRegistration(unittest.TestCase):
    """Проверяет регистрацию IPC-метода export_history_json."""

    def test_ipc_method_registered(self) -> None:
        """Метод export_history_json должен быть зарегистрирован в handle_request."""
        # Читаем service.py и ищем строку регистрации
        service_path = Path(__file__).resolve().parents[1] / "backend" / "service.py"
        content = service_path.read_text(encoding="utf-8")
        self.assertIn('"export_history_json"', content,
                      "IPC-метод export_history_json не зарегистрирован в service.py")


if __name__ == "__main__":
    unittest.main()
