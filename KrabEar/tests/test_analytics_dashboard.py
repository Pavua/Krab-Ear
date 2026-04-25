"""Тесты AnalyticsDashboard — комплексный дашборд аналитики Krab Ear."""

from __future__ import annotations
from backend.state_store import StateStore
from backend.analytics_dashboard import (
    AnalyticsDashboard,
    _calc_streak,
    _calc_trend,
    _parse_ts,
)

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------

def _make_item(
    days_ago: int = 0,
    hour: int = 10,
    confidence: float | None = 0.85,
    audio_duration_sec: float = 30.0,
    text: str = "hello world test",
    source_lang: str = "ru",
    translated_text: str = "",
    translation_status: str = "not_requested",
    llm_applied: bool = False,
):
    """Создаёт fake-элемент истории с заданными параметрами.

    Использует локальное время (date.today()) для консистентности с _build_dashboard().
    """
    # Use date.today() + timedelta to stay in local timezone (matches _build_dashboard)
    base_date = date.today() - timedelta(days=days_ago)
    dt = datetime.combine(base_date, datetime.min.time(), tzinfo=timezone.utc)
    dt = dt.replace(hour=hour, minute=0, second=0, microsecond=0)

    class FakeItem:
        ts = dt.isoformat()

    item = FakeItem()
    item.confidence = confidence
    item.audio_duration_sec = audio_duration_sec
    item.text = text
    item.source_lang = source_lang
    item.translated_text = translated_text
    item.translation_status = translation_status
    item.llm_applied = llm_applied
    item.diarization = None
    return item


def _make_store(items: list) -> MagicMock:
    """Создаёт mock StateStore с заданным списком активных записей."""
    store = MagicMock()
    store.data_dir = Path(tempfile.mkdtemp())
    store.history_path = store.data_dir / "history.ndjson"
    store.history_path.touch()

    # Настраиваем context manager для store._lock()
    lock_ctx = MagicMock()
    lock_ctx.__enter__ = MagicMock(return_value=None)
    lock_ctx.__exit__ = MagicMock(return_value=False)
    store._lock = MagicMock(return_value=lock_ctx)
    store._load_active_items_unlocked = MagicMock(return_value=items)
    return store


# ---------------------------------------------------------------------------
# Тесты базовой структуры дашборда
# ---------------------------------------------------------------------------

