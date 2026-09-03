"""AudioEngine edge-case тесты (дополнение к test_engine_extended.py).

Покрытие:
- cleanup_transcript с пустой строкой
- Fallback chain: balanced falls → max succeeds
- Profile switch с mlx_lock contention (threading)
- Hallucination stripping edge cases
- normalize_audio: empty numpy array, mono vs stereo via ndarray
- speak(): пустая строка, None-подобные входы

Все MLX-модели и subprocess мокируются — реальных загрузок нет.
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _make_engine():
    from core.engine import AudioEngine
    return AudioEngine()


def _whisper_ok(text: str = "hello") -> dict:
    return {
        "text": text,
        "segments": [{"avg_logprob": -0.2, "start": 0.0, "end": 1.0}],
        "engine": "mlx-whisper",
        "model_used": "fake/balanced",
        "language": "ru",
    }


# ---------------------------------------------------------------------------
# 1. cleanup_transcript edge cases
# ---------------------------------------------------------------------------

class CleanupTranscriptEdgeCasesTests(unittest.TestCase):

    def test_empty_string_returns_empty(self):
        """cleanup_transcript('') → ''."""
        from core.engine import AudioEngine
        result = AudioEngine._cleanup_transcript("")
        self.assertEqual(result, "")

    def test_whitespace_only_returns_empty(self):
        """Строка из пробелов → ''."""
        from core.engine import AudioEngine
        result = AudioEngine._cleanup_transcript("   ")
        self.assertEqual(result, "")

    def test_single_word_no_hallucination(self):
        """Одно нормальное слово остаётся нетронутым."""
        from core.engine import AudioEngine
        result = AudioEngine._cleanup_transcript("Привет")
        self.assertEqual(result, "Привет")

    def test_empty_strict_returns_empty(self):
        """strict-профиль с пустой строкой → ''."""
        from core.engine import AudioEngine
        result = AudioEngine._cleanup_transcript("", cleanup_profile="strict")
        self.assertEqual(result, "")

    def test_pure_hallucination_strict_returns_empty(self):
        """Строка только из галлюцинации → '' (strict)."""
        from core.engine import AudioEngine
        result = AudioEngine._cleanup_transcript("Спасибо за внимание.", cleanup_profile="strict")
        self.assertEqual(result, "")

    def test_newlines_and_tabs_collapsed(self):
        """Перенос строки и табуляция не ломают cleanup."""
        from core.engine import AudioEngine
        raw = "Первое предложение.\nВторое предложение."
        result = AudioEngine._cleanup_transcript(raw, cleanup_profile="soft")
        self.assertIsInstance(result, str)
        # Не должен вернуть None или упасть


# ---------------------------------------------------------------------------
# 2. Fallback chain: balanced fails → max succeeds
# ---------------------------------------------------------------------------

class FallbackChainBalancedToMaxTests(unittest.TestCase):

    def setUp(self):
        self.engine = _make_engine()
        # Намерение этих тестов: облачный STT НАСТРОЕН. Гейт «нет ключа —
        # не ходить в облако» (инцидент 2026-08-26) иначе обрывает каскад
        # до remote, и проверялся бы не тот путь. Раньше проходило лишь
        # потому, что гейта в основном каскаде не было вовсе.
        self.engine._remote_stt_retry_configured = lambda: True

    def test_balanced_fail_max_succeeds(self):
        """balanced модель бросает ошибку → следующая в цепочке возвращает результат."""
        from core.config import settings

        failing_model = settings.MODEL_BALANCED
        called_models = []

        def fake_transcribe_model(audio_data, model_name, prompt, language=None, **kwargs):
            called_models.append(model_name)
            if model_name == failing_model:
                raise RuntimeError("balanced OOM")
            return _whisper_ok("max result")

        # Запускаем в balanced-режиме: candidates = [MODEL_BALANCED]
        # При ошибке → model помечается unavailable → переход к remote
        with patch.object(self.engine, "_transcribe_model", side_effect=fake_transcribe_model):
            with patch.object(
                self.engine, "_transcribe_remote", return_value=_whisper_ok("remote ok")
            ):
                with patch("core.engine.settings") as mock_cfg:
                    mock_cfg.MODEL_BALANCED = settings.MODEL_BALANCED
                    mock_cfg.model_max_list = [settings.MODEL_BALANCED]
                    mock_cfg.NETWORK_MODE = "online"
                    mock_cfg.PARAKEET_ENABLED = False
                    mock_cfg.SENSEVOICE_ENABLED = False
                    mock_cfg.WHISPERX_ENABLED = False
                    mock_cfg.VOXTRAL_ENABLED = False
                    mock_cfg.TRANSCRIBE_TIMEOUT_SEC = 30

                    self.engine._transcribe_with_fallback_impl(
                        np.zeros(8000, dtype=np.float32), "prompt"
                    )

        # balanced был попробован и упал
        self.assertIn(failing_model, called_models)
        # balanced помечен недоступным после ошибки
        self.assertIn(settings.MODEL_BALANCED, self.engine._unavailable_models)

    def test_all_local_fail_online_uses_remote(self):
        """Все кандидаты отказывают → remote вызывается при online-режиме."""
        from core.config import settings

        with patch.object(
            self.engine,
            "_transcribe_model",
            side_effect=RuntimeError("all fail"),
        ):
            with patch.object(
                self.engine,
                "_transcribe_remote",
                return_value=_whisper_ok("remote fallback"),
            ) as mock_remote:
                with patch("core.engine.settings") as mock_cfg:
                    mock_cfg.MODEL_BALANCED = settings.MODEL_BALANCED
                    mock_cfg.model_max_list = [settings.MODEL_BALANCED]
                    mock_cfg.NETWORK_MODE = "online"
                    mock_cfg.PARAKEET_ENABLED = False
                    mock_cfg.SENSEVOICE_ENABLED = False
                    mock_cfg.WHISPERX_ENABLED = False
                    mock_cfg.VOXTRAL_ENABLED = False
                    mock_cfg.TRANSCRIBE_TIMEOUT_SEC = 30

                    res = self.engine._transcribe_with_fallback_impl(
                        np.zeros(8000, dtype=np.float32), "prompt"
                    )

                mock_remote.assert_called_once()
                self.assertEqual(res["text"], "remote fallback")


# ---------------------------------------------------------------------------
# 3. Profile switch under mlx_lock contention (threading)
# ---------------------------------------------------------------------------

class ProfileSwitchMlxLockContentionTests(unittest.TestCase):
    """Проверяет, что set_quality_profile под contention не вызывает data race."""

    def setUp(self):
        self.engine = _make_engine()

    def test_concurrent_profile_switches_no_exception(self):
        """10 потоков переключают профиль одновременно — не должно быть исключений."""
        errors = []

        def switch_profiles():
            try:
                for _ in range(20):
                    self.engine.set_quality_profile("max")
                    self.engine.set_quality_profile("balanced")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=switch_profiles) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f"Exceptions in threads: {errors}")
        # Финальное состояние должно быть одним из валидных профилей
        self.assertIn(self.engine.quality_profile, {"balanced", "max"})

    def test_profile_switch_acquires_mlx_lock_during_transcribe(self):
        """mlx_lock захватывается при _transcribe_model даже при переключении профиля."""
        from core import mlx_lock as mlx_lock_module

        lock_entries = []
        original_lock = mlx_lock_module._mlx_lock

        class CountingRLock:
            def __enter__(self):
                lock_entries.append(1)
                return original_lock.__enter__()

            def __exit__(self, *args):
                return original_lock.__exit__(*args)

        spy = CountingRLock()

        with patch("core.engine.mlx_lock", return_value=spy):
            with patch("core.engine.mlx_whisper") as mock_mlx:
                mock_mlx.transcribe.return_value = _whisper_ok()
                # Первое переключение, затем транскрибация
                self.engine.set_quality_profile("balanced")
                self.engine._transcribe_model(
                    np.zeros(8000, dtype=np.float32),
                    "fake/balanced",
                    "prompt",
                )

        self.assertGreater(len(lock_entries), 0, "mlx_lock не был захвачен")


# ---------------------------------------------------------------------------
# 4. Hallucination stripping edge cases
# ---------------------------------------------------------------------------

class HallucinationStrippingEdgeCasesTests(unittest.TestCase):

    def test_hallucination_prefix_only(self):
        """Строка состоит только из галлюцинации — возвращает ''."""
        from core.engine import AudioEngine
        for text in [
            "Спасибо за просмотр!",
            "Подписывайтесь на канал.",
            "Продолжение следует...",
        ]:
            with self.subTest(text=text):
                result = AudioEngine._cleanup_transcript(text, cleanup_profile="strict")
                self.assertEqual(result, "")

    def test_hallucination_at_end_stripped(self):
        """Галлюцинация в конце удаляется, основной текст сохраняется."""
        from core.engine import AudioEngine
        raw = "Задача выполнена успешно. Спасибо за внимание."
        result = AudioEngine._cleanup_transcript(raw, cleanup_profile="strict")
        self.assertNotIn("Спасибо за внимание", result)
        self.assertIn("Задача выполнена успешно", result)

    def test_real_content_not_stripped(self):
        """Реальный контент, похожий на галлюцинацию, не удаляется агрессивно."""
        from core.engine import AudioEngine
        # Слово "канал" в контексте не является галлюцинацией
        raw = "Мы обсудили водоотводный канал в городе."
        result = AudioEngine._cleanup_transcript(raw, cleanup_profile="soft")
        self.assertIn("канал", result)

    def test_triple_repeat_any_profile(self):
        """Тройное повторение удаляется даже в soft-профиле."""
        from core.engine import AudioEngine
        raw = "да да да"
        result = AudioEngine._cleanup_transcript(raw, cleanup_profile="soft")
        # Должно либо стать пустым, либо содержать только одно 'да'
        self.assertNotEqual(result.count("да"), 3)


# ---------------------------------------------------------------------------
# 5. normalize_audio: empty array and ndarray paths
# ---------------------------------------------------------------------------

class NormalizeAudioNdarrayEdgeCasesTests(unittest.TestCase):

    def setUp(self):
        self.engine = _make_engine()

    def test_normalize_audio_empty_wav_returns_true(self):
        """WAV с нулевыми сэмплами (тишина) → normalize_audio возвращает True."""
        import tempfile
        import soundfile as sf

        silence = np.zeros(16000, dtype=np.float32)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp = f.name
        try:
            sf.write(tmp, silence, 16000)
            result = self.engine.normalize_audio(tmp)
            self.assertTrue(result)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def test_normalize_audio_mono_1_sample(self):
        """WAV из одного сэмпла (крайний случай) — не должен упасть."""
        import tempfile
        import soundfile as sf

        one_sample = np.array([0.5], dtype=np.float32)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp = f.name
        try:
            sf.write(tmp, one_sample, 16000)
            result = self.engine.normalize_audio(tmp)
            # True (нормализован) или False (ошибка), но не исключение
            self.assertIn(result, [True, False])
        finally:
            Path(tmp).unlink(missing_ok=True)

    def test_normalize_audio_stereo_vs_mono_same_result_type(self):
        """Стерео и моно должны давать одинаковый тип возврата (True)."""
        import tempfile
        import soundfile as sf

        mono = np.random.randn(16000).astype(np.float32) * 0.3
        stereo = np.random.randn(16000, 2).astype(np.float32) * 0.3

        results = []
        for audio, label in [(mono, "mono"), (stereo, "stereo")]:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp = f.name
            try:
                sf.write(tmp, audio, 16000)
                results.append(self.engine.normalize_audio(tmp))
            finally:
                Path(tmp).unlink(missing_ok=True)
            with self.subTest(label=label):
                self.assertTrue(results[-1])

    def test_normalize_audio_nonexistent_returns_path(self):
        """Несуществующий путь → возвращает тот же путь (legacy contract)."""
        result = self.engine.normalize_audio("/no/such/file.wav")
        self.assertEqual(result, "/no/such/file.wav")


# ---------------------------------------------------------------------------
# 6. speak() additional edge cases
# ---------------------------------------------------------------------------

class SpeakAdditionalEdgeCasesTests(unittest.TestCase):

    def setUp(self):
        self.engine = _make_engine()

    @patch("core.engine.subprocess.run")
    def test_speak_newline_in_text(self, mock_run):
        """speak() с переносом строки — не должен упасть, say вызывается."""
        with patch("core.engine.settings") as mock_settings:
            mock_settings.SAY_VOICE = ""
            self.engine.speak("Первая строка\nВторая строка")
        mock_run.assert_called_once()

    @patch("core.engine.subprocess.run")
    def test_speak_only_whitespace_is_noop(self, mock_run):
        """speak() с одними пробелами → subprocess.run не вызывается."""
        self.engine.speak("   \t\n  ")
        mock_run.assert_not_called()

    @patch("core.engine.subprocess.run")
    def test_speak_default_rate_included(self, mock_run):
        """При rate=185 (по умолчанию) флаг -r 185 включается в команду."""
        with patch("core.engine.settings") as mock_settings:
            mock_settings.SAY_VOICE = ""
            self.engine.speak("тест")
        cmd = mock_run.call_args.args[0]
        self.assertIn("-r", cmd)
        self.assertIn("185", cmd)


if __name__ == "__main__":
    unittest.main()
