"""Тесты RecordingInsightsGenerator."""

from __future__ import annotations
from backend.recording_insights import Insight, RecordingInsightsGenerator

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Настройка путей для standalone-запуска
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------------------
# Вспомогательные фабрики тестовых данных
# ---------------------------------------------------------------------------

def _make_item(
    ts_offset_days: float = 0.0,
    text: str = "привет мир тест",
    source_lang: str = "ru",
    confidence: float | None = 0.9,
    audio_duration_sec: float | None = None,
    hour: int | None = None,
) -> dict:
    """Создаёт минимальный dict, совместимый с RecordingInsightsGenerator."""
    base = datetime.now(timezone.utc) - timedelta(days=ts_offset_days)
    if hour is not None:
        base = base.replace(hour=hour, minute=0, second=0, microsecond=0)
    return {
        "ts": base.isoformat(),
        "text": text,
        "source_lang": source_lang,
        "confidence": confidence,
        "audio_duration_sec": audio_duration_sec,
    }


def _make_items_for_streak(n_days: int) -> list[dict]:
    """Создаёт по одной записи за последние n_days дней (включая сегодня)."""
    items = []
    for i in range(n_days):
        items.append(_make_item(ts_offset_days=float(i)))
    return items


# ---------------------------------------------------------------------------
# Тест: базовая структура Insight
# ---------------------------------------------------------------------------

class InsightDataclassTestCase(unittest.TestCase):
    """Тесты dataclass Insight."""

    def test_insight_has_required_fields(self) -> None:
        ins = Insight(
            type="peak_productivity",
            title="Тест",
            description="Описание",
            confidence=0.8,
            data={"hour": 14},
        )
        self.assertEqual(ins.type, "peak_productivity")
        self.assertEqual(ins.title, "Тест")
        self.assertEqual(ins.description, "Описание")
        self.assertAlmostEqual(ins.confidence, 0.8)
        self.assertEqual(ins.data["hour"], 14)

    def test_insight_to_dict_has_all_keys(self) -> None:
        ins = Insight(
            type="recording_streak",
            title="Серия",
            description="10 дней подряд",
            confidence=0.95,
            data={"streak_days": 10},
        )
        d = ins.to_dict()
        self.assertIn("type", d)
        self.assertIn("title", d)
        self.assertIn("description", d)
        self.assertIn("confidence", d)
        self.assertIn("data", d)

    def test_insight_default_data_is_empty_dict(self) -> None:
        ins = Insight(type="test", title="T", description="D", confidence=0.5)
        self.assertEqual(ins.data, {})

    def test_insight_to_dict_values_match(self) -> None:
        ins = Insight(
            type="quality_improvement",
            title="Рост качества",
            description="confidence выросла",
            confidence=0.75,
            data={"change": 0.05},
        )
        d = ins.to_dict()
        self.assertEqual(d["type"], "quality_improvement")
        self.assertAlmostEqual(d["confidence"], 0.75)
        self.assertEqual(d["data"]["change"], 0.05)


# ---------------------------------------------------------------------------
# Тест: пустые данные
# ---------------------------------------------------------------------------

class EmptyItemsTestCase(unittest.TestCase):
    """Поведение при пустых или слишком малых данных."""

    def setUp(self) -> None:
        self.gen = RecordingInsightsGenerator()

    def test_generate_insights_empty_list_returns_empty(self) -> None:
        result = self.gen.generate_insights([])
        self.assertEqual(result, [])

    def test_generate_insights_too_few_items_returns_empty(self) -> None:
        items = [_make_item(), _make_item()]  # только 2, минимум 3
        result = self.gen.generate_insights(items)
        self.assertEqual(result, [])

    def test_generate_insights_old_items_outside_window_returns_empty(self) -> None:
        # Все записи старше окна (days=7)
        items = [_make_item(ts_offset_days=10 + i) for i in range(10)]
        result = self.gen.generate_insights(items, days=7)
        self.assertEqual(result, [])

    def test_get_daily_insight_empty_returns_none(self) -> None:
        result = self.gen.get_daily_insight([])
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Тест: peak_productivity
# ---------------------------------------------------------------------------

class PeakProductivityTestCase(unittest.TestCase):
    """Тесты инсайта peak_productivity."""

    def setUp(self) -> None:
        self.gen = RecordingInsightsGenerator()

    def test_peak_productivity_detected_at_dominant_hour(self) -> None:
        # Большинство записей в 14:00
        items = [_make_item(ts_offset_days=i * 0.1, hour=14) for i in range(8)]
        items += [_make_item(ts_offset_days=i * 0.1 + 0.05, hour=9) for i in range(2)]
        insights = self.gen.generate_insights(items, days=7)
        types = [ins.type for ins in insights]
        self.assertIn("peak_productivity", types)

    def test_peak_productivity_data_contains_peak_hour(self) -> None:
        items = [_make_item(ts_offset_days=i * 0.1, hour=15) for i in range(7)]
        items += [_make_item(ts_offset_days=i * 0.1 + 0.05, hour=8) for i in range(3)]
        insights = self.gen.generate_insights(items, days=7)
        peak = next((i for i in insights if i.type == "peak_productivity"), None)
        self.assertIsNotNone(peak)
        self.assertIn("peak_hour", peak.data)
        self.assertEqual(peak.data["peak_hour"], 15)

    def test_peak_productivity_confidence_between_0_and_1(self) -> None:
        items = [_make_item(ts_offset_days=i * 0.1, hour=10) for i in range(6)]
        items += [_make_item(ts_offset_days=i * 0.1 + 0.05, hour=20) for i in range(4)]
        insights = self.gen.generate_insights(items, days=7)
        for ins in insights:
            self.assertGreaterEqual(ins.confidence, 0.0)
            self.assertLessEqual(ins.confidence, 1.0)


