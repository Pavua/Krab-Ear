"""Тесты безлимитного NDJSON-хранилища истории Krab Ear."""

from __future__ import annotations
from backend.state_store import StateStore

from pathlib import Path
import json
import sys
import tempfile
import threading
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class HistoryStoreTestCase(unittest.TestCase):
    """Проверяет append/read/pagination/search/delete/compact."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data", compact_threshold_bytes=1024)

    def test_append_and_paginate(self) -> None:
        for idx in range(120):
            self.store.add_history_item(text=f"строка-{idx}", paste_status="failed")

        page1, cursor1 = self.store.get_history_page(cursor=None, limit=50)
        page2, cursor2 = self.store.get_history_page(cursor=cursor1, limit=50)
        page3, cursor3 = self.store.get_history_page(cursor=cursor2, limit=50)

        self.assertEqual(len(page1), 50)
        self.assertEqual(len(page2), 50)
        self.assertEqual(len(page3), 20)
        self.assertIsNone(cursor3)
        self.assertEqual(page1[0]["text"], "строка-119")
        self.assertEqual(page3[-1]["text"], "строка-0")

    def test_search_history(self) -> None:
        self.store.add_history_item(text="купи молоко", paste_status="ok")
        self.store.add_history_item(text="позвони маме", paste_status="failed")
        self.store.add_history_item(text="молоко и хлеб", paste_status="failed")

        page, cursor = self.store.search_history(query="молоко", cursor=None, limit=50)
        self.assertEqual(len(page), 2)
        self.assertIsNone(cursor)
        self.assertIn("молоко", page[0]["text"])

    def test_search_in_translation_fields(self) -> None:
        self.store.add_history_item(
            text="hola amigo",
            paste_status="failed",
            source_text="привет друг",
            translated_text="hola amigo",
            translation_mode="ru_to_es",
            source_lang="ru",
            target_lang="es",
            translation_status="ok",
            translation_engine="hf_marian",
        )

        page_source, _ = self.store.search_history(query="привет", cursor=None, limit=50)
        self.assertEqual(len(page_source), 1)

        page_translated, _ = self.store.search_history(query="hola", cursor=None, limit=50)
        self.assertEqual(len(page_translated), 1)

    def test_get_history_page_with_filters(self) -> None:
        self.store.add_history_item(
            text="ok item",
            paste_status="ok",
            translation_mode="ru_to_es",
        )
        self.store.add_history_item(
            text="failed item",
            paste_status="failed",
            translation_mode="off",
        )

        page_ok, _ = self.store.get_history_page_filtered(
            cursor=None,
            limit=20,
            paste_status="ok",
            translation_mode=None,
        )
        self.assertEqual(len(page_ok), 1)
        self.assertEqual(page_ok[0]["paste_status"], "ok")

        page_mode, _ = self.store.get_history_page_filtered(
            cursor=None,
            limit=20,
            paste_status=None,
            translation_mode="ru_to_es",
        )
        self.assertEqual(len(page_mode), 1)
        self.assertEqual(page_mode[0]["translation_mode"], "ru_to_es")

    def test_get_history_page_with_translation_status_and_date_filters(self) -> None:
        self.store.add_history_item(
            text="old",
            paste_status="ok",
            translation_mode="ru_to_es",
            translation_status="ok",
        )
        self.store.add_history_item(
            text="new",
            paste_status="ok",
            translation_mode="ru_to_es",
            translation_status="unavailable_offline",
        )
        page_status, _ = self.store.get_history_page_filtered(
            cursor=None,
            limit=20,
            paste_status=None,
            translation_mode=None,
            translation_status="unavailable_offline",
        )
        self.assertEqual(len(page_status), 1)
        self.assertEqual(page_status[0]["text"], "new")

        # Фильтр по дате в формате YYYY-MM-DD не должен отбрасывать записи текущего дня.
        today = page_status[0]["ts"][:10]
        page_today, _ = self.store.get_history_page_filtered(
            cursor=None,
            limit=20,
            paste_status=None,
            translation_mode=None,
            from_ts=today,
            to_ts=today,
        )
        self.assertGreaterEqual(len(page_today), 1)

    def test_import_history_ndjson(self) -> None:
        item = self.store.add_history_item(text="origin", paste_status="failed")
        source = Path(self.tmp.name) / "import.ndjson"
        payloads = [
            {
                "id": item.id,
                "ts": "2026-02-11T10:00:00",
                "text": "duplicate",
                "paste_status": "failed",
            },
            {
                "id": "external-1",
                "ts": "2026-02-11T10:01:00",
                "text": "imported",
                "paste_status": "ok",
                "translation_mode": "off",
            },
            {
                "id": "",
                "ts": "",
                "text": "",
            },
        ]
        source.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in payloads) + "\n", encoding="utf-8")
        result = self.store.import_history_ndjson(source)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["errors"], 1)

    def test_delete_and_status_update(self) -> None:
        item = self.store.add_history_item(text="черновик", paste_status="failed")
        self.store.set_paste_status(item.id, "ok")

        page, _ = self.store.get_history_page(cursor=None, limit=10)
        self.assertEqual(page[0]["paste_status"], "ok")

        self.store.delete_history_item(item.id)
        page_after_delete, _ = self.store.get_history_page(cursor=None, limit=10)
        self.assertEqual(page_after_delete, [])

    def test_concurrent_append(self) -> None:
        total_threads = 8
        items_per_thread = 200

        def worker(thread_idx: int) -> None:
            for n in range(items_per_thread):
                self.store.add_history_item(
                    text=f"thread-{thread_idx}-item-{n}",
                    paste_status="failed",
                )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(total_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(self.store.count_active_items(), total_threads * items_per_thread)

    def test_compaction(self) -> None:
        ids = []
        for idx in range(30):
            item = self.store.add_history_item(text=f"запись-{idx}", paste_status="failed")
            ids.append(item.id)

        for item_id in ids[:10]:
            self.store.delete_history_item(item_id)

        for item_id in ids[10:20]:
            self.store.set_paste_status(item_id, "ok")

        self.store.compact()

        page, _ = self.store.get_history_page(cursor=None, limit=100)
        self.assertEqual(len(page), 20)
        statuses = {item["id"]: item["paste_status"] for item in page}
        for item_id in ids[10:20]:
            self.assertEqual(statuses[item_id], "ok")

    def test_compact_with_stats_and_get_history_stats(self) -> None:
        for idx in range(8):
            self.store.add_history_item(text=f"item-{idx}", paste_status="failed")
        page, _ = self.store.get_history_page(cursor=None, limit=2)
        self.store.delete_history_item(page[0]["id"])

        compact_stats = self.store.compact_with_stats()
        self.assertIn("before_total_bytes", compact_stats)
        self.assertIn("after_total_bytes", compact_stats)
        self.assertIn("reclaimed_bytes", compact_stats)

        stats = self.store.get_history_stats()
        self.assertIn("active_count", stats)
        self.assertIn("total_bytes", stats)
        self.assertGreaterEqual(stats["history_lines"], stats["active_count"])

    def test_get_history_overview(self) -> None:
        self.store.add_history_item(
            text="ok translated",
            paste_status="ok",
            translation_mode="ru_to_es",
            translation_status="ok",
            source_lang="ru",
            target_lang="es",
        )
        self.store.add_history_item(
            text="translate error",
            paste_status="failed",
            translation_mode="ru_to_es",
            translation_status="translate_error",
            source_lang="ru",
            target_lang="es",
        )
        self.store.add_history_item(
            text="no translation",
            paste_status="failed",
            translation_mode="off",
            translation_status="not_requested",
        )
        overview = self.store.get_history_overview()
        self.assertEqual(overview["active_count"], 3)
        self.assertEqual(overview["paste_ok"], 1)
        self.assertEqual(overview["paste_failed"], 2)
        self.assertEqual(overview["translated_ok"], 1)
        self.assertEqual(overview["translated_error"], 1)
        self.assertEqual(overview["no_translation"], 1)
        self.assertGreaterEqual(overview["today_count"], 1)
        self.assertGreaterEqual(overview["last_24h_count"], 1)
        self.assertIsInstance(overview["top_modes"], list)

        # Новые поля: языковая статистика
        self.assertIsInstance(overview["source_langs"], list)
        self.assertIsInstance(overview["target_langs"], list)
        self.assertEqual(overview["source_langs"][0]["lang"], "ru")
        self.assertEqual(overview["source_langs"][0]["count"], 2)
        self.assertEqual(overview["target_langs"][0]["lang"], "es")
        self.assertEqual(overview["target_langs"][0]["count"], 2)

        # Диаризация и LLM
        self.assertEqual(overview["diarization_count"], 0)
        self.assertEqual(overview["llm_applied_count"], 0)

        # Средняя длина текста
        self.assertGreater(overview["avg_text_chars"], 0)
        self.assertGreater(overview["today_text_chars"], 0)

    def test_overview_diarization_and_llm_stats(self) -> None:
        """Проверяет подсчёт записей с диаризацией и LLM-обработкой."""
        self.store.add_history_item(
            text="with diarization",
            paste_status="ok",
            diarization={"speakers": 2, "segments": []},
        )
        self.store.add_history_item(
            text="with llm",
            paste_status="ok",
            llm_applied=True,
            llm_latency_ms=150,
        )
        self.store.add_history_item(
            text="with both",
            paste_status="ok",
            diarization={"speakers": 3, "segments": []},
            llm_applied=True,
            llm_latency_ms=200,
        )
        self.store.add_history_item(text="plain", paste_status="ok")

        overview = self.store.get_history_overview()
        self.assertEqual(overview["diarization_count"], 2)
        self.assertEqual(overview["llm_applied_count"], 2)

    def test_overview_empty_history(self) -> None:
        """Проверяет обзор при пустой истории — не должен падать."""
        overview = self.store.get_history_overview()
        self.assertEqual(overview["active_count"], 0)
        self.assertEqual(overview["avg_text_chars"], 0)
        self.assertEqual(overview["today_count"], 0)
        self.assertEqual(overview["diarization_count"], 0)
        self.assertEqual(overview["source_langs"], [])
        self.assertEqual(overview["target_langs"], [])

    def test_date_filter_early_termination(self) -> None:
        """Проверяет, что фильтрация по дате с from_ts корректно возвращает результаты.

        Записи с разными датами — фильтр по дате должен вернуть только нужные.
        Это также неявно тестирует early termination (break при item.ts < from_ts).
        """
        # Записываем напрямую в NDJSON для контроля ts.
        import json as _json
        items_data = [
            {"id": "old-1", "ts": "2025-01-15T10:00:00", "text": "january", "paste_status": "ok"},
            {"id": "old-2", "ts": "2025-02-10T10:00:00", "text": "february", "paste_status": "ok"},
            {"id": "mid-1", "ts": "2025-03-05T10:00:00", "text": "march", "paste_status": "ok"},
            {"id": "new-1", "ts": "2025-04-01T10:00:00", "text": "april", "paste_status": "ok"},
            {"id": "new-2", "ts": "2025-04-10T10:00:00", "text": "april-late", "paste_status": "ok"},
        ]
        with self.store.history_path.open("w", encoding="utf-8") as fh:
            for item in items_data:
                fh.write(_json.dumps(item, ensure_ascii=False) + "\n")

        # Фильтр: только март
        page, _ = self.store.get_history_page_filtered(
            cursor=None, limit=50,
            paste_status=None, translation_mode=None,
            from_ts="2025-03-01", to_ts="2025-03-31",
        )
        self.assertEqual(len(page), 1)
        self.assertEqual(page[0]["text"], "march")

        # Фильтр: с марта по конец (no to_ts)
        page2, _ = self.store.get_history_page_filtered(
            cursor=None, limit=50,
            paste_status=None, translation_mode=None,
            from_ts="2025-03-01",
        )
        self.assertEqual(len(page2), 3)  # march, april, april-late

        # Search с from_ts
        results, _ = self.store.search_history(
            query="april", cursor=None, limit=50,
            from_ts="2025-04-01",
        )
        self.assertEqual(len(results), 2)

    def test_load_settings_handles_corrupted_json(self) -> None:
        """Проверяет, что load_settings возвращает дефолты при битом JSON."""
        self.store.settings_path.write_text("{broken json!!!", encoding="utf-8")
        result = self.store.load_settings()
        from backend.models import DEFAULT_SETTINGS
        self.assertEqual(result, dict(DEFAULT_SETTINGS))

    def test_load_settings_handles_empty_file(self) -> None:
        """Проверяет, что load_settings возвращает дефолты при пустом файле."""
        self.store.settings_path.write_text("", encoding="utf-8")
        result = self.store.load_settings()
        from backend.models import DEFAULT_SETTINGS
        self.assertEqual(result, dict(DEFAULT_SETTINGS))

    # ------------------------------------------------------------------
    # Новые edge-case тесты
    # ------------------------------------------------------------------

    def test_concurrent_writes(self) -> None:
        """5 потоков одновременно пишут записи — все должны оказаться в истории."""
        num_threads = 5
        items_per_thread = 20
        errors: list[Exception] = []

        def worker(thread_idx: int) -> None:
            try:
                for n in range(items_per_thread):
                    self.store.add_history_item(
                        text=f"t{thread_idx}-i{n}",
                        paste_status="failed",
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Ошибки в потоках: {errors}")
        expected = num_threads * items_per_thread
        self.assertEqual(self.store.count_active_items(), expected)

    def test_tombstone_delete_and_compaction(self) -> None:
        """Удалённая через tombstone запись не должна появляться после компактации."""
        item_keep = self.store.add_history_item(text="keep me", paste_status="ok")
        item_del = self.store.add_history_item(text="delete me", paste_status="ok")

        self.store.delete_history_item(item_del.id)

        # До компактации tombstone уже скрывает запись
        page_before, _ = self.store.get_history_page(cursor=None, limit=50)
        ids_before = [i["id"] for i in page_before]
        self.assertIn(item_keep.id, ids_before)
        self.assertNotIn(item_del.id, ids_before)

        self.store.compact()

        page_after, _ = self.store.get_history_page(cursor=None, limit=50)
        ids_after = [i["id"] for i in page_after]
        self.assertIn(item_keep.id, ids_after)
        self.assertNotIn(item_del.id, ids_after)
        self.assertEqual(len(ids_after), 1)

    def test_large_history_pagination(self) -> None:
        """100 записей — полная постраничная навигация собирает все элементы."""
        total = 100
        for idx in range(total):
            self.store.add_history_item(text=f"item-{idx}", paste_status="failed")

        collected: list[dict] = []
        cursor: str | None = None
        page_size = 30
        iterations = 0

        while True:
            page, cursor = self.store.get_history_page(cursor=cursor, limit=page_size)
            collected.extend(page)
            iterations += 1
            if cursor is None:
                break
            self.assertLessEqual(iterations, total, "Бесконечная пагинация")

        self.assertEqual(len(collected), total)
        # Порядок: новые вперёд
        self.assertEqual(collected[0]["text"], "item-99")
        self.assertEqual(collected[-1]["text"], "item-0")

    def test_corrupted_ndjson_line(self) -> None:
        """Битая строка в NDJSON-файле не должна ронять store — остальные записи читаются."""
        item_before = self.store.add_history_item(text="before corrupt", paste_status="ok")

        # Добавляем мусорную строку напрямую в файл
        with self.store.history_path.open("a", encoding="utf-8") as fh:
            fh.write("{invalid json line!!!\n")

        item_after = self.store.add_history_item(text="after corrupt", paste_status="ok")

        # Store должен вернуть обе нормальные записи, игнорируя мусор
        page, _ = self.store.get_history_page(cursor=None, limit=50)
        ids = [i["id"] for i in page]
        self.assertIn(item_before.id, ids)
        self.assertIn(item_after.id, ids)
        self.assertEqual(len(page), 2)

    def test_settings_save_and_load(self) -> None:
        """Сохранённые настройки должны совпадать с загруженными после reload."""
        custom = {
            "translation_mode": "ru_to_es",
            "llm_rewrite_enabled": True,
            "stt_model": "medium",
        }
        saved = self.store.save_settings(custom)

        # Reload через новый экземпляр store (тот же data_dir)
        store2 = StateStore(self.store.data_dir)
        loaded = store2.load_settings()

        for key, val in custom.items():
            self.assertEqual(loaded[key], val, f"Ключ {key!r} не совпадает")

        # save_settings должен вернуть словарь с нашими ключами
        for key, val in custom.items():
            self.assertEqual(saved[key], val)


if __name__ == "__main__":
    unittest.main()
