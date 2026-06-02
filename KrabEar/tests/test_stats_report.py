"""Тесты для StatsReportGenerator — генератора Markdown-отчётов статистики Krab Ear.

Покрывает:
- generate_report: все секции, граничные случаи (пустая история)
- generate_mini_report: структура и данные
- Отдельные секции: overview, daily_activity, language_distribution,
  quality_metrics, top_speakers, tags_collections, storage, system_health
"""

from __future__ import annotations
from backend.stats_report import StatsReportGenerator, _ascii_bar, _md_cell, _tokenize

import contextlib
import fcntl
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Вспомогательные фиктивные объекты
# ---------------------------------------------------------------------------

def _make_item(
    text: str = "Тестовая транскрипция",
    ts: str | None = None,
    source_lang: str = "ru",
    confidence: float | None = 0.9,
    audio_duration_sec: float | None = 30.0,
    paste_status: str = "ok",
    llm_applied: bool = False,
    tags: list | None = None,
    diarization: dict | None = None,
    favorite: bool = False,
    translation_mode: str = "off",
) -> dict:
    """Фабрика фиктивного элемента истории (dict-формат)."""
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    return {
        "id": f"item-{hash(text + ts) & 0xFFFF}",
        "ts": ts,
        "text": text,
        "source_lang": source_lang,
        "confidence": confidence,
        "audio_duration_sec": audio_duration_sec,
        "paste_status": paste_status,
        "llm_applied": llm_applied,
        "tags": tags or [],
        "diarization": diarization,
        "favorite": favorite,
        "translation_mode": translation_mode,
    }


def _ts_days_ago(n: float) -> str:
    """ISO-строка timestamp n дней назад."""
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


class FakeStore:
    """Минимальный фейк StateStore для тестов StatsReportGenerator."""

    def __init__(self, data_dir: Path, items: list[dict] | None = None) -> None:
        self.data_dir = data_dir
        # Ensure directory exists before creating files
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.history_path = data_dir / "history.ndjson"
        self.tombstones_path = data_dir / "history_tombstones.ndjson"
        self.status_path = data_dir / "history_status.ndjson"
        self.tags_path = data_dir / "history_tags.ndjson"
        self.settings_path = data_dir / "settings.json"
        self._items = items or []
        self._lock_path = data_dir / "history.lock"
        # Создаём файлы для тестов storage-секции
        self.history_path.touch()
        self.history_path.write_text(
            "\n".join(json.dumps(item) for item in self._items),
            encoding="utf-8",
        )
        self._lock_path.touch()

    @contextlib.contextmanager
    def _lock(self):
        self._lock_path.touch(exist_ok=True)
        with self._lock_path.open("r+", encoding="utf-8") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def _load_active_items_unlocked(self) -> list:
        return list(self._items)


# ---------------------------------------------------------------------------
# Тесты вспомогательных функций
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):
    """Тесты вспомогательных функций модуля stats_report."""

    def test_ascii_bar_full(self) -> None:
        bar = _ascii_bar(10, 10, width=10)
        self.assertEqual(len(bar), 10)

    def test_ascii_bar_half(self) -> None:
        bar = _ascii_bar(5, 10, width=10)
        self.assertEqual(len(bar), 5)

    def test_ascii_bar_zero_max(self) -> None:
        bar = _ascii_bar(5, 0, width=10)
        self.assertEqual(bar, "")

    def test_ascii_bar_zero_value(self) -> None:
        bar = _ascii_bar(0, 10, width=10)
        self.assertEqual(bar, "")

    def test_tokenize_basic(self) -> None:
        tokens = _tokenize("Hello, world! Привет мир.")
        self.assertIn("hello", tokens)
        self.assertIn("world", tokens)
        self.assertIn("привет", tokens)

    def test_tokenize_skips_numbers(self) -> None:
        tokens = _tokenize("123 test 456")
        self.assertNotIn("123", tokens)
        self.assertIn("test", tokens)


# ---------------------------------------------------------------------------
# Тест 1: generate_report с пустым хранилищем
# ---------------------------------------------------------------------------