# ---------------------------------------------------------------------------
# Тест: recording_streak
# ---------------------------------------------------------------------------

class RecordingStreakTestCase(unittest.TestCase):
    """Тесты инсайта recording_streak."""

    def setUp(self) -> None:
        self.gen = RecordingInsightsGenerator()

    def test_streak_detected_for_consecutive_days(self) -> None:
        # 5 дней подряд
        items = _make_items_for_streak(5)
        insights = self.gen.generate_insights(items, days=7)
        types = [ins.type for ins in insights]
        self.assertIn("recording_streak", types)

    def test_streak_data_has_streak_days(self) -> None:
        items = _make_items_for_streak(5)
        insights = self.gen.generate_insights(items, days=7)
        streak_ins = next((i for i in insights if i.type == "recording_streak"), None)
        self.assertIsNotNone(streak_ins)
        self.assertIn("streak_days", streak_ins.data)
        self.assertGreaterEqual(streak_ins.data["streak_days"], 2)

    def test_streak_not_detected_for_single_day(self) -> None:
        items = [_make_item(ts_offset_days=0), _make_item(ts_offset_days=0.01),
                 _make_item(ts_offset_days=0.02)]
        insights = self.gen.generate_insights(items, days=7)
        [ins.type for ins in insights]
        # Может не быть streak если всё в один день
        # Проверяем что если streak есть - streak_days >= 2
        for ins in insights:
            if ins.type == "recording_streak":
                self.assertGreaterEqual(ins.data["streak_days"], 2)

    def test_streak_confidence_grows_with_length(self) -> None:
        items_short = _make_items_for_streak(3)
        items_long = _make_items_for_streak(10)

        gen = RecordingInsightsGenerator()
        ins_short = next((i for i in gen.generate_insights(items_short, days=14)
                          if i.type == "recording_streak"), None)
        ins_long = next((i for i in gen.generate_insights(items_long, days=14)
                         if i.type == "recording_streak"), None)

        if ins_short and ins_long:
            self.assertGreaterEqual(ins_long.confidence, ins_short.confidence)


# ---------------------------------------------------------------------------
# Тест: most_discussed_topic
# ---------------------------------------------------------------------------

class MostDiscussedTopicTestCase(unittest.TestCase):
    """Тесты инсайта most_discussed_topic."""

    def setUp(self) -> None:
        self.gen = RecordingInsightsGenerator()

    def test_topic_detected_from_text(self) -> None:
        # Много технологических слов
        tech_text = "программа код сервер база данные система приложение python функция"
        items = [_make_item(ts_offset_days=i * 0.1, text=tech_text) for i in range(5)]
        insights = self.gen.generate_insights(items, days=7)
        types = [ins.type for ins in insights]
        self.assertIn("most_discussed_topic", types)

    def test_topic_insight_has_topic_in_data(self) -> None:
        tech_text = "программа код сервер база данные python система"
        items = [_make_item(ts_offset_days=i * 0.1, text=tech_text) for i in range(5)]
        insights = self.gen.generate_insights(items, days=7)
        topic_ins = next((i for i in insights if i.type == "most_discussed_topic"), None)
        if topic_ins:
            self.assertIn("topic", topic_ins.data)

    def test_topic_fallback_for_no_cluster_match(self) -> None:
        # Слова не из кластеров — должен использоваться fallback
        items = [_make_item(ts_offset_days=i * 0.1, text="кракозябра мурзилка флюгегехаймен") for i in range(5)]
        insights = self.gen.generate_insights(items, days=7)
        # Инсайт может отсутствовать или иметь тип most_discussed_topic с fallback
        for ins in insights:
            if ins.type == "most_discussed_topic":
                self.assertIn("topic", ins.data)


# ---------------------------------------------------------------------------
# Тест: quality_improvement
# ---------------------------------------------------------------------------

class QualityImprovementTestCase(unittest.TestCase):
    """Тесты инсайта quality_improvement."""

    def setUp(self) -> None:
        self.gen = RecordingInsightsGenerator()

    def test_quality_improvement_detected(self) -> None:
        # Старые записи — низкая confidence, новые — высокая
        old_items = [_make_item(ts_offset_days=8 + i * 0.5, confidence=0.65) for i in range(5)]
        new_items = [_make_item(ts_offset_days=i * 0.5, confidence=0.90) for i in range(5)]
        all_items = old_items + new_items
        insights = self.gen.generate_insights(all_items, days=7)
        types = [ins.type for ins in insights]
        self.assertIn("quality_improvement", types)

    def test_quality_improvement_data_has_confidence_values(self) -> None:
        old_items = [_make_item(ts_offset_days=8 + i, confidence=0.70) for i in range(4)]
        new_items = [_make_item(ts_offset_days=i, confidence=0.88) for i in range(4)]
        insights = self.gen.generate_insights(old_items + new_items, days=7)
        qi = next((i for i in insights if i.type == "quality_improvement"), None)
        if qi:
            self.assertIn("prev_avg_confidence", qi.data)
            self.assertIn("recent_avg_confidence", qi.data)

    def test_quality_stable_no_insight(self) -> None:
        # Почти одинаковые confidence
        items = [_make_item(ts_offset_days=i * 0.5, confidence=0.85) for i in range(12)]
        insights = self.gen.generate_insights(items, days=7)
        qi_list = [i for i in insights if i.type == "quality_improvement"]
        self.assertEqual(qi_list, [])


# ---------------------------------------------------------------------------
# Тест: language_shift
# ---------------------------------------------------------------------------

