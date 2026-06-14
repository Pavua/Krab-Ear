"""test_playback_record_float_coercion.py — non-numeric duration на IPC-границе
handle_record_playback (PlaybackTracker) НЕ должен крашить.

Реальный баг: float("abc") бросал ValueError и крашил хендлер. Фикс направляет
невалидное (non-numeric/список) значение в NaN, чтобы существующий non-finite
guard внутри record_playback отклонил его с {"ok": False, "reason":
"invalid_duration"} — КОНСИСТЕНТНО с прямыми NaN/Inf (см. wave-34
test_wave34_playback_usage_guards.py). NaN/Inf НЕ коэрсятся в 0.0 — бессмысленная
длительность отклоняется, а не молча записывается.

Связан с: backend/playback_tracker.py::handle_record_playback.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Добавляем PROJECT_ROOT в sys.path — как в остальных тестах проекта.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.playback_tracker import PlaybackTracker  # noqa: E402


def _make_tracker() -> PlaybackTracker:
    """Конструирует PlaybackTracker без data_dir (in-memory), privacy OFF."""
    return PlaybackTracker(data_dir=None, privacy_mode_enabled=False)


class TestHandleRecordPlaybackFloatCoercion(unittest.TestCase):
    """Граничные случаи duration_listened_sec на IPC-границе."""

    def test_non_numeric_str_does_not_crash_and_rejected(self) -> None:
        """Non-numeric строка ("abc") не крашит, отклоняется как invalid_duration."""
        result = _make_tracker().handle_record_playback(
            {"item_id": "x", "duration_listened_sec": "abc"}
        )
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("ok"), False)
        self.assertEqual(result.get("reason"), "invalid_duration")

    def test_list_duration_does_not_crash_and_rejected(self) -> None:
        """Список ["bad"] не крашит (TypeError → NaN), отклоняется."""
        result = _make_tracker().handle_record_playback(
            {"item_id": "x", "duration_listened_sec": ["bad"]}
        )
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("ok"), False)
        self.assertEqual(result.get("reason"), "invalid_duration")

    def test_numeric_string_parsed_and_recorded(self) -> None:
        """Числовая строка "12.5" парсится и НЕ отклоняется (записывается)."""
        result = _make_tracker().handle_record_playback(
            {"item_id": "x", "duration_listened_sec": "12.5"}
        )
        self.assertIsInstance(result, dict)
        self.assertNotEqual(result.get("reason"), "invalid_duration")

    def test_nan_duration_still_rejected(self) -> None:
        """Прямой NaN отклоняется (wave-34 контракт сохранён, не коэрсится в 0.0)."""
        result = _make_tracker().handle_record_playback(
            {"item_id": "x", "duration_listened_sec": float("nan")}
        )
        self.assertEqual(result.get("ok"), False)
        self.assertEqual(result.get("reason"), "invalid_duration")

    def test_inf_duration_still_rejected(self) -> None:
        """Прямой Inf отклоняется (wave-34 контракт сохранён)."""
        result = _make_tracker().handle_record_playback(
            {"item_id": "x", "duration_listened_sec": float("inf")}
        )
        self.assertEqual(result.get("ok"), False)
        self.assertEqual(result.get("reason"), "invalid_duration")

    def test_missing_duration_defaults_zero(self) -> None:
        """Отсутствие ключа → 0.0, не отклоняется."""
        result = _make_tracker().handle_record_playback({"item_id": "x"})
        self.assertIsInstance(result, dict)
        self.assertNotEqual(result.get("reason"), "invalid_duration")


if __name__ == "__main__":
    unittest.main()
