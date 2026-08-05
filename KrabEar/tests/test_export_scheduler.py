"""Тесты ExportScheduler — планировщик авто-экспорта истории Krab Ear."""

from __future__ import annotations
from backend.export_scheduler import ExportScheduler, SUPPORTED_FORMATS, MAX_EXPORTS_DEFAULT

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

# Путь для импорта
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(items: list[dict] | None = None) -> MagicMock:
    """Создаёт fake StateStore, возвращающий заданные записи."""
    store = MagicMock()
    items_list = items or [
        {"id": "1", "ts": "2026-04-12T10:00:00", "text": "Привет мир", "paste_status": "ok"},
        {"id": "2", "ts": "2026-04-12T11:00:00", "text": "Hello world", "paste_status": "ok"},
    ]
    store.get_history_page_filtered.return_value = (items_list, None)
    return store


# ---------------------------------------------------------------------------
# Тесты конфигурации
# ---------------------------------------------------------------------------

class TestExportSchedulerConfigure(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        self.scheduler = ExportScheduler(data_dir=self.data_dir)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_configure_saves_format(self):
        result = self.scheduler.configure(fmt="csv", interval_hours=12)
        self.assertEqual(result["format"], "csv")
        self.assertEqual(result["interval_hours"], 12)

    def test_configure_persists_to_file(self):
        self.scheduler.configure(fmt="json", interval_hours=6)
        schedule_path = self.data_dir / ExportScheduler.SCHEDULE_FILENAME
        self.assertTrue(schedule_path.exists())
        data = json.loads(schedule_path.read_text(encoding="utf-8"))
        self.assertEqual(data["format"], "json")
        self.assertEqual(data["interval_hours"], 6)

    def test_configure_enabled_flag(self):
        result = self.scheduler.configure(fmt="markdown", enabled=True)
        self.assertTrue(result["enabled"])
        result2 = self.scheduler.configure(fmt="markdown", enabled=False)
        self.assertFalse(result2["enabled"])

    def test_configure_invalid_format_raises(self):
        with self.assertRaises(ValueError):
            self.scheduler.configure(fmt="pdf")

    def test_configure_all_supported_formats(self):
        for fmt in SUPPORTED_FORMATS:
            result = self.scheduler.configure(fmt=fmt)
            self.assertEqual(result["format"], fmt, f"Формат {fmt!r} не сохранился")

    def test_configure_output_dir_custom(self):
        custom_dir = str(self.data_dir / "my_exports")
        result = self.scheduler.configure(fmt="json", output_dir=custom_dir)
        # compare resolved paths to handle macOS /tmp → /private/tmp symlink
        from pathlib import Path
        self.assertEqual(Path(result["output_dir"]).resolve(), Path(custom_dir).resolve())

    def test_configure_output_dir_none(self):
        result = self.scheduler.configure(fmt="json", output_dir=None)
        self.assertIsNone(result["output_dir"])

    def test_configure_interval_minimum_is_1(self):
        result = self.scheduler.configure(fmt="json", interval_hours=0)
        self.assertGreaterEqual(result["interval_hours"], 1)


# ---------------------------------------------------------------------------
# Тесты check_and_export
# ---------------------------------------------------------------------------

class TestCheckAndExport(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        self.scheduler = ExportScheduler(data_dir=self.data_dir)
        self.store = _make_store()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_returns_none_when_disabled(self):
        self.scheduler.configure(fmt="json", enabled=False)
        result = self.scheduler.check_and_export(self.store)
        self.assertIsNone(result)

    def test_first_export_is_performed(self):
        self.scheduler.configure(fmt="json", enabled=True)
        result = self.scheduler.check_and_export(self.store)
        self.assertIsNotNone(result)
        self.assertIn("path", result)
        self.assertIn("format", result)
        self.assertEqual(result["format"], "json")

    def test_export_file_created_on_disk(self):
        self.scheduler.configure(fmt="json", enabled=True)
        result = self.scheduler.check_and_export(self.store)
        p = Path(result["path"])
        self.assertTrue(p.exists(), f"Файл экспорта не найден: {p}")
        self.assertGreater(p.stat().st_size, 0)

    def test_second_export_skipped_too_soon(self):
        self.scheduler.configure(fmt="json", interval_hours=24, enabled=True)
        self.scheduler.check_and_export(self.store)
        result = self.scheduler.check_and_export(self.store)
        self.assertIsNone(result, "Второй экспорт должен быть пропущен (интервал не прошёл)")

    def test_export_performed_after_interval(self):
        self.scheduler.configure(fmt="json", interval_hours=1, enabled=True)
        self.scheduler.check_and_export(self.store)
        # Симулируем прошедший интервал
        schedule = self.scheduler._load_schedule()
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        schedule["last_export_ts"] = past
        self.scheduler._save_schedule(schedule)
        result = self.scheduler.check_and_export(self.store)
        self.assertIsNotNone(result)

    def test_last_export_ts_updated(self):
        self.scheduler.configure(fmt="csv", enabled=True)
        self.scheduler.check_and_export(self.store)
        status = self.scheduler.get_schedule_status()
        self.assertIsNotNone(status["last_export_ts"])

    def test_export_added_to_exports_list(self):
        self.scheduler.configure(fmt="json", enabled=True)
        self.scheduler.check_and_export(self.store)
        exports = self.scheduler.list_exports()
        self.assertEqual(len(exports), 1)


# ---------------------------------------------------------------------------
# Тесты форматов
# ---------------------------------------------------------------------------

class TestExportFormats(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        self.scheduler = ExportScheduler(data_dir=self.data_dir)
        self.store = _make_store()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _run_format(self, fmt: str) -> str:
        self.scheduler.configure(fmt=fmt, enabled=True)
        result = self.scheduler.check_and_export(self.store)
        self.assertIsNotNone(result)
        p = Path(result["path"])
        return p.read_text(encoding="utf-8")

    def test_json_format_is_valid_json(self):
        content = self._run_format("json")
        data = json.loads(content)
        self.assertIn("items", data)
        self.assertIsInstance(data["items"], list)

    def test_json_format_contains_items(self):
        content = self._run_format("json")
        data = json.loads(content)
        self.assertEqual(data["total"], 2)

    def test_csv_format_has_header(self):
        content = self._run_format("csv")
        first_line = content.strip().split("\n")[0]
        self.assertIn("timestamp", first_line)
        self.assertIn("text", first_line)

    def test_markdown_format_has_header(self):
        content = self._run_format("markdown")
        self.assertIn("# Krab Ear", content)

    def test_obsidian_format_has_frontmatter(self):
        content = self._run_format("obsidian")
        self.assertIn("---", content)
        self.assertIn("tags:", content)

    def test_html_format_is_html(self):
        content = self._run_format("html")
        self.assertIn("<!DOCTYPE html>", content)
        self.assertIn("<table>", content)

    def test_srt_format_has_sequence_numbers(self):
        content = self._run_format("srt")
        self.assertIn("1", content)
        self.assertIn("-->", content)

    def test_file_extension_matches_format(self):
        ext_map = {
            "json": ".json",
            "csv": ".csv",
            "markdown": ".md",
            "obsidian": ".md",
            "html": ".html",
            "srt": ".srt",
        }
        for fmt, expected_ext in ext_map.items():
            # Сброс расписания для каждого формата
            self.scheduler.configure(fmt=fmt, enabled=True)
            schedule = self.scheduler._load_schedule()
            schedule["last_export_ts"] = None
            self.scheduler._save_schedule(schedule)
            result = self.scheduler.check_and_export(self.store)
            self.assertIsNotNone(result)
            p = Path(result["path"])
            self.assertEqual(p.suffix, expected_ext, f"Неверное расширение для формата {fmt!r}")


# ---------------------------------------------------------------------------
# Тесты get_schedule_status
# ---------------------------------------------------------------------------

class TestGetScheduleStatus(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        self.scheduler = ExportScheduler(data_dir=self.data_dir)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_status_default_state(self):
        status = self.scheduler.get_schedule_status()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["format"], "json")
        self.assertEqual(status["interval_hours"], 24)
        self.assertIsNone(status["last_export_ts"])
        self.assertIsNone(status["next_export_ts"])
        self.assertEqual(status["total_exports"], 0)

    def test_status_after_configure(self):
        self.scheduler.configure(fmt="csv", interval_hours=6, enabled=True)
        status = self.scheduler.get_schedule_status()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["format"], "csv")
        self.assertEqual(status["interval_hours"], 6)

    def test_status_next_export_ts_calculated(self):
        store = _make_store()
        self.scheduler.configure(fmt="json", interval_hours=24, enabled=True)
        self.scheduler.check_and_export(store)
        status = self.scheduler.get_schedule_status()
        self.assertIsNotNone(status["next_export_ts"])
        # next должен быть позже last
        last = datetime.fromisoformat(status["last_export_ts"])
        nxt = datetime.fromisoformat(status["next_export_ts"])
        self.assertGreater(nxt, last)

    def test_status_output_dir_reflected(self):
        custom = str(self.data_dir / "exports_custom")
        self.scheduler.configure(fmt="json", output_dir=custom)
        status = self.scheduler.get_schedule_status()
        # compare resolved paths to handle macOS /tmp → /private/tmp symlink
        from pathlib import Path
        self.assertEqual(Path(status["output_dir"]).resolve(), Path(custom).resolve())


# ---------------------------------------------------------------------------
# Тесты list_exports
# ---------------------------------------------------------------------------

class TestListExports(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        self.scheduler = ExportScheduler(data_dir=self.data_dir)
        self.store = _make_store()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_empty_before_any_export(self):
        exports = self.scheduler.list_exports()
        self.assertEqual(exports, [])

    def test_contains_entry_after_export(self):
        self.scheduler.configure(fmt="json", enabled=True)
        self.scheduler.check_and_export(self.store)
        exports = self.scheduler.list_exports()
        self.assertEqual(len(exports), 1)
        entry = exports[0]
        self.assertIn("path", entry)
        self.assertIn("format", entry)
        self.assertIn("size_bytes", entry)
        self.assertIn("date", entry)

    def test_excludes_deleted_files(self):
        self.scheduler.configure(fmt="json", enabled=True)
        self.scheduler.check_and_export(self.store)
        exports_before = self.scheduler.list_exports()
        # Удаляем файл с диска
        Path(exports_before[0]["path"]).unlink()
        exports_after = self.scheduler.list_exports()
        self.assertEqual(len(exports_after), 0)

    def test_multiple_exports_listed(self):
        self.scheduler.configure(fmt="json", enabled=True)
        # Создаём 3 экспорта с обнулением last_export_ts
        for _ in range(3):
            schedule = self.scheduler._load_schedule()
            schedule["last_export_ts"] = None
            self.scheduler._save_schedule(schedule)
            self.scheduler.check_and_export(self.store)
            time.sleep(0.01)  # разные имена файлов
        exports = self.scheduler.list_exports()
        self.assertEqual(len(exports), 3)


# ---------------------------------------------------------------------------
# Тесты очистки старых экспортов
# ---------------------------------------------------------------------------

class TestPruneOldExports(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        self.store = _make_store()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_prune_keeps_max_exports(self):
        scheduler = ExportScheduler(data_dir=self.data_dir, max_exports=3)
        scheduler.configure(fmt="json", enabled=True)
        for _ in range(5):
            schedule = scheduler._load_schedule()
            schedule["last_export_ts"] = None
            scheduler._save_schedule(schedule)
            scheduler.check_and_export(self.store)
            time.sleep(0.01)
        exports = scheduler.list_exports()
        self.assertLessEqual(len(exports), 3)

    def test_prune_removes_files_from_disk(self):
        scheduler = ExportScheduler(data_dir=self.data_dir, max_exports=2)
        scheduler.configure(fmt="json", enabled=True)
        paths = []
        for _ in range(4):
            schedule = scheduler._load_schedule()
            schedule["last_export_ts"] = None
            scheduler._save_schedule(schedule)
            result = scheduler.check_and_export(self.store)
            paths.append(Path(result["path"]))
            time.sleep(0.01)
        # Проверяем что на диске <= 2 файлов
        existing = [p for p in paths if p.exists()]
        self.assertLessEqual(len(existing), 2)


# ---------------------------------------------------------------------------
# Тесты DEFAULT_SETTINGS присутствия AUTO_EXPORT_ENABLED
# ---------------------------------------------------------------------------

class TestCancel(unittest.TestCase):
    """Тесты ExportScheduler.cancel — отключение расписания."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        self.scheduler = ExportScheduler(data_dir=self.data_dir)
        self.store = _make_store()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_cancel_disables_scheduler(self):
        self.scheduler.configure(fmt="json", enabled=True)
        self.assertTrue(self.scheduler.get_schedule_status()["enabled"])
        result = self.scheduler.cancel()
        self.assertFalse(result["enabled"])

    def test_cancel_prevents_export(self):
        self.scheduler.configure(fmt="json", enabled=True)
        self.scheduler.cancel()
        result = self.scheduler.check_and_export(self.store)
        self.assertIsNone(result)

    def test_cancel_persists(self):
        self.scheduler.configure(fmt="json", enabled=True)
        self.scheduler.cancel()
        # Reload scheduler from disk — state must persist
        new_sched = ExportScheduler(data_dir=self.data_dir)
        self.assertFalse(new_sched.get_schedule_status()["enabled"])


class TestAutoExportEnabledSetting(unittest.TestCase):

    def test_setting_exists_in_config(self):
        """AUTO_EXPORT_ENABLED должен существовать в core.config.Settings."""
        from core.config import settings
        self.assertFalse(
            settings.AUTO_EXPORT_ENABLED,
            "AUTO_EXPORT_ENABLED должен быть False по умолчанию",
        )

    def test_supported_formats_constant(self):
        self.assertIn("json", SUPPORTED_FORMATS)
        self.assertIn("csv", SUPPORTED_FORMATS)
        self.assertIn("markdown", SUPPORTED_FORMATS)
        self.assertIn("srt", SUPPORTED_FORMATS)
        self.assertIn("obsidian", SUPPORTED_FORMATS)
        self.assertIn("html", SUPPORTED_FORMATS)

    def test_max_exports_default(self):
        self.assertEqual(MAX_EXPORTS_DEFAULT, 30)


# ---------------------------------------------------------------------------
# Тесты IPC-интеграции через BackendService
# ---------------------------------------------------------------------------

class TestIpcIntegration(unittest.TestCase):
    """Проверяем, что IPC-методы зарегистрированы и работают корректно."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make_service(self):
        """Создаёт BackendService с фейковыми зависимостями."""
        from backend.state_store import StateStore
        from backend.service import BackendService
        from unittest.mock import patch

        store = StateStore(data_dir=self.data_dir)

        with patch("backend.service.AudioRecorder"), \
                patch("backend.service.Transcriber"), \
                patch("backend.service.Translator"), \
                patch("backend.service.settings") as mock_settings:
            mock_settings.LLM_ENABLED = False
            mock_settings.AUTO_BACKUP_ENABLED = False
            mock_settings.AUTO_EXPORT_ENABLED = False
            mock_settings.IPC_THROTTLE_ENABLED = False
            mock_settings.IPC_SIGNING_ENABLED = False
            mock_settings.PIPELINE_V2 = False
            mock_settings.TELEGRAM_BRIDGE_URL = "http://localhost:8080"
            # Recording-duration watchdog (2026-08-05): __init__ compares these
            # two numerically — real defaults from core/config.py, not just any
            # placeholder, so the mock behaves like the actual settings object.
            mock_settings.RECORDING_DURATION_WARN_SEC = 600.0
            mock_settings.MAX_DICTATION_DURATION_SEC = 2700.0
            svc = BackendService(store=store)
        return svc

    def test_configure_auto_export_ipc_method(self):
        svc = self._make_service()
        response = svc.handle_request({
            "id": "t1",
            "method": "configure_auto_export",
            "params": {"format": "csv", "interval_hours": 12},
        })
        self.assertTrue(response.get("ok"), response)
        self.assertEqual(response["result"]["format"], "csv")

    def test_get_export_schedule_status_ipc_method(self):
        svc = self._make_service()
        response = svc.handle_request({
            "id": "t2",
            "method": "get_export_schedule_status",
            "params": {},
        })
        self.assertTrue(response.get("ok"), response)
        result = response["result"]
        self.assertIn("enabled", result)
        self.assertIn("format", result)
        self.assertIn("interval_hours", result)

    def test_list_auto_exports_ipc_method(self):
        svc = self._make_service()
        response = svc.handle_request({
            "id": "t3",
            "method": "list_auto_exports",
            "params": {},
        })
        self.assertTrue(response.get("ok"), response)
        self.assertIn("exports", response["result"])
        self.assertIsInstance(response["result"]["exports"], list)

    def test_configure_then_status_consistent(self):
        svc = self._make_service()
        svc.handle_request({
            "id": "t4",
            "method": "configure_auto_export",
            "params": {"format": "markdown", "interval_hours": 6, "enabled": True},
        })
        response = svc.handle_request({
            "id": "t5",
            "method": "get_export_schedule_status",
            "params": {},
        })
        self.assertTrue(response.get("ok"), response)
        result = response["result"]
        self.assertEqual(result["format"], "markdown")
        self.assertEqual(result["interval_hours"], 6)
        self.assertTrue(result["enabled"])


# ---------------------------------------------------------------------------
# W982: тест периодического фонового потока ExportScheduler
# ---------------------------------------------------------------------------

class TestExportSchedulerPeriodicWorker(unittest.TestCase):
    """Проверяем, что BackendService запускает фоновый поток ExportScheduler
    и что он вызывает check_and_export() с правильным store."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make_service(self):
        from backend.state_store import StateStore
        from backend.service import BackendService
        from unittest.mock import patch

        store = StateStore(data_dir=self.data_dir)
        with patch("backend.service.AudioRecorder"), \
                patch("backend.service.Transcriber"), \
                patch("backend.service.Translator"), \
                patch("backend.service.settings") as mock_settings:
            mock_settings.LLM_ENABLED = False
            mock_settings.AUTO_BACKUP_ENABLED = False
            mock_settings.AUTO_EXPORT_ENABLED = False
            mock_settings.IPC_THROTTLE_ENABLED = False
            mock_settings.IPC_SIGNING_ENABLED = False
            mock_settings.PIPELINE_V2 = False
            mock_settings.TELEGRAM_BRIDGE_URL = "http://localhost:8080"
            # Recording-duration watchdog (2026-08-05): __init__ compares these
            # two numerically — real defaults from core/config.py, not just any
            # placeholder, so the mock behaves like the actual settings object.
            mock_settings.RECORDING_DURATION_WARN_SEC = 600.0
            mock_settings.MAX_DICTATION_DURATION_SEC = 2700.0
            svc = BackendService(store=store)
        return svc

    def test_export_scheduler_thread_is_started(self):
        """BackendService.__init__ должен запустить поток 'export-scheduler'."""
        svc = self._make_service()
        thread = getattr(svc, "_export_scheduler_thread", None)
        self.assertIsNotNone(thread, "_export_scheduler_thread должен существовать")
        self.assertTrue(thread.is_alive(), "Поток export-scheduler должен быть запущен")
        self.assertEqual(thread.name, "export-scheduler")
        self.assertTrue(thread.daemon, "Поток export-scheduler должен быть daemon")
        # Cleanup
        svc.close()

    def test_export_scheduler_stop_event_exists(self):
        """BackendService.__init__ должен создать _export_scheduler_stop Event."""
        svc = self._make_service()
        stop = getattr(svc, "_export_scheduler_stop", None)
        self.assertIsNotNone(stop, "_export_scheduler_stop должен существовать")
        svc.close()

    def test_close_stops_export_scheduler_thread(self):
        """close() должен остановить поток export-scheduler."""
        svc = self._make_service()
        thread = svc._export_scheduler_thread
        self.assertTrue(thread.is_alive())
        svc.close()
        # После close() stop_event должен быть выставлен
        self.assertTrue(svc._export_scheduler_stop.is_set())
        # Поток должен завершиться в течение 3 секунд
        thread.join(timeout=3.0)
        self.assertFalse(thread.is_alive(), "Поток export-scheduler должен остановиться после close()")

    def test_export_scheduler_periodic_tick_calls_check_and_export(self):
        """_export_scheduler_loop вызывает check_and_export со store BackendService.

        Тест подменяет check_and_export на Mock, заменяет _EXPORT_SCHEDULER_INTERVAL_SEC=0
        и использует side_effect чтобы выйти из цикла после первого вызова.
        """
        from backend.state_store import StateStore
        from backend.service import BackendService
        from unittest.mock import patch, MagicMock

        store = StateStore(data_dir=self.data_dir)
        with patch("backend.service.AudioRecorder"), \
                patch("backend.service.Transcriber"), \
                patch("backend.service.Translator"), \
                patch("backend.service.settings") as mock_settings:
            mock_settings.LLM_ENABLED = False
            mock_settings.AUTO_BACKUP_ENABLED = False
            mock_settings.AUTO_EXPORT_ENABLED = False
            mock_settings.IPC_THROTTLE_ENABLED = False
            mock_settings.IPC_SIGNING_ENABLED = False
            mock_settings.PIPELINE_V2 = False
            mock_settings.TELEGRAM_BRIDGE_URL = "http://localhost:8080"
            # Recording-duration watchdog (2026-08-05): __init__ compares these
            # two numerically — real defaults from core/config.py, not just any
            # placeholder, so the mock behaves like the actual settings object.
            mock_settings.RECORDING_DURATION_WARN_SEC = 600.0
            mock_settings.MAX_DICTATION_DURATION_SEC = 2700.0
            svc = BackendService(store=store)

        # Stop the background thread immediately so it doesn't race.
        svc._export_scheduler_stop.set()
        svc._export_scheduler_thread.join(timeout=2.0)
        svc._export_scheduler_stop.clear()

        # Подменяем check_and_export на Mock; side_effect останавливает цикл после первого тика.
        def _check_and_stop(store_arg):
            svc._export_scheduler_stop.set()  # прерывает while loop
            return None

        mock_check = MagicMock(side_effect=_check_and_stop)
        svc._export_scheduler.check_and_export = mock_check

        # Устанавливаем нулевой интервал ожидания чтобы цикл не спал.
        svc._EXPORT_SCHEDULER_INTERVAL_SEC = 0

        # Запускаем loop синхронно — он выполнит ровно один тик и выйдет.
        svc._export_scheduler_loop()

        # check_and_export должен был вызваться ровно один раз с self.store.
        mock_check.assert_called_once_with(svc.store)


if __name__ == "__main__":
    unittest.main()