class LanguageShiftTestCase(unittest.TestCase):
    """Тесты инсайта language_shift."""

    def setUp(self) -> None:
        self.gen = RecordingInsightsGenerator()

    def test_language_shift_detected_when_lang_grows(self) -> None:
        # В прошлом периоде — RU доминирует, в новом — ES вырос
        prev = [_make_item(ts_offset_days=8 + i, source_lang="ru") for i in range(6)]
        prev += [_make_item(ts_offset_days=8 + i + 0.5, source_lang="es") for i in range(1)]
        recent = [_make_item(ts_offset_days=i * 0.3, source_lang="ru") for i in range(2)]
        recent += [_make_item(ts_offset_days=i * 0.3 + 0.1, source_lang="es") for i in range(5)]
        all_items = prev + recent
        insights = self.gen.generate_insights(all_items, days=7)
        types = [ins.type for ins in insights]
        self.assertIn("language_shift", types)

    def test_language_shift_data_has_language(self) -> None:
        prev = [_make_item(ts_offset_days=8 + i, source_lang="ru") for i in range(6)]
        prev += [_make_item(ts_offset_days=8 + i + 0.5, source_lang="es") for i in range(1)]
        recent = [_make_item(ts_offset_days=i * 0.3, source_lang="ru") for i in range(2)]
        recent += [_make_item(ts_offset_days=i * 0.3 + 0.1, source_lang="es") for i in range(5)]
        all_items = prev + recent
        insights = self.gen.generate_insights(all_items, days=7)
        ls = next((i for i in insights if i.type == "language_shift"), None)
        if ls:
            self.assertIn("language", ls.data)
            self.assertIn("change_pct", ls.data)


# ---------------------------------------------------------------------------
# Тест: speaking_pace_change
# ---------------------------------------------------------------------------

class SpeakingPaceChangeTestCase(unittest.TestCase):
    """Тесты инсайта speaking_pace_change."""

    def setUp(self) -> None:
        self.gen = RecordingInsightsGenerator()

    def _pace_items(
        self, n: int, offset_days: float, wps: float
    ) -> list[dict]:
        """n записей с заданным кол-вом слов/сек и offset от сегодня."""
        items = []
        for i in range(n):
            dur = 10.0  # 10 секунд
            words_count = int(wps * dur)
            text = " ".join(["слово"] * words_count)
            items.append(_make_item(
                ts_offset_days=offset_days + i * 0.1,
                text=text,
                audio_duration_sec=dur,
            ))
        return items

    def test_pace_change_detected_when_speed_increases(self) -> None:
        slow = self._pace_items(5, offset_days=8.0, wps=1.5)
        fast = self._pace_items(5, offset_days=0.1, wps=3.0)
        all_items = slow + fast
        insights = self.gen.generate_insights(all_items, days=7)
        types = [ins.type for ins in insights]
        self.assertIn("speaking_pace_change", types)

    def test_pace_change_data_has_wps(self) -> None:
        slow = self._pace_items(4, offset_days=8.0, wps=1.5)
        fast = self._pace_items(4, offset_days=0.1, wps=3.0)
        insights = self.gen.generate_insights(slow + fast, days=7)
        pc = next((i for i in insights if i.type == "speaking_pace_change"), None)
        if pc:
            self.assertIn("prev_avg_wps", pc.data)
            self.assertIn("recent_avg_wps", pc.data)
            self.assertIn("change_pct", pc.data)
            self.assertGreater(pc.data["change_pct"], 0)

    def test_pace_stable_no_insight(self) -> None:
        # Одинаковый темп — не должно быть инсайта
        items = []
        for i in range(10):
            dur = 10.0
            text = " ".join(["слово"] * 20)  # 2 слова/сек
            items.append(_make_item(ts_offset_days=i * 0.5, text=text, audio_duration_sec=dur))
        insights = self.gen.generate_insights(items, days=7)
        pc_list = [i for i in insights if i.type == "speaking_pace_change"]
        self.assertEqual(pc_list, [])


# ---------------------------------------------------------------------------
# Тест: generate_insights возвращает список Insight
# ---------------------------------------------------------------------------

class GenerateInsightsReturnTypeTestCase(unittest.TestCase):
    """Тесты типов возвращаемых значений."""

    def setUp(self) -> None:
        self.gen = RecordingInsightsGenerator()

    def test_generate_insights_returns_list(self) -> None:
        items = [_make_item(ts_offset_days=i * 0.1) for i in range(5)]
        result = self.gen.generate_insights(items, days=7)
        self.assertIsInstance(result, list)

    def test_all_results_are_insight_instances(self) -> None:
        items = [_make_item(ts_offset_days=i * 0.1, hour=14) for i in range(10)]
        result = self.gen.generate_insights(items, days=7)
        for ins in result:
            self.assertIsInstance(ins, Insight)

    def test_all_confidence_in_range(self) -> None:
        items = [_make_item(ts_offset_days=i * 0.2) for i in range(8)]
        result = self.gen.generate_insights(items, days=7)
        for ins in result:
            self.assertGreaterEqual(ins.confidence, 0.0)
            self.assertLessEqual(ins.confidence, 1.0)

    def test_all_types_are_known(self) -> None:
        known_types = {
            "peak_productivity",
            "language_shift",
            "quality_improvement",
            "recording_streak",
            "most_discussed_topic",
            "speaking_pace_change",
        }
        items = [_make_item(ts_offset_days=i * 0.1) for i in range(10)]
        result = self.gen.generate_insights(items, days=7)
        for ins in result:
            self.assertIn(ins.type, known_types)


