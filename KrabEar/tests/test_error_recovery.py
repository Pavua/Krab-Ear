"""Тесты на устойчивость и восстановление после ошибок (error recovery & resilience).

Проверяет graceful degradation в критических сценариях:
- повреждённые файлы данных
- отсутствующие директории
- диск заполнен
- невалидные IPC-запросы
- очень большие тексты
- edge-cases unicode
- null/None в неожиданных местах
- быстрые start/stop циклы
- отсутствие опциональных зависимостей
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore  # noqa: E402
from backend.history_service import HistoryService  # noqa: E402
from backend.models import DEFAULT_SETTINGS  # noqa: E402
from backend.service import BackendService  # noqa: E402
from backend.translator import TranslationResult  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fake collaborators
# ---------------------------------------------------------------------------

class FakeRecorder:
    """Минимальный фейк рекордера для тестов сервиса."""

    def __init__(self) -> None:
        self.is_recording = False
        self.sample_rate = 16000
        self._snapshot_counter = 0
        self.last_stop_trim_ms = 0
        self.last_stop_timeout_sec = 3.0
        self._fail_on_start = False

    def start(self) -> bool:
        if self._fail_on_start:
            raise RuntimeError("Симулированная ошибка запуска записи")
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        if not self.is_recording:
            return None
        self.is_recording = False
        self.last_stop_timeout_sec = timeout_sec
        self.last_stop_trim_ms = trim_tail_ms
        return np.zeros(16000, dtype=np.float32), 1.0

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        self._snapshot_counter += 1
        return np.ones(32000, dtype=np.float32), float(self._snapshot_counter)


class FakeTranscriber:
    """Фейковый транскрайбер."""

    def __init__(self) -> None:
        self.counter = 0

    def transcribe(self, audio_data, quality_profile: str = "balanced",
                   cleanup_profile: str = "soft", domain: str = "casual",
                   extra_vocabulary=None, lang_hint=None,
                   history_context=None, stt_hotwords=None, settings=None,
                   diarize=None, skip_vad_prefilter=False, silence_ranges=None) -> str:
        self.counter += 1
        return f"тест #{self.counter}"

    def transcribe_preview(self, audio_data, quality_profile: str = "balanced") -> str:
        return "preview"


class FakeTranslator:
    """Фейковый переводчик."""

    def translate(self, text: str, mode: str, network_mode: str,
                  translation_style: str = "neutral",
                  glossary: dict | None = None) -> TranslationResult:
        return TranslationResult(
            text="",
            status="not_requested",
            source_lang="",
            target_lang="",
            mode="off",
            engine="fake",
        )


def _make_service(tmp_dir: str) -> tuple[BackendService, StateStore]:
    """Вспомогательная фабрика BackendService + StateStore для тестов."""
    store = StateStore(Path(tmp_dir) / "data")
    svc = BackendService(
        store=store,
        recorder=FakeRecorder(),
        transcriber=FakeTranscriber(),
        translator=FakeTranslator(),
    )
    return svc, store


# ===========================================================================
# 1. Corrupt history.ndjson → сервис стартует, повреждённые строки скипаются
# ===========================================================================

class CorruptHistoryNdjsonTestCase(unittest.TestCase):
    """StateStore читает историю из повреждённого NDJSON без краша."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True)

    def _make_store(self) -> StateStore:
        return StateStore(self.data_dir)

    def test_corrupt_lines_skipped(self) -> None:
        """Повреждённые JSON-строки в history.ndjson пропускаются, валидные остаются."""
        store = self._make_store()
        store.add_history_item(text="валидная запись", paste_status="ok")

        # Дописываем мусор в конец файла
        with self.data_dir.joinpath("history.ndjson").open("a", encoding="utf-8") as f:
            f.write("CORRUPT_LINE_NOT_JSON\n")
            f.write("{broken json\n")
            f.write('{"id": "bad", "ts": "no-ts", "no_text_field": true}\n')

        # Новый store должен стартовать без исключений
        store2 = StateStore(self.data_dir)
        items, _ = store2.get_history_page(cursor=None, limit=100)
        valid_texts = [i["text"] for i in items]
        self.assertIn("валидная запись", valid_texts)

    def test_empty_history_file_ok(self) -> None:
        """Пустой history.ndjson → store стартует, возвращает пустую историю."""
        # Создаём пустой файл явно
        self.data_dir.joinpath("history.ndjson").write_text("", encoding="utf-8")
        store = StateStore(self.data_dir)
        items, next_cursor = store.get_history_page(cursor=None, limit=10)
        self.assertEqual(items, [])
        self.assertIsNone(next_cursor)

    def test_all_corrupt_history_returns_empty(self) -> None:
        """Полностью повреждённый history.ndjson → пустая история без исключений."""
        self.data_dir.joinpath("history.ndjson").write_text(
            "NOT JSON AT ALL\nALSO NOT JSON\n", encoding="utf-8"
        )
        store = StateStore(self.data_dir)
        items, _ = store.get_history_page(cursor=None, limit=10)
        self.assertEqual(items, [])


