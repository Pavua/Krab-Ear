"""Tests for STT adapter base class, router, and Parakeet scaffold."""
import sys
import os

# Ensure project root is on path for direct invocation
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import unittest
from core.pipeline.stt_adapter import STTAdapterBase, STTResult
from core.pipeline.stt_router import STTRouter

try:
    from core.pipeline.stt_parakeet import ParakeetAdapter
    PARAKEET_AVAILABLE = True
except ImportError:
    PARAKEET_AVAILABLE = False


class _FakeAdapter(STTAdapterBase):
    def __init__(self, model_id, langs, available=True):
        self._mid = model_id
        self._langs = langs
        self._available = available

    @property
    def model_id(self): return self._mid

    def supports_language(self, lang): return lang in self._langs

    def is_available(self): return self._available

    def transcribe(self, audio, *, language=None, max_duration_sec=None):
        return STTResult(text=f"transcribed by {self._mid}", engine=self._mid,
                         language=language, confidence=0.9, duration_sec=5.0,
                         word_count=2, metadata={})


class STTRouterTests(unittest.TestCase):
    def test_select_adapter_no_adapters_returns_none(self):
        r = STTRouter([])
        self.assertIsNone(r.select_adapter("ru"))

    def test_select_adapter_filters_by_language(self):
        a_ru = _FakeAdapter("a-ru", ["ru"])
        a_en = _FakeAdapter("a-en", ["en"])
        r = STTRouter([a_ru, a_en])
        self.assertEqual(r.select_adapter("ru").model_id, "a-ru")
        self.assertEqual(r.select_adapter("en").model_id, "a-en")

    def test_select_adapter_filters_unavailable(self):
        a = _FakeAdapter("a", ["ru"], available=False)
        r = STTRouter([a])
        self.assertIsNone(r.select_adapter("ru"))

    def test_select_adapter_force_engine(self):
        a1 = _FakeAdapter("a1", ["ru"])
        a2 = _FakeAdapter("a2", ["ru"])
        r = STTRouter([a1, a2], settings_provider=lambda: {"stt_force_engine": "a2"})
        self.assertEqual(r.select_adapter("ru").model_id, "a2")

    def test_transcribe_routes_to_best_adapter(self):
        a = _FakeAdapter("test", ["ru"])
        r = STTRouter([a])
        result = r.transcribe(b"audio", language="ru")
        self.assertEqual(result.engine, "test")

    def test_select_adapter_no_language_returns_first_available(self):
        a1 = _FakeAdapter("a1", ["ru"])
        a2 = _FakeAdapter("a2", ["en"])
        r = STTRouter([a1, a2])
        # No language filter — first available wins
        self.assertEqual(r.select_adapter(None).model_id, "a1")

    def test_transcribe_raises_when_no_adapter_for_language(self):
        a = _FakeAdapter("a-ru", ["ru"])
        r = STTRouter([a])
        with self.assertRaises(RuntimeError):
            r.transcribe(b"audio", language="es")

    def test_force_engine_unavailable_falls_through(self):
        # Forced engine is unavailable → should fall through to candidates
        a1 = _FakeAdapter("a1", ["ru"], available=False)
        a2 = _FakeAdapter("a2", ["ru"])
        r = STTRouter([a1, a2], settings_provider=lambda: {"stt_force_engine": "a1"})
        # a1 is pinned but unavailable → fallback to a2
        selected = r.select_adapter("ru")
        self.assertEqual(selected.model_id, "a2")

    def test_warmup_default_returns_true(self):
        a = _FakeAdapter("a", ["ru"])
        self.assertTrue(a.warmup())

    def test_unload_default_no_exception(self):
        a = _FakeAdapter("a", ["ru"])
        a.unload()  # should not raise

    def test_repr_includes_model_id(self):
        a = _FakeAdapter("my-model", ["ru"])
        self.assertIn("my-model", repr(a))


@unittest.skipUnless(PARAKEET_AVAILABLE, "ParakeetAdapter not yet implemented")
class ParakeetScaffoldTests(unittest.TestCase):
    def test_parakeet_model_id(self):
        # W1707: model is parakeet-tdt-0.6b-v2 (not 1.1b — production uses 0.6b-v2)
        a = ParakeetAdapter()
        self.assertIn("parakeet", a.model_id.lower())

    def test_parakeet_display_name(self):
        # W1707: display_name reflects actual model path (0.6b-v2 not 1.1B)
        a = ParakeetAdapter()
        self.assertIn("Parakeet", a.display_name)

    def test_parakeet_supports_english_only(self):
        a = ParakeetAdapter()
        self.assertTrue(a.supports_language("en"))
        self.assertFalse(a.supports_language("ru"))
        self.assertFalse(a.supports_language("es"))

    def test_parakeet_not_available_yet(self):
        a = ParakeetAdapter()
        self.assertFalse(a.is_available())

    def test_parakeet_transcribe_raises_not_implemented(self):
        # W1707: parakeet-mlx not installed → ImportError (not NotImplementedError)
        a = ParakeetAdapter()
        with self.assertRaises((NotImplementedError, ImportError)):
            a.transcribe(b"audio")

    def test_parakeet_not_routed_when_unavailable(self):
        a = ParakeetAdapter()
        r = STTRouter([a])
        # Parakeet is not available → no adapter for 'en'
        self.assertIsNone(r.select_adapter("en"))

    def test_parakeet_repr(self):
        # W1707: repr contains the actual model name and avail=False
        a = ParakeetAdapter()
        r = repr(a)
        self.assertIn("parakeet", r.lower())
        self.assertIn("False", r)  # avail=False


class STTResultTests(unittest.TestCase):
    def test_stt_result_defaults(self):
        r = STTResult(
            text="hello",
            engine="test-engine",
            language="en",
            confidence=0.95,
            duration_sec=3.5,
            word_count=1,
        )
        self.assertEqual(r.text, "hello")
        self.assertEqual(r.metadata, {})

    def test_stt_result_metadata_independent(self):
        # Each instance should have its own metadata dict
        r1 = STTResult("a", "eng", "en", 0.9, 1.0, 1)
        r2 = STTResult("b", "eng", "en", 0.9, 1.0, 1)
        r1.metadata["key"] = "val"
        self.assertNotIn("key", r2.metadata)


if __name__ == "__main__":
    unittest.main()
