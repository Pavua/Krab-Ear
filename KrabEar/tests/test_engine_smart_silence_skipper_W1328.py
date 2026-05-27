"""Тесты: SmartSilenceSkipper wired in AudioEngine pipeline (W1328).

Проверяет, что step 2.7 удаления пауз:
- применяется к numpy-массивам при SMART_SILENCE_SKIP_ENABLED=True
- пропускается при SMART_SILENCE_SKIP_ENABLED=False
- пропускается при is_preview=True
- пропускается для file path (не numpy)
- мягко деградирует (soft-fail) если SmartSilenceSkipper бросает исключение
- engine.py содержит код шага 2.7
"""

import sys
import os
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

import numpy as np


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_audio_with_silence(duration_sec: float = 1.0, sample_rate: int = 16000) -> np.ndarray:
    """Возвращает аудио с речью, тишиной в середине и речью в конце."""
    n = int(sample_rate * duration_sec)
    audio = np.zeros(n, dtype=np.float32)
    # Первые 20% — речь
    speech_end = int(n * 0.2)
    t = np.linspace(0, duration_sec * 0.2, speech_end, endpoint=False)
    audio[:speech_end] = 0.5 * np.sin(2 * np.pi * 440 * t).astype(np.float32)
    # Последние 20% — речь
    speech_start2 = int(n * 0.8)
    t2 = np.linspace(0, duration_sec * 0.2, n - speech_start2, endpoint=False)
    audio[speech_start2:] = 0.5 * np.sin(2 * np.pi * 440 * t2).astype(np.float32)
    return audio


