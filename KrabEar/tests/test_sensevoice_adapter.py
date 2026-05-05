"""Tests for SenseVoice STT adapter — Phase D.2.2.

All tests use mocks; real funasr/model is never loaded.
Follows the same mock pattern as test_parakeet_mlx_adapter.py.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.pipeline.stt_sensevoice import (
    SenseVoiceSTTAdapter,
    _SUPPORTED_LANGUAGES,
    _strip_emotion_tags,
)
from core.pipeline.stt_adapter import STTResult


# ---------------------------------------------------------------------------
# Availability tests
# ---------------------------------------------------------------------------

class TestSenseVoiceAvailability(unittest.TestCase):
    """is_available() reflects whether funasr is importable."""

    def test_is_available_when_funasr_installed(self) -> None:
        """is_available() → True when funasr can be imported."""
        mock_auto_model = MagicMock()
        with patch("core.pipeline.stt_sensevoice._try_import_funasr", return_value=mock_auto_model):
            adapter = SenseVoiceSTTAdapter()
            self.assertTrue(adapter.is_available())

    def test_is_available_when_funasr_missing(self) -> None:
        """is_available() → False when funasr import fails."""
        with patch("core.pipeline.stt_sensevoice._try_import_funasr", return_value=None):
            adapter = SenseVoiceSTTAdapter()
            self.assertFalse(adapter.is_available())


# ---------------------------------------------------------------------------
# Language support tests
# ---------------------------------------------------------------------------

class TestSenseVoiceLanguageSupport(unittest.TestCase):
    """supports_language() covers the expected East-Asian + EN set."""

    def setUp(self) -> None:
        self.adapter = SenseVoiceSTTAdapter()

    def test_supports_language_zh_yue_ja_ko_en(self) -> None:
        """Adapter supports zh, yue, ja, ko, en, and auto."""
        for lang in ("zh", "yue", "ja", "ko", "en", "auto"):
            with self.subTest(lang=lang):
                self.assertTrue(
                    self.adapter.supports_language(lang),
                    f"Expected supports_language({lang!r}) == True",
                )

    def test_does_not_support_language_ru_es(self) -> None:
        """Adapter does NOT support ru or es (Whisper handles those)."""
        for lang in ("ru", "es", "de", "fr", "pt"):
            with self.subTest(lang=lang):
                self.assertFalse(
                    self.adapter.supports_language(lang),
                    f"Expected supports_language({lang!r}) == False",
                )

    def test_supported_languages_set_is_correct(self) -> None:
        """The module-level _SUPPORTED_LANGUAGES constant has expected members."""
        self.assertIn("zh", _SUPPORTED_LANGUAGES)
        self.assertIn("yue", _SUPPORTED_LANGUAGES)
        self.assertIn("ja", _SUPPORTED_LANGUAGES)
        self.assertIn("ko", _SUPPORTED_LANGUAGES)
        self.assertIn("en", _SUPPORTED_LANGUAGES)
        self.assertNotIn("ru", _SUPPORTED_LANGUAGES)
        self.assertNotIn("es", _SUPPORTED_LANGUAGES)


# ---------------------------------------------------------------------------
# Transcription tests
# ---------------------------------------------------------------------------

class TestSenseVoiceTranscribe(unittest.TestCase):
    """transcribe() returns well-formed STTResult (no real model loaded)."""

    def _make_adapter_with_mock_model(self, raw_text: str = "你好世界") -> tuple:
        """Return adapter with a mock model pre-injected (bypasses lazy load)."""
        adapter = SenseVoiceSTTAdapter(model_id_or_path="FunAudioLLM/SenseVoiceSmall")
        mock_model = MagicMock()
        mock_model.generate.return_value = [{"text": raw_text, "key": "input_0"}]
        adapter._model = mock_model
        adapter._load_failed = False
        return adapter, mock_model

    def test_transcribe_returns_stt_result(self) -> None:
        """transcribe() returns STTResult instance with clean text."""
        import numpy as np
        adapter, _ = self._make_adapter_with_mock_model("<|zh|><|NEUTRAL|><|Speech|>你好世界")
        audio = np.zeros(16000, dtype="float32")

        with patch("core.pipeline.stt_sensevoice._try_import_funasr", return_value=MagicMock()):
            result = adapter.transcribe(audio, language="zh")

        self.assertIsInstance(result, STTResult)
        self.assertIsInstance(result.text, str)
        # Emotion tags must be stripped from text
        self.assertNotIn("<|NEUTRAL|>", result.text)
        self.assertNotIn("<|Speech|>", result.text)
        self.assertNotIn("<|zh|>", result.text)
        self.assertEqual(result.engine, "sensevoice/SenseVoiceSmall")

    def test_transcribe_strips_emotion_tags_keeps_in_metadata(self) -> None:
        """Emotion/event tags removed from text but preserved in metadata."""
        import numpy as np
        raw = "<|zh|><|HAPPY|><|Speech|>你好"
        adapter, _ = self._make_adapter_with_mock_model(raw)
        audio = np.zeros(8000, dtype="float32")

        with patch("core.pipeline.stt_sensevoice._try_import_funasr", return_value=MagicMock()):
            result = adapter.transcribe(audio, language="zh")

        self.assertNotIn("<|HAPPY|>", result.text)
        self.assertIn("<|HAPPY|>", result.metadata.get("emotion_tags", []))

    def test_transcribe_empty_result_returns_empty_stt_result(self) -> None:
        """Empty generate() output → STTResult with empty text, no exception."""
        import numpy as np
        adapter = SenseVoiceSTTAdapter()
        mock_model = MagicMock()
        mock_model.generate.return_value = []
        adapter._model = mock_model
        adapter._load_failed = False
        audio = np.zeros(16000, dtype="float32")

        with patch("core.pipeline.stt_sensevoice._try_import_funasr", return_value=MagicMock()):
            result = adapter.transcribe(audio)

        self.assertEqual(result.text, "")
        self.assertEqual(result.word_count, 0)

    def test_transcribe_raises_import_error_when_funasr_missing(self) -> None:
        """transcribe() raises ImportError when funasr is not installed."""
        import numpy as np
        with patch("core.pipeline.stt_sensevoice._try_import_funasr", return_value=None):
            adapter = SenseVoiceSTTAdapter()
            with self.assertRaises(ImportError):
                adapter.transcribe(np.zeros(16000, dtype="float32"))

    def test_transcribe_uses_mps_when_available(self) -> None:
        """When device='auto' and MPS available, model loaded with device='mps'."""
        mock_auto_model_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.generate.return_value = [{"text": "hello", "key": "0"}]
        mock_auto_model_cls.return_value = mock_instance

        with patch("core.pipeline.stt_sensevoice._try_import_funasr", return_value=mock_auto_model_cls):
            with patch("torch.backends.mps.is_available", return_value=True):
                adapter = SenseVoiceSTTAdapter(device="auto")
                adapter.warmup()
                call_kwargs = mock_auto_model_cls.call_args
                device_used = (
                    call_kwargs.kwargs.get("device")
                    or (call_kwargs[1].get("device") if call_kwargs[1] else None)
                )
                self.assertEqual(device_used, "mps")

    def test_transcribe_uses_cpu_when_mps_unavailable(self) -> None:
        """When device='auto' and MPS unavailable, model loaded with device='cpu'."""
        mock_auto_model_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.generate.return_value = [{"text": "hello", "key": "0"}]
        mock_auto_model_cls.return_value = mock_instance

        with patch("core.pipeline.stt_sensevoice._try_import_funasr", return_value=mock_auto_model_cls):
            with patch("torch.backends.mps.is_available", return_value=False):
                adapter = SenseVoiceSTTAdapter(device="auto")
                adapter.warmup()
                call_kwargs = mock_auto_model_cls.call_args
                device_used = (
                    call_kwargs.kwargs.get("device")
                    or (call_kwargs[1].get("device") if call_kwargs[1] else None)
                )
                self.assertEqual(device_used, "cpu")


# ---------------------------------------------------------------------------
# Router factory integration tests
# ---------------------------------------------------------------------------

class TestSenseVoiceRouterFactory(unittest.TestCase):
    """build_router() correctly includes/excludes SenseVoice based on settings."""

    def test_factory_excludes_when_disabled(self) -> None:
        """SenseVoice NOT included when stt_sensevoice_enabled=False."""
        from core.pipeline.stt_router_factory import build_router

        with patch("core.pipeline.stt_router_factory.WhisperMLXAdapter") as mock_whisper_cls:
            mock_whisper = MagicMock()
            mock_whisper.is_available.return_value = True
            mock_whisper_cls.return_value = mock_whisper

            router = build_router({"stt_sensevoice_enabled": False})

        adapter_types = [type(a).__name__ for a in router._adapters]
        self.assertNotIn("SenseVoiceSTTAdapter", adapter_types)

    def test_factory_includes_when_enabled_and_available(self) -> None:
        """SenseVoice IS included (before Whisper) when enabled and available."""
        from core.pipeline.stt_router_factory import build_router

        with patch("core.pipeline.stt_router_factory.SenseVoiceSTTAdapter") as mock_sv_cls:
            mock_sv = MagicMock()
            mock_sv.is_available.return_value = True
            mock_sv._model_id_or_path = "FunAudioLLM/SenseVoiceSmall"
            mock_sv._device_setting = "auto"
            mock_sv_cls.return_value = mock_sv

            with patch("core.pipeline.stt_router_factory.WhisperMLXAdapter") as mock_whisper_cls:
                mock_whisper = MagicMock()
                mock_whisper.is_available.return_value = True
                mock_whisper_cls.return_value = mock_whisper

                router = build_router({
                    "stt_sensevoice_enabled": True,
                    "stt_sensevoice_model": "FunAudioLLM/SenseVoiceSmall",
                    "stt_sensevoice_device": "auto",
                })

        adapters = router._adapters
        self.assertGreaterEqual(len(adapters), 2)
        # SenseVoice should appear BEFORE WhisperMLX
        self.assertIs(adapters[0], mock_sv)

    def test_factory_skips_sensevoice_when_not_installed(self) -> None:
        """SenseVoice omitted from router when enabled but funasr not installed."""
        from core.pipeline.stt_router_factory import build_router

        with patch("core.pipeline.stt_router_factory.SenseVoiceSTTAdapter") as mock_sv_cls:
            mock_sv = MagicMock()
            mock_sv.is_available.return_value = False
            mock_sv._model_id_or_path = "FunAudioLLM/SenseVoiceSmall"
            mock_sv._device_setting = "auto"
            mock_sv_cls.return_value = mock_sv

            with patch("core.pipeline.stt_router_factory.WhisperMLXAdapter") as mock_whisper_cls:
                mock_whisper = MagicMock()
                mock_whisper.is_available.return_value = True
                mock_whisper_cls.return_value = mock_whisper

                router = build_router({"stt_sensevoice_enabled": True})

        self.assertNotIn(mock_sv, router._adapters)


# ---------------------------------------------------------------------------
# Utility function tests
# ---------------------------------------------------------------------------

class TestStripEmotionTags(unittest.TestCase):
    """_strip_emotion_tags() correctly removes SenseVoice embedded tags."""

    def test_strips_language_and_emotion_tags(self) -> None:
        raw = "<|zh|><|HAPPY|><|Speech|>你好世界"
        self.assertEqual(_strip_emotion_tags(raw), "你好世界")

    def test_strips_multiple_tags(self) -> None:
        raw = "<|en|><|NEUTRAL|><|BGM|>hello world"
        self.assertEqual(_strip_emotion_tags(raw), "hello world")

    def test_no_tags_unchanged(self) -> None:
        plain = "hello world こんにちは"
        self.assertEqual(_strip_emotion_tags(plain), plain)

    def test_only_tags_returns_empty(self) -> None:
        self.assertEqual(_strip_emotion_tags("<|zh|><|HAPPY|>"), "")


if __name__ == "__main__":
    unittest.main()

