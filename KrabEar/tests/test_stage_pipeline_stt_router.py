"""Unit tests for core/pipeline/stt_router.STTRouter.

Covers:
- select_adapter: no adapters → None
- select_adapter: language filter (supports_language)
- select_adapter: user-pinned engine (stt_force_engine setting)
- select_adapter: forced engine unavailable → None
- select_adapter: forced engine not found → skip, use candidates
- transcribe: delegates to selected adapter
- transcribe: raises when no adapter available
- transcribe: passes language + kwargs to adapter
- Concurrent calls (thread-safety of settings_provider)
- STTAdapterBase: default warmup/unload/repr
- STTResult dataclass construction
"""
from __future__ import annotations

import sys
import os
import unittest
import threading

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.pipeline.stt_router import STTRouter  # noqa: E402
from core.pipeline.stt_adapter import STTAdapterBase, STTResult  # noqa: E402


# ---------------------------------------------------------------------------
# Stub adapter
# ---------------------------------------------------------------------------

class _StubAdapter(STTAdapterBase):
    """Minimal concrete STTAdapter for tests — avoids loading real models."""

    def __init__(
        self,
        model_id_: str = "stub-model",
        languages: frozenset | None = None,
        available: bool = True,
        result_text: str = "transcribed",
    ):
        self._model_id = model_id_
        self._languages = languages or frozenset({"ru", "en", "es"})
        self._available = available
        self._result_text = result_text
        self.transcribe_calls: list = []

    @property
    def model_id(self) -> str:
        return self._model_id

    def supports_language(self, language: str) -> bool:
        return language in self._languages

    def is_available(self) -> bool:
        return self._available

    def transcribe(self, audio, *, language=None, max_duration_sec=None) -> STTResult:
        self.transcribe_calls.append({"audio": audio, "language": language})
        return STTResult(
            text=self._result_text,
            engine=self._model_id,
            language=language,
            confidence=0.9,
            duration_sec=1.0,
            word_count=len(self._result_text.split()),
        )


# ---------------------------------------------------------------------------
# STTResult construction tests
# ---------------------------------------------------------------------------

class TestSTTResult(unittest.TestCase):
    def test_basic_construction(self):
        r = STTResult(
            text="Привет",
            engine="test-engine",
            language="ru",
            confidence=0.85,
            duration_sec=2.0,
            word_count=1,
        )
        self.assertEqual(r.text, "Привет")
        self.assertEqual(r.engine, "test-engine")
        self.assertEqual(r.language, "ru")
        self.assertAlmostEqual(r.confidence, 0.85)
        self.assertEqual(r.word_count, 1)

    def test_metadata_default_empty(self):
        r = STTResult(text="hi", engine="e", language="en",
                      confidence=None, duration_sec=None, word_count=1)
        self.assertEqual(r.metadata, {})

    def test_metadata_custom(self):
        r = STTResult(text="hi", engine="e", language="en",
                      confidence=None, duration_sec=None, word_count=1,
                      metadata={"segments": [1, 2, 3]})
        self.assertEqual(r.metadata["segments"], [1, 2, 3])

    def test_confidence_none_allowed(self):
        r = STTResult(text="x", engine="e", language=None,
                      confidence=None, duration_sec=None, word_count=0)
        self.assertIsNone(r.confidence)

    def test_language_none_allowed(self):
        r = STTResult(text="x", engine="e", language=None,
                      confidence=0.5, duration_sec=None, word_count=1)
        self.assertIsNone(r.language)


# ---------------------------------------------------------------------------
# STTAdapterBase default methods
# ---------------------------------------------------------------------------

class TestSTTAdapterBaseDefaults(unittest.TestCase):
    def _adapter(self) -> _StubAdapter:
        return _StubAdapter(model_id_="test-v1")

    def test_warmup_returns_true_by_default(self):
        adapter = self._adapter()
        self.assertTrue(adapter.warmup())

    def test_unload_is_noop(self):
        adapter = self._adapter()
        # Should not raise
        adapter.unload()

    def test_display_name_defaults_to_model_id(self):
        # Base class display_name = model_id (our stub inherits it)
        adapter = self._adapter()
        # The base STTAdapterBase.display_name returns self.model_id
        self.assertEqual(adapter.display_name, "test-v1")

    def test_repr_contains_class_model_available(self):
        adapter = self._adapter()
        r = repr(adapter)
        self.assertIn("_StubAdapter", r)
        self.assertIn("test-v1", r)
        self.assertIn("avail=True", r)

    def test_repr_unavailable_adapter(self):
        adapter = _StubAdapter(model_id_="ghost-model", available=False)
        r = repr(adapter)
        self.assertIn("avail=False", r)