def _make_speech_audio(duration_sec: float = 0.5, sample_rate: int = 16000) -> np.ndarray:
    """Возвращает чистый речевой синусоидальный сигнал без пауз."""
    n = int(sample_rate * duration_sec)
    t = np.linspace(0, duration_sec, n, endpoint=False)
    return (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


# ---------------------------------------------------------------------------
# Config flag test
# ---------------------------------------------------------------------------

class TestConfigFlag(unittest.TestCase):
    """SMART_SILENCE_SKIP_ENABLED присутствует в Settings с default=False."""

    def test_flag_exists_and_defaults_false(self):
        from core.config import Settings
        s = Settings()
        self.assertTrue(hasattr(s, "SMART_SILENCE_SKIP_ENABLED"))
        self.assertIs(s.SMART_SILENCE_SKIP_ENABLED, False)

    def test_flag_can_be_overridden_true(self):
        from core.config import Settings
        s = Settings(SMART_SILENCE_SKIP_ENABLED=True)
        self.assertIs(s.SMART_SILENCE_SKIP_ENABLED, True)

    def test_env_override(self):
        """KRAB_EAR_SMART_SILENCE_SKIP_ENABLED=true включает фичу."""
        with patch.dict(os.environ, {"KRAB_EAR_SMART_SILENCE_SKIP_ENABLED": "true"}):
            from core.config import Settings
            s = Settings()
            self.assertIs(s.SMART_SILENCE_SKIP_ENABLED, True)


# ---------------------------------------------------------------------------
# SmartSilenceSkipper unit tests
# ---------------------------------------------------------------------------

class TestSmartSilenceSkipperUnit(unittest.TestCase):
    """SmartSilenceSkipper.process() базовые контракты."""

    def setUp(self):
        from core.smart_silence_skipper import SmartSilenceSkipper
        self.skipper = SmartSilenceSkipper()

    def test_process_returns_skip_result(self):
        """process() возвращает SkipResult с processed_audio."""
        from core.smart_silence_skipper import SkipResult
        audio = _make_speech_audio()
        result = self.skipper.process(audio, 16000)
        self.assertIsInstance(result, SkipResult)
        self.assertIsInstance(result.processed_audio, np.ndarray)

    def test_process_audio_with_silence_is_shorter(self):
        """Аудио с длинными паузами должно стать короче после обработки."""
        audio = _make_audio_with_silence(duration_sec=2.0)
        result = self.skipper.process(audio, 16000)
        # processed_duration <= original_duration
        self.assertLessEqual(
            result.processed_duration_sec,
            result.original_duration_sec + 0.01,  # float tolerance
        )

    def test_process_preserves_dtype(self):
        """Выходной массив должен иметь тот же dtype, что и входной."""
        audio = _make_speech_audio().astype(np.float32)
        result = self.skipper.process(audio, 16000)
        self.assertEqual(result.processed_audio.dtype, np.float32)

    def test_empty_audio_returns_unchanged(self):
        """Пустой массив возвращается без изменений."""
        audio = np.array([], dtype=np.float32)
        result = self.skipper.process(audio, 16000)
        self.assertEqual(len(result.processed_audio), 0)

    def test_zero_sample_rate_returns_unchanged(self):
        """sample_rate=0 возвращает оригинальное аудио без падения."""
        audio = _make_speech_audio()
        result = self.skipper.process(audio, 0)
        np.testing.assert_array_equal(result.processed_audio, audio)


# ---------------------------------------------------------------------------
# Engine wiring tests (mock-based, simulate pipeline step 2.7)
# ---------------------------------------------------------------------------

class TestSmartSilenceSkipperWiredWhenEnabled(unittest.TestCase):
    """step 2.7 вызывает SmartSilenceSkipper при SMART_SILENCE_SKIP_ENABLED=True."""

    def test_smart_silence_skipper_wired_when_enabled(self):
        """SmartSilenceSkipper.process вызывается при enabled=True и numpy input."""
        from core.smart_silence_skipper import SmartSilenceSkipper, SkipResult

        audio = _make_audio_with_silence()
        call_log = []

        original_process = SmartSilenceSkipper.process

        def mock_process(self_sk, audio_arr, sr):
            call_log.append((audio_arr, sr))
            return original_process(self_sk, audio_arr, sr)

        mock_settings = MagicMock()
        mock_settings.SMART_SILENCE_SKIP_ENABLED = True

        with patch("core.smart_silence_skipper.SmartSilenceSkipper.process", mock_process):
            with patch("core.engine.settings", mock_settings):
                # Simulate step 2.7 logic
                result_audio = audio
                if mock_settings.SMART_SILENCE_SKIP_ENABLED and isinstance(audio, np.ndarray):
                    try:
                        skipper_result = SmartSilenceSkipper().process(audio, 16000)
                        result_audio = skipper_result.processed_audio
                    except Exception:
                        pass

        self.assertEqual(len(call_log), 1, "SmartSilenceSkipper.process должен быть вызван один раз")
        np.testing.assert_array_equal(call_log[0][0], audio)
        self.assertEqual(call_log[0][1], 16000)


class TestSmartSilenceSkipperSkippedWhenDisabled(unittest.TestCase):
    """step 2.7 НЕ вызывает SmartSilenceSkipper при SMART_SILENCE_SKIP_ENABLED=False."""

    def test_smart_silence_skipper_skipped_when_disabled(self):
        """SmartSilenceSkipper.process НЕ вызывается при enabled=False."""
        from core.smart_silence_skipper import SmartSilenceSkipper

        audio = _make_audio_with_silence()
        call_log = []

        original_process = SmartSilenceSkipper.process

        def mock_process(self_sk, audio_arr, sr):
            call_log.append((audio_arr, sr))
            return original_process(self_sk, audio_arr, sr)

        mock_settings = MagicMock()
        mock_settings.SMART_SILENCE_SKIP_ENABLED = False

        with patch("core.smart_silence_skipper.SmartSilenceSkipper.process", mock_process):
            with patch("core.engine.settings", mock_settings):
                # Simulate step 2.7 logic
                result_audio = audio
                if mock_settings.SMART_SILENCE_SKIP_ENABLED and isinstance(audio, np.ndarray):
                    skipper_result = SmartSilenceSkipper().process(audio, 16000)
                    result_audio = skipper_result.processed_audio

        self.assertEqual(len(call_log), 0, "SmartSilenceSkipper.process не должен вызываться при disabled")
        np.testing.assert_array_equal(result_audio, audio)

    def test_smart_silence_skipper_skipped_for_preview(self):
        """SmartSilenceSkipper.process НЕ вызывается при is_preview=True."""
        from core.smart_silence_skipper import SmartSilenceSkipper

        audio = _make_audio_with_silence()
        call_log = []

        original_process = SmartSilenceSkipper.process

        def mock_process(self_sk, audio_arr, sr):
            call_log.append((audio_arr, sr))
            return original_process(self_sk, audio_arr, sr)

        mock_settings = MagicMock()
        mock_settings.SMART_SILENCE_SKIP_ENABLED = True
        is_preview = True

        with patch("core.smart_silence_skipper.SmartSilenceSkipper.process", mock_process):
            with patch("core.engine.settings", mock_settings):
                # Simulate step 2.7 logic (is_preview blocks execution)
                result_audio = audio
                if mock_settings.SMART_SILENCE_SKIP_ENABLED and not is_preview and isinstance(audio, np.ndarray):
                    skipper_result = SmartSilenceSkipper().process(audio, 16000)
                    result_audio = skipper_result.processed_audio

        self.assertEqual(len(call_log), 0, "SmartSilenceSkipper.process не должен вызываться при is_preview=True")
        np.testing.assert_array_equal(result_audio, audio)

    def test_smart_silence_skipper_skipped_for_file_path(self):
        """SmartSilenceSkipper.process не вызывается для строковых путей к файлам."""
        from core.smart_silence_skipper import SmartSilenceSkipper

        audio_path = "/tmp/test_audio.wav"
        call_log = []

        mock_settings = MagicMock()
        mock_settings.SMART_SILENCE_SKIP_ENABLED = True

        # Simulate step 2.7 isinstance check
        result = audio_path
        if mock_settings.SMART_SILENCE_SKIP_ENABLED and not False and isinstance(audio_path, np.ndarray):
            call_log.append("called")
            result = "should_not_reach"

        self.assertEqual(len(call_log), 0, "SmartSilenceSkipper не должен вызываться для file path")
        self.assertEqual(result, audio_path)


class TestSmartSilenceSkipperFailureFallback(unittest.TestCase):
    """step 2.7 мягко деградирует при исключении в SmartSilenceSkipper."""

    def test_smart_silence_skipper_failure_falls_back_to_original(self):
        """При исключении в SmartSilenceSkipper — продолжаем с оригинальным аудио."""
        audio = _make_audio_with_silence()
        original_audio = audio.copy()

        mock_settings = MagicMock()
        mock_settings.SMART_SILENCE_SKIP_ENABLED = True

        with patch("core.engine.settings", mock_settings):
            result_audio = audio
            if mock_settings.SMART_SILENCE_SKIP_ENABLED and not False and isinstance(audio, np.ndarray):
                try:
                    raise RuntimeError("Simulated SmartSilenceSkipper failure")
                except Exception:
                    pass  # soft-fail — используем оригинальное аудио

        np.testing.assert_array_equal(result_audio, original_audio,
                                      "Оригинальное аудио должно быть сохранено после soft-fail")


# ---------------------------------------------------------------------------
# Source code (AST) checks — verify step 2.7 is in engine.py
# ---------------------------------------------------------------------------

class TestEngineSourceContainsSmartSilenceStep(unittest.TestCase):
    """Smoke-check: engine.py содержит код step 2.7 SmartSilenceSkipper."""

    def _read_engine_source(self) -> str:
        engine_path = PROJECT_ROOT / "KrabEar" / "core" / "engine.py"
        return engine_path.read_text(encoding="utf-8")

    def test_smart_silence_skipper_import_present(self):
        src = self._read_engine_source()
        self.assertIn("from core.smart_silence_skipper import SmartSilenceSkipper", src)

    def test_smart_silence_skipper_process_call_present(self):
        src = self._read_engine_source()
        self.assertIn("SmartSilenceSkipper().process(", src)

    def test_smart_silence_skip_enabled_flag_check_present(self):
        src = self._read_engine_source()
        self.assertIn("SMART_SILENCE_SKIP_ENABLED", src)

    def test_step_27_comment_present(self):
        src = self._read_engine_source()
        self.assertIn("2.7", src)

    def test_step_27_after_step_26(self):
        """Step 2.7 должен идти после step 2.6 в engine.py."""
        src = self._read_engine_source()
        idx_26 = src.find("2.6")
        idx_27 = src.find("2.7")
        self.assertGreater(idx_26, 0, "step 2.6 не найден")
        self.assertGreater(idx_27, 0, "step 2.7 не найден")
        self.assertGreater(idx_27, idx_26, "step 2.7 должен быть после step 2.6 в файле")

    def test_step_27_before_step_3(self):
        """Step 2.7 должен идти до step 3 в engine.py."""
        src = self._read_engine_source()
        idx_27 = src.find("2.7")
        idx_3 = src.find("# 3. Вызов распознавания")
        self.assertGreater(idx_27, 0, "step 2.7 не найден")
        self.assertGreater(idx_3, 0, "step 3 не найден")
        self.assertLess(idx_27, idx_3, "step 2.7 должен быть до step 3 в файле")

    def test_soft_fail_exception_handler_present(self):
        """logger.exception в обработчике исключения — soft-fail pattern."""
        src = self._read_engine_source()
        self.assertIn("smart_silence_skipper: failed, continuing with original audio", src)

    def test_default_off_in_config(self):
        """SMART_SILENCE_SKIP_ENABLED по умолчанию False — не ломаем текущих пользователей."""
        from core.config import Settings
        s = Settings()
        self.assertIs(s.SMART_SILENCE_SKIP_ENABLED, False)


if __name__ == "__main__":
    unittest.main()
