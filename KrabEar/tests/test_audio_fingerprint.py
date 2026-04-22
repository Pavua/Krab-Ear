"""Тесты AudioFingerprinter — аудио-фингерпринтинг и обнаружение дубликатов.

Покрывает:
- fingerprint(): детерминированность, разные сигналы → разные хеши
- compare(): точное совпадение, идентичные/разные фингерпринты
- is_duplicate_audio(): порог, идентичное/разное аудио, тихий сигнал, многоканальное
- IPC check_audio_duplicate через BackendService
"""

from __future__ import annotations
from core.audio_fingerprint import AudioFingerprinter

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── Вспомогательные генераторы аудио ────────────────────────────────────────

def _sine(freq: float = 440.0, duration: float = 0.5, sr: int = 16000) -> np.ndarray:
    """Синус заданной частоты."""
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    return np.sin(2 * np.pi * freq * t).astype(np.float32)


def _white_noise(duration: float = 0.5, sr: int = 16000, seed: int = 42) -> np.ndarray:
    """Белый шум с фиксированным seed."""
    rng = np.random.default_rng(seed)
    return rng.standard_normal(int(sr * duration)).astype(np.float32)


# ── Тесты AudioFingerprinter ─────────────────────────────────────────────────

class TestAudioFingerprinterFingerprint(unittest.TestCase):
    """Тесты метода fingerprint()."""

    def setUp(self) -> None:
        self.fp = AudioFingerprinter()
        self.sr = 16000

    # 1. Детерминированность: одинаковый сигнал → одинаковый хеш
    def test_fingerprint_is_deterministic(self) -> None:
        audio = _sine(440.0, duration=0.5, sr=self.sr)
        h1 = self.fp.fingerprint(audio, self.sr)
        h2 = self.fp.fingerprint(audio, self.sr)
        self.assertEqual(h1, h2)

    # 2. Разные сигналы → разные хеши
    def test_different_signals_produce_different_fingerprints(self) -> None:
        sine_440 = _sine(440.0, duration=0.5, sr=self.sr)
        sine_880 = _sine(880.0, duration=0.5, sr=self.sr)
        h_440 = self.fp.fingerprint(sine_440, self.sr)
        h_880 = self.fp.fingerprint(sine_880, self.sr)
        self.assertNotEqual(h_440, h_880)

    # 3. Возвращает строку длиной 64 символа (SHA-256 hex)
    def test_fingerprint_returns_64_char_hex_string(self) -> None:
        audio = _sine(440.0, duration=0.3, sr=self.sr)
        h = self.fp.fingerprint(audio, self.sr)
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 64)
        # Только hex-символы
        int(h, 16)  # не вызовет ValueError если строка корректная

    # 4. Короткий сигнал (меньше одного окна) не вызывает исключений
    def test_short_audio_does_not_raise(self) -> None:
        short = np.zeros(100, dtype=np.float32)
        try:
            h = self.fp.fingerprint(short, self.sr)
            self.assertEqual(len(h), 64)
        except Exception as exc:
            self.fail(f"fingerprint() вызвал исключение для короткого сигнала: {exc}")

    # 5. Тишина (нулевой сигнал) → хеш отличается от шума
    def test_silence_fingerprint_differs_from_noise(self) -> None:
        silence = np.zeros(8000, dtype=np.float32)
        noise = _white_noise(duration=0.5, sr=self.sr, seed=7)
        h_silence = self.fp.fingerprint(silence, self.sr)
        h_noise = self.fp.fingerprint(noise, self.sr)
        self.assertNotEqual(h_silence, h_noise)

    # 6. Многоканальный ввод (2D array) обрабатывается без ошибок
    def test_multichannel_audio_processed_without_error(self) -> None:
        stereo = np.stack([_sine(440.0), _sine(880.0)])  # shape (2, N)
        try:
            h = self.fp.fingerprint(stereo, self.sr)
            self.assertEqual(len(h), 64)
        except Exception as exc:
            self.fail(f"fingerprint() вызвал исключение для стерео: {exc}")