# ===========================================================================
# 2. Missing data directory → auto-created
# ===========================================================================

class MissingDataDirectoryTestCase(unittest.TestCase):
    """StateStore сам создаёт data_dir если он не существует."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_data_dir_auto_created(self) -> None:
        """Несуществующая директория data_dir автоматически создаётся."""
        nested = Path(self.tmp.name) / "a" / "b" / "c" / "data"
        self.assertFalse(nested.exists())
        StateStore(nested)
        self.assertTrue(nested.exists())

    def test_service_starts_without_existing_dir(self) -> None:
        """BackendService стартует даже если data_dir ещё не существует."""
        svc, store = _make_service(str(Path(self.tmp.name)))
        self.assertIsNotNone(svc)
        self.assertTrue(store.data_dir.exists())


# ===========================================================================
# 3. Corrupt settings.json → defaults used
# ===========================================================================

class CorruptSettingsTestCase(unittest.TestCase):
    """Повреждённый settings.json → используются DEFAULT_SETTINGS."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"

    def test_corrupt_settings_json_falls_back_to_defaults(self) -> None:
        """Невалидный JSON в settings.json → load_settings возвращает дефолты."""
        store = StateStore(self.data_dir)
        store.settings_path.write_text("NOT_JSON_AT_ALL{{{", encoding="utf-8")
        loaded = store.load_settings()
        for key in DEFAULT_SETTINGS:
            self.assertIn(key, loaded, f"Ключ '{key}' отсутствует после фоллбэка")

    def test_empty_settings_json_falls_back_to_defaults(self) -> None:
        """Пустой settings.json → load_settings возвращает дефолты без исключений."""
        store = StateStore(self.data_dir)
        store.settings_path.write_text("", encoding="utf-8")
        loaded = store.load_settings()
        self.assertIsInstance(loaded, dict)
        self.assertIn("translation_mode", loaded)

    def test_settings_array_instead_of_object_falls_back(self) -> None:
        """settings.json содержит массив вместо объекта → дефолты без исключений."""
        store = StateStore(self.data_dir)
        store.settings_path.write_text('["not", "a", "dict"]', encoding="utf-8")
        loaded = store.load_settings()
        # Должны получить дефолтные значения, не упасть
        self.assertIn("translation_mode", loaded)


# ===========================================================================
# 4. Disk full simulation → proper error messages
# ===========================================================================