# ---------------------------------------------------------------------------
# STTRouter.select_adapter
# ---------------------------------------------------------------------------

class TestSTTRouterSelectAdapter(unittest.TestCase):

    def test_no_adapters_returns_none(self):
        router = STTRouter(adapters=[], settings_provider=lambda: {})
        self.assertIsNone(router.select_adapter(language="ru"))

    def test_no_available_adapters_returns_none(self):
        a = _StubAdapter(available=False)
        router = STTRouter(adapters=[a], settings_provider=lambda: {})
        self.assertIsNone(router.select_adapter(language="ru"))

    def test_returns_first_available_adapter_when_no_language(self):
        a1 = _StubAdapter(model_id_="a1")
        a2 = _StubAdapter(model_id_="a2")
        router = STTRouter(adapters=[a1, a2], settings_provider=lambda: {})
        result = router.select_adapter(language=None)
        self.assertIs(result, a1)

    def test_filters_by_language(self):
        ru_only = _StubAdapter(model_id_="ru-only", languages=frozenset({"ru"}))
        en_only = _StubAdapter(model_id_="en-only", languages=frozenset({"en"}))
        router = STTRouter(adapters=[ru_only, en_only], settings_provider=lambda: {})
        result = router.select_adapter(language="en")
        self.assertIs(result, en_only)

    def test_no_matching_language_returns_none(self):
        ru_only = _StubAdapter(model_id_="ru-only", languages=frozenset({"ru"}))
        router = STTRouter(adapters=[ru_only], settings_provider=lambda: {})
        result = router.select_adapter(language="zh")
        self.assertIsNone(result)

    def test_forced_engine_selected_when_available(self):
        a1 = _StubAdapter(model_id_="generic")
        a2 = _StubAdapter(model_id_="specialist")
        router = STTRouter(
            adapters=[a1, a2],
            settings_provider=lambda: {"stt_force_engine": "specialist"},
        )
        result = router.select_adapter(language="ru")
        self.assertIs(result, a2)

    def test_forced_engine_unavailable_returns_none(self):
        a = _StubAdapter(model_id_="specialist", available=False)
        router = STTRouter(
            adapters=[a],
            settings_provider=lambda: {"stt_force_engine": "specialist"},
        )
        result = router.select_adapter(language="ru")
        # forced engine exists but is unavailable → pinned engine NOT found → None
        self.assertIsNone(result)

    def test_forced_engine_not_in_adapters_falls_through_to_candidates(self):
        a = _StubAdapter(model_id_="actual-model")
        router = STTRouter(
            adapters=[a],
            settings_provider=lambda: {"stt_force_engine": "nonexistent"},
        )
        # "nonexistent" not found → skip forced → return from candidates
        result = router.select_adapter(language="ru")
        self.assertIs(result, a)

    def test_settings_provider_called_each_call(self):
        call_count = [0]

        def provider():
            call_count[0] += 1
            return {}

        a = _StubAdapter()
        router = STTRouter(adapters=[a], settings_provider=provider)
        router.select_adapter(language="ru")
        router.select_adapter(language="en")
        self.assertGreaterEqual(call_count[0], 2)

    def test_prefer_speed_parameter_accepted(self):
        # prefer_speed is accepted but currently a no-op in routing logic
        a = _StubAdapter()
        router = STTRouter(adapters=[a], settings_provider=lambda: {})
        result = router.select_adapter(language="ru", prefer_speed=True)
        self.assertIs(result, a)

    def test_default_settings_provider_empty_dict(self):
        # When no settings_provider given, default is lambda: {}
        a = _StubAdapter()
        router = STTRouter(adapters=[a])
        result = router.select_adapter(language="ru")
        self.assertIs(result, a)