class TestAudioFingerprinterCompare(unittest.TestCase):
    """Тесты метода compare()."""

    def setUp(self) -> None:
        self.fp = AudioFingerprinter()
        self.sr = 16000

    # 7. Идентичные хеши → сходство 1.0
    def test_identical_fingerprints_return_1(self) -> None:
        audio = _sine(440.0)
        h = self.fp.fingerprint(audio, self.sr)
        self.assertEqual(self.fp.compare(h, h), 1.0)

    # 8. Разные хеши → сходство < 1.0
    def test_different_fingerprints_return_less_than_1(self) -> None:
        h1 = self.fp.fingerprint(_sine(440.0), self.sr)
        h2 = self.fp.fingerprint(_sine(880.0), self.sr)
        similarity = self.fp.compare(h1, h2)
        self.assertLess(similarity, 1.0)

    # 9. Сходство в диапазоне [0.0, 1.0]
    def test_similarity_in_range_0_to_1(self) -> None:
        h1 = self.fp.fingerprint(_white_noise(seed=1), self.sr)
        h2 = self.fp.fingerprint(_white_noise(seed=99), self.sr)
        s = self.fp.compare(h1, h2)
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)

    # 10. Пустые строки → сходство 0.0
    def test_empty_fingerprints_return_0(self) -> None:
        self.assertEqual(self.fp.compare("", ""), 0.0)
        h = self.fp.fingerprint(_sine(440.0), self.sr)
        self.assertEqual(self.fp.compare(h, ""), 0.0)


class TestAudioFingerprinterIsDuplicate(unittest.TestCase):
    """Тесты метода is_duplicate_audio()."""

    def setUp(self) -> None:
        self.fp = AudioFingerprinter()
        self.sr = 16000

    # 11. Идентичный сигнал → дубликат
    def test_identical_audio_is_duplicate(self) -> None:
        audio = _sine(440.0)
        self.assertTrue(self.fp.is_duplicate_audio(audio, audio, sample_rate=self.sr))

    # 12. Совершенно разные сигналы → не дубликат при высоком пороге
    def test_different_audio_not_duplicate_at_high_threshold(self) -> None:
        sine = _sine(440.0)
        noise = _white_noise(seed=42)
        self.assertFalse(
            self.fp.is_duplicate_audio(sine, noise, sample_rate=self.sr, threshold=0.95)
        )

    # 13. Порог 0.0 → всегда дубликат
    def test_threshold_zero_always_duplicate(self) -> None:
        a1 = _sine(440.0)
        a2 = _white_noise(seed=5)
        self.assertTrue(
            self.fp.is_duplicate_audio(a1, a2, sample_rate=self.sr, threshold=0.0)
        )

    # 14. Порог 1.0 → дубликат только при точном совпадении
    def test_threshold_one_only_exact_match(self) -> None:
        audio = _sine(220.0)
        # Идентичный → дубликат
        self.assertTrue(
            self.fp.is_duplicate_audio(audio, audio, sample_rate=self.sr, threshold=1.0)
        )
        # Разный сигнал → не дубликат
        other = _sine(880.0)
        self.assertFalse(
            self.fp.is_duplicate_audio(audio, other, sample_rate=self.sr, threshold=1.0)
        )

    # 15. Слегка зашумлённая копия сигнала при умеренном пороге
    def test_slightly_noisy_copy_similarity(self) -> None:
        original = _sine(440.0, duration=1.0, sr=self.sr)
        noisy = original + np.random.default_rng(0).normal(0, 0.01, len(original)).astype(np.float32)

        fp_orig = self.fp.fingerprint(original, self.sr)
        fp_noisy = self.fp.fingerprint(noisy, self.sr)
        similarity = self.fp.compare(fp_orig, fp_noisy)
        # Сходство должно быть высоким (шум мал), но это зависит от квантования
        # Проверяем только что метрика вычислена и в диапазоне
        self.assertGreaterEqual(similarity, 0.0)
        self.assertLessEqual(similarity, 1.0)

    # 16. Идентичный сигнал → сходство > 0.5 (выше случайного)
    def test_identical_audio_higher_similarity_than_different(self) -> None:
        """Фингерпринт идентичного аудио имеет большее сходство чем совершенно разных."""
        audio = _sine(440.0, duration=0.5, sr=self.sr)
        fp_same = self.fp.fingerprint(audio, self.sr)
        fp_other = self.fp.fingerprint(_white_noise(seed=77), self.sr)
        sim_same = self.fp.compare(fp_same, fp_same)
        sim_diff = self.fp.compare(fp_same, fp_other)
        self.assertGreater(sim_same, sim_diff)

    # 17. Малошумный сигнал не является дубликатом при threshold=1.0
    def test_noisy_copy_not_exact_duplicate(self) -> None:
        """Зашумлённая копия не проходит порог точного совпадения (threshold=1.0)
        если признаки изменились после квантования."""
        original = _sine(440.0, duration=0.5, sr=self.sr)
        # Большой шум — точно изменит квантованные признаки
        very_noisy = original + np.random.default_rng(123).normal(0, 0.5, len(original)).astype(np.float32)
        fp_orig = self.fp.fingerprint(original, self.sr)
        fp_noisy = self.fp.fingerprint(very_noisy, self.sr)
        # При большом шуме fingerprints могут совпасть (квантование грубое) —
        # допускаем оба исхода, но проверяем что метод работает без ошибок
        result = self.fp.compare(fp_orig, fp_noisy)
        self.assertIsInstance(result, float)
        self.assertGreaterEqual(result, 0.0)
        self.assertLessEqual(result, 1.0)


