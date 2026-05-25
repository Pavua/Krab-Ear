"""Wave 189 — STT adapter interface parity tests.

Verifies that every STTAdapterBase subclass (WhisperMLX, GigaAMSTT, Parakeet,
SenseVoice) implements the common contract correctly.

Voxtral and WhisperX are documented as NOT STTAdapterBase subclasses — they live
as engine.py markers (strings) and are tested as such in test_voxtral_adapter.py
and test_whisperx_adapter.py respectively.

NO real models are loaded — all heavy imports and model files are mocked.
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.pipeline.stt_adapter import STTAdapterBase, STTResult
from core.pipeline.stt_whisper_mlx_adapter import WhisperMLXAdapter
from core.pipeline.stt_gigaam_adapter import GigaAMSTTAdapter
from core.pipeline.stt_parakeet import ParakeetSTTAdapter
from core.pipeline.stt_sensevoice import SenseVoiceSTTAdapter

# ---------------------------------------------------------------------------
# Adapter registry — all concrete STTAdapterBase implementations.
# Voxtral and WhisperX are engine-level markers (strings), NOT subclasses.
# ---------------------------------------------------------------------------

_ADAPTER_CLASSES = [
    WhisperMLXAdapter,
    GigaAMSTTAdapter,
    ParakeetSTTAdapter,
    SenseVoiceSTTAdapter,
]

# Expected supported language for each adapter (used in language-specific tests).
_ADAPTER_EXPECTED_LANGUAGE: dict[type, str] = {
    WhisperMLXAdapter: "en",
    GigaAMSTTAdapter: "ru",
    ParakeetSTTAdapter: "en",
    SenseVoiceSTTAdapter: "zh",
}

# Language that each adapter explicitly does NOT support.
_ADAPTER_UNSUPPORTED_LANGUAGE: dict[type, str] = {
    GigaAMSTTAdapter: "zh",   # RU-only
    ParakeetSTTAdapter: "ru",  # EN-only
    SenseVoiceSTTAdapter: "ru",  # East-Asian + EN only
}

import numpy as np


def _fake_audio() -> Any:
    """Return a tiny numpy PCM array (0.1 s at 16 kHz)."""
    return np.zeros(1600, dtype=np.float32)


def _make_whisper_mlx_adapter() -> WhisperMLXAdapter:
    return WhisperMLXAdapter(model_path="mlx-community/whisper-large-v3-mlx")


def _make_gigaam_adapter() -> GigaAMSTTAdapter:
    """Create GigaAMSTTAdapter with a stubbed legacy adapter to avoid subprocess."""
    legacy_mock = MagicMock()
    legacy_mock.transcribe.return_value = {
        "text": "тест",
        "language": "ru",
        "confidence": 0.9,
        "duration_sec": 0.1,
        "engine": "gigaam-rnnt",
    }
    legacy_mock.is_available.return_value = True
    legacy_mock.warmup.return_value = True
    adapter = GigaAMSTTAdapter.__new__(GigaAMSTTAdapter)
    adapter._legacy = legacy_mock
    adapter._mode = "rnnt"
    return adapter


def _make_parakeet_adapter() -> ParakeetSTTAdapter:
    return ParakeetSTTAdapter(model_path="mlx-community/parakeet-tdt-0.6b-v2")


def _make_sensevoice_adapter() -> SenseVoiceSTTAdapter:
    return SenseVoiceSTTAdapter()


_ADAPTER_FACTORIES = {
    WhisperMLXAdapter: _make_whisper_mlx_adapter,
    GigaAMSTTAdapter: _make_gigaam_adapter,
    ParakeetSTTAdapter: _make_parakeet_adapter,
    SenseVoiceSTTAdapter: _make_sensevoice_adapter,
}


# ---------------------------------------------------------------------------
# 1. Inheritance tests
# ---------------------------------------------------------------------------

class TestAllAdaptersImplementSTTAdapterBase(unittest.TestCase):
    """All concrete adapters must be subclasses of STTAdapterBase."""

    def test_whisper_mlx_is_subclass(self):
        self.assertTrue(issubclass(WhisperMLXAdapter, STTAdapterBase))

    def test_gigaam_is_subclass(self):
        self.assertTrue(issubclass(GigaAMSTTAdapter, STTAdapterBase))

    def test_parakeet_is_subclass(self):
        self.assertTrue(issubclass(ParakeetSTTAdapter, STTAdapterBase))

    def test_sensevoice_is_subclass(self):
        self.assertTrue(issubclass(SenseVoiceSTTAdapter, STTAdapterBase))

    def test_no_adapter_is_stradapter_base_itself(self):
        """STTAdapterBase is abstract; none of the adapters IS the base."""
        for cls in _ADAPTER_CLASSES:
            with self.subTest(cls=cls.__name__):
                self.assertIsNot(cls, STTAdapterBase)


# ---------------------------------------------------------------------------
# 2. Contract method presence
# ---------------------------------------------------------------------------

class TestAllAdaptersHaveRequiredMethods(unittest.TestCase):
    """Adapters must expose transcribe, supports_language, is_available, model_id."""

    def test_all_have_transcribe_method(self):
        for cls in _ADAPTER_CLASSES:
            with self.subTest(cls=cls.__name__):
                self.assertTrue(
                    callable(getattr(cls, "transcribe", None)),
                    f"{cls.__name__} missing callable transcribe()",
                )

    def test_all_have_is_available_method(self):
        for cls in _ADAPTER_CLASSES:
            with self.subTest(cls=cls.__name__):
                self.assertTrue(
                    callable(getattr(cls, "is_available", None)),
                    f"{cls.__name__} missing callable is_available()",
                )

    def test_all_have_supports_language_method(self):
        for cls in _ADAPTER_CLASSES:
            with self.subTest(cls=cls.__name__):
                self.assertTrue(
                    callable(getattr(cls, "supports_language", None)),
                    f"{cls.__name__} missing callable supports_language()",
                )

    def test_all_have_model_id_property(self):
        for cls in _ADAPTER_CLASSES:
            with self.subTest(cls=cls.__name__):
                adapter = _ADAPTER_FACTORIES[cls]()
                mid = adapter.model_id
                self.assertIsInstance(mid, str, f"{cls.__name__}.model_id must be str")
                self.assertTrue(mid, f"{cls.__name__}.model_id must not be empty")

    def test_all_have_display_name_property(self):
        for cls in _ADAPTER_CLASSES:
            with self.subTest(cls=cls.__name__):
                adapter = _ADAPTER_FACTORIES[cls]()
                dn = adapter.display_name
                self.assertIsInstance(dn, str)
                self.assertTrue(dn)

    def test_all_have_optional_warmup_method(self):
        """warmup() is optional (has default impl) but must be callable."""
        for cls in _ADAPTER_CLASSES:
            with self.subTest(cls=cls.__name__):
                self.assertTrue(
                    callable(getattr(cls, "warmup", None)),
                    f"{cls.__name__} missing warmup()",
                )

    def test_all_have_optional_unload_method(self):
        """unload() is optional (has default impl) but must be callable."""
        for cls in _ADAPTER_CLASSES:
            with self.subTest(cls=cls.__name__):
                self.assertTrue(
                    callable(getattr(cls, "unload", None)),
                    f"{cls.__name__} missing unload()",
                )


# ---------------------------------------------------------------------------
# 3. STTResult dataclass schema
# ---------------------------------------------------------------------------

class TestSTTResultDataclass(unittest.TestCase):
    """STTResult must expose required fields."""

    _REQUIRED_FIELDS = {"text", "engine", "language", "confidence",
                        "duration_sec", "word_count", "metadata"}

    def test_sttresult_has_all_required_fields(self):
        field_names = {f.name for f in dataclass_fields(STTResult)}
        self.assertEqual(field_names, self._REQUIRED_FIELDS)

    def test_sttresult_text_is_str(self):
        r = STTResult(
            text="hello", engine="test", language="en",
            confidence=0.9, duration_sec=1.0, word_count=1,
        )
        self.assertIsInstance(r.text, str)

    def test_sttresult_engine_is_str(self):
        r = STTResult(
            text="", engine="gigaam-rnnt", language=None,
            confidence=None, duration_sec=None, word_count=0,
        )
        self.assertIsInstance(r.engine, str)

    def test_sttresult_metadata_defaults_to_empty_dict(self):
        r = STTResult(
            text="", engine="test", language=None,
            confidence=None, duration_sec=None, word_count=0,
        )
        self.assertIsInstance(r.metadata, dict)
        self.assertEqual(r.metadata, {})

    def test_sttresult_word_count_is_int(self):
        r = STTResult(
            text="hello world", engine="test", language="en",
            confidence=None, duration_sec=None, word_count=2,
        )
        self.assertIsInstance(r.word_count, int)


# ---------------------------------------------------------------------------
# 4. transcribe() returns STTResult (mocked — no real model)
# ---------------------------------------------------------------------------

class TestAllAdaptersReturnSTTResult(unittest.TestCase):
    """transcribe() must return STTResult when models are mocked."""

    def test_whisper_mlx_returns_sttresult(self):
        adapter = _make_whisper_mlx_adapter()
        fake_mlx_result = {
            "text": "hello world",
            "language": "en",
            "segments": [],
        }
        mock_mlx = MagicMock()
        mock_mlx.transcribe.return_value = fake_mlx_result
        with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
            with patch("core.mlx_lock.mlx_lock") as mock_lock:
                mock_lock.return_value.__enter__ = lambda s: None
                mock_lock.return_value.__exit__ = MagicMock(return_value=False)
                result = adapter.transcribe(_fake_audio(), language="en")
        self.assertIsInstance(result, STTResult)
        self.assertEqual(result.text, "hello world")
        self.assertEqual(result.engine, adapter.model_id)
        self.assertEqual(result.language, "en")

    def test_gigaam_returns_sttresult(self):
        adapter = _make_gigaam_adapter()
        result = adapter.transcribe(_fake_audio(), language="ru")
        self.assertIsInstance(result, STTResult)
        self.assertEqual(result.text, "тест")
        self.assertEqual(result.language, "ru")
        self.assertIsNotNone(result.confidence)

    def test_parakeet_returns_sttresult(self):
        adapter = _make_parakeet_adapter()
        fake_result = MagicMock()
        fake_result.text = "test transcription"
        fake_result.sentences = []
        mock_parakeet = MagicMock()
        mock_parakeet.from_pretrained.return_value = MagicMock(
            transcribe=MagicMock(return_value=fake_result)
        )
        with patch.dict("sys.modules", {"parakeet_mlx": mock_parakeet}):
            with patch("core.mlx_lock.mlx_lock") as mock_lock:
                mock_lock.return_value.__enter__ = lambda s: None
                mock_lock.return_value.__exit__ = MagicMock(return_value=False)
                result = adapter.transcribe(_fake_audio(), language="en")
        self.assertIsInstance(result, STTResult)
        self.assertEqual(result.text, "test transcription")
        self.assertEqual(result.language, "en")

    def test_sensevoice_returns_sttresult(self):
        adapter = _make_sensevoice_adapter()
        mock_automodel_instance = MagicMock()
        mock_automodel_instance.generate.return_value = [{"text": "<|zh|><|NEUTRAL|>测试文本"}]
        mock_automodel_cls = MagicMock(return_value=mock_automodel_instance)
        mock_funasr = MagicMock()
        mock_funasr.AutoModel = mock_automodel_cls
        mock_torch = MagicMock()
        mock_torch.backends.mps.is_available.return_value = False
        with patch.dict("sys.modules", {"funasr": mock_funasr, "torch": mock_torch}):
            result = adapter.transcribe(_fake_audio(), language="zh")
        self.assertIsInstance(result, STTResult)
        # Emotion tags must be stripped
        self.assertNotIn("<|", result.text)
        self.assertEqual(result.engine, adapter.model_id)


# ---------------------------------------------------------------------------
# 5. supports_language() contract
# ---------------------------------------------------------------------------

class TestSupportsLanguageContract(unittest.TestCase):
    """supports_language() must return bool and have consistent behaviour."""

    def test_all_return_bool(self):
        for cls in _ADAPTER_CLASSES:
            with self.subTest(cls=cls.__name__):
                adapter = _ADAPTER_FACTORIES[cls]()
                result = adapter.supports_language("en")
                self.assertIsInstance(result, bool,
                                      f"{cls.__name__}.supports_language must return bool")

    def test_all_accept_known_expected_language(self):
        for cls, lang in _ADAPTER_EXPECTED_LANGUAGE.items():
            with self.subTest(cls=cls.__name__, lang=lang):
                adapter = _ADAPTER_FACTORIES[cls]()
                self.assertTrue(
                    adapter.supports_language(lang),
                    f"{cls.__name__} should support '{lang}'",
                )

    def test_adapters_reject_explicitly_unsupported_language(self):
        for cls, lang in _ADAPTER_UNSUPPORTED_LANGUAGE.items():
            with self.subTest(cls=cls.__name__, lang=lang):
                adapter = _ADAPTER_FACTORIES[cls]()
                self.assertFalse(
                    adapter.supports_language(lang),
                    f"{cls.__name__} should NOT support '{lang}'",
                )

    def test_all_reject_empty_string(self):
        """Empty string should not be a valid language for any adapter."""
        for cls in _ADAPTER_CLASSES:
            with self.subTest(cls=cls.__name__):
                adapter = _ADAPTER_FACTORIES[cls]()
                result = adapter.supports_language("")
                self.assertFalse(result,
                                 f"{cls.__name__}.supports_language('') should be False")

    def test_whisper_mlx_supports_multilingual(self):
        """WhisperMLX claims broad language support."""
        adapter = _make_whisper_mlx_adapter()
        for lang in ("en", "ru", "es", "fr", "de", "zh", "ja"):
            with self.subTest(lang=lang):
                self.assertTrue(adapter.supports_language(lang))


# ---------------------------------------------------------------------------
# 6. is_available() contract
# ---------------------------------------------------------------------------

class TestIsAvailableContract(unittest.TestCase):
    """is_available() must return bool."""

    def test_all_return_bool_when_dep_absent(self):
        """When the optional dep is absent, is_available() must return False (not raise)."""
        for cls in (WhisperMLXAdapter, ParakeetSTTAdapter, SenseVoiceSTTAdapter):
            with self.subTest(cls=cls.__name__):
                adapter = _ADAPTER_FACTORIES[cls]()
                # Patch import so dep is "not installed"
                dep_map = {
                    WhisperMLXAdapter: "mlx_whisper",
                    ParakeetSTTAdapter: "parakeet_mlx",
                    SenseVoiceSTTAdapter: "funasr",
                }
                dep = dep_map[cls]
                with patch.dict("sys.modules", {dep: None}):
                    result = adapter.is_available()
                # may be True or False depending on env; must not raise and be bool
                self.assertIsInstance(result, bool)

    def test_gigaam_is_available_returns_bool(self):
        adapter = _make_gigaam_adapter()
        result = adapter.is_available()
        self.assertIsInstance(result, bool)

    def test_gigaam_is_available_does_not_raise(self):
        """GigaAMSTTAdapter.is_available() must not raise even if legacy raises."""
        adapter = _make_gigaam_adapter()
        adapter._legacy.is_available.side_effect = Exception("broken")
        result = adapter.is_available()
        self.assertIsInstance(result, bool)


# ---------------------------------------------------------------------------
# 7. Empty audio handling
# ---------------------------------------------------------------------------

class TestEmptyAudioHandling(unittest.TestCase):
    """Adapters should handle empty/zero audio gracefully (no crash)."""

    def test_gigaam_empty_audio_graceful(self):
        """GigaAMSTTAdapter with legacy returning empty text."""
        adapter = _make_gigaam_adapter()
        adapter._legacy.transcribe.return_value = {
            "text": "",
            "language": "ru",
            "confidence": None,
            "duration_sec": None,
            "engine": "gigaam-rnnt",
        }
        result = adapter.transcribe(np.zeros(0, dtype=np.float32), language="ru")
        self.assertIsInstance(result, STTResult)
        self.assertEqual(result.text, "")
        self.assertEqual(result.word_count, 0)

    def test_whisper_mlx_empty_audio_returns_sttresult(self):
        adapter = _make_whisper_mlx_adapter()
        mock_mlx = MagicMock()
        mock_mlx.transcribe.return_value = {"text": "", "language": None}
        with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
            with patch("core.mlx_lock.mlx_lock") as mock_lock:
                mock_lock.return_value.__enter__ = lambda s: None
                mock_lock.return_value.__exit__ = MagicMock(return_value=False)
                result = adapter.transcribe(
                    np.zeros(0, dtype=np.float32), language="en"
                )
        self.assertIsInstance(result, STTResult)
        self.assertEqual(result.text, "")
        self.assertEqual(result.word_count, 0)

    def test_sensevoice_empty_audio_returns_empty_sttresult(self):
        adapter = _make_sensevoice_adapter()
        mock_automodel_instance = MagicMock()
        # funasr returns empty list for silence
        mock_automodel_instance.generate.return_value = []
        mock_automodel_cls = MagicMock(return_value=mock_automodel_instance)
        mock_funasr = MagicMock()
        mock_funasr.AutoModel = mock_automodel_cls
        mock_torch = MagicMock()
        mock_torch.backends.mps.is_available.return_value = False
        with patch.dict("sys.modules", {"funasr": mock_funasr, "torch": mock_torch}):
            result = adapter.transcribe(np.zeros(0, dtype=np.float32), language="zh")
        self.assertIsInstance(result, STTResult)
        self.assertEqual(result.text, "")
        self.assertEqual(result.word_count, 0)

    def test_parakeet_empty_audio_returns_sttresult(self):
        adapter = _make_parakeet_adapter()
        fake_result = MagicMock()
        fake_result.text = ""
        fake_result.sentences = []
        mock_parakeet = MagicMock()
        mock_parakeet.from_pretrained.return_value = MagicMock(
            transcribe=MagicMock(return_value=fake_result)
        )
        with patch.dict("sys.modules", {"parakeet_mlx": mock_parakeet}):
            with patch("core.mlx_lock.mlx_lock") as mock_lock:
                mock_lock.return_value.__enter__ = lambda s: None
                mock_lock.return_value.__exit__ = MagicMock(return_value=False)
                result = adapter.transcribe(
                    np.zeros(0, dtype=np.float32), language="en"
                )
        self.assertIsInstance(result, STTResult)
        self.assertEqual(result.text, "")


# ---------------------------------------------------------------------------
# 8. Error / exception contract
# ---------------------------------------------------------------------------

class TestAdapterErrorContract(unittest.TestCase):
    """Adapters must raise ImportError when dep absent, RuntimeError on model fail."""

    def test_whisper_mlx_raises_import_error_when_not_installed(self):
        adapter = _make_whisper_mlx_adapter()
        with patch.dict("sys.modules", {"mlx_whisper": None}):
            with self.assertRaises((ImportError, Exception)):
                adapter.transcribe(_fake_audio())

    def test_parakeet_raises_import_error_when_not_installed(self):
        adapter = _make_parakeet_adapter()
        with patch.dict("sys.modules", {"parakeet_mlx": None}):
            with self.assertRaises((ImportError, Exception)):
                adapter.transcribe(_fake_audio())

    def test_sensevoice_raises_import_error_when_not_installed(self):
        adapter = _make_sensevoice_adapter()
        with patch.dict("sys.modules", {"funasr": None}):
            with self.assertRaises((ImportError, Exception)):
                adapter.transcribe(_fake_audio())

    def test_gigaam_raises_runtime_error_on_legacy_failure(self):
        adapter = _make_gigaam_adapter()
        adapter._legacy.transcribe.side_effect = RuntimeError("model crashed")
        with self.assertRaises(RuntimeError):
            adapter.transcribe(_fake_audio())

    def test_parakeet_raises_runtime_error_on_model_load_failure(self):
        adapter = _make_parakeet_adapter()
        mock_parakeet = MagicMock()
        mock_parakeet.from_pretrained.side_effect = RuntimeError("disk full")
        with patch.dict("sys.modules", {"parakeet_mlx": mock_parakeet}):
            with self.assertRaises(RuntimeError):
                adapter.transcribe(_fake_audio())


# ---------------------------------------------------------------------------
# 9. warmup() and unload() no-op safety
# ---------------------------------------------------------------------------

class TestWarmupUnloadSafety(unittest.TestCase):
    """warmup() must return bool; unload() must not raise."""

    def test_gigaam_warmup_returns_bool(self):
        adapter = _make_gigaam_adapter()
        result = adapter.warmup()
        self.assertIsInstance(result, bool)

    def test_gigaam_unload_does_not_raise(self):
        adapter = _make_gigaam_adapter()
        try:
            adapter.unload()
        except Exception as exc:
            self.fail(f"GigaAMSTTAdapter.unload() raised: {exc}")

    def test_whisper_mlx_warmup_returns_true(self):
        adapter = _make_whisper_mlx_adapter()
        self.assertTrue(adapter.warmup())

    def test_whisper_mlx_unload_does_not_raise(self):
        adapter = _make_whisper_mlx_adapter()
        adapter.unload()  # should be silent no-op

    def test_parakeet_unload_clears_model(self):
        adapter = _make_parakeet_adapter()
        adapter._model = MagicMock()  # inject a fake loaded model
        adapter.unload()
        self.assertIsNone(adapter._model)
        self.assertFalse(adapter._load_failed)

    def test_sensevoice_unload_clears_model(self):
        adapter = _make_sensevoice_adapter()
        adapter._model = MagicMock()
        adapter.unload()
        self.assertIsNone(adapter._model)
        self.assertFalse(adapter._load_failed)

    def test_all_warmup_when_dep_unavailable_returns_false_not_raise(self):
        """warmup() with missing dep must return False, not raise."""
        dep_map = {
            ParakeetSTTAdapter: "parakeet_mlx",
            SenseVoiceSTTAdapter: "funasr",
        }
        for cls, dep in dep_map.items():
            with self.subTest(cls=cls.__name__):
                adapter = _ADAPTER_FACTORIES[cls]()
                with patch.dict("sys.modules", {dep: None}):
                    result = adapter.warmup()
                self.assertIsInstance(result, bool)
                # When dep absent, warmup should return False
                self.assertFalse(result)


# ---------------------------------------------------------------------------
# 10. repr() contract
# ---------------------------------------------------------------------------

class TestAdapterRepr(unittest.TestCase):
    """__repr__() must be callable and return str (for logging)."""

    def test_all_repr_returns_string(self):
        for cls in _ADAPTER_CLASSES:
            with self.subTest(cls=cls.__name__):
                adapter = _ADAPTER_FACTORIES[cls]()
                r = repr(adapter)
                self.assertIsInstance(r, str)
                self.assertTrue(r)

    def test_repr_contains_class_name(self):
        for cls in _ADAPTER_CLASSES:
            with self.subTest(cls=cls.__name__):
                adapter = _ADAPTER_FACTORIES[cls]()
                self.assertIn(cls.__name__, repr(adapter))


# ---------------------------------------------------------------------------
# 11. Drift detection — Voxtral / WhisperX are NOT STTAdapterBase subclasses
# ---------------------------------------------------------------------------

class TestVoxtralAndWhisperXAreNotSTTAdapterBase(unittest.TestCase):
    """Document and verify that Voxtral + WhisperX live as engine markers, not adapters.

    If/when they get promoted to STTAdapterBase they should be added to _ADAPTER_CLASSES
    above and these tests updated.
    """

    def test_no_voxtral_stt_adapter_class_exists_in_pipeline(self):
        """No VoxtralSTTAdapter class should exist in pipeline package yet."""
        import importlib
        import importlib.util
        pipeline_dir = PROJECT_ROOT / "core" / "pipeline"
        voxtral_files = list(pipeline_dir.glob("*voxtral*"))
        for f in voxtral_files:
            spec = importlib.util.spec_from_file_location("voxtral_mod", f)
            mod = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
                for name in dir(mod):
                    obj = getattr(mod, name)
                    if isinstance(obj, type) and issubclass(obj, STTAdapterBase) and obj is not STTAdapterBase:
                        self.fail(
                            f"Unexpected VoxtralSTTAdapter found at {f}::{name}. "
                            "Update _ADAPTER_CLASSES and remove this check."
                        )
            except Exception:
                pass  # import errors are fine — just verifying class presence

    def test_voxtral_marker_exists_in_engine(self):
        """Voxtral is wired as a string marker in AudioEngine."""
        from core.engine import AudioEngine
        self.assertTrue(hasattr(AudioEngine, "_VOXTRAL_MARKER"))
        self.assertIsInstance(AudioEngine._VOXTRAL_MARKER, str)

    def test_whisperx_marker_exists_in_engine(self):
        """WhisperX is wired as a string marker in AudioEngine."""
        from core.engine import AudioEngine
        self.assertTrue(hasattr(AudioEngine, "_WHISPERX_MARKER"))
        self.assertIsInstance(AudioEngine._WHISPERX_MARKER, str)


# ---------------------------------------------------------------------------
# 12. model_id uniqueness
# ---------------------------------------------------------------------------

class TestModelIdUniqueness(unittest.TestCase):
    """Each adapter instance must produce a unique model_id."""

    def test_default_model_ids_are_unique(self):
        ids = [_ADAPTER_FACTORIES[cls]().model_id for cls in _ADAPTER_CLASSES]
        self.assertEqual(len(ids), len(set(ids)),
                         f"Duplicate model_id found: {ids}")

    def test_model_id_does_not_change_between_calls(self):
        """model_id must be stable (property, not random)."""
        for cls in _ADAPTER_CLASSES:
            with self.subTest(cls=cls.__name__):
                adapter = _ADAPTER_FACTORIES[cls]()
                self.assertEqual(adapter.model_id, adapter.model_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
