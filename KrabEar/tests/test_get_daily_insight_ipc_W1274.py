"""W1274 — тесты IPC-хэндлера get_daily_insight и токен-капа в
_compute_most_discussed_topic.

Покрывает:
- test_get_daily_insight_ipc_returns_result: нормальный путь — хэндлер
  делегирует RecordingInsightsGenerator.get_daily_insight и возвращает insight.
- test_get_daily_insight_skipped_in_privacy_mode: privacy_mode_enabled=True →
  хэндлер возвращает {"insight": None, "privacy_mode": True} без обращения к
  истории.
- test_compute_most_discussed_topic_capped_at_1000_items: при подаче корпуса,
  который порождает >1000 токенов, all_tokens обрезается до _MAX_TOPIC_TOKENS
  и Counter строится по капу.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Настройка путей (standalone-запуск)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for _p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.recording_insights import Insight, RecordingInsightsGenerator


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------

def _make_insight(confidence: float = 0.8) -> Insight:
    return Insight(
        type="peak_productivity",
        title="Вы наиболее продуктивны",
        description="Тест",
        confidence=confidence,
        data={"peak_hour": 10},
    )


def _make_cached_settings(privacy: bool = False):
    return lambda: {"privacy_mode_enabled": privacy}


def _make_store_with_items(items: list) -> MagicMock:
    """Возвращает mock StateStore, отдающий items из _load_active_items_with_lock."""
    store = MagicMock()
    store._load_active_items_with_lock.return_value = list(items)
    return store


class _MinimalService:
    """Минимальный стаб BackendService, содержащий только то, что нужно для
    _handle_get_daily_insight."""

    def __init__(
        self,
        privacy: bool = False,
        insight: Insight | None = None,
        store_items: list | None = None,
    ) -> None:
        self._cached_settings = _make_cached_settings(privacy)
        self.store = _make_store_with_items(store_items or [])

        # Стаб RecordingInsightsGenerator
        gen = MagicMock(spec=RecordingInsightsGenerator)
        gen.get_daily_insight.return_value = insight
        self._recording_insights = gen

    def _handle_get_daily_insight(self, params: dict[str, Any]) -> dict[str, Any]:
        """Копия реального хэндлера из service.py (для изоляции от загрузки всего сервиса)."""
        if self._cached_settings().get("privacy_mode_enabled"):
            return {"insight": None, "privacy_mode": True}
        try:
            items = self.store._load_active_items_with_lock()
        except Exception:
            items = []
        insight = self._recording_insights.get_daily_insight(items)
        return {
            "insight": insight.to_dict() if insight is not None else None,
            "privacy_mode": False,
        }


# ---------------------------------------------------------------------------
# Тест 1: нормальный путь — возвращает insight
# ---------------------------------------------------------------------------

class GetDailyInsightIPCReturnsResultTestCase(unittest.TestCase):
    """test_get_daily_insight_ipc_returns_result."""

    def test_get_daily_insight_ipc_returns_result(self) -> None:
        """Хэндлер get_daily_insight возвращает insight.to_dict() в поле 'insight'."""
        expected_insight = _make_insight(confidence=0.75)
        svc = _MinimalService(privacy=False, insight=expected_insight, store_items=[{"ts": "2026-01-01T10:00:00Z"}])

        result = svc._handle_get_daily_insight({})

        self.assertFalse(result["privacy_mode"])
        self.assertIsNotNone(result["insight"])
        self.assertEqual(result["insight"]["type"], "peak_productivity")
        self.assertAlmostEqual(result["insight"]["confidence"], 0.75)

    def test_get_daily_insight_ipc_returns_none_when_no_data(self) -> None:
        """Когда RecordingInsightsGenerator.get_daily_insight возвращает None,
        хэндлер возвращает {'insight': None, 'privacy_mode': False}."""
        svc = _MinimalService(privacy=False, insight=None)

        result = svc._handle_get_daily_insight({})

        self.assertFalse(result["privacy_mode"])
        self.assertIsNone(result["insight"])

    def test_get_daily_insight_ipc_calls_generator_with_store_items(self) -> None:
        """Хэндлер передаёт items из store в get_daily_insight."""
        items = [{"ts": "2026-01-01T10:00:00Z"}, {"ts": "2026-01-02T12:00:00Z"}]
        svc = _MinimalService(privacy=False, insight=_make_insight(), store_items=items)

        svc._handle_get_daily_insight({})

        svc._recording_insights.get_daily_insight.assert_called_once_with(items)

    def test_get_daily_insight_ipc_store_exception_passes_empty_list(self) -> None:
        """Если store._load_active_items_with_lock() бросает исключение, хэндлер передаёт [] в генератор."""
        insight = _make_insight()
        svc = _MinimalService(privacy=False, insight=insight)
        svc.store._load_active_items_with_lock.side_effect = RuntimeError("store error")

        result = svc._handle_get_daily_insight({})

        svc._recording_insights.get_daily_insight.assert_called_once_with([])
        self.assertIsNotNone(result["insight"])


# ---------------------------------------------------------------------------
# Тест 2: privacy mode gate
# ---------------------------------------------------------------------------

class GetDailyInsightPrivacyModeTestCase(unittest.TestCase):
    """test_get_daily_insight_skipped_in_privacy_mode."""

    def test_get_daily_insight_skipped_in_privacy_mode(self) -> None:
        """privacy_mode_enabled=True → возвращает {insight:None, privacy_mode:True}
        без вызова RecordingInsightsGenerator и без обращения к store."""
        svc = _MinimalService(privacy=True, insight=_make_insight())

        result = svc._handle_get_daily_insight({})

        self.assertTrue(result["privacy_mode"])
        self.assertIsNone(result["insight"])
        # Генератор и store не должны были быть вызваны
        svc._recording_insights.get_daily_insight.assert_not_called()
        svc.store._load_active_items_with_lock.assert_not_called()

    def test_get_daily_insight_not_gated_when_privacy_false(self) -> None:
        """privacy_mode_enabled=False → генератор вызывается нормально."""
        svc = _MinimalService(privacy=False, insight=_make_insight())

        result = svc._handle_get_daily_insight({})

        self.assertFalse(result["privacy_mode"])
        svc._recording_insights.get_daily_insight.assert_called_once()


# ---------------------------------------------------------------------------
# Тест 3: токен-кап в _compute_most_discussed_topic
# ---------------------------------------------------------------------------

class TopicTokensCapTestCase(unittest.TestCase):
    """test_compute_most_discussed_topic_capped_at_1000_items."""

    def _make_big_items(self, n_items: int = 200, words_per_item: int = 20) -> list[dict]:
        """Генерирует items с текстами, суммарный токен-счёт которых >> 1000."""
        word = "технологии"  # длиннее 3 символов и не стоп-слово
        text = " ".join([word] * words_per_item)
        return [{"ts": f"2026-01-01T10:00:0{i % 10}Z", "text": text} for i in range(n_items)]

    def test_compute_most_discussed_topic_capped_at_1000_items(self) -> None:
        """all_tokens не превышает _MAX_TOPIC_TOKENS=1000 даже при большом корпусе.

        Патчим Counter в модуле recording_insights, чтобы захватить размер
        итерируемого, переданного из all_tokens.
        """
        import backend.recording_insights as _mod
        from collections import Counter as _Counter

        gen = RecordingInsightsGenerator()
        # 200 items × 20 слов = 4000 потенциальных токенов > 1000
        items = self._make_big_items(n_items=200, words_per_item=20)

        captured_sizes: list[int] = []

        class _SpyCounter(_Counter):
            def __init__(self, iterable=None, **kwargs):
                if iterable is not None:
                    lst = list(iterable)
                    captured_sizes.append(len(lst))
                    iterable = lst
                super().__init__(iterable or [], **kwargs)

        with patch.object(_mod, "Counter", _SpyCounter):
            gen._compute_most_discussed_topic(items)

        # Должен быть хотя бы один вызов Counter (со всеми токенами)
        self.assertTrue(len(captured_sizes) >= 1, "Counter не был вызван")
        for size in captured_sizes:
            self.assertLessEqual(
                size,
                gen._MAX_TOPIC_TOKENS,
                f"Counter получил {size} токенов, ожидалось ≤ {gen._MAX_TOPIC_TOKENS}",
            )

    def test_compute_most_discussed_topic_cap_constant_is_1000(self) -> None:
        """_MAX_TOPIC_TOKENS должен быть равен 1000."""
        gen = RecordingInsightsGenerator()
        self.assertEqual(gen._MAX_TOPIC_TOKENS, 1000)

    def test_compute_most_discussed_topic_small_corpus_not_capped(self) -> None:
        """Маленький корпус (<1000 токенов) проходит без обрезки и возвращает инсайт."""
        from datetime import datetime, timedelta, timezone
        gen = RecordingInsightsGenerator()
        # 5 items × "программа python сервер база данные" = 5 × 5 = 25 токенов
        now = datetime.now(timezone.utc)
        items = []
        for i in range(5):
            ts = (now - timedelta(days=i)).isoformat()
            items.append({"ts": ts, "text": "программа python сервер база данные"})
        result = gen._compute_most_discussed_topic(items)
        # Должен вернуть что-то (не None) — топик найден
        self.assertIsNotNone(result)

    def test_max_topic_tokens_stops_extending_mid_item(self) -> None:
        """Кап обрезает даже токены внутри одного item, если уже достигнут лимит."""
        gen = RecordingInsightsGenerator()
        # Один гигантский item с 2000 уникальных слов-фиктивных (длина>3, не стоп-слово)
        words = [f"слов{i:04d}" for i in range(2000)]  # 2000 уникальных токенов
        items = [{"ts": "2026-01-01T10:00:00Z", "text": " ".join(words)}]

        # Отслеживаем итоговый размер all_tokens через патч Counter
        import backend.recording_insights as _mod
        from collections import Counter as _Counter
        sizes: list[int] = []

        class _SpyC(_Counter):
            def __init__(self, iterable=None, **kwargs):
                if iterable is not None:
                    lst = list(iterable)
                    sizes.append(len(lst))
                    iterable = lst
                super().__init__(iterable or [], **kwargs)

        with patch.object(_mod, "Counter", _SpyC):
            gen._compute_most_discussed_topic(items)

        for s in sizes:
            self.assertLessEqual(s, gen._MAX_TOPIC_TOKENS)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
