"""Тесты безлимитного NDJSON-хранилища истории Krab Ear."""

from __future__ import annotations

from pathlib import Path
import json
import sys
import tempfile
import threading
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore


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
        )
        self.store.add_history_item(
            text="translate error",
            paste_status="failed",
            translation_mode="ru_to_es",
            translation_status="translate_error",
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


if __name__ == "__main__":
    unittest.main()