# ---------------------------------------------------------------------------
# STTRouter.transcribe
# ---------------------------------------------------------------------------

class TestSTTRouterTranscribe(unittest.TestCase):

    def test_transcribe_delegates_to_adapter(self):
        a = _StubAdapter(result_text="Привет мир")
        router = STTRouter(adapters=[a], settings_provider=lambda: {})
        result = router.transcribe(b"fake_audio", language="ru")
        self.assertEqual(result.text, "Привет мир")
        self.assertEqual(result.engine, "stub-model")

    def test_transcribe_passes_language_to_adapter(self):
        a = _StubAdapter(languages=frozenset({"ru"}))
        router = STTRouter(adapters=[a], settings_provider=lambda: {})
        router.transcribe(b"audio", language="ru")
        self.assertEqual(a.transcribe_calls[0]["language"], "ru")

    def test_transcribe_raises_when_no_adapter(self):
        router = STTRouter(adapters=[], settings_provider=lambda: {})
        with self.assertRaises(RuntimeError) as cm:
            router.transcribe(b"audio", language="ru")
        self.assertIn("No STT adapter", str(cm.exception))

    def test_transcribe_error_message_contains_language(self):
        router = STTRouter(adapters=[], settings_provider=lambda: {})
        with self.assertRaises(RuntimeError) as cm:
            router.transcribe(b"audio", language="zh")
        self.assertIn("zh", str(cm.exception))

    def test_transcribe_returns_stt_result(self):
        a = _StubAdapter()
        router = STTRouter(adapters=[a], settings_provider=lambda: {})
        result = router.transcribe(b"audio", language="en")
        self.assertIsInstance(result, STTResult)

    def test_transcribe_none_language_uses_first_available(self):
        a = _StubAdapter(model_id_="universal")
        router = STTRouter(adapters=[a], settings_provider=lambda: {})
        result = router.transcribe(b"audio", language=None)
        self.assertEqual(result.engine, "universal")

    def test_transcribe_kwargs_passed_through(self):
        """Extra kwargs must reach adapter.transcribe() without crashing."""
        # Our stub doesn't capture kwargs beyond language — just ensure no crash
        a = _StubAdapter()
        router = STTRouter(adapters=[a], settings_provider=lambda: {})
        result = router.transcribe(b"audio", language="en", prefer_speed=True)
        self.assertIsInstance(result, STTResult)


# ---------------------------------------------------------------------------
# Thread-safety: concurrent select_adapter calls
# ---------------------------------------------------------------------------

class TestSTTRouterConcurrency(unittest.TestCase):
    def test_concurrent_select_adapter_no_exception(self):
        adapters = [_StubAdapter(model_id_=f"m{i}") for i in range(5)]
        router = STTRouter(adapters=adapters, settings_provider=lambda: {})
        errors = []

        def worker():
            try:
                for _ in range(20):
                    router.select_adapter(language="ru")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(errors, [], f"Concurrent errors: {errors}")

    def test_concurrent_transcribe_no_exception(self):
        a = _StubAdapter()
        router = STTRouter(adapters=[a], settings_provider=lambda: {})
        errors = []

        def worker():
            try:
                for _ in range(10):
                    router.transcribe(b"x", language="ru")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(errors, [])


# ---------------------------------------------------------------------------
# Protocol compliance: STTAdapterBase is @runtime_checkable equivalent
# ---------------------------------------------------------------------------

class TestSTTAdapterProtocol(unittest.TestCase):
    def test_stub_is_subclass_of_base(self):
        self.assertTrue(issubclass(_StubAdapter, STTAdapterBase))

    def test_stub_instance_is_instance_of_base(self):
        self.assertIsInstance(_StubAdapter(), STTAdapterBase)

    def test_model_id_is_str(self):
        a = _StubAdapter(model_id_="my-model")
        self.assertIsInstance(a.model_id, str)

    def test_supports_language_returns_bool(self):
        a = _StubAdapter()
        self.assertIsInstance(a.supports_language("ru"), bool)
        self.assertIsInstance(a.supports_language("zz"), bool)

    def test_is_available_returns_bool(self):
        a = _StubAdapter()
        self.assertIsInstance(a.is_available(), bool)


if __name__ == "__main__":
    unittest.main()
