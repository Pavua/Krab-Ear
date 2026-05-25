"""Unit tests for core/pipeline/stt_router_factory.build_router.

Covers:
- build_router returns STTRouter instance
- Empty settings → only WhisperMLX attempted (may or may not succeed)
- stt_gigaam_enabled=True → GigaAM adapter attempted; ImportError logged gracefully
- stt_parakeet_enabled=True → Parakeet adapter attempted; ImportError logged gracefully
- stt_sensevoice_enabled=True → SenseVoice adapter attempted; ImportError logged gracefully
- Adapter ordering: GigaAM → Parakeet → SenseVoice → Whisper
- settings_provider binding: router reads settings dict at select_adapter time
- No adapters case: router has empty adapter list (graceful, no crash)
- stt_ru_primary_model: custom model path propagated to WhisperMLX adapter
- All optional adapters disabled → only Whisper in router
- build_router called with None settings (default empty dict)
"""
from __future__ import annotations

import sys
import os
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.pipeline.stt_router import STTRouter  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_whisper_adapter(model_path="mlx-community/whisper-large-v3-mlx"):
    """Create a mock WhisperMLXAdapter instance."""
    m = MagicMock()
    m.model_id = f"whisper-mlx/{model_path.split('/')[-1]}"
    m.is_available.return_value = True
    m.supports_language.return_value = True
    return m


def _mock_gigaam_adapter(mode="rnnt"):
    m = MagicMock()
    m.model_id = f"gigaam-{mode}"
    m._mode = mode
    m.is_available.return_value = True
    m.supports_language.return_value = True
    return m


def _mock_parakeet_adapter(available=True):
    m = MagicMock()
    m.model_id = "parakeet-tdt-1.1b"
    m._model_path = None
    m.is_available.return_value = available
    m.supports_language.return_value = True
    return m


def _mock_sensevoice_adapter(available=True):
    m = MagicMock()
    m.model_id = "sensevoice"
    m._model_id_or_path = None
    m._device_setting = "auto"
    m.is_available.return_value = available
    m.supports_language.return_value = True
    return m


# ---------------------------------------------------------------------------
# build_router tests — WhisperMLX always attempted
# ---------------------------------------------------------------------------

class TestBuildRouterWhisperDefault(unittest.TestCase):
    """WhisperMLXAdapter is always attempted by default."""

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    def test_returns_stt_router(self, MockWhisper):
        MockWhisper.return_value = _mock_whisper_adapter()
        from core.pipeline.stt_router_factory import build_router
        router = build_router()
        self.assertIsInstance(router, STTRouter)

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    def test_empty_settings_includes_whisper(self, MockWhisper):
        mock_w = _mock_whisper_adapter()
        MockWhisper.return_value = mock_w
        from core.pipeline.stt_router_factory import build_router
        router = build_router({})
        self.assertIn(mock_w, router._adapters)

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    def test_none_settings_treated_as_empty(self, MockWhisper):
        MockWhisper.return_value = _mock_whisper_adapter()
        from core.pipeline.stt_router_factory import build_router
        # Should not raise
        router = build_router(None)
        self.assertIsInstance(router, STTRouter)

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    def test_custom_model_path_passed_to_whisper(self, MockWhisper):
        custom_model = "my-org/whisper-small-ru"
        MockWhisper.return_value = _mock_whisper_adapter(custom_model)
        from core.pipeline.stt_router_factory import build_router
        build_router({"stt_ru_primary_model": custom_model})
        MockWhisper.assert_called_once_with(model_path=custom_model)

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    def test_whisper_import_error_logged_gracefully(self, MockWhisper):
        MockWhisper.side_effect = ImportError("mlx_whisper not installed")
        from core.pipeline.stt_router_factory import build_router
        # Should not raise — logs warning and continues
        router = build_router({})
        self.assertIsInstance(router, STTRouter)
        # No whisper adapter added
        self.assertEqual(router._adapters, [])

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    def test_settings_provider_bound_to_input_dict(self, MockWhisper):
        MockWhisper.return_value = _mock_whisper_adapter()
        from core.pipeline.stt_router_factory import build_router
        settings = {"stt_force_engine": "my-model"}
        router = build_router(settings)
        # settings_provider should return the same dict
        self.assertEqual(router._settings_provider(), settings)


