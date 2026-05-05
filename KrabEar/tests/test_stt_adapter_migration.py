"""Tests for Phase D.2 STTAdapterBase migration — GigaAM + Whisper MLX wrappers.

All tests use Mock/patch to avoid loading real models.
"""
import sys
import os
import contextlib
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.pipeline.stt_adapter import STTAdapterBase, STTResult  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_legacy_gigaam(text="привет мир", confidence=0.9):
    """Create a mock legacy GigaAMAdapter that returns a canned transcription dict."""
    mock = MagicMock()
    mock.transcribe.return_value = {
        "text": text,
        "language": "ru",
        "confidence": confidence,
        "engine": "gigaam-rnnt",
    }
    mock.close = MagicMock()
    return mock


def _null_context():
    """Return a no-op context manager (replaces mlx_lock in tests)."""
    return contextlib.nullcontext()


# ---------------------------------------------------------------------------
# GigaAMSTTAdapter tests
# ---------------------------------------------------------------------------

class GigaAMAdapterTests(unittest.TestCase):

    def _make_adapter(self, mode="rnnt", legacy_mock=None):
        """Return GigaAMSTTAdapter with a pre-set legacy mock (bypasses __init__)."""
        from core.pipeline.stt_gigaam_adapter import GigaAMSTTAdapter
        adapter = GigaAMSTTAdapter.__new__(GigaAMSTTAdapter)
        adapter._legacy = legacy_mock if legacy_mock is not None else _make_legacy_gigaam()
        adapter._mode = mode
        return adapter

    def test_gigaam_adapter_is_subclass_of_base(self):
        from core.pipeline.stt_gigaam_adapter import GigaAMSTTAdapter
        self.assertTrue(issubclass(GigaAMSTTAdapter, STTAdapterBase))

    def test_gigaam_adapter_wraps_legacy(self):
        legacy = _make_legacy_gigaam(text="тест")
        adapter = self._make_adapter(legacy_mock=legacy)

        import numpy as np
        audio = np.zeros(16000, dtype=np.float32)
        result = adapter.transcribe(audio)

        legacy.transcribe.assert_called_once_with(audio)
        self.assertIsInstance(result, STTResult)
        self.assertEqual(result.text, "тест")
        self.assertEqual(result.engine, "gigaam-rnnt")

    def test_gigaam_adapter_model_id(self):
        adapter = self._make_adapter(mode="rnnt")
        self.assertEqual(adapter.model_id, "gigaam-rnnt")

    def test_gigaam_adapter_model_id_ctc(self):
        adapter = self._make_adapter(mode="ctc")
        self.assertEqual(adapter.model_id, "gigaam-ctc")

    def test_gigaam_adapter_display_name(self):
        adapter = self._make_adapter(mode="rnnt")
        self.assertEqual(adapter.display_name, "GigaAM (RNNT)")

    def test_gigaam_adapter_supports_only_ru(self):
        adapter = self._make_adapter()
        self.assertTrue(adapter.supports_language("ru"))
        self.assertFalse(adapter.supports_language("en"))
        self.assertFalse(adapter.supports_language("es"))
        self.assertFalse(adapter.supports_language(""))

    def test_gigaam_adapter_is_available_no_method(self):
        """Legacy without is_available() → assume True."""
        # spec=[] means MagicMock has no attributes; accessing .is_available raises AttributeError
        legacy = MagicMock(spec=[])
        adapter = self._make_adapter(legacy_mock=legacy)
        self.assertTrue(adapter.is_available())

    def test_gigaam_adapter_is_available_delegates(self):
        legacy = _make_legacy_gigaam()
        legacy.is_available = MagicMock(return_value=True)
        adapter = self._make_adapter(legacy_mock=legacy)
        self.assertTrue(adapter.is_available())
        legacy.is_available.assert_called_once()

    def test_gigaam_adapter_result_word_count(self):
        legacy = _make_legacy_gigaam(text="один два три")
        adapter = self._make_adapter(legacy_mock=legacy)
        import numpy as np
        result = adapter.transcribe(np.zeros(16000, dtype=np.float32))
        self.assertEqual(result.word_count, 3)

    def test_gigaam_adapter_result_language_from_legacy(self):
        legacy = _make_legacy_gigaam()
        adapter = self._make_adapter(legacy_mock=legacy)
        import numpy as np
        result = adapter.transcribe(np.zeros(16000, dtype=np.float32))
        self.assertEqual(result.language, "ru")

    def test_gigaam_adapter_warmup_no_method(self):
        legacy = MagicMock(spec=[])  # empty spec → no attributes
        adapter = self._make_adapter(legacy_mock=legacy)
        self.assertTrue(adapter.warmup())

    def test_gigaam_adapter_unload_calls_close(self):
        legacy = _make_legacy_gigaam()
        adapter = self._make_adapter(legacy_mock=legacy)
        adapter.unload()
        legacy.close.assert_called_once()

    def test_gigaam_adapter_unload_tolerates_exception(self):
        legacy = _make_legacy_gigaam()
        legacy.close.side_effect = RuntimeError("already closed")
        adapter = self._make_adapter(legacy_mock=legacy)
        adapter.unload()  # should not raise

    def test_gigaam_adapter_repr(self):
        adapter = self._make_adapter()
        r = repr(adapter)
        self.assertIn("gigaam-rnnt", r)

    def test_gigaam_adapter_v2_mode_model_id(self):
        adapter = self._make_adapter(mode="v2_rnnt")
        self.assertEqual(adapter.model_id, "gigaam-rnnt")

    def test_gigaam_adapter_v1_mode_model_id(self):
        adapter = self._make_adapter(mode="v1_ctc")
        self.assertEqual(adapter.model_id, "gigaam-ctc")