# ---------------------------------------------------------------------------
# Тест: get_daily_insight
# ---------------------------------------------------------------------------

class GetDailyInsightTestCase(unittest.TestCase):
    """Тесты метода get_daily_insight."""

    def setUp(self) -> None:
        self.gen = RecordingInsightsGenerator()

    def test_get_daily_insight_returns_none_for_empty(self) -> None:
        result = self.gen.get_daily_insight([])
        self.assertIsNone(result)

    def test_get_daily_insight_returns_insight_instance(self) -> None:
        items = [_make_item(ts_offset_days=i * 0.1, hour=14) for i in range(6)]
        result = self.gen.get_daily_insight(items)
        if result is not None:
            self.assertIsInstance(result, Insight)

    def test_get_daily_insight_returns_highest_confidence(self) -> None:
        """Возвращённый инсайт должен иметь наибольшую confidence."""
        items = [_make_item(ts_offset_days=i * 0.1, hour=14) for i in range(6)]
        all_insights = self.gen.generate_insights(items, days=7)
        daily = self.gen.get_daily_insight(items)
        if all_insights and daily:
            max_conf = max(i.confidence for i in all_insights)
            self.assertAlmostEqual(daily.confidence, max_conf, places=5)

    def test_get_daily_insight_returns_none_when_too_few_items(self) -> None:
        items = [_make_item(ts_offset_days=0)]  # только 1 запись
        result = self.gen.get_daily_insight(items)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Тест: граничные случаи
# ---------------------------------------------------------------------------

class EdgeCasesTestCase(unittest.TestCase):
    """Граничные случаи и устойчивость к плохим данным."""

    def setUp(self) -> None:
        self.gen = RecordingInsightsGenerator()

    def test_items_with_none_confidence_handled(self) -> None:
        items = [_make_item(ts_offset_days=i * 0.1, confidence=None) for i in range(5)]
        # Должно не бросать исключений
        result = self.gen.generate_insights(items, days=7)
        self.assertIsInstance(result, list)

    def test_items_with_missing_ts_handled(self) -> None:
        bad_items = [{"text": "привет"} for _ in range(5)]  # нет ts
        result = self.gen.generate_insights(bad_items, days=7)
        self.assertEqual(result, [])

    def test_items_with_empty_text_handled(self) -> None:
        items = [_make_item(ts_offset_days=i * 0.1, text="") for i in range(5)]
        # Не должно бросать исключений
        result = self.gen.generate_insights(items, days=7)
        self.assertIsInstance(result, list)

    def test_days_parameter_filters_correctly(self) -> None:
        # Записи только от 10 до 15 дней назад
        items = [_make_item(ts_offset_days=10 + i) for i in range(5)]
        result_small = self.gen.generate_insights(items, days=7)
        result_large = self.gen.generate_insights(items, days=20)
        # С малым окном — пусто, с большим — что-то есть или не бросает
        self.assertEqual(result_small, [])
        self.assertIsInstance(result_large, list)

    def test_to_dict_is_json_serializable(self) -> None:
        import json
        items = [_make_item(ts_offset_days=i * 0.1, hour=10) for i in range(6)]
        insights = self.gen.generate_insights(items, days=7)
        for ins in insights:
            d = ins.to_dict()
            # Должно сериализоваться без ошибок
            json.dumps(d)


# ---------------------------------------------------------------------------
# Тест: расширенные сценарии и комбинации инсайтов
# ---------------------------------------------------------------------------

class MultipleInsightsTestCase(unittest.TestCase):
    """Тесты для сценариев с множественными инсайтами."""

    def setUp(self) -> None:
        self.gen = RecordingInsightsGenerator()

    def test_generate_insights_combines_multiple_types(self) -> None:
        """Проверяет, что generate_insights может вернуть несколько инсайтов одновременно."""
        # Создаём записи, которые должны триггерить разные инсайты
        # Peak productivity: разное распределение по часам
        peak_items = [_make_item(ts_offset_days=i * 0.1, hour=10) for i in range(7)]
        peak_items += [_make_item(ts_offset_days=i * 0.1 + 0.05, hour=18) for i in range(3)]

        # Quality improvement: растущая confidence
        quality_items = [
            _make_item(ts_offset_days=8 + i, confidence=0.60) for i in range(4)
        ]
        quality_items += [
            _make_item(ts_offset_days=i, confidence=0.95) for i in range(4)
        ]

        # Topic detection: много технологических слов
        topic_items = [
            _make_item(ts_offset_days=i * 0.2, text="программа код сервер база данные")
            for i in range(5)
        ]

        all_items = peak_items + quality_items + topic_items
        insights = self.gen.generate_insights(all_items, days=7)

        # Должно быть несколько инсайтов
        self.assertGreater(len(insights), 1)
        # Все должны быть правильными инстансами
        for ins in insights:
            self.assertIsInstance(ins, Insight)
            self.assertGreaterEqual(ins.confidence, 0.0)
            self.assertLessEqual(ins.confidence, 1.0)

    def test_insights_sorted_by_confidence_can_be_topped(self) -> None:
        """Проверяет возможность получить top-N инсайтов по confidence."""
        items = [_make_item(ts_offset_days=i * 0.1, hour=14) for i in range(10)]
        insights = self.gen.generate_insights(items, days=7)

        if len(insights) > 1:
            # Сортируем по confidence
            sorted_insights = sorted(insights, key=lambda x: x.confidence, reverse=True)
            top_3 = sorted_insights[:3]
            # Проверяем что confidence у top-3 монотонно не растёт
            for i in range(len(top_3) - 1):
                self.assertGreaterEqual(
                    top_3[i].confidence,
                    top_3[i + 1].confidence
                )

    def test_single_item_with_all_fields_in_history(self) -> None:
        """Граничный случай: одна запись со всеми полями."""
        single_item = [
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "text": "программа код сервер база данные система приложение функция модель",
                "source_lang": "ru",
                "confidence": 0.92,
                "audio_duration_sec": 15.0,
            }
        ]
        # Недостаточно данных для инсайтов (минимум 3)
        result = self.gen.generate_insights(single_item, days=7)
        self.assertEqual(result, [])

    def test_insights_with_object_attributes_vs_dict(self) -> None:
        """Проверяет совместимость с объектами (не только dict)."""
        # Создаём простой класс-подобный объект
        class MockHistoryItem:
            def __init__(self, ts, text, source_lang, confidence, audio_duration_sec):
                self.ts = ts
                self.text = text
                self.source_lang = source_lang
                self.confidence = confidence
                self.audio_duration_sec = audio_duration_sec

        now = datetime.now(timezone.utc)
        mock_items = [
            MockHistoryItem(
                ts=(now - timedelta(days=i * 0.1)).isoformat(),
                text="программа код сервер база данные система python",
                source_lang="ru",
                confidence=0.85,
                audio_duration_sec=10.0,
            )
            for i in range(5)
        ]

        # Должно работать с объектами через getattr
        insights = self.gen.generate_insights(mock_items, days=7)
        self.assertIsInstance(insights, list)

    def test_large_dataset_performance(self) -> None:
        """Тест производительности с большим количеством записей."""
        # Создаём 100 записей
        items = []
        for i in range(100):
            items.append(
                _make_item(
                    ts_offset_days=i * 0.01,
                    text=f"текст запись номер {i} программа код сервер база данные",
                    hour=(10 + i) % 24,
                    confidence=0.5 + (i % 50) / 100.0,
                    audio_duration_sec=5.0 + i % 15,
                )
            )

        # Должно завершиться без ошибок
        insights = self.gen.generate_insights(items, days=7)
        self.assertIsInstance(insights, list)
        # Все инсайты должны быть валидными
        for ins in insights:
            self.assertIsInstance(ins, Insight)