# ---------------------------------------------------------------------------
# GigaAM adapter (optional — enabled by flag)
# ---------------------------------------------------------------------------

class TestBuildRouterGigaAM(unittest.TestCase):

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    @patch("core.pipeline.stt_router_factory.GigaAMSTTAdapter")
    def test_gigaam_added_when_enabled(self, MockGigaAM, MockWhisper):
        mock_g = _mock_gigaam_adapter()
        MockGigaAM.return_value = mock_g
        MockWhisper.return_value = _mock_whisper_adapter()
        from core.pipeline.stt_router_factory import build_router
        router = build_router({"stt_gigaam_enabled": True})
        self.assertIn(mock_g, router._adapters)

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    @patch("core.pipeline.stt_router_factory.GigaAMSTTAdapter")
    def test_gigaam_not_added_when_disabled(self, MockGigaAM, MockWhisper):
        mock_g = _mock_gigaam_adapter()
        MockGigaAM.return_value = mock_g
        MockWhisper.return_value = _mock_whisper_adapter()
        from core.pipeline.stt_router_factory import build_router
        router = build_router({"stt_gigaam_enabled": False})
        self.assertNotIn(mock_g, router._adapters)

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    @patch("core.pipeline.stt_router_factory.GigaAMSTTAdapter")
    def test_gigaam_import_error_logged_gracefully(self, MockGigaAM, MockWhisper):
        MockGigaAM.side_effect = ImportError("gigaam not installed")
        MockWhisper.return_value = _mock_whisper_adapter()
        from core.pipeline.stt_router_factory import build_router
        # Should not raise
        router = build_router({"stt_gigaam_enabled": True})
        self.assertIsInstance(router, STTRouter)

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    @patch("core.pipeline.stt_router_factory.GigaAMSTTAdapter")
    def test_gigaam_mode_passed_from_settings(self, MockGigaAM, MockWhisper):
        mock_g = _mock_gigaam_adapter(mode="ctc")
        MockGigaAM.return_value = mock_g
        MockWhisper.return_value = _mock_whisper_adapter()
        from core.pipeline.stt_router_factory import build_router
        build_router({"stt_gigaam_enabled": True, "stt_gigaam_mode": "ctc"})
        MockGigaAM.assert_called_once()
        kwargs = MockGigaAM.call_args
        self.assertEqual(kwargs.kwargs.get("mode") or kwargs.args[0], "ctc")


# ---------------------------------------------------------------------------
# Parakeet adapter (optional)
# ---------------------------------------------------------------------------

class TestBuildRouterParakeet(unittest.TestCase):

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    @patch("core.pipeline.stt_router_factory.ParakeetSTTAdapter")
    def test_parakeet_added_when_enabled_and_available(self, MockParakeet, MockWhisper):
        mock_p = _mock_parakeet_adapter(available=True)
        MockParakeet.return_value = mock_p
        MockWhisper.return_value = _mock_whisper_adapter()
        from core.pipeline.stt_router_factory import build_router
        router = build_router({"stt_parakeet_enabled": True})
        self.assertIn(mock_p, router._adapters)

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    @patch("core.pipeline.stt_router_factory.ParakeetSTTAdapter")
    def test_parakeet_not_added_when_unavailable(self, MockParakeet, MockWhisper):
        mock_p = _mock_parakeet_adapter(available=False)
        MockParakeet.return_value = mock_p
        MockWhisper.return_value = _mock_whisper_adapter()
        from core.pipeline.stt_router_factory import build_router
        router = build_router({"stt_parakeet_enabled": True})
        self.assertNotIn(mock_p, router._adapters)

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    @patch("core.pipeline.stt_router_factory.ParakeetSTTAdapter")
    def test_parakeet_not_added_when_disabled(self, MockParakeet, MockWhisper):
        mock_p = _mock_parakeet_adapter()
        MockParakeet.return_value = mock_p
        MockWhisper.return_value = _mock_whisper_adapter()
        from core.pipeline.stt_router_factory import build_router
        router = build_router({"stt_parakeet_enabled": False})
        self.assertNotIn(mock_p, router._adapters)

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    @patch("core.pipeline.stt_router_factory.ParakeetSTTAdapter")
    def test_parakeet_exception_logged_gracefully(self, MockParakeet, MockWhisper):
        MockParakeet.side_effect = RuntimeError("init failed")
        MockWhisper.return_value = _mock_whisper_adapter()
        from core.pipeline.stt_router_factory import build_router
        router = build_router({"stt_parakeet_enabled": True})
        self.assertIsInstance(router, STTRouter)


