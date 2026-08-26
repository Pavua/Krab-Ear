"""Расширенные тесты публичного API AudioEngine.

Покрытие:
- set_quality_profile (balanced/max, idempotent, invalid input)
- _unavailable_models: tracking и skip при следующем вызове
- normalize_audio (missing file, silence, клиппинг)
- mlx_lock: захват lock при вызове _transcribe_model
- fallback chain: balanced fail → max → remote; offline_strict не использует remote
- _resolve_diarization_device: auto-select mps/cpu через mock torch
- speak(): subprocess вызов macOS `say`, пустой текст — no-op
- _resolve_language: lang_hint маппинг и unknown → None
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine():
    """Создаёт AudioEngine без реальных моделей (они not imported в CI)."""
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
# 1. set_quality_profile
# ---------------------------------------------------------------------------

class SetQualityProfileTests(unittest.TestCase):

    def setUp(self):
        self.engine = _make_engine()

    def test_switch_balanced_to_max_changes_profile(self):
        """balanced → max меняет quality_profile и current_model."""
        from core.config import settings
        changed = self.engine.set_quality_profile("max")
        self.assertTrue(changed)
        self.assertEqual(self.engine.quality_profile, "max")
        self.assertIn(self.engine.current_model, settings.model_max_list)

    def test_switch_max_to_balanced_changes_profile(self):
        """max → balanced возвращает к balanced модели."""
        from core.config import settings
        self.engine.set_quality_profile("max")
        changed = self.engine.set_quality_profile("balanced")
        self.assertTrue(changed)
        self.assertEqual(self.engine.quality_profile, "balanced")
        self.assertEqual(self.engine.current_model, settings.MODEL_BALANCED)

    def test_idempotent_same_profile_returns_false(self):
        """Повторный вызов с тем же профилем → returns False (no-op)."""
        # Engine starts as balanced
        result = self.engine.set_quality_profile("balanced")
        self.assertFalse(result)

    def test_invalid_profile_coerces_to_balanced(self):
        """Неизвестный профиль приводится к 'balanced'."""
        from core.config import settings
        # First switch to max so we actually change state
        self.engine.set_quality_profile("max")
        self.engine.set_quality_profile("INVALID_PROFILE")
        self.assertEqual(self.engine.quality_profile, "balanced")
        self.assertEqual(self.engine.current_model, settings.MODEL_BALANCED)

    def test_profile_case_insensitive(self):
        """'MAX' (upper) обрабатывается как 'max'."""
        changed = self.engine.set_quality_profile("MAX")
        self.assertTrue(changed)
        self.assertEqual(self.engine.quality_profile, "max")


# ---------------------------------------------------------------------------
# 2. _unavailable_models tracking
# ---------------------------------------------------------------------------

class UnavailableModelsTrackingTests(unittest.TestCase):

    def setUp(self):
        self.engine = _make_engine()
        # Намерение этих тестов: облачный STT НАСТРОЕН. Гейт «нет ключа —
        # не ходить в облако» (инцидент 2026-08-26) иначе обрывает каскад
        # до remote, и проверялся бы не тот путь. Раньше проходило лишь
        # потому, что гейта в основном каскаде не было вовсе.
        self.engine._remote_stt_retry_configured = lambda: True

    def test_failed_model_added_to_unavailable_set(self):
        """Если mlx_whisper.transcribe бросает исключение — модель попадает в _unavailable_models."""
        from core.config import settings

        with patch("core.engine.mlx_whisper") as mock_mlx:
            mock_mlx.transcribe.side_effect = RuntimeError("OOM")
            with patch("core.engine.AudioEngine._transcribe_remote") as mock_remote:
                mock_remote.return_value = _whisper_ok("remote text")
                # offline_strict=False → после fail chain пойдёт к remote
                with patch("core.engine.settings") as mock_settings:
                    mock_settings.MODEL_BALANCED = settings.MODEL_BALANCED
                    mock_settings.model_max_list = settings.model_max_list
                    mock_settings.NETWORK_MODE = "online"
                    mock_settings.PARAKEET_ENABLED = False
                    mock_settings.SENSEVOICE_ENABLED = False
                    mock_settings.WHISPERX_ENABLED = False
                    mock_settings.VOXTRAL_ENABLED = False
                    mock_settings.TRANSCRIBE_TIMEOUT_SEC = 30
                    mock_settings.DIARIZATION_ENABLED = False
                    mock_settings.TRANSCRIBE_LANGUAGE = None
                    mock_settings.TRANSCRIBE_PROMPT = ""
                    mock_settings.MAX_AUDIO_MB = 1000
                    mock_settings.SENSEVOICE_EMOTION_TO_HISTORY = False

                    # Trigger fallback via _transcribe_with_fallback_impl
                    self.engine._transcribe_with_fallback_impl(
                        np.zeros(16000, dtype=np.float32), "prompt"
                    )

                    # The balanced model should now be in _unavailable_models
                    self.assertIn(settings.MODEL_BALANCED, self.engine._unavailable_models)

    def test_model_in_unavailable_set_is_skipped(self):
        """Модель из _unavailable_models пропускается в fallback chain без вызова."""
        from core.config import settings

        # Pre-populate the unavailable set with balanced model
        self.engine._unavailable_models[settings.MODEL_BALANCED] = __import__("time").monotonic()

        with patch("core.engine.AudioEngine._transcribe_model") as mock_tm:
            mock_tm.return_value = _whisper_ok()
            with patch("core.engine.AudioEngine._transcribe_remote") as mock_remote:
                mock_remote.return_value = _whisper_ok("remote")
                with patch("core.engine.settings") as mock_settings:
                    mock_settings.MODEL_BALANCED = settings.MODEL_BALANCED
                    mock_settings.model_max_list = [settings.MODEL_BALANCED]
                    mock_settings.NETWORK_MODE = "online"
                    mock_settings.PARAKEET_ENABLED = False
                    mock_settings.SENSEVOICE_ENABLED = False
                    mock_settings.WHISPERX_ENABLED = False
                    mock_settings.VOXTRAL_ENABLED = False
                    mock_settings.TRANSCRIBE_TIMEOUT_SEC = 30

                    self.engine._transcribe_with_fallback_impl(
                        np.zeros(8000, dtype=np.float32), "prompt"
                    )

                    # _transcribe_model should NOT have been called with the balanced model
                    for c in mock_tm.call_args_list:
                        self.assertNotEqual(c.args[1], settings.MODEL_BALANCED)


# ---------------------------------------------------------------------------
# 3. normalize_audio
# ---------------------------------------------------------------------------

class NormalizeAudioTests(unittest.TestCase):

    def setUp(self):
        self.engine = _make_engine()

    def test_missing_file_returns_path(self):
        """Если файл не существует — normalize_audio возвращает путь (legacy contract)."""
        result = self.engine.normalize_audio("/nonexistent/path/audio.wav")
        self.assertEqual(result, "/nonexistent/path/audio.wav")

    def test_silence_returns_true(self):
        """Тихий файл (RMS < 1e-6) → возвращает True (без записи)."""
        import tempfile
        import soundfile as sf

        silence = np.zeros(16000, dtype=np.float32)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name
        try:
            sf.write(tmp_path, silence, 16000)
            result = self.engine.normalize_audio(tmp_path)
            self.assertTrue(result)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_normal_audio_returns_true(self):
        """Нормальный звуковой сигнал → нормализуется и возвращает True."""
        import tempfile
        import soundfile as sf

        # Синусоида 440 Hz, 1 секунда
        t = np.linspace(0, 1, 16000, dtype=np.float32)
        audio = 0.5 * np.sin(2 * np.pi * 440 * t)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name
        try:
            sf.write(tmp_path, audio, 16000)
            result = self.engine.normalize_audio(tmp_path)
            self.assertTrue(result)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_stereo_audio_converted_to_mono_for_normalization(self):
        """Стерео файл → конвертируется в моно перед нормализацией (no error)."""
        import tempfile
        import soundfile as sf

        stereo = np.random.randn(16000, 2).astype(np.float32) * 0.3
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name
        try:
            sf.write(tmp_path, stereo, 16000)
            result = self.engine.normalize_audio(tmp_path)
            self.assertTrue(result)
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 4. mlx_lock wrapping in _transcribe_model
# ---------------------------------------------------------------------------

class MlxLockWrappingTests(unittest.TestCase):
    """Проверяет, что mlx_lock захватывается при каждом вызове _transcribe_model."""

    def setUp(self):
        self.engine = _make_engine()

    def test_mlx_lock_acquired_during_transcribe_model(self):
        """mlx_lock() должен быть захвачен внутри _transcribe_model."""
        from core import mlx_lock as mlx_lock_module

        lock_entered = threading.Event()
        original_lock = mlx_lock_module._mlx_lock

        class SpyRLock:
            """Прокси-RLock, фиксирующий вход в context manager."""
            def __enter__(self):
                lock_entered.set()
                return original_lock.__enter__()

            def __exit__(self, *args):
                return original_lock.__exit__(*args)

        spy = SpyRLock()

        with patch("core.engine.mlx_lock", return_value=spy):
            with patch("core.engine.mlx_whisper") as mock_mlx:
                mock_mlx.transcribe.return_value = _whisper_ok()
                self.engine._transcribe_model(
                    np.zeros(16000, dtype=np.float32),
                    "fake/balanced",
                    "test prompt",
                )

        self.assertTrue(lock_entered.is_set(), "mlx_lock не был захвачен в _transcribe_model")


# ---------------------------------------------------------------------------
# 5. Fallback chain: offline_strict does not use remote
# ---------------------------------------------------------------------------

class FallbackChainTests(unittest.TestCase):

    def setUp(self):
        self.engine = _make_engine()
        # Намерение этих тестов: облачный STT НАСТРОЕН. Гейт «нет ключа —
        # не ходить в облако» (инцидент 2026-08-26) иначе обрывает каскад
        # до remote, и проверялся бы не тот путь. Раньше проходило лишь
        # потому, что гейта в основном каскаде не было вовсе.
        self.engine._remote_stt_retry_configured = lambda: True

    def test_offline_strict_raises_when_all_models_unavailable(self):
        """При offline_strict и все локальные модели недоступны → RuntimeError (без remote)."""
        from core.config import settings

        # Mark balanced model as unavailable
        self.engine._unavailable_models[settings.MODEL_BALANCED] = __import__("time").monotonic()

        with patch("core.engine.settings") as mock_settings:
            mock_settings.MODEL_BALANCED = settings.MODEL_BALANCED
            mock_settings.model_max_list = [settings.MODEL_BALANCED]
            mock_settings.NETWORK_MODE = "offline_strict"
            mock_settings.PARAKEET_ENABLED = False
            mock_settings.SENSEVOICE_ENABLED = False
            mock_settings.WHISPERX_ENABLED = False
            mock_settings.VOXTRAL_ENABLED = False
            mock_settings.TRANSCRIBE_TIMEOUT_SEC = 30

            with self.assertRaises(RuntimeError) as ctx:
                self.engine._transcribe_with_fallback_impl(
                    np.zeros(8000, dtype=np.float32), "prompt"
                )
            self.assertIn("вышли из строя", str(ctx.exception))

    def test_online_mode_falls_back_to_remote_when_local_fails(self):
        """При online и все локальные модели недоступны → вызывает _transcribe_remote."""
        from core.config import settings

        self.engine._unavailable_models[settings.MODEL_BALANCED] = __import__("time").monotonic()

        with patch("core.engine.AudioEngine._transcribe_remote") as mock_remote:
            mock_remote.return_value = _whisper_ok("remote text")
            with patch("core.engine.settings") as mock_settings:
                mock_settings.MODEL_BALANCED = settings.MODEL_BALANCED
                mock_settings.model_max_list = [settings.MODEL_BALANCED]
                mock_settings.NETWORK_MODE = "online"
                mock_settings.PARAKEET_ENABLED = False
                mock_settings.SENSEVOICE_ENABLED = False
                mock_settings.WHISPERX_ENABLED = False
                mock_settings.VOXTRAL_ENABLED = False
                mock_settings.TRANSCRIBE_TIMEOUT_SEC = 30

                result = self.engine._transcribe_with_fallback_impl(
                    np.zeros(8000, dtype=np.float32), "prompt"
                )

            mock_remote.assert_called_once()
            self.assertEqual(result["text"], "remote text")


# ---------------------------------------------------------------------------
# 6. _resolve_diarization_device
# ---------------------------------------------------------------------------

class DiarizationDeviceTests(unittest.TestCase):

    def setUp(self):
        from core.engine import AudioEngine
        self.AudioEngine = AudioEngine

    def test_mps_selected_when_available(self):
        """Если mps доступен → выбирается torch.device('mps')."""
        import torch
        with patch.object(torch.backends.mps, "is_available", return_value=True):
            with patch.object(torch.cuda, "is_available", return_value=False):
                device = self.AudioEngine._resolve_diarization_device()
        self.assertEqual(str(device), "mps")

    def test_cuda_selected_when_mps_unavailable(self):
        """Если mps недоступен и cuda доступен → выбирается cuda."""
        import torch
        with patch.object(torch.backends.mps, "is_available", return_value=False):
            with patch.object(torch.cuda, "is_available", return_value=True):
                device = self.AudioEngine._resolve_diarization_device()
        self.assertEqual(str(device), "cuda")

    def test_cpu_fallback_when_no_gpu(self):
        """Без GPU → fallback на cpu."""
        import torch
        with patch.object(torch.backends.mps, "is_available", return_value=False):
            with patch.object(torch.cuda, "is_available", return_value=False):
                device = self.AudioEngine._resolve_diarization_device()
        self.assertEqual(str(device), "cpu")


# ---------------------------------------------------------------------------
# 7. speak() — TTS via macOS `say`
# ---------------------------------------------------------------------------

class SpeakTTSTests(unittest.TestCase):

    def setUp(self):
        self.engine = _make_engine()

    @patch("core.engine.subprocess.run")
    def test_speak_calls_say_with_text(self, mock_run):
        """speak() вызывает subprocess.run с командой 'say'."""
        with patch("core.engine.settings") as mock_settings:
            mock_settings.SAY_VOICE = ""
            self.engine.speak("Привет мир")
        mock_run.assert_called_once()
        cmd = mock_run.call_args.args[0]
        self.assertIn("say", cmd)
        self.assertIn("Привет мир", cmd)

    @patch("core.engine.subprocess.run")
    def test_speak_empty_text_is_noop(self, mock_run):
        """speak() с пустой строкой не вызывает subprocess.run."""
        self.engine.speak("   ")
        mock_run.assert_not_called()

    @patch("core.engine.subprocess.run")
    def test_speak_uses_voice_from_settings(self, mock_run):
        """Если SAY_VOICE настроен → передаётся флаг '-v <voice>'."""
        with patch("core.engine.settings") as mock_settings:
            mock_settings.SAY_VOICE = "Milena"
            self.engine.speak("тест")
        cmd = mock_run.call_args.args[0]
        self.assertIn("-v", cmd)
        self.assertIn("Milena", cmd)

    @patch("core.engine.subprocess.run")
    def test_speak_rate_passed_as_flag(self, mock_run):
        """Параметр rate передаётся как '-r <rate>'."""
        with patch("core.engine.settings") as mock_settings:
            mock_settings.SAY_VOICE = ""
            self.engine.speak("тест", rate=200)
        cmd = mock_run.call_args.args[0]
        self.assertIn("-r", cmd)
        self.assertIn("200", cmd)


# ---------------------------------------------------------------------------
# 8. _resolve_language
# ---------------------------------------------------------------------------

class ResolveLanguageTests(unittest.TestCase):

    def setUp(self):
        from core.engine import AudioEngine
        self.AudioEngine = AudioEngine

    def test_none_returns_none(self):
        self.assertIsNone(self.AudioEngine._resolve_language(None))

    def test_auto_returns_none(self):
        self.assertIsNone(self.AudioEngine._resolve_language("auto"))

    def test_known_lang_passed_through(self):
        self.assertEqual(self.AudioEngine._resolve_language("ru"), "ru")
        self.assertEqual(self.AudioEngine._resolve_language("en"), "en")
        self.assertEqual(self.AudioEngine._resolve_language("es"), "es")

    def test_unknown_lang_returns_none_with_warning(self):
        """Неизвестный lang_hint → None (со warning в лог, но не исключение)."""
        with self.assertLogs("KrabEar.Engine", level="WARNING"):
            result = self.AudioEngine._resolve_language("klingon")
        self.assertIsNone(result)

    def test_lang_stripped_and_lowercased(self):
        """Пробелы и регистр нормализуются."""
        self.assertEqual(self.AudioEngine._resolve_language("  RU  "), "ru")


if __name__ == "__main__":
    unittest.main()