# ---------------------------------------------------------------------------
# WhisperMLXAdapter tests
# ---------------------------------------------------------------------------

class WhisperMLXAdapterTests(unittest.TestCase):

    def _fake_mlx_module(self, text="hello world", detected_lang="en"):
        """Return a fake mlx_whisper module mock."""
        fake = MagicMock()
        fake.transcribe.return_value = {
            "text": text,
            "language": detected_lang,
            "segments": [],
        }
        return fake

    def test_whisper_mlx_adapter_is_subclass_of_base(self):
        from core.pipeline.stt_whisper_mlx_adapter import WhisperMLXAdapter
        self.assertTrue(issubclass(WhisperMLXAdapter, STTAdapterBase))

    def test_whisper_mlx_adapter_model_id_default(self):
        from core.pipeline.stt_whisper_mlx_adapter import WhisperMLXAdapter
        a = WhisperMLXAdapter()
        self.assertIn("whisper-mlx", a.model_id)
        self.assertIn("whisper-large-v3-mlx", a.model_id)

    def test_whisper_mlx_adapter_model_id_custom(self):
        from core.pipeline.stt_whisper_mlx_adapter import WhisperMLXAdapter
        a = WhisperMLXAdapter(model_path="mlx-community/whisper-small-mlx")
        self.assertIn("whisper-small-mlx", a.model_id)

    def test_whisper_mlx_adapter_display_name(self):
        from core.pipeline.stt_whisper_mlx_adapter import WhisperMLXAdapter
        a = WhisperMLXAdapter()
        self.assertIn("Whisper MLX", a.display_name)

    def test_whisper_mlx_supports_multilingual(self):
        from core.pipeline.stt_whisper_mlx_adapter import WhisperMLXAdapter
        a = WhisperMLXAdapter()
        self.assertTrue(a.supports_language("ru"))
        self.assertTrue(a.supports_language("en"))
        self.assertTrue(a.supports_language("es"))
        self.assertTrue(a.supports_language("zh"))
        self.assertTrue(a.supports_language("de"))

    def test_whisper_mlx_empty_language_not_supported(self):
        from core.pipeline.stt_whisper_mlx_adapter import WhisperMLXAdapter
        a = WhisperMLXAdapter()
        self.assertFalse(a.supports_language(""))

    def test_whisper_mlx_adapter_is_available_when_importable(self):
        from core.pipeline.stt_whisper_mlx_adapter import WhisperMLXAdapter
        a = WhisperMLXAdapter()
        with patch.dict("sys.modules", {"mlx_whisper": MagicMock()}):
            self.assertTrue(a.is_available())

    def test_whisper_mlx_adapter_not_available_when_import_fails(self):
        from core.pipeline.stt_whisper_mlx_adapter import WhisperMLXAdapter
        a = WhisperMLXAdapter()
        # Temporarily remove mlx_whisper from sys.modules so import raises ImportError
        with patch.dict("sys.modules", {"mlx_whisper": None}):
            self.assertFalse(a.is_available())

    def test_whisper_mlx_adapter_wraps_legacy(self):
        from core.pipeline.stt_whisper_mlx_adapter import WhisperMLXAdapter
        import numpy as np

        fake_mlx = self._fake_mlx_module(text=" transcribed text ", detected_lang="ru")
        a = WhisperMLXAdapter(model_path="mlx-community/whisper-large-v3-mlx")
        audio = np.zeros(16000, dtype=np.float32)

        # Patch mlx_whisper import and mlx_lock inside the transcribe method
        with patch.dict("sys.modules", {"mlx_whisper": fake_mlx}), \
             patch("core.mlx_lock.mlx_lock", return_value=contextlib.nullcontext()):
            result = a.transcribe(audio, language="ru")

        self.assertIsInstance(result, STTResult)
        self.assertEqual(result.text, "transcribed text")
        self.assertEqual(result.language, "ru")
        self.assertIn("whisper-mlx", result.engine)

    def test_whisper_mlx_adapter_word_count(self):
        from core.pipeline.stt_whisper_mlx_adapter import WhisperMLXAdapter
        import numpy as np

        fake_mlx = self._fake_mlx_module(text="один два три четыре", detected_lang="ru")
        a = WhisperMLXAdapter()
        audio = np.zeros(16000, dtype=np.float32)

        with patch.dict("sys.modules", {"mlx_whisper": fake_mlx}), \
             patch("core.mlx_lock.mlx_lock", return_value=contextlib.nullcontext()):
            result = a.transcribe(audio)

        self.assertEqual(result.word_count, 4)

    def test_whisper_mlx_adapter_warmup_returns_true(self):
        from core.pipeline.stt_whisper_mlx_adapter import WhisperMLXAdapter
        a = WhisperMLXAdapter()
        self.assertTrue(a.warmup())

    def test_whisper_mlx_adapter_unload_no_exception(self):
        from core.pipeline.stt_whisper_mlx_adapter import WhisperMLXAdapter
        a = WhisperMLXAdapter()
        a.unload()  # should not raise

    def test_whisper_mlx_raises_import_error_when_unavailable(self):
        from core.pipeline.stt_whisper_mlx_adapter import WhisperMLXAdapter
        import numpy as np
        a = WhisperMLXAdapter()
        audio = np.zeros(16000, dtype=np.float32)
        with patch.dict("sys.modules", {"mlx_whisper": None}):
            with self.assertRaises(ImportError):
                a.transcribe(audio)