# ---------------------------------------------------------------------------
# SenseVoice adapter (optional)
# ---------------------------------------------------------------------------

class TestBuildRouterSenseVoice(unittest.TestCase):

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    @patch("core.pipeline.stt_router_factory.SenseVoiceSTTAdapter")
    def test_sensevoice_added_when_enabled_and_available(self, MockSV, MockWhisper):
        mock_sv = _mock_sensevoice_adapter(available=True)
        MockSV.return_value = mock_sv
        MockWhisper.return_value = _mock_whisper_adapter()
        from core.pipeline.stt_router_factory import build_router
        router = build_router({"stt_sensevoice_enabled": True})
        self.assertIn(mock_sv, router._adapters)

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    @patch("core.pipeline.stt_router_factory.SenseVoiceSTTAdapter")
    def test_sensevoice_not_added_when_unavailable(self, MockSV, MockWhisper):
        mock_sv = _mock_sensevoice_adapter(available=False)
        MockSV.return_value = mock_sv
        MockWhisper.return_value = _mock_whisper_adapter()
        from core.pipeline.stt_router_factory import build_router
        router = build_router({"stt_sensevoice_enabled": True})
        self.assertNotIn(mock_sv, router._adapters)

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    @patch("core.pipeline.stt_router_factory.SenseVoiceSTTAdapter")
    def test_sensevoice_not_added_when_disabled(self, MockSV, MockWhisper):
        mock_sv = _mock_sensevoice_adapter()
        MockSV.return_value = mock_sv
        MockWhisper.return_value = _mock_whisper_adapter()
        from core.pipeline.stt_router_factory import build_router
        router = build_router({})
        self.assertNotIn(mock_sv, router._adapters)

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    @patch("core.pipeline.stt_router_factory.SenseVoiceSTTAdapter")
    def test_sensevoice_exception_logged_gracefully(self, MockSV, MockWhisper):
        MockSV.side_effect = RuntimeError("funasr unavailable")
        MockWhisper.return_value = _mock_whisper_adapter()
        from core.pipeline.stt_router_factory import build_router
        router = build_router({"stt_sensevoice_enabled": True})
        self.assertIsInstance(router, STTRouter)


# ---------------------------------------------------------------------------
# Adapter ordering
# ---------------------------------------------------------------------------

