"""Unit tests for CallSilenceProbe (Phase 3 step 2/4)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_silence_probe import CallSilenceProbe, _rms_db  # noqa: E402


def _make_silence(duration_sec: float, sample_rate: int = 16000) -> np.ndarray:
    """Буфер тишины (нулей)."""
    return np.zeros(int(duration_sec * sample_rate), dtype=np.float32)


def _make_noise(duration_sec: float, sample_rate: int = 16000, amp: float = 0.1) -> np.ndarray:
    """Буфер белого шума заданной амплитуды."""
    rng = np.random.default_rng(42)
    return (rng.uniform(-amp, amp, int(duration_sec * sample_rate))).astype(np.float32)


class TestRmsDb(unittest.TestCase):
    def test_silence_returns_min(self) -> None:
        audio = np.zeros(1600, dtype=np.float32)
        self.assertAlmostEqual(_rms_db(audio), -96.0)

    def test_full_scale_sine(self) -> None:
        t = np.linspace(0, 1, 16000)
        audio = np.sin(2 * np.pi * 440 * t).astype(np.float32)
        db = _rms_db(audio)
        # RMS of sine = 1/sqrt(2) ≈ -3 dBFS
        self.assertAlmostEqual(db, -3.0, delta=0.2)

    def test_empty_array(self) -> None:
        self.assertAlmostEqual(_rms_db(np.array([], dtype=np.float32)), -96.0)


class TestDetectSilenceWindow(unittest.TestCase):
    def setUp(self) -> None:
        self.probe = CallSilenceProbe()

    def test_pure_silence_10sec_detected(self) -> None:
        audio = _make_silence(12.0)
        self.assertTrue(
            self.probe.detect_silence_window(audio, duration_sec=10.0)
        )

    def test_short_silence_not_detected(self) -> None:
        audio = _make_silence(5.0)
        # Buffer < required_samples → False
        self.assertFalse(
            self.probe.detect_silence_window(audio, duration_sec=10.0)
        )

    def test_noise_not_silence(self) -> None:
        audio = _make_noise(15.0, amp=0.3)  # loud noise -10 dBFS ≈
        self.assertFalse(
            self.probe.detect_silence_window(audio, duration_sec=10.0, threshold_db=-40.0)
        )

    def test_noise_then_silence_detected(self) -> None:
        """Сначала шум, потом 10 сек тишины — должно детектировать."""
        noise = _make_noise(5.0, amp=0.3)
        silence = _make_silence(12.0)
        audio = np.concatenate([noise, silence])
        self.assertTrue(
            self.probe.detect_silence_window(audio, duration_sec=10.0)
        )

    def test_silence_then_noise_not_detected(self) -> None:
        """Тишина в начале, шум в конце — хвост не тихий."""
        silence = _make_silence(12.0)
        noise = _make_noise(5.0, amp=0.3)
        audio = np.concatenate([silence, noise])
        self.assertFalse(
            self.probe.detect_silence_window(audio, duration_sec=10.0)
        )

    def test_stereo_input_handled(self) -> None:
        """Стерео буфер автоматически усредняется в моно."""
        stereo = np.zeros((16000 * 12, 2), dtype=np.float32)
        self.assertTrue(
            self.probe.detect_silence_window(stereo, duration_sec=10.0)
        )

    def test_empty_buffer_returns_false(self) -> None:
        self.assertFalse(
            self.probe.detect_silence_window(np.array([], dtype=np.float32))
        )

    def test_none_buffer_returns_false(self) -> None:
        self.assertFalse(
            self.probe.detect_silence_window(None)  # type: ignore[arg-type]
        )


class TestConfirmSilenceWithProbe(unittest.TestCase):
    def setUp(self) -> None:
        self.probe = CallSilenceProbe(probe_wait_sec=0.05)

    def test_no_response_returns_false(self) -> None:
        # Нет сигнала → False (тишина подтверждена)
        calls: list[tuple[str, str]] = []

        def fake_say(phrase: str, language: str) -> None:
            calls.append((phrase, language))

        result = self.probe.confirm_silence_with_probe(language="ru", _say_fn=fake_say)
        self.assertFalse(result)
        self.assertEqual(len(calls), 1)

    def test_response_received_returns_true(self) -> None:
        """Если signal_response_received() вызывается из отдельного потока — True."""
        import threading

        self.probe = CallSilenceProbe(probe_wait_sec=0.5)

        def fake_say(phrase: str, language: str) -> None:
            # Симулируем ответ через 50 мс
            t = threading.Timer(0.05, self.probe.signal_response_received)
            t.start()

        result = self.probe.confirm_silence_with_probe(language="en", _say_fn=fake_say)
        self.assertTrue(result)

    def test_probe_phrase_ru(self) -> None:
        received: list[str] = []

        def fake_say(phrase: str, language: str) -> None:
            received.append(phrase)

        self.probe.confirm_silence_with_probe(language="ru", _say_fn=fake_say)
        self.assertIn("линии", received[0])

    def test_probe_phrase_en(self) -> None:
        received: list[str] = []

        def fake_say(phrase: str, language: str) -> None:
            received.append(phrase)

        self.probe.confirm_silence_with_probe(language="en", _say_fn=fake_say)
        self.assertIn("still", received[0])


class TestCheckSilencePublicMethod(unittest.TestCase):
    def setUp(self) -> None:
        self.probe = CallSilenceProbe()

    def test_check_silence_returns_dict(self) -> None:
        audio = _make_silence(12.0)
        result = self.probe.check_silence(audio, duration_sec=10.0)
        self.assertIn("is_silent", result)
        self.assertIn("threshold_db", result)
        self.assertIn("window_sec", result)

    def test_check_silence_true_for_silence(self) -> None:
        audio = _make_silence(12.0)
        result = self.probe.check_silence(audio, duration_sec=10.0)
        self.assertTrue(result["is_silent"])

    def test_check_silence_false_for_noise(self) -> None:
        audio = _make_noise(12.0, amp=0.3)
        result = self.probe.check_silence(audio, duration_sec=10.0)
        self.assertFalse(result["is_silent"])


if __name__ == "__main__":
    unittest.main()