# ---------------------------------------------------------------------------
# STTRouterFactory tests
# ---------------------------------------------------------------------------

class RouterFactoryTests(unittest.TestCase):
    """Tests for build_router() factory.

    Patches module-level names in stt_router_factory (GigaAMSTTAdapter,
    WhisperMLXAdapter) since the factory imports them at module level.
    """

    _FACTORY = "core.pipeline.stt_router_factory"

    def _fake_whisper(self, model_id="whisper-mlx/whisper-large-v3-mlx"):
        m = MagicMock()
        m.model_id = model_id
        m._mode = None
        m.is_available.return_value = True
        return m

    def _fake_gigaam(self, mode="rnnt"):
        m = MagicMock()
        m.model_id = f"gigaam-{mode}"
        m._mode = mode
        m.is_available.return_value = True
        return m

    def test_build_router_always_has_whisper_mlx(self):
        from core.pipeline.stt_router_factory import build_router
        fake_whisper = self._fake_whisper()
        WhisperCls = MagicMock(return_value=fake_whisper)
        with patch(f"{self._FACTORY}.WhisperMLXAdapter", WhisperCls):
            router = build_router(settings_dict={})
        adapter_ids = [a.model_id for a in router._adapters]
        self.assertIn("whisper-mlx/whisper-large-v3-mlx", adapter_ids)

    def test_build_router_includes_gigaam_when_enabled(self):
        from core.pipeline.stt_router_factory import build_router
        fake_gigaam = self._fake_gigaam()
        fake_whisper = self._fake_whisper()
        GigaAMCls = MagicMock(return_value=fake_gigaam)
        WhisperCls = MagicMock(return_value=fake_whisper)
        with patch(f"{self._FACTORY}.GigaAMSTTAdapter", GigaAMCls), \
             patch(f"{self._FACTORY}.WhisperMLXAdapter", WhisperCls):
            router = build_router(settings_dict={"stt_gigaam_enabled": True})
        adapter_ids = [a.model_id for a in router._adapters]
        self.assertIn("gigaam-rnnt", adapter_ids)
        self.assertIn("whisper-mlx/whisper-large-v3-mlx", adapter_ids)

    def test_build_router_skips_gigaam_when_disabled(self):
        from core.pipeline.stt_router_factory import build_router
        fake_whisper = self._fake_whisper()
        WhisperCls = MagicMock(return_value=fake_whisper)
        with patch(f"{self._FACTORY}.WhisperMLXAdapter", WhisperCls):
            router = build_router(settings_dict={"stt_gigaam_enabled": False})
        adapter_ids = [a.model_id for a in router._adapters]
        self.assertFalse(any("gigaam" in aid for aid in adapter_ids))

    def test_build_router_skips_gigaam_when_not_in_settings(self):
        from core.pipeline.stt_router_factory import build_router
        fake_whisper = self._fake_whisper()
        WhisperCls = MagicMock(return_value=fake_whisper)
        with patch(f"{self._FACTORY}.WhisperMLXAdapter", WhisperCls):
            router = build_router(settings_dict={})
        adapter_ids = [a.model_id for a in router._adapters]
        self.assertFalse(any("gigaam" in aid for aid in adapter_ids))

    def test_build_router_passes_gigaam_settings(self):
        from core.pipeline.stt_router_factory import build_router
        fake_gigaam = self._fake_gigaam(mode="ctc")
        fake_whisper = self._fake_whisper()
        GigaAMCls = MagicMock(return_value=fake_gigaam)
        WhisperCls = MagicMock(return_value=fake_whisper)
        with patch(f"{self._FACTORY}.GigaAMSTTAdapter", GigaAMCls), \
             patch(f"{self._FACTORY}.WhisperMLXAdapter", WhisperCls):
            build_router(settings_dict={
                "stt_gigaam_enabled": True,
                "stt_gigaam_mode": "ctc",
                "stt_gigaam_device": "cpu",
                "stt_gigaam_transport": "subprocess",
            })
        GigaAMCls.assert_called_once_with(mode="ctc", device="cpu", transport="subprocess")

    def test_build_router_returns_stt_router_instance(self):
        from core.pipeline.stt_router_factory import build_router
        from core.pipeline.stt_router import STTRouter
        fake_whisper = self._fake_whisper()
        WhisperCls = MagicMock(return_value=fake_whisper)
        with patch(f"{self._FACTORY}.WhisperMLXAdapter", WhisperCls):
            router = build_router()
        self.assertIsInstance(router, STTRouter)

    def test_build_router_empty_when_all_import_fail(self):
        from core.pipeline.stt_router_factory import build_router
        from core.pipeline.stt_router import STTRouter
        # WhisperMLXAdapter constructor raises ImportError
        WhisperCls = MagicMock(side_effect=ImportError("no mlx_whisper"))
        with patch(f"{self._FACTORY}.WhisperMLXAdapter", WhisperCls):
            router = build_router(settings_dict={"stt_gigaam_enabled": False})
        self.assertIsInstance(router, STTRouter)
        self.assertEqual(len(router._adapters), 0)

    def test_build_router_passes_whisper_model_path(self):
        from core.pipeline.stt_router_factory import build_router
        fake_whisper = self._fake_whisper("whisper-mlx/whisper-small-mlx")
        WhisperCls = MagicMock(return_value=fake_whisper)
        with patch(f"{self._FACTORY}.WhisperMLXAdapter", WhisperCls):
            build_router(settings_dict={
                "stt_ru_primary_model": "mlx-community/whisper-small-mlx",
            })
        WhisperCls.assert_called_once_with(model_path="mlx-community/whisper-small-mlx")


if __name__ == "__main__":
    unittest.main()
