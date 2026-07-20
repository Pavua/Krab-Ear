"""Тесты STT startup warmup — AudioEngine.warmup() и BackendService интеграция.

Покрывает:
- warmup() загружает модель (mock mlx_whisper.transcribe, проверяем вызов)
- warmup() использует тихий (нулевой) аудио-буфер в 1 секунду при 16 кГц
- warmup() держит mlx_lock (проверяем вход/выход контекст-менеджера)
- warmup() отключён когда stt_warmup_on_startup=False (BackendService не запускает поток)
- warmup() возвращает latency_ms >= 0 при успехе
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Stubs used across tests
# ---------------------------------------------------------------------------

class FakeRecorder:
    is_recording = False
    sample_rate = 16000
    last_stop_trim_ms = 0
    last_stop_timeout_sec = 3.0

    def start(self) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        if not self.is_recording:
            return None
        self.is_recording = False
        import numpy as np
        audio = np.zeros(16000, dtype=np.float32)
        return audio, 1.0

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        import numpy as np
        return np.zeros(16000, dtype=np.float32), 1.0


class FakeTranscriber:
    counter = 0

    def transcribe(self, audio_data, **kwargs) -> str:
        self.counter += 1
        return f"тест#{self.counter}"

    def transcribe_preview(self, audio_data, quality_profile: str = "balanced") -> str:
        return "preview"


class FakeTranslator:
    def translate(self, text, mode, network_mode, translation_style="neutral", glossary=None):
        from backend.translator import TranslationResult
        return TranslationResult(
            text="",
            status="not_requested",
            source_lang="",
            target_lang="",
            mode="off",
            engine="fake",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_service(tmp_dir, transcriber=None):
    from backend.state_store import StateStore
    from backend.service import BackendService
    store = StateStore(Path(tmp_dir) / "data")
    return BackendService(
        store=store,
        recorder=FakeRecorder(),
        transcriber=transcriber or FakeTranscriber(),
        translator=FakeTranslator(),
    )


# ---------------------------------------------------------------------------
# Unit tests for AudioEngine.warmup()
# ---------------------------------------------------------------------------

class TestAudioEngineWarmup(unittest.TestCase):
    """Unit-тесты AudioEngine.warmup() — mlx_whisper максимально замокан."""

    def _make_engine(self):
        from core.engine import AudioEngine
        return AudioEngine()

    @patch("core.engine.mlx_whisper")
    def test_warmup_loads_model_in_thread(self, mock_mlx):
        """warmup() вызывает mlx_whisper.transcribe с текущей моделью."""
        mock_mlx.transcribe.return_value = {"text": ""}
        engine = self._make_engine()
        model_name = engine.current_model

        # Reset mock after engine construction to ignore any background init calls
        # that may fire before warmup() in CI (e.g. lang-id or other lazy init).
        mock_mlx.reset_mock()
        mock_mlx.transcribe.return_value = {"text": ""}

        result = engine.warmup()

        self.assertTrue(result["loaded"])
        self.assertEqual(result["model_name"], model_name)
        mock_mlx.transcribe.assert_called_once()
        call_kwargs = mock_mlx.transcribe.call_args
        # path_or_hf_repo должен совпасть с current_model
        self.assertEqual(call_kwargs.kwargs.get("path_or_hf_repo"), model_name)

    @patch("core.engine.mlx_whisper")
    def test_warmup_uses_silent_audio_buffer(self, mock_mlx):
        """warmup() передаёт в transcribe массив нулей длиной 16000 (1 сек @ 16 кГц)."""
        import numpy as np
        mock_mlx.transcribe.return_value = {"text": ""}

        engine = self._make_engine()
        engine.warmup()

        call_args = mock_mlx.transcribe.call_args
        audio_arg = call_args.args[0]
        self.assertEqual(len(audio_arg), 16000)
        self.assertTrue(np.all(audio_arg == 0.0), "ожидается буфер из нулей (тишина)")
        self.assertEqual(audio_arg.dtype, np.float32)

    @patch("core.engine.mlx_whisper")
    def test_warmup_holds_mlx_lock(self, mock_mlx):
        """warmup() входит и выходит из mlx_lock context manager."""
        mock_mlx.transcribe.return_value = {"text": ""}

        enter_called = []
        exit_called = []

        class FakeLock:
            def __enter__(self):
                enter_called.append(True)
                return self

            def __exit__(self, *args):
                exit_called.append(True)

        engine = self._make_engine()
        with patch("core.engine.mlx_lock", return_value=FakeLock()):
            engine.warmup()

        self.assertEqual(len(enter_called), 1, "mlx_lock.__enter__ должен быть вызван 1 раз")
        self.assertEqual(len(exit_called), 1, "mlx_lock.__exit__ должен быть вызван 1 раз")

    @patch("core.engine.mlx_whisper")
    def test_warmup_returns_latency(self, mock_mlx):
        """warmup() возвращает latency_ms >= 0 и корректную структуру."""
        mock_mlx.transcribe.return_value = {"text": ""}

        engine = self._make_engine()
        result = engine.warmup()

        self.assertIn("loaded", result)
        self.assertIn("latency_ms", result)
        self.assertIn("model_name", result)
        self.assertIn("error", result)
        self.assertTrue(result["loaded"])
        self.assertIsNone(result["error"])
        self.assertIsInstance(result["latency_ms"], int)
        self.assertGreaterEqual(result["latency_ms"], 0)

    @patch("core.engine.mlx_whisper")
    def test_warmup_returns_error_on_exception(self, mock_mlx):
        """warmup() возвращает loaded=False и заполняет error при исключении."""
        mock_mlx.transcribe.side_effect = RuntimeError("Metal OOM")

        engine = self._make_engine()
        result = engine.warmup()

        self.assertFalse(result["loaded"])
        self.assertIn("Metal OOM", result["error"])
        self.assertGreaterEqual(result["latency_ms"], 0)

    def test_warmup_returns_not_loaded_when_mlx_unavailable(self):
        """warmup() возвращает loaded=False когда mlx_whisper is None."""
        from core.engine import AudioEngine
        engine = AudioEngine()
        with patch("core.engine.mlx_whisper", None):
            result = engine.warmup()
        self.assertFalse(result["loaded"])
        self.assertIn("not available", result["error"])


# ---------------------------------------------------------------------------
# Integration tests for BackendService warmup startup behaviour
# ---------------------------------------------------------------------------

class TestBackendServiceWarmupStartup(unittest.TestCase):
    """Тесты BackendService: запуск STT warmup background thread при инициализации."""

    def test_warmup_thread_started_when_enabled(self):
        """BackendService запускает поток 'stt-warmup' если stt_warmup_on_startup=True."""
        warmup_called = threading.Event()

        class WarmupTrackingEngine:
            current_model = "mlx-community/whisper-large-v3-turbo"

            def warmup(self):
                warmup_called.set()
                return {"loaded": True, "latency_ms": 0, "model_name": self.current_model, "error": None}

        class TrackingTranscriber(FakeTranscriber):
            engine = WarmupTrackingEngine()

        with tempfile.TemporaryDirectory() as tmp:
            import core.config as _cfg
            orig = _cfg.DEFAULT_SETTINGS.get("stt_warmup_on_startup", True)
            _cfg.DEFAULT_SETTINGS["stt_warmup_on_startup"] = True
            try:
                with closing(make_service(tmp, transcriber=TrackingTranscriber())):
                    self.assertTrue(
                        warmup_called.wait(timeout=2.0),
                        "warmup() должен быть вызван в background thread",
                    )
            finally:
                _cfg.DEFAULT_SETTINGS["stt_warmup_on_startup"] = orig

    def test_warmup_not_started_when_disabled(self):
        """BackendService НЕ запускает STT warmup если stt_warmup_on_startup=False."""
        warmup_called = threading.Event()

        class WarmupTrackingEngine:
            current_model = "mlx-community/whisper-large-v3-turbo"

            def warmup(self):
                warmup_called.set()
                return {"loaded": True, "latency_ms": 0, "model_name": self.current_model, "error": None}

        class TrackingTranscriber(FakeTranscriber):
            engine = WarmupTrackingEngine()

        with tempfile.TemporaryDirectory() as tmp:
            import core.config as _cfg
            orig = _cfg.DEFAULT_SETTINGS.get("stt_warmup_on_startup", True)
            _cfg.DEFAULT_SETTINGS["stt_warmup_on_startup"] = False
            try:
                with closing(make_service(tmp, transcriber=TrackingTranscriber())):
                    self.assertFalse(
                        warmup_called.wait(timeout=0.5),
                        "warmup() не должен вызываться когда настройка False",
                    )
            finally:
                _cfg.DEFAULT_SETTINGS["stt_warmup_on_startup"] = orig


# ---------------------------------------------------------------------------
# IPC handler test
# ---------------------------------------------------------------------------

class TestHandleWarmupStt(unittest.TestCase):
    """Тест IPC-метода warmup_stt (live extracted handler: STTManagementService)."""

    @patch("core.engine.mlx_whisper")
    def test_handle_warmup_stt_returns_correct_shape(self, mock_mlx):
        """warmup_stt возвращает dict с ожидаемыми полями."""
        mock_mlx.transcribe.return_value = {"text": ""}

        with tempfile.TemporaryDirectory() as tmp:
            from backend.state_store import StateStore
            from backend.service import BackendService

            store = StateStore(Path(tmp) / "data")
            with closing(BackendService(
                store=store,
                recorder=FakeRecorder(),
                translator=FakeTranslator(),
            )) as svc:
                result = svc._stt_mgmt_svc.handle_warmup_stt({})

        self.assertIn("loaded", result)
        self.assertIn("latency_ms", result)
        self.assertIn("model_name", result)
        self.assertIn("error", result)

    def test_handle_warmup_stt_no_engine(self):
        """warmup_stt возвращает error=engine not available если нет engine."""
        with tempfile.TemporaryDirectory() as tmp:
            from backend.state_store import StateStore
            from backend.service import BackendService

            store = StateStore(Path(tmp) / "data")
            with closing(BackendService(
                store=store,
                recorder=FakeRecorder(),
                transcriber=FakeTranscriber(),  # no .engine attribute with warmup
                translator=FakeTranslator(),
            )) as svc:
                # FakeTranscriber has no .engine — engine check should fail gracefully
                result = svc._stt_mgmt_svc.handle_warmup_stt({})

        # Either success (if service fell back) or error with useful message
        self.assertIn("loaded", result)
        self.assertIn("error", result)
        # loaded=False because FakeTranscriber has no engine
        self.assertFalse(result["loaded"])


if __name__ == "__main__":
    unittest.main()