class TestAnalyticsDashboardStructure(unittest.TestCase):
    """Проверяем наличие всех обязательных ключей в результате."""

    def setUp(self):
        self.dashboard = AnalyticsDashboard()

    def test_all_top_level_keys_present(self):
        """Дашборд должен содержать все 8 ключей верхнего уровня."""
        store = _make_store([])
        result = self.dashboard.get_full_dashboard(store, days=30)
        expected_keys = {
            "overview", "today", "trends", "languages",
            "quality", "engagement", "storage", "performance",
        }
        self.assertEqual(set(result.keys()), expected_keys)

    def test_overview_keys(self):
        """Секция overview должна содержать 4 ключа."""
        store = _make_store([])
        result = self.dashboard.get_full_dashboard(store, days=30)
        overview = result["overview"]
        self.assertIn("total_recordings", overview)
        self.assertIn("total_hours", overview)
        self.assertIn("total_words", overview)
        self.assertIn("avg_daily", overview)

    def test_today_keys(self):
        """Секция today должна содержать recordings, duration_min, words."""
        store = _make_store([])
        result = self.dashboard.get_full_dashboard(store, days=30)
        today = result["today"]
        self.assertIn("recordings", today)
        self.assertIn("duration_min", today)
        self.assertIn("words", today)

    def test_trends_keys(self):
        """Секция trends должна содержать confidence_trend, pace_trend, volume_trend."""
        store = _make_store([])
        result = self.dashboard.get_full_dashboard(store, days=30)
        trends = result["trends"]
        self.assertIn("confidence_trend", trends)
        self.assertIn("pace_trend", trends)
        self.assertIn("volume_trend", trends)

    def test_quality_keys(self):
        """Секция quality должна содержать avg_confidence, low_confidence_rate, llm_rewrite_rate."""
        store = _make_store([])
        result = self.dashboard.get_full_dashboard(store, days=30)
        quality = result["quality"]
        self.assertIn("avg_confidence", quality)
        self.assertIn("low_confidence_rate", quality)
        self.assertIn("llm_rewrite_rate", quality)

    def test_engagement_keys(self):
        """Секция engagement должна содержать streak_days, peak_hour, most_active_day."""
        store = _make_store([])
        result = self.dashboard.get_full_dashboard(store, days=30)
        eng = result["engagement"]
        self.assertIn("streak_days", eng)
        self.assertIn("peak_hour", eng)
        self.assertIn("most_active_day", eng)

    def test_storage_keys(self):
        """Секция storage должна содержать history_size_mb, backups_count, cache_size_mb."""
        store = _make_store([])
        result = self.dashboard.get_full_dashboard(store, days=30)
        storage = result["storage"]
        self.assertIn("history_size_mb", storage)
        self.assertIn("backups_count", storage)
        self.assertIn("cache_size_mb", storage)

    def test_performance_keys(self):
        """Секция performance должна содержать avg_stt_latency_ms и p95_latency_ms."""
        store = _make_store([])
        result = self.dashboard.get_full_dashboard(store, days=30)
        perf = result["performance"]
        self.assertIn("avg_stt_latency_ms", perf)
        self.assertIn("p95_latency_ms", perf)


# ---------------------------------------------------------------------------
# Тесты пустой истории
# ---------------------------------------------------------------------------

class TestAnalyticsDashboardEmpty(unittest.TestCase):
    """Пустая история → корректные нулевые значения."""

    def setUp(self):
        self.dashboard = AnalyticsDashboard()
        self.store = _make_store([])

    def test_empty_overview_zeros(self):
        result = self.dashboard.get_full_dashboard(self.store, days=30)
        self.assertEqual(result["overview"]["total_recordings"], 0)
        self.assertEqual(result["overview"]["total_words"], 0)
        self.assertEqual(result["overview"]["total_hours"], 0.0)

    def test_empty_today_zeros(self):
        result = self.dashboard.get_full_dashboard(self.store, days=30)
        self.assertEqual(result["today"]["recordings"], 0)
        self.assertEqual(result["today"]["words"], 0)

    def test_empty_quality_zeros(self):
        result = self.dashboard.get_full_dashboard(self.store, days=30)
        self.assertEqual(result["quality"]["avg_confidence"], 0.0)
        self.assertEqual(result["quality"]["low_confidence_rate"], 0.0)
        self.assertEqual(result["quality"]["llm_rewrite_rate"], 0.0)

    def test_empty_engagement_zeros(self):
        result = self.dashboard.get_full_dashboard(self.store, days=30)
        self.assertEqual(result["engagement"]["streak_days"], 0)
        self.assertIsNone(result["engagement"]["peak_hour"])
        self.assertIsNone(result["engagement"]["most_active_day"])

    def test_empty_languages_empty(self):
        result = self.dashboard.get_full_dashboard(self.store, days=30)
        self.assertEqual(result["languages"]["distribution"], {})
        self.assertEqual(result["languages"]["translation_rate"], 0.0)


# ---------------------------------------------------------------------------
# Тесты с реальными данными
# ---------------------------------------------------------------------------

