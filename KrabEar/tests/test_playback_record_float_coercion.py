"""test_playback_record_float_coercion.py — тесты безопасной коэрции duration
в handle_record_playback (IPC-хендлер PlaybackTracker).

Проверяет, что non-numeric/NaN/Inf/список в поле duration_listened_sec
НЕ крашат хендлер, а деградируют до 0.0 gracefully.
Связан с: backend/playback_tracker.py::handle_record_playback (wave-security fix).
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
    """Конструирует PlaybackTracker без data_dir (только in-memory, без файла).

    privacy_mode=False — чтобы record_playback реально записывал, а не был no-op.
    """
    return PlaybackTracker(data_dir=None, privacy_mode_enabled=False)


class TestHandleRecordPlaybackFloatCoercion(unittest.TestCase):
    """Граничные случаи коэрции поля duration_listened_sec на IPC-границе."""

    def test_non_numeric_duration_does_not_crash(self) -> None:
        """Non-numeric строка ("abc") → коэрция в 0.0, хендлер возвращает dict."""
        tracker = _make_tracker()
        result = tracker.handle_record_playback(
            {"item_id": "x", "duration_listened_sec": "abc"}
        )
        self.assertIsInstance(result, dict)

    def test_list_duration_does_not_crash(self) -> None:
        """Список ["bad"] → TypeError поглощается, хендлер возвращает dict."""
        tracker = _make_tracker()
        result = tracker.handle_record_playback(
            {"item_id": "x", "duration_listened_sec": ["bad"]}
        )
        self.assertIsInstance(result, dict)

    def test_numeric_string_parsed(self) -> None:
        """Числовая строка "12.5" корректно парсится, хендлер возвращает dict."""
        tracker = _make_tracker()
        result = tracker.handle_record_playback(
            {"item_id": "x", "duration_listened_sec": "12.5"}
        )
        self.assertIsInstance(result, dict)

    def test_nan_duration_coerced(self) -> None:
        """NaN → коэрция в 0.0 в хендлере, record_playback получает чистый float."""
        tracker = _make_tracker()
        # Передаём NaN напрямую через dict — до правки это вызывало invalid_duration
        # внутри record_playback. Теперь хендлер коэрцирует до вызова.
        result = tracker.handle_record_playback(
            {"item_id": "x", "duration_listened_sec": float("nan")}
        )
        self.assertIsInstance(result, dict)

    def test_missing_duration_defaults_zero(self) -> None:
        """Отсутствие ключа duration_listened_sec → defaults 0.0, хендлер ОК."""
        tracker = _make_tracker()
        result = tracker.handle_record_playback({"item_id": "x"})
        self.assertIsInstance(result, dict)


if __name__ == "__main__":
    unittest.main()