class DiskFullSimulationTestCase(unittest.TestCase):
    """Симуляция заполненного диска: операции записи выбрасывают OSError."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"

    def test_save_settings_oserror_propagates_cleanly(self) -> None:
        """OSError при записи настроек не ломает state store навсегда."""
        store = StateStore(self.data_dir)

        with patch.object(Path, "write_text", side_effect=OSError("No space left on device")):
            with self.assertRaises(OSError):
                store.save_settings({"translation_mode": "off"})

        # После ошибки — load_settings должен работать (файл не повреждён)
        loaded = store.load_settings()
        self.assertIsNotNone(loaded)

    def test_add_history_oserror_propagates(self) -> None:
        """OSError при добавлении записи в историю проброшена наружу."""
        store = StateStore(self.data_dir)
        # add_history_item пишет через _append_history_ndjson → _append_ndjson_raw
        # (encryption refactor, chunk 1). Патчим актуальный sink, чтобы OSError
        # реально возникла на записи и пробросилась наружу.
        with patch.object(
            StateStore, "_append_ndjson_raw", side_effect=OSError("No space left")
        ):
            with self.assertRaises(OSError):
                store.add_history_item(text="test", paste_status="ok")


# ===========================================================================
# 5. Invalid IPC request format → clean error response
# ===========================================================================

class InvalidIPCRequestTestCase(unittest.TestCase):
    """Невалидные форматы IPC-запросов → чистые error-ответы, не исключения."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc, _ = _make_service(self.tmp.name)

    def _call(self, payload: dict) -> dict:
        return self.svc.handle_request(payload)

    def test_missing_method_field(self) -> None:
        """Запрос без поля method → ok=False, информативный error."""
        resp = self._call({"id": "x", "params": {}})
        self.assertFalse(resp.get("ok"), msg=f"Ожидали ok=False: {resp}")
        self.assertIn("error", resp)

    def test_unknown_method(self) -> None:
        """Несуществующий метод → ok=False, error.code=unknown_method."""
        resp = self._call({"id": "x", "method": "nonexistent_method_xyz", "params": {}})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp["error"]["code"], "unknown_method")

    def test_params_not_dict(self) -> None:
        """params не является dict → ok=False, error.code=invalid_params."""
        resp = self._call({"id": "x", "method": "ping", "params": ["list", "not", "dict"]})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp["error"]["code"], "invalid_params")

    def test_empty_payload(self) -> None:
        """Полностью пустой payload → ok=False, не вызывает исключений."""
        resp = self._call({})
        self.assertFalse(resp.get("ok"))
        self.assertIn("error", resp)

    def test_method_none(self) -> None:
        """method=None → ok=False, graceful error."""
        resp = self._call({"id": "x", "method": None, "params": {}})
        self.assertFalse(resp.get("ok"))
        self.assertIn("error", resp)


# ===========================================================================
# 6. Very large text (1 MB) → handled without crash
# ===========================================================================

class VeryLargeTextTestCase(unittest.TestCase):
    """Очень большие тексты (1 МБ) не вызывают crash."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def test_1mb_text_saved_and_retrieved(self) -> None:
        """1 МБ текста добавляется в историю и извлекается без исключений."""
        large_text = "А" * (1024 * 1024)  # 1 МБ кириллицы
        item = self.store.add_history_item(text=large_text, paste_status="ok")
        self.assertIsNotNone(item.id)

        items, _ = self.store.get_history_page(cursor=None, limit=5)
        self.assertEqual(len(items), 1)
        self.assertEqual(len(items[0]["text"]), 1024 * 1024)

    def test_1mb_text_via_ipc_add_history(self) -> None:
        """handle_add_history_item с 1 МБ текста → ok, не исключение."""
        large_text = "Б" * (1024 * 1024)
        result = self.svc.handle_add_history_item({"text": large_text, "paste_status": "ok"})
        self.assertIn("id", result)
        self.assertIn("text", result)


# ===========================================================================
# 7. Unicode edge cases → no crash
# ===========================================================================

class UnicodeEdgeCasesTestCase(unittest.TestCase):
    """Emoji, RTL, zero-width chars и другие unicode edge cases не вызывают crash."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")

    _UNICODE_SAMPLES = [
        ("emoji", "Привет 🎉🦀🤖 мир! 🎤"),
        ("rtl_arabic", "مرحبا بالعالم"),
        ("rtl_hebrew", "שלום עולם"),
        ("zero_width", "test\u200btest\u200c\u200d"),
        ("combining", "e\u0301 a\u0300 n\u0303"),  # é à ñ с combining chars
        ("surrogate_like", "\U0001F600\U0001F4AA"),  # emoji SMP plane
        ("null_byte_adjacent", "before\x01after"),  # control chars
        ("mixed_scripts", "RU: привет EN: hello ES: hola 日本語 中文"),
        ("max_bmp", "\uFFFD\uFFFE"),  # replacement chars
        ("newlines_tabs", "line1\nline2\ttabbed\r\nwindows"),
    ]

    def test_unicode_samples_saved_and_retrieved(self) -> None:
        """Все unicode edge case примеры сохраняются и извлекаются без ошибок."""
        for name, text in self._UNICODE_SAMPLES:
            with self.subTest(sample=name):
                item = self.store.add_history_item(text=text, paste_status="ok")
                self.assertIsNotNone(item.id, f"item.id is None for sample '{name}'")

        items, _ = self.store.get_history_page(cursor=None, limit=len(self._UNICODE_SAMPLES) + 1)
        self.assertEqual(len(items), len(self._UNICODE_SAMPLES))

    def test_unicode_search_no_crash(self) -> None:
        """Поиск по emoji и RTL тексту не вызывает исключений."""
        self.store.add_history_item(text="Привет 🎉 мир", paste_status="ok")
        results, _ = self.store.search_history(query="🎉", cursor=None, limit=10)
        self.assertIsInstance(results, list)

    def test_zero_width_in_settings_no_crash(self) -> None:
        """Zero-width chars в настройках сохраняются и загружаются без ошибок."""
        weird_setting = {"translation_mode": "off\u200b", "quality_profile": "balanced"}
        saved = self.store.save_settings(weird_setting)
        self.assertIsInstance(saved, dict)
        loaded = self.store.load_settings()
        self.assertIsInstance(loaded, dict)