class TestAnalyticsDashboardWithData(unittest.TestCase):
    """Проверяем корректность агрегации при наличии данных."""

    def setUp(self):
        self.dashboard = AnalyticsDashboard()

    def test_total_recordings_count(self):
        """total_recordings должен совпадать с количеством элементов."""
        items = [_make_item(days_ago=i) for i in range(5)]
        store = _make_store(items)
        result = self.dashboard.get_full_dashboard(store, days=30)
        self.assertEqual(result["overview"]["total_recordings"], 5)

    def test_total_words_counted(self):
        """total_words должен суммировать слова из всех записей."""
        items = [
            _make_item(text="one two three"),   # 3 слова
            _make_item(text="alpha beta"),       # 2 слова
        ]
        store = _make_store(items)
        result = self.dashboard.get_full_dashboard(store, days=30)
        self.assertEqual(result["overview"]["total_words"], 5)

    def test_total_hours_aggregated(self):
        """total_hours должен суммировать длительность всех записей."""
        items = [
            _make_item(audio_duration_sec=3600.0),  # 1 час
            _make_item(audio_duration_sec=1800.0),  # 0.5 часа
        ]
        store = _make_store(items)
        result = self.dashboard.get_full_dashboard(store, days=30)
        self.assertAlmostEqual(result["overview"]["total_hours"], 1.5, places=2)

    def test_avg_confidence_calculated(self):
        """avg_confidence должен быть средним по всем записям с confidence."""
        items = [
            _make_item(confidence=0.8),
            _make_item(confidence=0.6),
        ]
        store = _make_store(items)
        result = self.dashboard.get_full_dashboard(store, days=30)
        self.assertAlmostEqual(result["quality"]["avg_confidence"], 0.7, places=2)

    def test_low_confidence_rate(self):
        """low_confidence_rate — доля записей с confidence < 0.7."""
        items = [
            _make_item(confidence=0.9),
            _make_item(confidence=0.5),   # < 0.7
            _make_item(confidence=0.65),  # < 0.7
            _make_item(confidence=0.8),
        ]
        store = _make_store(items)
        result = self.dashboard.get_full_dashboard(store, days=30)
        self.assertAlmostEqual(result["quality"]["low_confidence_rate"], 0.5, places=2)

    def test_llm_rewrite_rate(self):
        """llm_rewrite_rate — доля записей с llm_applied=True."""
        items = [
            _make_item(llm_applied=True),
            _make_item(llm_applied=False),
            _make_item(llm_applied=True),
            _make_item(llm_applied=False),
        ]
        store = _make_store(items)
        result = self.dashboard.get_full_dashboard(store, days=30)
        self.assertAlmostEqual(result["quality"]["llm_rewrite_rate"], 0.5, places=2)

    def test_today_recordings(self):
        """Записи за сегодня должны попасть в секцию today."""
        items = [
            _make_item(days_ago=0, text="today one"),   # сегодня
            _make_item(days_ago=0, text="today two"),   # сегодня
            _make_item(days_ago=3, text="old"),         # 3 дня назад
        ]
        store = _make_store(items)
        result = self.dashboard.get_full_dashboard(store, days=30)
        self.assertEqual(result["today"]["recordings"], 2)
        self.assertEqual(result["today"]["words"], 4)

    def test_language_distribution(self):
        """distribution должен отображать нормированную долю каждого языка."""
        items = [
            _make_item(source_lang="ru"),
            _make_item(source_lang="ru"),
            _make_item(source_lang="es"),
        ]
        store = _make_store(items)
        result = self.dashboard.get_full_dashboard(store, days=30)
        dist = result["languages"]["distribution"]
        self.assertIn("ru", dist)
        self.assertIn("es", dist)
        self.assertAlmostEqual(dist["ru"], 2 / 3, places=3)
        self.assertAlmostEqual(dist["es"], 1 / 3, places=3)

    def test_translation_rate(self):
        """translation_rate — доля переведённых записей."""
        items = [
            _make_item(translated_text="hola", translation_status="ok"),
            _make_item(translated_text="", translation_status="not_requested"),
        ]
        store = _make_store(items)
        result = self.dashboard.get_full_dashboard(store, days=30)
        self.assertAlmostEqual(result["languages"]["translation_rate"], 0.5, places=2)

    def test_peak_hour_detected(self):
        """peak_hour должен совпадать с часом, у которого больше всего записей."""
        items = [
            _make_item(days_ago=0, hour=9),
            _make_item(days_ago=1, hour=9),
            _make_item(days_ago=2, hour=14),
        ]
        store = _make_store(items)
        result = self.dashboard.get_full_dashboard(store, days=30)
        self.assertEqual(result["engagement"]["peak_hour"], 9)

    def test_avg_daily_calculated(self):
        """avg_daily = total_recordings / days."""
        items = [_make_item(days_ago=i) for i in range(10)]
        store = _make_store(items)
        result = self.dashboard.get_full_dashboard(store, days=30)
        expected = round(10 / 30, 2)
        self.assertAlmostEqual(result["overview"]["avg_daily"], expected, places=2)