class TestBuildRouterAdapterOrdering(unittest.TestCase):
    """GigaAM → Parakeet → SenseVoice → Whisper (specification order)."""

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    @patch("core.pipeline.stt_router_factory.SenseVoiceSTTAdapter")
    @patch("core.pipeline.stt_router_factory.ParakeetSTTAdapter")
    @patch("core.pipeline.stt_router_factory.GigaAMSTTAdapter")
    def test_gigaam_before_whisper(self, MockGigaAM, MockParakeet, MockSV, MockWhisper):
        mock_g = _mock_gigaam_adapter()
        mock_w = _mock_whisper_adapter()
        MockGigaAM.return_value = mock_g
        MockWhisper.return_value = mock_w
        MockParakeet.return_value = _mock_parakeet_adapter(available=False)
        MockSV.return_value = _mock_sensevoice_adapter(available=False)

        from core.pipeline.stt_router_factory import build_router
        router = build_router({"stt_gigaam_enabled": True})

        adapters = router._adapters
        g_idx = adapters.index(mock_g)
        w_idx = adapters.index(mock_w)
        self.assertLess(g_idx, w_idx, "GigaAM must appear before Whisper")

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    @patch("core.pipeline.stt_router_factory.SenseVoiceSTTAdapter")
    @patch("core.pipeline.stt_router_factory.ParakeetSTTAdapter")
    @patch("core.pipeline.stt_router_factory.GigaAMSTTAdapter")
    def test_sensevoice_before_whisper(self, MockGigaAM, MockParakeet, MockSV, MockWhisper):
        mock_sv = _mock_sensevoice_adapter(available=True)
        mock_w = _mock_whisper_adapter()
        MockGigaAM.side_effect = ImportError("no gigaam")
        MockParakeet.return_value = _mock_parakeet_adapter(available=False)
        MockSV.return_value = mock_sv
        MockWhisper.return_value = mock_w

        from core.pipeline.stt_router_factory import build_router
        router = build_router({
            "stt_gigaam_enabled": True,
            "stt_sensevoice_enabled": True,
        })
        adapters = router._adapters
        sv_idx = adapters.index(mock_sv)
        w_idx = adapters.index(mock_w)
        self.assertLess(sv_idx, w_idx, "SenseVoice must appear before Whisper")

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    @patch("core.pipeline.stt_router_factory.SenseVoiceSTTAdapter")
    @patch("core.pipeline.stt_router_factory.ParakeetSTTAdapter")
    @patch("core.pipeline.stt_router_factory.GigaAMSTTAdapter")
    def test_all_adapters_ordering(self, MockGigaAM, MockParakeet, MockSV, MockWhisper):
        mock_g = _mock_gigaam_adapter()
        mock_p = _mock_parakeet_adapter(available=True)
        mock_sv = _mock_sensevoice_adapter(available=True)
        mock_w = _mock_whisper_adapter()
        MockGigaAM.return_value = mock_g
        MockParakeet.return_value = mock_p
        MockSV.return_value = mock_sv
        MockWhisper.return_value = mock_w

        from core.pipeline.stt_router_factory import build_router
        router = build_router({
            "stt_gigaam_enabled": True,
            "stt_parakeet_enabled": True,
            "stt_sensevoice_enabled": True,
        })
        adapters = router._adapters
        g_idx = adapters.index(mock_g)
        p_idx = adapters.index(mock_p)
        sv_idx = adapters.index(mock_sv)
        w_idx = adapters.index(mock_w)
        # Expected order: GigaAM < Parakeet < SenseVoice < Whisper
        self.assertLess(g_idx, p_idx, "GigaAM before Parakeet")
        self.assertLess(p_idx, sv_idx, "Parakeet before SenseVoice")
        self.assertLess(sv_idx, w_idx, "SenseVoice before Whisper")


# ---------------------------------------------------------------------------
# No adapters edge case
# ---------------------------------------------------------------------------

class TestBuildRouterNoAdapters(unittest.TestCase):

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    def test_empty_router_does_not_crash(self, MockWhisper):
        MockWhisper.side_effect = ImportError("mlx_whisper not installed")
        from core.pipeline.stt_router_factory import build_router
        router = build_router({})
        self.assertIsInstance(router, STTRouter)
        # Router exists but has no adapters
        self.assertEqual(router._adapters, [])

    @patch("core.pipeline.stt_router_factory.WhisperMLXAdapter")
    def test_empty_router_select_adapter_returns_none(self, MockWhisper):
        MockWhisper.side_effect = ImportError("no mlx_whisper")
        from core.pipeline.stt_router_factory import build_router
        router = build_router({})
        self.assertIsNone(router.select_adapter(language="ru"))


if __name__ == "__main__":
    unittest.main()
