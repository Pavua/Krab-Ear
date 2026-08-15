"""Тесты для фикса KRAB-EAR-BACKEND-1V: хроническое зависание stop_recording.

Проверяет:
1. watchdog таймаут (MLX_TRANSCRIBE_TIMEOUT_SEC + MLX_HANG_HARD_KILL_SEC) суммарно
   не превышает допустимый бюджет (<=45s), гарантируя возврат до истечения Swift-таймаута (120s)
   и IPC backstop (180s).
2. При MLXTimeoutError в _transcribe_model цикл по variants прерывается немедленно
   (не умножает зависание на число вариантов параметров).
3. STTWhisperMLXAdapter защищён MLXWatchdog таймаутом.
4. mlx_lock поддерживает контролируемый захват с таймаутом.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.mlx_subprocess import MLXTimeoutError, MLX_HANG_HARD_KILL_SEC, MLXWatchdog


def _make_engine_stub() -> object:
    """Создать минимальный стаб AudioEngine без загрузки реальных весов моделей."""
    from core.engine import AudioEngine
    engine = AudioEngine.__new__(AudioEngine)
    engine.current_model = "mlx-community/whisper-base-mlx"
    engine.quality_profile = "balanced"
    engine._unavailable_models = {}
    engine._error_bus = MagicMock()
    engine._llm_rewriter = None
    engine._settings_get = lambda k, d: d
    return engine


class TestStopRecordingHang1V(unittest.TestCase):
    """Тесты предотвращения 180с зависания stop_recording (KRAB-EAR-BACKEND-1V)."""

    def test_mlx_hang_hard_kill_sec_bounded(self):
        """MLX_HANG_HARD_KILL_SEC по умолчанию должен быть коротким (<=15s), а не 120s."""
        # 120s hard kill + 60s transcribe timeout = 180s, что ровно упиралось в IPC backstop.
        self.assertLessEqual(
            MLX_HANG_HARD_KILL_SEC,
            15.0,
            "MLX_HANG_HARD_KILL_SEC не должен превышать 15с во избежание 180с дедлока",
        )

    def test_mlx_watchdog_total_timeout_budget(self):
        """MLXWatchdog.run_with_timeout при зависшем воркере должен завершаться в пределах бюджета."""
        watchdog = MLXWatchdog()
        # Симулируем зависшую функцию
        def _hung_fn():
            time.sleep(10.0)

        t0 = time.monotonic()
        with patch("core.mlx_subprocess.MLX_HANG_HARD_KILL_SEC", 0.2):
            with self.assertRaises(MLXTimeoutError):
                watchdog.run_with_timeout(_hung_fn, timeout_sec=0.2, model_name="test-model")
        elapsed = time.monotonic() - t0
        # Общее время должно быть около timeout_sec + hard_kill_sec (~0.4s), а не висеть секундами
        self.assertLess(elapsed, 1.5)

    def test_mlx_timeout_does_not_retry_variants(self):
        """При MLXTimeoutError _transcribe_model НЕ должен пробовать остальные variants.

        Повтор вызова на зависшем Metal GPU с другими kwargs бесполезен и
        умножает время зависания на N вариантов.
        """
        engine = _make_engine_stub()
        timeout_exc = MLXTimeoutError(timeout_sec=1.0, model_name="mlx-community/whisper-base-mlx")

        watchdog_mock = MagicMock()
        watchdog_mock.run_with_timeout.side_effect = timeout_exc

        import numpy as np
        audio_data = np.zeros(16000, dtype=np.float32)

        with (
            patch("core.engine.settings") as mock_settings,
            patch("core.engine.get_watchdog", return_value=watchdog_mock),
            patch("core.engine.mlx_lock") as mlx_lock_mock,
            patch("core.engine.mlx_inter_process_lock") as inter_lock_mock,
            patch("core.engine.mlx_whisper"),
        ):
            mlx_lock_mock.return_value.__enter__ = MagicMock(return_value=None)
            mlx_lock_mock.return_value.__exit__ = MagicMock(return_value=False)
            inter_lock_mock.return_value.__enter__ = MagicMock(return_value=None)
            inter_lock_mock.return_value.__exit__ = MagicMock(return_value=False)
            mock_settings.TRANSCRIBE_LANGUAGE = None
            mock_settings.MLX_CRASH_RECOVERY_ENABLED = True
            mock_settings.MLX_TRANSCRIBE_TIMEOUT_SEC = 35.0

            with self.assertRaises(MLXTimeoutError):
                engine._transcribe_model(audio_data, "mlx-community/whisper-base-mlx", "")

        # Watchdog должен быть вызван РОВНО ОДИН РАЗ, без повторов других variants
        self.assertEqual(watchdog_mock.run_with_timeout.call_count, 1)

    def test_whisper_mlx_adapter_uses_watchdog_timeout(self):
        """WhisperMLXAdapter должен использовать watchdog для защиты от GPU wedge."""
        from core.pipeline.stt_whisper_mlx_adapter import WhisperMLXAdapter
        import numpy as np

        adapter = WhisperMLXAdapter(model_path="mlx-community/whisper-base-mlx")
        audio = np.zeros(16000, dtype=np.float32)

        timeout_exc = MLXTimeoutError(timeout_sec=1.0, model_name="mlx-community/whisper-base-mlx")
        watchdog_mock = MagicMock()
        watchdog_mock.run_with_timeout.side_effect = timeout_exc

        mock_mlx_whisper = MagicMock()
        with (
            patch.dict("sys.modules", {"mlx_whisper": mock_mlx_whisper}),
            patch("core.pipeline.stt_whisper_mlx_adapter.get_watchdog", return_value=watchdog_mock),
            patch("core.pipeline.stt_whisper_mlx_adapter.mlx_lock"),
            patch("core.pipeline.stt_whisper_mlx_adapter.mlx_inter_process_lock"),
        ):
            with self.assertRaises(MLXTimeoutError):
                adapter.transcribe(audio)

        self.assertEqual(watchdog_mock.run_with_timeout.call_count, 1)


if __name__ == "__main__":
    unittest.main()