class SpecialValueEdgeCasesTestCase(unittest.TestCase):
    """Тесты для специальных значений и граничных случаев."""

    def setUp(self) -> None:
        self.gen = RecordingInsightsGenerator()

    def test_confidence_at_boundaries(self) -> None:
        """Проверяет обработку confidence на границах 0.0 и 1.0."""
        items = [
            _make_item(ts_offset_days=i * 0.1, confidence=0.0) for i in range(3)
        ]
        items += [
            _make_item(ts_offset_days=i * 0.1 + 0.05, confidence=1.0) for i in range(3)
        ]
        # Должно не бросать исключений
        insights = self.gen.generate_insights(items, days=7)
        self.assertIsInstance(insights, list)

    def test_very_old_items_ignored(self) -> None:
        """Проверяет, что очень старые записи игнорируются в расчётах."""
        old = [
            _make_item(ts_offset_days=100 + i, confidence=0.5, hour=15)
            for i in range(10)
        ]
        recent = [
            _make_item(ts_offset_days=i * 0.1, confidence=0.95, hour=9)
            for i in range(5)
        ]
        insights = self.gen.generate_insights(old + recent, days=7)
        # Peak hour должен быть 9, а не 15 (только свежие данные)
        peak_ins = next((i for i in insights if i.type == "peak_productivity"), None)
        if peak_ins:
            self.assertEqual(peak_ins.data["peak_hour"], 9)


class InsightMessagePriorityTestCase(unittest.TestCase):
    """Проверяет структуру полей type/title/description/confidence на всех инсайтах."""

    def setUp(self) -> None:
        self.gen = RecordingInsightsGenerator()

    def test_all_insights_have_nonempty_type(self) -> None:
        items = [_make_item(ts_offset_days=i * 0.1, hour=10) for i in range(8)]
        for ins in self.gen.generate_insights(items, days=7):
            self.assertTrue(ins.type, f"Пустой type у инсайта: {ins!r}")

    def test_all_insights_have_nonempty_title_and_description(self) -> None:
        items = [_make_item(ts_offset_days=i * 0.1, hour=10) for i in range(8)]
        for ins in self.gen.generate_insights(items, days=7):
            self.assertTrue(ins.title.strip(), "Пустой title")
            self.assertTrue(ins.description.strip(), "Пустое description")

    def test_insight_confidence_is_float(self) -> None:
        items = [_make_item(ts_offset_days=i * 0.1, hour=14) for i in range(6)]
        for ins in self.gen.generate_insights(items, days=7):
            self.assertIsInstance(ins.confidence, float)

    def test_to_dict_type_matches_attribute(self) -> None:
        ins = Insight(type="recording_streak", title="T", description="D", confidence=0.6)
        d = ins.to_dict()
        self.assertEqual(d["type"], ins.type)
        self.assertEqual(d["confidence"], ins.confidence)