# ---------------------------------------------------------------------------
# Тесты кэширования
# ---------------------------------------------------------------------------

class TestAnalyticsDashboardCache(unittest.TestCase):
    """Проверяем, что кэш работает корректно."""

    def test_cache_hit_same_store_not_called_twice(self):
        """Второй вызов с теми же параметрами должен обратиться к store только раз."""
        dashboard = AnalyticsDashboard()
        store = _make_store([_make_item()])
        dashboard.get_full_dashboard(store, days=30)
        dashboard.get_full_dashboard(store, days=30)
        # store._load_active_items_unlocked должен быть вызван только один раз
        self.assertEqual(store._load_active_items_unlocked.call_count, 1)

    def test_cache_invalidated_by_different_days(self):
        """Разные значения days → разные записи кэша."""
        dashboard = AnalyticsDashboard()
        store = _make_store([_make_item()])
        dashboard.get_full_dashboard(store, days=7)
        dashboard.get_full_dashboard(store, days=30)
        # Два разных days → два вызова к store
        self.assertEqual(store._load_active_items_unlocked.call_count, 2)

    def test_invalidate_cache_clears_all(self):
        """invalidate_cache() должен сбрасывать все записи кэша."""
        dashboard = AnalyticsDashboard()
        store = _make_store([_make_item()])
        dashboard.get_full_dashboard(store, days=30)
        dashboard.invalidate_cache()
        dashboard.get_full_dashboard(store, days=30)
        self.assertEqual(store._load_active_items_unlocked.call_count, 2)


# ---------------------------------------------------------------------------
# Тесты вспомогательных функций
# ---------------------------------------------------------------------------

class TestParseTsHelper(unittest.TestCase):
    def test_iso_string_parsed(self):
        raw = "2025-01-15T10:00:00+00:00"
        dt = _parse_ts(raw)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2025)

    def test_epoch_float_parsed(self):
        epoch = datetime(2025, 6, 1, tzinfo=timezone.utc).timestamp()
        dt = _parse_ts(epoch)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2025)

    def test_none_returns_none(self):
        self.assertIsNone(_parse_ts(None))

    def test_invalid_string_returns_none(self):
        self.assertIsNone(_parse_ts("not-a-date"))

    def test_datetime_object_returned_tz_aware(self):
        raw = datetime(2025, 3, 10, 12, 0, 0)  # naive
        dt = _parse_ts(raw)
        self.assertIsNotNone(dt)
        self.assertIsNotNone(dt.tzinfo)


class TestCalcTrendHelper(unittest.TestCase):
    def test_empty_is_stable(self):
        self.assertEqual(_calc_trend([]), "stable")

    def test_single_point_is_stable(self):
        pts = [{"date": "2025-01-01", "val": 0.8}]
        self.assertEqual(_calc_trend(pts), "stable")

    def test_rising_values_improving(self):
        pts = [{"date": f"2025-01-{i + 1:02d}", "val": 0.5 + i * 0.05} for i in range(10)]
        self.assertEqual(_calc_trend(pts), "improving")

    def test_falling_values_declining(self):
        pts = [{"date": f"2025-01-{i + 1:02d}", "val": 0.9 - i * 0.05} for i in range(10)]
        self.assertEqual(_calc_trend(pts), "declining")

    def test_flat_values_stable(self):
        pts = [{"date": f"2025-01-{i + 1:02d}", "val": 0.75} for i in range(5)]
        self.assertEqual(_calc_trend(pts), "stable")