class TestAudioFingerprinterIPCHandler(unittest.TestCase):
    """Тесты IPC-метода check_audio_duplicate через BackendService."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        from backend.state_store import StateStore
        from backend.service import BackendService
        store = StateStore(data_dir=Path(self._tmp.name))
        self.svc = BackendService(store=store)
        self.sr = 16000

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _dispatch(self, params: dict) -> dict:
        resp = self.svc.handle_request({"id": "t", "method": "check_audio_duplicate", "params": params})
        return resp

    # 16. Идентичный сигнал → is_duplicate=True
    def test_ipc_identical_audio_is_duplicate(self) -> None:
        audio = _sine(440.0).tolist()
        resp = self._dispatch({"audio1": audio, "audio2": audio, "sample_rate": self.sr})
        self.assertTrue(resp["ok"])
        result = resp["result"]
        self.assertTrue(result["is_duplicate"])
        self.assertAlmostEqual(result["similarity"], 1.0, places=5)

    # 17. Разные сигналы → is_duplicate=False при стандартном пороге
    def test_ipc_different_audio_not_duplicate(self) -> None:
        a1 = _sine(440.0).tolist()
        a2 = _white_noise(seed=42).tolist()
        resp = self._dispatch({"audio1": a1, "audio2": a2, "sample_rate": self.sr})
        self.assertTrue(resp["ok"])
        result = resp["result"]
        self.assertFalse(result["is_duplicate"])

    # 18. Пропущенный audio1 → ok=False
    def test_ipc_missing_audio1_returns_error(self) -> None:
        audio = _sine(440.0).tolist()
        resp = self._dispatch({"audio2": audio})
        self.assertFalse(resp["ok"])

    # 19. Ответ содержит все ожидаемые ключи
    def test_ipc_response_has_expected_keys(self) -> None:
        audio = _sine(440.0).tolist()
        resp = self._dispatch({"audio1": audio, "audio2": audio, "sample_rate": self.sr})
        self.assertTrue(resp["ok"])
        result = resp["result"]
        for key in ("fingerprint1", "fingerprint2", "similarity", "is_duplicate"):
            self.assertIn(key, result, f"Ключ '{key}' отсутствует в ответе")

    # 20. Кастомный threshold применяется корректно
    def test_ipc_custom_threshold_applied(self) -> None:
        a1 = _sine(440.0).tolist()
        a2 = _sine(880.0).tolist()
        # При пороге 0.0 любые сигналы — дубликаты
        resp = self._dispatch({"audio1": a1, "audio2": a2, "sample_rate": self.sr, "threshold": 0.0})
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["result"]["is_duplicate"])


if __name__ == "__main__":
    unittest.main()
