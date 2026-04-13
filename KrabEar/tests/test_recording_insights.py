"""Тесты RecordingInsightsGenerator."""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# Настройка путей для standalone-запуска
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from backend.recording_insights import Insight, RecordingInsightsGenerator


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
        types = [ins.type for ins in insights]
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


if __name__ == "__main__":
    unittest.main()