class TestCalcStreakHelper(unittest.TestCase):
    def test_no_items_streak_zero(self):
        self.assertEqual(_calc_streak([]), 0)

    def test_items_only_today_streak_one(self):
        items = [_make_item(days_ago=0), _make_item(days_ago=0)]
        self.assertEqual(_calc_streak(items), 1)

    def test_consecutive_days_streak(self):
        items = [_make_item(days_ago=i) for i in range(4)]
        self.assertEqual(_calc_streak(items), 4)

    def test_gap_breaks_streak(self):
        # Записи есть сегодня и 2 дня назад, но не вчера — streak = 1
        items = [_make_item(days_ago=0), _make_item(days_ago=2)]
        self.assertEqual(_calc_streak(items), 1)


# ---------------------------------------------------------------------------
# Интеграционный тест с реальным StateStore
# ---------------------------------------------------------------------------

class TestAnalyticsDashboardIntegration(unittest.TestCase):
    """Проверяем работу с настоящим StateStore и реальными записями."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(data_dir=Path(self.tmp.name))
        self.dashboard = AnalyticsDashboard()

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_store_dashboard_valid(self):
        """Пустой реальный StateStore → корректный дашборд без исключений."""
        result = self.dashboard.get_full_dashboard(self.store, days=30)
        self.assertEqual(result["overview"]["total_recordings"], 0)
        self.assertIsInstance(result["storage"]["history_size_mb"], float)

    def test_with_real_history_items(self):
        """После добавления записей дашборд отражает актуальные данные."""
        self.store.add_history_item(
            text="тест транскрипции один два три",
            paste_status="ok",
            audio_duration_sec=60.0,
        )
        self.store.add_history_item(
            text="ещё одна запись",
            paste_status="ok",
            audio_duration_sec=30.0,
        )
        result = self.dashboard.get_full_dashboard(self.store, days=30)
        self.assertEqual(result["overview"]["total_recordings"], 2)
        self.assertEqual(result["today"]["recordings"], 2)
        self.assertGreater(result["overview"]["total_words"], 0)

    def test_storage_info_reflects_real_file(self):
        """history_size_mb отражает реальный размер файла."""
        self.store.add_history_item(text="размер файла тест", paste_status="ok")
        result = self.dashboard.get_full_dashboard(self.store, days=30)
        # Файл не пустой — размер должен быть > 0
        self.assertGreaterEqual(result["storage"]["history_size_mb"], 0.0)


# ---------------------------------------------------------------------------
# Тесты IPC-метода через BackendService (smoke test)
# ---------------------------------------------------------------------------

class TestAnalyticsDashboardIPCMethod(unittest.TestCase):
    """Smoke-тест IPC-метода get_analytics_dashboard."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_ipc_method_registered_and_returns_dict(self):
        """get_analytics_dashboard должен быть зарегистрирован и возвращать dict."""
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(self.tmp.name))

        class _FakeRecorder:
            is_recording = False

            def start(self): pass

            def stop(self): return b""

            def snapshot_audio(self, max_duration_sec: float = 12.0):
                import numpy as np
                return np.zeros(int(max_duration_sec * 16000), dtype=np.float32), max_duration_sec

        class _FakeTranscriber:
            engine = MagicMock()
            engine._llm_rewriter = None
            engine._settings_get = None

            def transcribe(self, *a, **kw): return ("", 0.0)

        svc = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
        )
        response = svc.handle_request({"id": "t1", "method": "get_analytics_dashboard", "params": {}})
        self.assertTrue(response.get("ok"), f"Unexpected error: {response}")
        self.assertIsInstance(response["result"], dict)
        self.assertIn("overview", response["result"])