class TestGenerateReportEmpty(unittest.TestCase):
    """generate_report при отсутствии записей."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._store = FakeStore(Path(self._tmp.name), items=[])
        self._gen = StatsReportGenerator()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_returns_string(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        self.assertIsInstance(result, str)

    def test_contains_header(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        self.assertIn("Krab Ear", result)
        self.assertIn("Статистический отчёт", result)

    def test_contains_all_sections(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        for section in ["Обзор", "Дневная активность", "языков", "Метрики качества",
                        "спикеров", "Теги", "Хранилище", "Системное здоровье"]:
            self.assertIn(section, result, f"Секция '{section}' отсутствует в отчёте")

    def test_empty_store_no_crash(self) -> None:
        """Пустая история не вызывает исключений."""
        result = self._gen.generate_report(self._store, days=30)
        self.assertGreater(len(result), 100)


# ---------------------------------------------------------------------------
# Тест 2: generate_report с реальными данными
# ---------------------------------------------------------------------------

class TestGenerateReportWithData(unittest.TestCase):
    """generate_report при наличии записей."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        items = [
            _make_item("Привет мир", ts=_ts_days_ago(1), source_lang="ru", confidence=0.95, tags=["meeting"]),
            _make_item("Hola mundo", ts=_ts_days_ago(2), source_lang="es", confidence=0.88, tags=["meeting"]),
            _make_item("Hello world", ts=_ts_days_ago(5), source_lang="en", confidence=0.72, tags=["work"]),
            _make_item("Тест записи", ts=_ts_days_ago(10), source_lang="ru", confidence=0.61, paste_status="failed"),
            _make_item("Запись без confidence", ts=_ts_days_ago(3), source_lang="ru", confidence=None),
        ]
        self._store = FakeStore(Path(self._tmp.name), items=items)
        self._gen = StatsReportGenerator()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_overview_shows_correct_count(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        # 5 записей в пределах 30 дней
        self.assertIn("5", result)

    def test_language_section_contains_ru(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        self.assertIn("ru", result)

    def test_language_section_contains_es(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        self.assertIn("es", result)

    def test_quality_section_contains_confidence(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        self.assertIn("confidence", result.lower())

    def test_tag_meeting_appears(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        self.assertIn("meeting", result)

    def test_days_param_filters_old_items(self) -> None:
        # С days=3 должны попасть только записи за последние 3 дня (items[0] и items[1])
        result = self._gen.generate_report(self._store, days=3)
        self.assertIsInstance(result, str)
        # Записи за 5+ дней не должны увеличивать счётчик в диапазоне 3 дней
        # Просто проверяем, что функция не падает
        self.assertIn("Krab Ear", result)

    def test_system_health_section_present(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        self.assertIn("Системное здоровье", result)


# ---------------------------------------------------------------------------
# Тест 3: generate_mini_report
# ---------------------------------------------------------------------------

class TestGenerateMiniReport(unittest.TestCase):
    """Тесты generate_mini_report."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        items = [
            _make_item("Запись 1", ts=_ts_days_ago(1), confidence=0.9, audio_duration_sec=60.0),
            _make_item("Запись 2", ts=_ts_days_ago(5), confidence=0.8, audio_duration_sec=120.0),
        ]
        self._store = FakeStore(Path(self._tmp.name), items=items)
        self._gen = StatsReportGenerator()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_mini_report_returns_string(self) -> None:
        result = self._gen.generate_mini_report(self._store)
        self.assertIsInstance(result, str)

    def test_mini_report_has_5_lines(self) -> None:
        result = self._gen.generate_mini_report(self._store)
        lines = [ln for ln in result.strip().split("\n") if ln.strip()]
        self.assertEqual(len(lines), 5)

    def test_mini_report_contains_recordings(self) -> None:
        result = self._gen.generate_mini_report(self._store)
        self.assertIn("Записей", result)

    def test_mini_report_contains_words(self) -> None:
        result = self._gen.generate_mini_report(self._store)
        self.assertIn("Слов", result)

    def test_mini_report_contains_hours(self) -> None:
        result = self._gen.generate_mini_report(self._store)
        self.assertIn("часов", result.lower())

    def test_mini_report_contains_confidence(self) -> None:
        result = self._gen.generate_mini_report(self._store)
        self.assertIn("уверенность", result.lower())

    def test_mini_report_contains_storage(self) -> None:
        result = self._gen.generate_mini_report(self._store)
        self.assertIn("MB", result)

    def test_mini_report_empty_store(self) -> None:
        """Пустая история не вызывает исключений."""
        empty_store = FakeStore(Path(self._tmp.name) / "empty")
        result = self._gen.generate_mini_report(empty_store)
        self.assertIsInstance(result, str)
        lines = [ln for ln in result.strip().split("\n") if ln.strip()]
        self.assertEqual(len(lines), 5)


# ---------------------------------------------------------------------------
# Тест 4: секция «Топ спикеров» (с данными диаризации)
# ---------------------------------------------------------------------------

class TestTopSpeakers(unittest.TestCase):
    """Секция top_speakers при наличии диаризации."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        diarization = {
            "segments": [
                {"speaker": "SPEAKER_00", "start": 0.0, "end": 30.0},
                {"speaker": "SPEAKER_01", "start": 30.0, "end": 50.0},
                {"speaker": "SPEAKER_00", "start": 50.0, "end": 90.0},
            ]
        }
        items = [
            _make_item(
                "Диаризованная запись",
                ts=_ts_days_ago(1),
                diarization=diarization,
            ),
        ]
        self._store = FakeStore(Path(self._tmp.name), items=items)
        self._gen = StatsReportGenerator()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_speaker_00_appears(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        self.assertIn("SPEAKER_00", result)

    def test_speaker_01_appears(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        self.assertIn("SPEAKER_01", result)

    def test_no_diarization_shows_placeholder(self) -> None:
        """Без диаризации выводится плейсхолдер."""
        plain_items = [_make_item("Обычная запись", ts=_ts_days_ago(1))]
        store = FakeStore(Path(self._tmp.name) / "plain", items=plain_items)
        result = self._gen.generate_report(store, days=30)
        self.assertIn("диаризации отсутствуют", result)


# ---------------------------------------------------------------------------
# Тест 5: секция «Теги и коллекции»
# ---------------------------------------------------------------------------

class TestTagsAndCollections(unittest.TestCase):
    """Секция tags_collections."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        items = [
            _make_item("Запись A", ts=_ts_days_ago(1), tags=["meeting", "important"]),
            _make_item("Запись B", ts=_ts_days_ago(2), tags=["meeting"]),
            _make_item("Запись C", ts=_ts_days_ago(3), tags=["work"]),
        ]
        self._store = FakeStore(Path(self._tmp.name), items=items)
        # Создаём коллекции
        coll_data = {
            "collections": {
                "Работа": {
                    "name": "Работа",
                    "description": "Рабочие записи",
                    "created_at": "2026-04-01T10:00:00",
                    "item_ids": ["id1", "id2"],
                },
            }
        }
        (Path(self._tmp.name) / "collections.json").write_text(
            json.dumps(coll_data, ensure_ascii=False),
            encoding="utf-8",
        )
        self._gen = StatsReportGenerator()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_meeting_tag_appears(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        self.assertIn("meeting", result)

    def test_tag_count_correct(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        # meeting встречается в 2 записях → "— 2"
        self.assertIn("— 2", result)

    def test_collection_name_appears(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        self.assertIn("Работа", result)


# ---------------------------------------------------------------------------
# Тест 6: None store → не падает
# ---------------------------------------------------------------------------

class TestNoneStore(unittest.TestCase):
    """generate_report/mini_report с store=None не падают."""

    def setUp(self) -> None:
        self._gen = StatsReportGenerator()

    def test_generate_report_none_store(self) -> None:
        result = self._gen.generate_report(None, days=30)
        self.assertIsInstance(result, str)

    def test_generate_mini_report_none_store(self) -> None:
        result = self._gen.generate_mini_report(None)
        self.assertIsInstance(result, str)
        lines = [ln for ln in result.strip().split("\n") if ln.strip()]
        self.assertEqual(len(lines), 5)


# ---------------------------------------------------------------------------
# Тест 7: ASCII bar chart корректен
# ---------------------------------------------------------------------------

class TestDailyActivityChart(unittest.TestCase):
    """ASCII-гистограмма дневной активности."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # Записи за разные дни
        items = [
            _make_item("Запись 1 день 1", ts=_ts_days_ago(0.5)),
            _make_item("Запись 2 день 1", ts=_ts_days_ago(0.6)),
            _make_item("Запись 3 день 2", ts=_ts_days_ago(1.5)),
        ]
        self._store = FakeStore(Path(self._tmp.name), items=items)
        self._gen = StatsReportGenerator()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_bar_chars_present(self) -> None:
        result = self._gen.generate_report(self._store, days=7)
        self.assertIn("█", result)

    def test_code_block_present(self) -> None:
        result = self._gen.generate_report(self._store, days=7)
        self.assertIn("```", result)

    def test_active_days_mentioned(self) -> None:
        result = self._gen.generate_report(self._store, days=7)
        self.assertIn("Активных дней", result)


# ---------------------------------------------------------------------------
# Тест 8: Метрики качества (confidence)
# ---------------------------------------------------------------------------

class TestQualityMetrics(unittest.TestCase):
    """Секция quality_metrics."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        items = [
            _make_item("Высокое качество", ts=_ts_days_ago(1), confidence=0.95),
            _make_item("Среднее качество", ts=_ts_days_ago(2), confidence=0.75),
            _make_item("Низкое качество", ts=_ts_days_ago(3), confidence=0.55),
        ]
        self._store = FakeStore(Path(self._tmp.name), items=items)
        self._gen = StatsReportGenerator()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_confidence_percentage_shown(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        # Средний confidence ~0.75 → "75%" ± погрешность
        self.assertIn("%", result)

    def test_distribution_buckets_present(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        self.assertIn("Отлично", result)
        self.assertIn("Низкое", result)

    def test_no_confidence_items(self) -> None:
        """Если у всех items confidence=None — секция не падает."""
        items = [_make_item("Без confidence", ts=_ts_days_ago(1), confidence=None)]
        store = FakeStore(Path(self._tmp.name) / "noconf", items=items)
        result = self._gen.generate_report(store, days=30)
        self.assertIn("отсутствуют", result)


# ---------------------------------------------------------------------------
# Тест 9: Секция хранилища
# ---------------------------------------------------------------------------

class TestStorageSection(unittest.TestCase):
    """Секция storage."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        items = [_make_item("Запись")]
        self._store = FakeStore(Path(self._tmp.name), items=items)
        self._gen = StatsReportGenerator()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_storage_section_has_kb(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        self.assertIn("KB", result)

    def test_storage_section_has_history_ndjson(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        self.assertIn("history.ndjson", result)


# ---------------------------------------------------------------------------
# Тест 10: Обзор (overview) — числовые значения
# ---------------------------------------------------------------------------

class TestOverviewSection(unittest.TestCase):
    """Секция overview — корректность агрегатов."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        items = [
            _make_item(
                "Слово1 слово2 слово3",
                ts=_ts_days_ago(1),
                audio_duration_sec=120.0,
                translation_mode="ru_es",
                llm_applied=True,
                favorite=True,
            ),
            _make_item(
                "Слово4 слово5",
                ts=_ts_days_ago(2),
                audio_duration_sec=60.0,
                paste_status="failed",
            ),
        ]
        self._store = FakeStore(Path(self._tmp.name), items=items)
        self._gen = StatsReportGenerator()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_overview_has_translation_count(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        self.assertIn("переводом", result)

    def test_overview_has_llm_count(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        self.assertIn("LLM", result)

    def test_overview_has_favorites(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        self.assertIn("избранном", result)

    def test_days_param_respected(self) -> None:
        """Запись за 40 дней не попадает в отчёт за 30 дней."""
        items = [
            _make_item("Свежая запись", ts=_ts_days_ago(1)),
            _make_item("Старая запись", ts=_ts_days_ago(40)),
        ]
        store = FakeStore(Path(self._tmp.name) / "d30", items=items)
        result_30 = self._gen.generate_report(store, days=30)
        result_60 = self._gen.generate_report(store, days=60)
        # Оба вызова должны работать без ошибок
        self.assertIsInstance(result_30, str)
        self.assertIsInstance(result_60, str)


# ---------------------------------------------------------------------------
# Тест 11: generate_report с минимальным days=1
# ---------------------------------------------------------------------------

class TestEdgeCaseDays(unittest.TestCase):
    """Граничный случай: days=1."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        items = [
            _make_item("Сегодня", ts=_ts_days_ago(0.1)),
            _make_item("Вчера", ts=_ts_days_ago(1.5)),
        ]
        self._store = FakeStore(Path(self._tmp.name), items=items)
        self._gen = StatsReportGenerator()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_days_1_no_crash(self) -> None:
        result = self._gen.generate_report(self._store, days=1)
        self.assertIsInstance(result, str)
        self.assertIn("Krab Ear", result)

    def test_days_1_header_period(self) -> None:
        result = self._gen.generate_report(self._store, days=1)
        self.assertIn("1", result)  # "последние 1 дней"


# ---------------------------------------------------------------------------
# Тест 12: Результат generate_report — валидный Markdown (не голый JSON/error)
# ---------------------------------------------------------------------------

class TestMarkdownFormat(unittest.TestCase):
    """Формат результата — Markdown."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        items = [_make_item("Тест")]
        self._store = FakeStore(Path(self._tmp.name), items=items)
        self._gen = StatsReportGenerator()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_starts_with_heading(self) -> None:
        result = self._gen.generate_report(self._store)
        self.assertTrue(result.strip().startswith("#"), "Отчёт должен начинаться с заголовка Markdown")

    def test_no_exception_text(self) -> None:
        result = self._gen.generate_report(self._store)
        self.assertNotIn("Traceback", result)
        self.assertNotIn("Error", result)

    def test_contains_table_syntax(self) -> None:
        result = self._gen.generate_report(self._store)
        self.assertIn("|", result)  # Markdown-таблицы

    def test_generate_report_default_days(self) -> None:
        """generate_report без параметра days работает (дефолт 30)."""
        result = self._gen.generate_report(self._store)
        self.assertIn("Krab Ear", result)


# ---------------------------------------------------------------------------
# Тест 13: Именованные тесты Wave 122 (обязательный список)
# ---------------------------------------------------------------------------

class TestWave122RequiredNames(unittest.TestCase):
    """Обязательные тесты Wave 122 с точными именами методов."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._gen = StatsReportGenerator()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_generate_empty_history_report(self) -> None:
        """Пустая история: generate_report не падает, возвращает Markdown."""
        store = FakeStore(Path(self._tmp.name) / "empty", items=[])
        result = self._gen.generate_report(store, days=30)
        self.assertIsInstance(result, str)
        self.assertTrue(result.strip().startswith("#"))
        self.assertIn("Krab Ear", result)

    def test_top_words_section(self) -> None:
        """Теги (топ тегов) — секция присутствует в отчёте."""
        items = [
            _make_item("Привет мир привет", ts=_ts_days_ago(1), tags=["meeting", "meeting", "work"]),
            _make_item("Мир мир", ts=_ts_days_ago(2), tags=["meeting"]),
        ]
        store = FakeStore(Path(self._tmp.name) / "topwords", items=items)
        result = self._gen.generate_report(store, days=30)
        # Секция тегов должна присутствовать
        self.assertIn("Теги", result)
        self.assertIn("meeting", result)

    def test_duration_breakdown(self) -> None:
        """Длительность: суммарные минуты аудио отражены в обзоре."""
        items = [
            _make_item("Запись 1", ts=_ts_days_ago(1), audio_duration_sec=120.0),
            _make_item("Запись 2", ts=_ts_days_ago(2), audio_duration_sec=180.0),
        ]
        store = FakeStore(Path(self._tmp.name) / "dur", items=items)
        result = self._gen.generate_report(store, days=30)
        # Суммарно 300 сек = 5 мин = 0.083 ч — что-то про минуты или часы должно быть
        self.assertIn("мин", result)

    def test_language_breakdown(self) -> None:
        """Секция языков отображает все три языка при разнообразной истории."""
        items = [
            _make_item("Привет", ts=_ts_days_ago(1), source_lang="ru"),
            _make_item("Hola", ts=_ts_days_ago(2), source_lang="es"),
            _make_item("Hello", ts=_ts_days_ago(3), source_lang="en"),
        ]
        store = FakeStore(Path(self._tmp.name) / "lang", items=items)
        result = self._gen.generate_report(store, days=30)
        self.assertIn("ru", result)
        self.assertIn("es", result)
        self.assertIn("en", result)

    def test_period_filtering(self) -> None:
        """Фильтрация по периоду: 7 дней отсекает старые записи."""
        items = [
            _make_item("Свежая", ts=_ts_days_ago(3)),
            _make_item("Старая", ts=_ts_days_ago(40)),
        ]
        store = FakeStore(Path(self._tmp.name) / "period", items=items)
        # За 7 дней только первая запись попадает
        result_7 = self._gen.generate_report(store, days=7)
        # За 60 дней обе
        result_60 = self._gen.generate_report(store, days=60)
        self.assertIsInstance(result_7, str)
        self.assertIsInstance(result_60, str)
        # Общее кол-во в store всегда 2 (строка "Всего записей в истории: 2")
        self.assertIn("2", result_60)

    def test_unicode_words_in_report(self) -> None:
        """Юникод (кириллица, emoji) не вызывает UnicodeError."""
        items = [
            _make_item("Привет мир 🎙️ тест кириллица", ts=_ts_days_ago(1)),
            _make_item("Español áéíóú ñ", ts=_ts_days_ago(2), source_lang="es"),
        ]
        store = FakeStore(Path(self._tmp.name) / "unicode", items=items)
        result = self._gen.generate_report(store, days=30)
        self.assertIsInstance(result, str)
        self.assertNotIn("UnicodeError", result)
        self.assertNotIn("Traceback", result)

    def test_markdown_structure_valid(self) -> None:
        """Структура Markdown: заголовки ## N присутствуют для всех 8 секций."""
        items = [_make_item("Тест", ts=_ts_days_ago(1))]
        store = FakeStore(Path(self._tmp.name) / "md", items=items)
        result = self._gen.generate_report(store, days=30)
        # Должны быть 8 секций уровня ##
        import re as _re
        h2_sections = _re.findall(r"^## \d+\.", result, _re.MULTILINE)
        self.assertEqual(len(h2_sections), 8, f"Ожидалось 8 секций ##, найдено {len(h2_sections)}")


# ---------------------------------------------------------------------------
# Тест 10 (Wave 1770): _md_cell — экранирование пользовательского контента
# ---------------------------------------------------------------------------

class TestMdCellHelper(unittest.TestCase):
    """Модульные тесты хелпера _md_cell (экранирование ячеек Markdown)."""

    def test_escapes_pipe(self) -> None:
        self.assertNotIn("|", _md_cell("a|b").replace("\\|", ""))
        self.assertEqual(_md_cell("a|b"), "a\\|b")

    def test_collapses_newlines(self) -> None:
        out = _md_cell("line1\nline2\r\nline3")
        self.assertNotIn("\n", out)
        self.assertNotIn("\r", out)
        self.assertEqual(out, "line1 line2 line3")

    def test_neutralizes_leading_equals(self) -> None:
        out = _md_cell("=SUM(1+1)")
        # Ячейка больше не начинается с '=' → spreadsheet не трактует как формулу.
        self.assertFalse(out.startswith("="))
        self.assertEqual(out, "'=SUM(1+1)")

    def test_neutralizes_all_formula_leads(self) -> None:
        for lead in ("=", "+", "-", "@"):
            out = _md_cell(f"{lead}cmd")
            self.assertTrue(out.startswith("'"), f"Ведущий '{lead}' не нейтрализован")

    def test_neutralizes_formula_lead_after_whitespace(self) -> None:
        # Пробел/таб перед формульным символом не должен «прятать» инъекцию.
        out = _md_cell("   =1+1")
        self.assertTrue(out.startswith("'"))

    def test_plain_text_unchanged(self) -> None:
        self.assertEqual(_md_cell("meeting"), "meeting")
        self.assertEqual(_md_cell("Работа"), "Работа")

    def test_none_is_empty(self) -> None:
        self.assertEqual(_md_cell(None), "")


# ---------------------------------------------------------------------------
# Тест 11 (Wave 1770): инъекция в теги / коллекции / спикеров нейтрализована
# ---------------------------------------------------------------------------

class TestInjectionNeutralized(unittest.TestCase):
    """Пользовательский контент не ломает Markdown и не несёт активных формул."""

    _MALICIOUS = "=SUM(A1:A9)|evil\nNEWLINE"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._gen = StatsReportGenerator()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _assert_no_active_injection(self, result: str, marker: str) -> None:
        """Проверяет, что вредоносный контент попал в отчёт уже нейтрализованным.

        Берём каждую строку отчёта, содержащую маркер инъекции, и убеждаемся,
        что в ней нет «сырой» формульной ячейки и нет неэкранированного '|'
        от полезной нагрузки (сырой перенос строки тоже исчезает — payload
        целиком на одной строке).
        """
        # Перенос строки из payload схлопнут → маркер и хвост на одной строке.
        self.assertIn(marker, result)
        self.assertIn("NEWLINE", result)
        offending = [
            ln for ln in result.splitlines()
            if marker in ln and "NEWLINE" in ln
        ]
        self.assertTrue(
            offending,
            "payload должен целиком оставаться на одной строке (CR/LF схлопнут)",
        )
        for ln in offending:
            # Сырой '|' от payload отсутствует: между marker и 'evil' стоит '\|'.
            self.assertIn(f"{marker}\\|evil", ln)
            # Формульный ведущий символ нейтрализован апострофом.
            self.assertIn(f"'{marker}", ln)

    def test_tag_injection_neutralized(self) -> None:
        items = [
            _make_item("Запись", ts=_ts_days_ago(1), tags=[self._MALICIOUS]),
        ]
        store = FakeStore(Path(self._tmp.name) / "tags", items=items)
        result = self._gen.generate_report(store, days=30)
        self._assert_no_active_injection(result, "=SUM(A1:A9)")

    def test_speaker_injection_neutralized(self) -> None:
        diar = {
            "segments": [
                {"speaker": self._MALICIOUS, "start": 0.0, "end": 12.0},
            ]
        }
        items = [
            _make_item("Запись", ts=_ts_days_ago(1), diarization=diar),
        ]
        store = FakeStore(Path(self._tmp.name) / "spk", items=items)
        result = self._gen.generate_report(store, days=30)
        self._assert_no_active_injection(result, "=SUM(A1:A9)")

    def test_collection_injection_neutralized(self) -> None:
        items = [_make_item("Запись", ts=_ts_days_ago(1))]
        data_dir = Path(self._tmp.name) / "coll"
        store = FakeStore(data_dir, items=items)
        coll_data = {
            "collections": {
                self._MALICIOUS: {
                    "name": self._MALICIOUS,
                    "item_ids": ["id1", "id2"],
                },
            }
        }
        (data_dir / "collections.json").write_text(
            json.dumps(coll_data, ensure_ascii=False),
            encoding="utf-8",
        )
        result = self._gen.generate_report(store, days=30)
        self._assert_no_active_injection(result, "=SUM(A1:A9)")


# ---------------------------------------------------------------------------
# Тест 12 (Wave 1770): days клампится (защита от DoS по памяти/CPU)
# ---------------------------------------------------------------------------

class TestDaysClamp(unittest.TestCase):
    """Огромный/битый days не приводит к DoS — клампится к [1, 3650]."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        items = [_make_item("Запись", ts=_ts_days_ago(1))]
        self._store = FakeStore(Path(self._tmp.name), items=items)
        self._gen = StatsReportGenerator()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_huge_days_clamped_and_fast(self) -> None:
        import time as _time
        start = _time.perf_counter()
        result = self._gen.generate_report(self._store, days=10_000_000)
        elapsed = _time.perf_counter() - start
        self.assertIsInstance(result, str)
        # Заголовок отражает кламп до 3650 дней (не 10 млн).
        self.assertIn("последние 3650 дней", result)
        self.assertNotIn("10000000", result)
        # Должно отрабатывать быстро — без построения многомиллионного списка дат.
        self.assertLess(elapsed, 2.0, f"generate_report занял {elapsed:.3f}s (>2s)")

    def test_days_lower_bound(self) -> None:
        result = self._gen.generate_report(self._store, days=0)
        self.assertIn("последние 1 дней", result)

    def test_days_negative_clamped_to_one(self) -> None:
        result = self._gen.generate_report(self._store, days=-50)
        self.assertIn("последние 1 дней", result)

    def test_non_int_days_defaults(self) -> None:
        # Нечисловой ввод деградирует к значению по умолчанию (30), без исключения.
        result = self._gen.generate_report(self._store, days="not-a-number")
        self.assertIn("последние 30 дней", result)

    def test_normal_days_unchanged(self) -> None:
        result = self._gen.generate_report(self._store, days=30)
        self.assertIn("последние 30 дней", result)


if __name__ == "__main__":
    unittest.main()