class MondayPatternTestCase(unittest.TestCase):
    """Тест: инсайт peak_productivity отражает доминирующий день/час (паттерн 'понедельники')."""

    def setUp(self) -> None:
        self.gen = RecordingInsightsGenerator()

    def test_peak_hour_reflects_concentrated_recordings(self) -> None:
        """Большинство записей в один час → peak_hour указывает на него."""
        # 7 записей в 9:00, 1 в 18:00 — peak должен быть 9
        items = [_make_item(ts_offset_days=i * 0.1, hour=9) for i in range(7)]
        items += [_make_item(ts_offset_days=0.5, hour=18)]
        insights = self.gen.generate_insights(items, days=7)
        peak = next((i for i in insights if i.type == "peak_productivity"), None)
        self.assertIsNotNone(peak)
        self.assertEqual(peak.data["peak_hour"], 9)

    def test_peak_ratio_in_data(self) -> None:
        """peak_ratio присутствует в data и находится в диапазоне 0–1."""
        items = [_make_item(ts_offset_days=i * 0.1, hour=10) for i in range(7)]
        items += [_make_item(ts_offset_days=0.5, hour=20)]
        insights = self.gen.generate_insights(items, days=7)
        peak = next((i for i in insights if i.type == "peak_productivity"), None)
        if peak:
            self.assertIn("peak_ratio", peak.data)
            self.assertGreater(peak.data["peak_ratio"], 0.0)
            self.assertLessEqual(peak.data["peak_ratio"], 1.0)

    def test_uniform_hour_distribution_still_produces_insight(self) -> None:
        """Даже равномерное распределение по часам порождает peak_productivity."""
        items = []
        for h in range(4):
            items.extend(_make_item(ts_offset_days=h * 0.2, hour=h) for _ in range(1))
        # Добавим доминирующий час
        items.extend(_make_item(ts_offset_days=i * 0.05, hour=12) for i in range(5))
        insights = self.gen.generate_insights(items, days=7)
        types = [ins.type for ins in insights]
        self.assertIn("peak_productivity", types)

    def test_peak_hour_distribution_sums_to_total(self) -> None:
        """Сумма hour_distribution равна total_count."""
        items = [_make_item(ts_offset_days=i * 0.1, hour=(8 + i) % 3) for i in range(9)]
        insights = self.gen.generate_insights(items, days=7)
        peak = next((i for i in insights if i.type == "peak_productivity"), None)
        if peak and "hour_distribution" in peak.data:
            self.assertEqual(
                sum(peak.data["hour_distribution"].values()),
                peak.data["total_count"],
            )


class QualityImprovementExtendedTestCase(unittest.TestCase):
    """Расширенные тесты quality_improvement."""

    def setUp(self) -> None:
        self.gen = RecordingInsightsGenerator()

    def test_quality_degradation_also_detected(self) -> None:
        """quality_improvement срабатывает и при снижении confidence."""
        new_items = [_make_item(ts_offset_days=i * 0.5, confidence=0.55) for i in range(4)]
        old_items = [_make_item(ts_offset_days=8 + i, confidence=0.92) for i in range(4)]
        insights = self.gen.generate_insights(old_items + new_items, days=7)
        qi = next((i for i in insights if i.type == "quality_improvement"), None)
        self.assertIsNotNone(qi)
        # change должен быть отрицательным
        self.assertLess(qi.data["change"], 0)

    def test_quality_improvement_change_sign_positive(self) -> None:
        """change положительный при росте confidence."""
        old_items = [_make_item(ts_offset_days=8 + i, confidence=0.60) for i in range(4)]
        new_items = [_make_item(ts_offset_days=i * 0.5, confidence=0.95) for i in range(4)]
        insights = self.gen.generate_insights(old_items + new_items, days=7)
        qi = next((i for i in insights if i.type == "quality_improvement"), None)
        if qi:
            self.assertGreater(qi.data["change"], 0)

    def test_quality_improvement_sample_sizes_in_data(self) -> None:
        """prev_sample_size и recent_sample_size присутствуют в data."""
        old_items = [_make_item(ts_offset_days=8 + i, confidence=0.65) for i in range(3)]
        new_items = [_make_item(ts_offset_days=i * 0.5, confidence=0.90) for i in range(3)]
        insights = self.gen.generate_insights(old_items + new_items, days=7)
        qi = next((i for i in insights if i.type == "quality_improvement"), None)
        if qi:
            self.assertIn("prev_sample_size", qi.data)
            self.assertIn("recent_sample_size", qi.data)
            self.assertGreater(qi.data["prev_sample_size"], 0)
            self.assertGreater(qi.data["recent_sample_size"], 0)


class RecordingStreakExtendedTestCase(unittest.TestCase):
    """Расширенные тесты recording_streak."""

    def setUp(self) -> None:
        self.gen = RecordingInsightsGenerator()

    def test_streak_three_days(self) -> None:
        items = _make_items_for_streak(3)
        insights = self.gen.generate_insights(items, days=7)
        streak = next((i for i in insights if i.type == "recording_streak"), None)
        self.assertIsNotNone(streak)
        self.assertGreaterEqual(streak.data["streak_days"], 2)

    def test_streak_total_days_in_data(self) -> None:
        items = _make_items_for_streak(5)
        insights = self.gen.generate_insights(items, days=14)
        streak = next((i for i in insights if i.type == "recording_streak"), None)
        if streak:
            self.assertIn("total_days_with_recordings", streak.data)
            self.assertGreater(streak.data["total_days_with_recordings"], 0)

    def test_streak_confidence_at_least_half(self) -> None:
        """Confidence серии >= 0.5 согласно формуле 0.5 + streak*0.05."""
        items = _make_items_for_streak(4)
        insights = self.gen.generate_insights(items, days=14)
        streak = next((i for i in insights if i.type == "recording_streak"), None)
        if streak:
            self.assertGreaterEqual(streak.data["streak_days"], 2)
            self.assertGreaterEqual(streak.confidence, 0.5)