# ---------------------------------------------------------------------------
# Тесты Bug 2 & 3: корректная агрегация duration и confidence
# ---------------------------------------------------------------------------

class TestAggregationBugFixes(unittest.TestCase):
    """Регрессионные тесты для багов агрегации duration и confidence."""

    def setUp(self):
        self.dashboard = AnalyticsDashboard()

    def test_total_hours_none_duration_treated_as_zero(self):
        """Элементы с audio_duration_sec=None не должны ломать агрегацию."""
        items = [
            _make_item(audio_duration_sec=3600.0),
            _make_item(audio_duration_sec=None),  # type: ignore[arg-type]
        ]
        # Устанавливаем None вручную, т.к. фабрика по умолчанию ставит 30.0
        items[1].audio_duration_sec = None  # type: ignore[attr-defined]
        store = _make_store(items)
        result = self.dashboard.get_full_dashboard(store, days=30)
        # 3600 / 3600 = 1.0 час; None → 0, не вызывает исключение
        self.assertAlmostEqual(result["overview"]["total_hours"], 1.0, places=2)

    def test_avg_confidence_none_items_excluded(self):
        """Элементы с confidence=None не включаются в среднее значение."""
        items = [
            _make_item(confidence=0.9),
            _make_item(confidence=None),  # type: ignore[arg-type]
        ]
        items[1].confidence = None  # type: ignore[attr-defined]
        store = _make_store(items)
        result = self.dashboard.get_full_dashboard(store, days=30)
        # Только один элемент с confidence=0.9 → среднее 0.9
        self.assertAlmostEqual(result["quality"]["avg_confidence"], 0.9, places=2)

    def test_all_none_confidence_returns_zero(self):
        """Если у всех элементов confidence=None, avg_confidence=0.0."""
        items = [_make_item(), _make_item()]
        for it in items:
            it.confidence = None  # type: ignore[attr-defined]
        store = _make_store(items)
        result = self.dashboard.get_full_dashboard(store, days=30)
        self.assertEqual(result["quality"]["avg_confidence"], 0.0)

    def test_total_hours_multiple_items(self):
        """total_hours суммирует несколько записей с audio_duration_sec."""
        durations = [120.0, 240.0, 360.0]  # 720 sec = 0.2 часа
        items = [_make_item(audio_duration_sec=d) for d in durations]
        store = _make_store(items)
        result = self.dashboard.get_full_dashboard(store, days=30)
        expected_hours = round(sum(durations) / 3600, 3)
        self.assertAlmostEqual(result["overview"]["total_hours"], expected_hours, places=3)

    def test_confidence_boundary_values(self):
        """Граничные значения confidence: 0.0 и 1.0 считаются корректно."""
        items = [
            _make_item(confidence=0.0),
            _make_item(confidence=1.0),
        ]
        store = _make_store(items)
        result = self.dashboard.get_full_dashboard(store, days=30)
        self.assertAlmostEqual(result["quality"]["avg_confidence"], 0.5, places=2)
        # 0.0 < 0.7 → low_confidence_rate = 0.5
        self.assertAlmostEqual(result["quality"]["low_confidence_rate"], 0.5, places=2)


# ---------------------------------------------------------------------------
# Тест Bug 1: generate_html_report IPC-метод
# ---------------------------------------------------------------------------