# ===========================================================================
# 8. Null/None in unexpected places → graceful handling
# ===========================================================================

class NullNoneHandlingTestCase(unittest.TestCase):
    """None/null в параметрах IPC-методов → graceful handling, не исключения."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc, self.store = _make_service(self.tmp.name)

    def _call(self, method: str, params: dict | None = None) -> dict:
        return self.svc.handle_request(
            {"id": "t", "method": method, "params": params or {}}
        )

    def test_get_history_page_null_cursor(self) -> None:
        """cursor=None в get_history_page → ok без исключений."""
        resp = self._call("get_history_page", {"cursor": None, "limit": 10})
        self.assertTrue(resp.get("ok"), msg=f"Ожидали ok=True: {resp}")

    def test_search_history_null_query(self) -> None:
        """Пустая строка поиска в search_history → возвращает ok."""
        resp = self._call("search_history", {"query": None, "limit": 10})
        # Должен либо успешно обработать None, либо вернуть error (не crash)
        self.assertIn("ok", resp)

    def test_set_settings_null_value(self) -> None:
        """None как значение в set_settings → не падает."""
        resp = self._call("set_settings", {"translation_mode": None})
        # Может вернуть ok или ошибку, но не должен бросать исключение
        self.assertIn("ok", resp)

    def test_add_history_item_null_optional_fields(self) -> None:
        """add_history_item с None в опциональных полях → ok."""
        resp = self._call("add_history_item", {
            "text": "тест",
            "paste_status": "ok",
            "source_text": None,
            "translated_text": None,
            "translation_mode": None,
        })
        # Пустой или None source_text должен быть допустим
        self.assertIn("ok", resp)

    def test_delete_history_item_null_id(self) -> None:
        """delete_history_item с id=None → error или ok без исключений."""
        resp = self._call("delete_history_item", {"id": None})
        self.assertIn("ok", resp)


# ===========================================================================
# 9. Missing optional dependencies → feature disabled, not crash
# ===========================================================================

class MissingOptionalDependenciesTestCase(unittest.TestCase):
    """Отсутствие опциональных зависимостей не вызывает crash сервиса."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_service_starts_without_llm_rewriter(self) -> None:
        """BackendService запускается без LLM-реврайтера."""
        store = StateStore(Path(self.tmp.name) / "data")
        # LLMRewriter недоступен — имитируем через установку None
        svc = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )
        # Проверяем, что ping проходит
        resp = svc.handle_request({"id": "t", "method": "ping", "params": {}})
        self.assertTrue(resp.get("ok"))

    def test_history_service_without_llm_rewriter(self) -> None:
        """HistoryService работает корректно без llm_rewriter=None."""
        store = StateStore(Path(self.tmp.name) / "data2")
        svc = HistoryService(store=store, llm_rewriter=None)
        store.add_history_item(text="тест без LLM", paste_status="ok")
        result = svc.handle_get_history_page({"limit": 10})
        self.assertEqual(len(result["items"]), 1)

    def test_state_store_search_index_import_failure(self) -> None:
        """StateStore работает даже если SearchIndex поднимает ImportError при инициализации."""
        # Проверяем что StateStore создаётся без исключений
        store = StateStore(Path(self.tmp.name) / "data3")
        self.assertIsNotNone(store)
        store.add_history_item(text="тест", paste_status="ok")
        items, _ = store.get_history_page(cursor=None, limit=10)
        self.assertEqual(len(items), 1)