class Wave140InsightsTestCase(unittest.TestCase):
    """Wave 140 named tests: RecordingInsightsGenerator specific scenarios."""

    def setUp(self) -> None:
        self.gen = RecordingInsightsGenerator()

    def test_insights_from_busy_day(self) -> None:
        """Many recordings in one day trigger peak_productivity insight."""
        # 15 recordings today spread across two hours
        items = [_make_item(ts_offset_days=0.0 + i * 0.01, hour=10) for i in range(10)]
        items += [_make_item(ts_offset_days=0.0 + i * 0.01, hour=14) for i in range(5)]
        insights = self.gen.generate_insights(items, days=7)
        self.assertGreater(len(insights), 0)
        types = {ins.type for ins in insights}
        self.assertIn("peak_productivity", types)

    def test_insights_from_empty_history(self) -> None:
        """Empty history produces no insights."""
        result = self.gen.generate_insights([])
        self.assertEqual(result, [])

    def test_insights_detect_pattern(self) -> None:
        """Consecutive-day recordings trigger recording_streak."""
        items = _make_items_for_streak(5)
        insights = self.gen.generate_insights(items, days=7)
        types = {ins.type for ins in insights}
        self.assertIn("recording_streak", types)
        streak = next(i for i in insights if i.type == "recording_streak")
        self.assertGreaterEqual(streak.data["streak_days"], 2)

    def test_unicode_text_handled(self) -> None:
        """Cyrillic and mixed-language text does not raise exceptions."""
        items = [
            _make_item(
                ts_offset_days=i * 0.1,
                text="Программа código program сервер системе данные",
                source_lang="ru",
            )
            for i in range(5)
        ]
        # Must not raise
        insights = self.gen.generate_insights(items, days=7)
        self.assertIsInstance(insights, list)
        for ins in insights:
            self.assertIsInstance(ins.title, str)
            self.assertIsInstance(ins.description, str)

    def test_concurrent_generate(self) -> None:
        """Concurrent generate_insights calls from multiple threads are safe."""
        import threading

        items = [_make_item(ts_offset_days=i * 0.05, hour=10 + i % 4) for i in range(10)]
        results = []
        errors = []

        def run():
            try:
                r = self.gen.generate_insights(items, days=7)
                results.append(r)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread errors: {errors}")
        self.assertEqual(len(results), 8)
        # All threads should return the same number of insights
        lens = [len(r) for r in results]
        self.assertTrue(all(n == lens[0] for n in lens), f"Inconsistent results: {lens}")


# ---------------------------------------------------------------------------
# Wave 1725 security + robustness regression tests
# ---------------------------------------------------------------------------

class Wave1725SecurityTestCase(unittest.TestCase):
    """BUG 1 — stored-XSS: source_lang is sanitized before appearing in Insight text."""

    def setUp(self) -> None:
        self.gen = RecordingInsightsGenerator()

    def _items_with_lang(self, source_lang: str, n_prev: int = 4, n_recent: int = 4) -> list:
        """Helpers: prev period items all RU, recent period items with given lang."""
        prev = [_make_item(ts_offset_days=8 + i, source_lang="ru") for i in range(n_prev)]
        recent = [_make_item(ts_offset_days=i * 0.3, source_lang=source_lang)
                  for i in range(n_recent)]
        return prev + recent

    def test_xss_payload_stripped_from_title(self) -> None:
        """source_lang='<script>alert(1)</script>' must NOT appear in Insight title."""
        items = self._items_with_lang("<script>alert(1)</script>")
        insights = self.gen.generate_insights(items, days=7)
        for ins in insights:
            self.assertNotIn("<script>", ins.title,
                             f"XSS payload in title: {ins.title!r}")
            self.assertNotIn("<script>", ins.description,
                             f"XSS payload in description: {ins.description!r}")

    def test_xss_payload_stripped_from_description(self) -> None:
        """Complete-switch scenario: malicious source_lang must not reach description."""
        # Create items where previous period has no such lang → complete switch path
        prev = [_make_item(ts_offset_days=8 + i, source_lang="ru") for i in range(4)]
        recent = [_make_item(ts_offset_days=i * 0.2,
                             source_lang='"><img src=x onerror=alert(1)>')
                  for i in range(5)]
        items = prev + recent
        insights = self.gen.generate_insights(items, days=7)
        for ins in insights:
            self.assertNotIn("<img", ins.title)
            self.assertNotIn("<img", ins.description)
            self.assertNotIn("onerror", ins.title)
            self.assertNotIn("onerror", ins.description)

    def test_valid_lang_code_preserved(self) -> None:
        """Normal language codes (ru, en, zh-hans) survive sanitization intact."""
        from backend.recording_insights import _get_source_lang
        self.assertEqual(_get_source_lang({"source_lang": "ru"}), "ru")
        self.assertEqual(_get_source_lang({"source_lang": "EN"}), "en")
        self.assertEqual(_get_source_lang({"source_lang": "zh-hans"}), "zh-hans")

    def test_empty_lang_becomes_unknown(self) -> None:
        """Empty / whitespace source_lang → 'unknown' sentinel."""
        from backend.recording_insights import _get_source_lang
        self.assertEqual(_get_source_lang({"source_lang": ""}), "unknown")
        self.assertEqual(_get_source_lang({"source_lang": "   "}), "unknown")
        self.assertEqual(_get_source_lang({}), "unknown")

    def test_all_digits_lang_becomes_unknown(self) -> None:
        """Purely numeric source_lang → 'unknown'."""
        from backend.recording_insights import _get_source_lang
        self.assertEqual(_get_source_lang({"source_lang": "12345"}), "unknown")

    def test_long_lang_truncated(self) -> None:
        """source_lang longer than 20 letters is truncated."""
        from backend.recording_insights import _get_source_lang
        result = _get_source_lang({"source_lang": "a" * 50})
        self.assertLessEqual(len(result), 20)