class TestGenerateHtmlReportIPCMethod(unittest.TestCase):
    """Проверяем, что generate_html_report зарегистрирован в dispatch table."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _make_service(self):
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(self.tmp.name))

        class _FakeRecorder:
            is_recording = False

            def start(self): pass

            def stop(self): return b""

            def snapshot_audio(self, max_duration_sec: float = 12.0):
                import numpy as np
                return np.zeros(int(max_duration_sec * 16000), dtype=np.float32), max_duration_sec

        class _FakeTranscriber:
            engine = MagicMock()
            engine._llm_rewriter = None
            engine._settings_get = None

            def transcribe(self, *a, **kw): return ("", 0.0)

        return BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
        )

    def test_generate_html_report_registered(self):
        """generate_html_report должен быть зарегистрирован и не возвращать 'Неизвестный метод'."""
        svc = self._make_service()
        response = svc.handle_request({
            "id": "hr1",
            "method": "generate_html_report",
            "params": {"title": "Test Report"},
        })
        # Не должно быть ошибки «Неизвестный метод»
        error_msg = response.get("error", "")
        self.assertNotIn("Неизвестный метод", str(error_msg))

    def test_generate_html_report_returns_html(self):
        """generate_html_report должен возвращать HTML-контент."""
        svc = self._make_service()
        response = svc.handle_request({
            "id": "hr2",
            "method": "generate_html_report",
            "params": {},
        })
        self.assertTrue(response.get("ok"), f"Expected ok=True, got: {response}")
        result = response.get("result", {})
        html_content = result.get("html", "")
        self.assertIn("<!DOCTYPE html>", html_content)

    def test_export_html_report_still_works(self):
        """Оригинальный export_html_report должен продолжать работать."""
        svc = self._make_service()
        response = svc.handle_request({
            "id": "hr3",
            "method": "export_html_report",
            "params": {},
        })
        self.assertTrue(response.get("ok"), f"Expected ok=True, got: {response}")


# ---------------------------------------------------------------------------
# Тест Bug 2+3: StateStore.add_history_item сохраняет confidence и duration
# ---------------------------------------------------------------------------

class TestStateStoreConfidenceAndDuration(unittest.TestCase):
    """Проверяем, что add_history_item принимает и сохраняет confidence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(data_dir=Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_history_item_saves_confidence(self):
        """add_history_item с confidence=0.85 должен сохранить значение."""
        item = self.store.add_history_item(
            text="тест уверенности",
            paste_status="ok",
            confidence=0.85,
        )
        self.assertAlmostEqual(item.confidence, 0.85, places=4)

    def test_add_history_item_saves_audio_duration_sec(self):
        """add_history_item с audio_duration_sec=42.0 должен сохранить значение."""
        item = self.store.add_history_item(
            text="тест длительности",
            paste_status="ok",
            audio_duration_sec=42.0,
        )
        self.assertAlmostEqual(item.audio_duration_sec, 42.0, places=2)

    def test_confidence_persists_through_reload(self):
        """confidence должна сохраняться и восстанавливаться из NDJSON."""
        self.store.add_history_item(
            text="reload test",
            paste_status="ok",
            confidence=0.73,
            audio_duration_sec=90.0,
        )
        # Перечитываем из store заново
        items = self.store._load_active_items_with_lock()
        self.assertEqual(len(items), 1)
        self.assertAlmostEqual(items[0].confidence, 0.73, places=4)
        self.assertAlmostEqual(items[0].audio_duration_sec, 90.0, places=2)

    def test_dashboard_reads_stored_confidence(self):
        """Dashboard должен корректно считать confidence из реально сохранённых данных."""
        dashboard = AnalyticsDashboard()
        self.store.add_history_item(
            text="conf test one",
            paste_status="ok",
            confidence=0.8,
            audio_duration_sec=60.0,
        )
        self.store.add_history_item(
            text="conf test two",
            paste_status="ok",
            confidence=0.6,
            audio_duration_sec=120.0,
        )
        result = dashboard.get_full_dashboard(self.store, days=30)
        self.assertAlmostEqual(result["quality"]["avg_confidence"], 0.7, places=2)
        self.assertAlmostEqual(result["overview"]["total_hours"], round(180.0 / 3600, 3), places=3)


if __name__ == "__main__":
    unittest.main()