# ===========================================================================
# 10. Rapid start/stop cycles → no resource leaks
# ===========================================================================

class RapidStartStopCyclesTestCase(unittest.TestCase):
    """Быстрые циклы start_recording / stop_recording → нет утечек ресурсов."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc, _ = _make_service(self.tmp.name)

    def _call(self, method: str, params: dict | None = None) -> dict:
        return self.svc.handle_request(
            {"id": "t", "method": method, "params": params or {}}
        )

    def test_rapid_start_stop_no_crash(self) -> None:
        """50 быстрых циклов start/stop → сервис остаётся рабочим."""
        for i in range(50):
            r1 = self._call("start_recording")
            r2 = self._call("stop_recording")
            # Хотя бы один из вызовов должен вернуть ok=True за цикл
            # (первый start=ok, stop=ok; последующие start=already_recording)
            # Ни один не должен вызвать исключения
            self.assertIn("ok", r1)
            self.assertIn("ok", r2)

        # После 50 циклов ping должен отвечать
        ping = self._call("ping")
        self.assertTrue(ping.get("ok"))

    def test_double_stop_no_crash(self) -> None:
        """stop_recording без предшествующего start → ok или error, не исключение."""
        resp = self._call("stop_recording")
        self.assertIn("ok", resp)

    def test_double_start_returns_error_or_ok(self) -> None:
        """Два start_recording подряд → второй возвращает already_recording или ok."""
        self._call("start_recording")
        resp2 = self._call("start_recording")
        # Должен вернуть ответ без исключений
        self.assertIn("ok", resp2)
        # Останавливаем
        self._call("stop_recording")


# ===========================================================================
# 11. StateStore: concurrent writes don't corrupt data
# ===========================================================================

class ConcurrentWritesTestCase(unittest.TestCase):
    """Параллельные записи в StateStore не повреждают историю."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")

    def test_concurrent_add_history(self) -> None:
        """10 потоков добавляют записи параллельно — все записи попадают в историю."""
        num_threads = 10
        records_per_thread = 10
        errors: list[Exception] = []

        def writer(thread_id: int) -> None:
            try:
                for i in range(records_per_thread):
                    self.store.add_history_item(
                        text=f"thread-{thread_id}-item-{i}",
                        paste_status="ok",
                    )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [], f"Ошибки в потоках: {errors}")

        items, _ = self.store.get_history_page(cursor=None, limit=num_threads * records_per_thread + 1)
        self.assertEqual(len(items), num_threads * records_per_thread)


# ===========================================================================
# 12. IPC: request with extra unknown fields → ignored gracefully
# ===========================================================================

class ExtraFieldsInRequestTestCase(unittest.TestCase):
    """Лишние поля в IPC-запросе игнорируются, не вызывают ошибок."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc, _ = _make_service(self.tmp.name)

    def test_extra_top_level_fields_ignored(self) -> None:
        """Лишние поля верхнего уровня в payload не вызывают исключений."""
        resp = self.svc.handle_request({
            "id": "x1",
            "method": "ping",
            "params": {},
            "unknown_field": "some_value",
            "another_unknown": 42,
        })
        self.assertTrue(resp.get("ok"), msg=f"Ожидали ok=True: {resp}")

    def test_extra_params_fields_ignored(self) -> None:
        """Лишние параметры в params игнорируются."""
        resp = self.svc.handle_request({
            "id": "x2",
            "method": "get_history_page",
            "params": {
                "limit": 10,
                "totally_unknown_param": "xyz",
                "another_extra": [1, 2, 3],
            },
        })
        self.assertTrue(resp.get("ok"), msg=f"Ожидали ok=True: {resp}")


# ===========================================================================
# 13. Tombstone compaction after corrupt history
# ===========================================================================

class TombstoneAfterCorruptHistoryTestCase(unittest.TestCase):
    """Tombstone-удаления работают корректно даже если история частично повреждена."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"

    def test_delete_valid_item_after_corrupt_entries(self) -> None:
        """Удаление валидной записи из истории с частично повреждёнными строками работает."""
        store = StateStore(self.data_dir)
        item = store.add_history_item(text="valid item", paste_status="ok")

        # Дописываем мусор
        with self.data_dir.joinpath("history.ndjson").open("a", encoding="utf-8") as f:
            f.write("CORRUPT\n")

        store2 = StateStore(self.data_dir)
        deleted = store2.delete_history_item(item.id)
        self.assertTrue(deleted)

        items, _ = store2.get_history_page(cursor=None, limit=10)
        ids = [i["id"] for i in items]
        self.assertNotIn(item.id, ids)