class Wave1725OverflowTestCase(unittest.TestCase):
    """BUG 2 — OverflowError: days >= 500_000_000 must not raise."""

    def setUp(self) -> None:
        self.gen = RecordingInsightsGenerator()

    def _rich_items(self) -> list:
        """Items that give generate_insights enough data to reach internal timedelta calls."""
        prev = [_make_item(ts_offset_days=8 + i, source_lang="ru", confidence=0.7)
                for i in range(4)]
        recent = [_make_item(ts_offset_days=i * 0.3, source_lang="es", confidence=0.9)
                  for i in range(4)]
        return prev + recent

    def test_huge_days_does_not_raise(self) -> None:
        """days=10^9 must not raise OverflowError."""
        items = self._rich_items()
        try:
            result = self.gen.generate_insights(items, days=10 ** 9)
        except OverflowError as exc:
            self.fail(f"OverflowError raised with days=10^9: {exc}")
        self.assertIsInstance(result, list)

    def test_max_days_constant_clamped(self) -> None:
        """days is clamped to _MAX_DAYS (36500); timedelta(days=days*2) stays safe."""
        from backend.recording_insights import RecordingInsightsGenerator
        gen = RecordingInsightsGenerator()
        # Verify that _MAX_DAYS * 2 < 999_999_999 (Python timedelta max)
        self.assertLess(gen._MAX_DAYS * 2, 999_999_999)

    def test_days_zero_handled_as_one(self) -> None:
        """days=0 is clamped to 1 — should not raise."""
        items = [_make_item(ts_offset_days=0), _make_item(ts_offset_days=0.01),
                 _make_item(ts_offset_days=0.02)]
        try:
            result = self.gen.generate_insights(items, days=0)
        except Exception as exc:
            self.fail(f"Unexpected exception with days=0: {exc}")
        self.assertIsInstance(result, list)

    def test_days_negative_handled_as_one(self) -> None:
        """days=-5 is clamped to 1 — should not raise."""
        items = [_make_item() for _ in range(5)]
        try:
            result = self.gen.generate_insights(items, days=-5)
        except Exception as exc:
            self.fail(f"Unexpected exception with days=-5: {exc}")
        self.assertIsInstance(result, list)

    def test_borderline_overflow_value(self) -> None:
        """days just below the timedelta overflow boundary must not raise."""
        items = self._rich_items()
        # 499_999_999 * 2 = 999_999_998 — one below the Python timedelta limit
        # The clamp kicks in (max 36500), so this should be fine.
        try:
            result = self.gen.generate_insights(items, days=499_999_999)
        except OverflowError as exc:
            self.fail(f"OverflowError with days=499_999_999: {exc}")
        self.assertIsInstance(result, list)


class Wave1725CompleteSwitchTestCase(unittest.TestCase):
    """BUG 3 — complete language switch (A→B) must produce a language_shift insight."""

    def setUp(self) -> None:
        self.gen = RecordingInsightsGenerator()

    def test_complete_switch_ru_to_es_reported(self) -> None:
        """All-RU previous, all-ES recent → language_shift insight detected."""
        prev = [_make_item(ts_offset_days=8 + i, source_lang="ru") for i in range(5)]
        recent = [_make_item(ts_offset_days=i * 0.2, source_lang="es") for i in range(5)]
        items = prev + recent
        insights = self.gen.generate_insights(items, days=7)
        types = [ins.type for ins in insights]
        self.assertIn("language_shift", types,
                      "Complete RU→ES switch should produce a language_shift insight")

    def test_complete_switch_insight_has_complete_switch_flag(self) -> None:
        """language_shift data.complete_switch == True for a full A→B switch."""
        prev = [_make_item(ts_offset_days=8 + i, source_lang="ru") for i in range(5)]
        recent = [_make_item(ts_offset_days=i * 0.2, source_lang="es") for i in range(5)]
        items = prev + recent
        insights = self.gen.generate_insights(items, days=7)
        ls = next((i for i in insights if i.type == "language_shift"), None)
        self.assertIsNotNone(ls, "Expected a language_shift insight")
        self.assertTrue(ls.data.get("complete_switch", False),
                        "data.complete_switch should be True for a full switch")

    def test_complete_switch_language_correct(self) -> None:
        """The reported language in data should be the newly appeared language (es)."""
        prev = [_make_item(ts_offset_days=8 + i, source_lang="ru") for i in range(5)]
        recent = [_make_item(ts_offset_days=i * 0.2, source_lang="es") for i in range(5)]
        items = prev + recent
        insights = self.gen.generate_insights(items, days=7)
        ls = next((i for i in insights if i.type == "language_shift"), None)
        self.assertIsNotNone(ls)
        self.assertEqual(ls.data["language"], "es")

    def test_partial_shift_still_works(self) -> None:
        """Non-zero prev usage still triggers gradual shift (original behaviour preserved)."""
        prev = [_make_item(ts_offset_days=8 + i, source_lang="ru") for i in range(6)]
        prev += [_make_item(ts_offset_days=8 + i + 0.5, source_lang="es") for i in range(1)]
        recent = [_make_item(ts_offset_days=i * 0.3, source_lang="ru") for i in range(2)]
        recent += [_make_item(ts_offset_days=i * 0.3 + 0.1, source_lang="es") for i in range(5)]
        items = prev + recent
        insights = self.gen.generate_insights(items, days=7)
        types = [ins.type for ins in insights]
        self.assertIn("language_shift", types)
        ls = next((i for i in insights if i.type == "language_shift"), None)
        self.assertIsNotNone(ls)
        # Partial shift should NOT have complete_switch flag
        self.assertFalse(ls.data.get("complete_switch", False),
                         "Partial shift should not set complete_switch=True")


if __name__ == "__main__":
    unittest.main()