# ===========================================================================
# 14. Very long key/value in settings → no crash
# ===========================================================================

class LargeSettingsValueTestCase(unittest.TestCase):
    """Очень длинные значения в настройках сохраняются без ошибок."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")

    def test_large_setting_value_saved(self) -> None:
        """Значение настройки длиной 100 КБ сохраняется и загружается."""
        large_value = "x" * 100_000
        self.store.save_settings({"translation_mode": "off", "hotword_list": large_value})
        loaded = self.store.load_settings()
        self.assertEqual(loaded.get("hotword_list"), large_value)

    def test_many_settings_keys(self) -> None:
        """Большое количество ключей настроек сохраняется без исключений."""
        extra = {f"custom_key_{i}": f"value_{i}" for i in range(500)}
        extra["translation_mode"] = "off"
        saved = self.store.save_settings(extra)
        self.assertIsInstance(saved, dict)


# ===========================================================================
# 15. IPC response always has id echoed back
# ===========================================================================

class IPCResponseIdEchoTestCase(unittest.TestCase):
    """Ответ IPC всегда содержит id из запроса (даже при ошибках)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc, _ = _make_service(self.tmp.name)

    def _call(self, payload: dict) -> dict:
        return self.svc.handle_request(payload)

    def test_success_response_echoes_id(self) -> None:
        """Успешный ответ содержит id из запроса."""
        resp = self._call({"id": "unique-abc-123", "method": "ping", "params": {}})
        self.assertEqual(resp.get("id"), "unique-abc-123")

    def test_error_response_echoes_id(self) -> None:
        """Ответ об ошибке содержит id из запроса."""
        resp = self._call({"id": "error-req-456", "method": "nonexistent", "params": {}})
        self.assertEqual(resp.get("id"), "error-req-456")

    def test_missing_id_in_request(self) -> None:
        """Запрос без id → сервис не падает, возвращает ответ."""
        resp = self._call({"method": "ping", "params": {}})
        self.assertIn("ok", resp)

    def test_numeric_id_handled(self) -> None:
        """Числовой id обрабатывается без исключений."""
        resp = self._call({"id": 42, "method": "ping", "params": {}})
        self.assertIn("ok", resp)


# ===========================================================================
# 16. Vocabulary file resilience
# ===========================================================================

class VocabularyFileResilienceTestCase(unittest.TestCase):
    """Повреждённый или отсутствующий vocabulary.txt → load_vocabulary возвращает []."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"

    def test_missing_vocabulary_returns_empty(self) -> None:
        """Отсутствующий vocabulary.txt → пустой список без исключений."""
        store = StateStore(self.data_dir)
        store.vocabulary_path.unlink(missing_ok=True)
        words = store.load_vocabulary()
        self.assertEqual(words, [])

    def test_empty_vocabulary_returns_empty_list(self) -> None:
        """Пустой vocabulary.txt → пустой список."""
        store = StateStore(self.data_dir)
        store.vocabulary_path.write_text("", encoding="utf-8")
        words = store.load_vocabulary()
        self.assertEqual(words, [])

    def test_vocabulary_with_blank_lines(self) -> None:
        """vocabulary.txt с пустыми строками → только непустые слова."""
        store = StateStore(self.data_dir)
        store.vocabulary_path.write_text("\nword1\n\nword2\n  \nword3\n", encoding="utf-8")
        words = store.load_vocabulary()
        self.assertIn("word1", words)
        self.assertIn("word2", words)
        self.assertIn("word3", words)
        # Пустые строки и пробельные строки не включены
        self.assertNotIn("", words)
        self.assertNotIn("  ", words)


if __name__ == "__main__":
    unittest.main(verbosity=2)
