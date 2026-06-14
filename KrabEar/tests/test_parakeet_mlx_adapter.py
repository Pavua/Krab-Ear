"""Tests for ParakeetSTTAdapter (parakeet-mlx) — Phase D.2.1.

All tests use mocks — parakeet-mlx is NOT required to be installed.
Covers:
  - availability detection (import success/failure)
  - language support (EN-only)
  - transcribe() result structure and mlx_lock wrapping
  - model caching across calls
  - graceful degradation when lib absent
  - router factory inclusion/exclusion logic
"""
import sys
import types
import unittest
import contextlib
from unittest.mock import MagicMock, patch
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_krabear = PROJECT_ROOT / "KrabEar"
if str(_krabear) not in sys.path:
    sys.path.insert(0, str(_krabear))

from core.pipeline.stt_adapter import STTResult
from core.pipeline.stt_parakeet import ParakeetSTTAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_sentence(text="Hello world", start=0.0, end=1.5):
    tok1 = MagicMock()
    tok1.text = "Hello"
    tok1.start = 0.0
    tok1.end = 0.7
    tok2 = MagicMock()
    tok2.text = "world"
    tok2.start = 0.8
    tok2.end = 1.5
    sent = MagicMock()
    sent.text = text
    sent.start = start
    sent.end = end
    sent.tokens = [tok1, tok2]
    return sent


def _make_fake_result(text="Hello world"):
    result = MagicMock()
    result.text = text
    result.sentences = [_make_fake_sentence(text)]
    return result


def _make_fake_parakeet_module(result_text="Hello world"):
    fake_module = types.ModuleType("parakeet_mlx")
    fake_model = MagicMock()
    fake_model.transcribe.return_value = _make_fake_result(result_text)
    fake_module.from_pretrained = MagicMock(return_value=fake_model)
    return fake_module, fake_model


# ---------------------------------------------------------------------------
# Availability tests
# ---------------------------------------------------------------------------

class TestParakeetMLXAvailability(unittest.TestCase):

    def test_is_available_when_parakeet_installed(self):
        fake_module, _ = _make_fake_parakeet_module()
        with patch("core.pipeline.stt_parakeet._try_import_parakeet", return_value=fake_module):
            adapter = ParakeetSTTAdapter()
            self.assertTrue(adapter.is_available())

    def test_is_available_when_parakeet_not_installed(self):
        with patch("core.pipeline.stt_parakeet._try_import_parakeet", return_value=None):
            adapter = ParakeetSTTAdapter()
            self.assertFalse(adapter.is_available())

    def test_init_does_not_raise_when_library_missing(self):
        with patch("core.pipeline.stt_parakeet._try_import_parakeet", return_value=None):
            try:
                ParakeetSTTAdapter()
            except Exception as exc:
                self.fail(f"__init__ raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# Language support
# ---------------------------------------------------------------------------

class TestParakeetMLXLanguage(unittest.TestCase):

    def setUp(self):
        self.adapter = ParakeetSTTAdapter()

    def test_supports_en(self):
        self.assertTrue(self.adapter.supports_language("en"))

    def test_does_not_support_ru(self):
        self.assertFalse(self.adapter.supports_language("ru"))

    def test_does_not_support_es(self):
        self.assertFalse(self.adapter.supports_language("es"))

    def test_does_not_support_empty(self):
        self.assertFalse(self.adapter.supports_language(""))

    def test_does_not_support_zh(self):
        self.assertFalse(self.adapter.supports_language("zh"))


# ---------------------------------------------------------------------------
# Model ID / display name
# ---------------------------------------------------------------------------

class TestParakeetMLXModelId(unittest.TestCase):

    def test_default_model_id_contains_parakeet(self):
        adapter = ParakeetSTTAdapter()
        self.assertIn("parakeet", adapter.model_id)

    def test_custom_model_id(self):
        adapter = ParakeetSTTAdapter(model_path="mlx-community/parakeet-tdt-0.6b-v3")
        self.assertEqual(adapter.model_id, "parakeet-mlx/parakeet-tdt-0.6b-v3")

    def test_display_name_contains_model_slug(self):
        adapter = ParakeetSTTAdapter(model_path="mlx-community/parakeet-tdt-0.6b-v2")
        self.assertIn("parakeet-tdt-0.6b-v2", adapter.display_name)


# ---------------------------------------------------------------------------
# Transcribe — result structure
# ---------------------------------------------------------------------------

class TestParakeetMLXTranscribe(unittest.TestCase):

    def _make_adapter(self, result_text="Hello world", model_path=None):
        fake_module, fake_model = _make_fake_parakeet_module(result_text)
        adapter = ParakeetSTTAdapter(
            model_path=model_path or "mlx-community/parakeet-tdt-0.6b-v2"
        )
        return adapter, fake_module, fake_model

    # ------------------------------------------------------------------

    def test_transcribe_raises_import_error_when_lib_missing(self):
        import numpy as np
        with patch("core.pipeline.stt_parakeet._try_import_parakeet", return_value=None):
            adapter = ParakeetSTTAdapter()
            with self.assertRaises(ImportError):
                adapter.transcribe(np.zeros(16000, dtype="float32"))

    def test_transcribe_returns_stt_result(self):
        import numpy as np
        adapter, fake_module, _ = self._make_adapter("Test output")
        audio = np.zeros(16000, dtype="float32")

        with patch("core.pipeline.stt_parakeet._try_import_parakeet", return_value=fake_module):
            with patch("core.mlx_lock.mlx_lock", contextlib.nullcontext, create=True):
                result = adapter.transcribe(audio)

        self.assertIsInstance(result, STTResult)
        self.assertEqual(result.text, "Test output")
        self.assertEqual(result.language, "en")
        self.assertEqual(result.engine, "parakeet-mlx/parakeet-tdt-0.6b-v2")
        self.assertGreater(result.word_count, 0)

    def test_transcribe_calls_model_transcribe_once(self):
        import numpy as np
        adapter, fake_module, fake_model = self._make_adapter()
        audio = np.zeros(16000, dtype="float32")

        with patch("core.pipeline.stt_parakeet._try_import_parakeet", return_value=fake_module):
            with patch("core.mlx_lock.mlx_lock", contextlib.nullcontext, create=True):
                adapter.transcribe(audio)

        fake_model.transcribe.assert_called_once_with(audio)

    def test_transcribe_result_has_segments_in_metadata(self):
        import numpy as np
        adapter, fake_module, _ = self._make_adapter("Hello world")
        audio = np.zeros(16000, dtype="float32")

        with patch("core.pipeline.stt_parakeet._try_import_parakeet", return_value=fake_module):
            with patch("core.mlx_lock.mlx_lock", contextlib.nullcontext, create=True):
                result = adapter.transcribe(audio)

        self.assertIn("segments", result.metadata)
        segments = result.metadata["segments"]
        self.assertIsInstance(segments, list)
        self.assertGreater(len(segments), 0)
        seg = segments[0]
        self.assertIn("text", seg)
        self.assertIn("start", seg)
        self.assertIn("end", seg)

    def test_transcribe_under_mlx_lock(self):
        """transcribe() wraps inference inside mlx_lock context manager."""
        import numpy as np
        adapter, fake_module, _ = self._make_adapter()
        audio = np.zeros(16000, dtype="float32")

        entered = []
        exited = []

        class TrackingLock:
            def __enter__(self):
                entered.append(True)
                return self

            def __exit__(self, *args):
                exited.append(True)

        with patch("core.pipeline.stt_parakeet._try_import_parakeet", return_value=fake_module):
            with patch("core.mlx_lock.mlx_lock", TrackingLock, create=True):
                adapter.transcribe(audio)

        self.assertGreater(len(entered), 0, "mlx_lock was never entered")
        self.assertEqual(len(entered), len(exited), "mlx_lock was entered but not exited")

    def test_parakeet_mlx_path_holds_intra_process_lock(self):
        """W1217 F2: mlx_lock (intra-process RLock) is acquired during transcribe()."""
        import numpy as np
        adapter, fake_module, _ = self._make_adapter()
        audio = np.zeros(16000, dtype="float32")

        lock_entries = []

        class TrackingIntraLock:
            def __enter__(self):
                lock_entries.append("intra")
                return self

            def __exit__(self, *args):
                return False

        with patch("core.pipeline.stt_parakeet._try_import_parakeet", return_value=fake_module):
            with patch("core.mlx_lock.mlx_lock", TrackingIntraLock, create=True):
                with patch(
                    "core.mlx_inter_lock.mlx_inter_process_lock",
                    contextlib.nullcontext,
                    create=True,
                ):
                    adapter.transcribe(audio)

        self.assertIn(
            "intra",
            lock_entries,
            "mlx_lock (intra-process) was not entered during transcribe()",
        )

    def test_parakeet_mlx_path_holds_inter_process_lock_when_enabled(self):
        """W1217 F2: mlx_inter_process_lock (cross-process flock) is acquired during
        transcribe() as the outer lock wrapping mlx_lock."""
        import numpy as np
        adapter, fake_module, _ = self._make_adapter()
        audio = np.zeros(16000, dtype="float32")

        call_order: list[str] = []

        class TrackingInterLock:
            def __enter__(self):
                call_order.append("inter_enter")
                return self

            def __exit__(self, *args):
                call_order.append("inter_exit")
                return False

        class TrackingIntraLock:
            def __enter__(self):
                call_order.append("intra_enter")
                return self

            def __exit__(self, *args):
                call_order.append("intra_exit")
                return False

        with patch("core.pipeline.stt_parakeet._try_import_parakeet", return_value=fake_module):
            with patch(
                "core.mlx_inter_lock.mlx_inter_process_lock",
                TrackingInterLock,
                create=True,
            ):
                with patch("core.mlx_lock.mlx_lock", TrackingIntraLock, create=True):
                    adapter.transcribe(audio)

        self.assertIn("inter_enter", call_order, "mlx_inter_process_lock was not entered")
        self.assertIn("intra_enter", call_order, "mlx_lock was not entered")
        # Outer (inter) must be entered before inner (intra)
        self.assertLess(
            call_order.index("inter_enter"),
            call_order.index("intra_enter"),
            "mlx_inter_process_lock must be the OUTER lock (entered before mlx_lock)",
        )
        # Both must be properly exited
        self.assertIn("inter_exit", call_order, "mlx_inter_process_lock was not exited")
        self.assertIn("intra_exit", call_order, "mlx_lock was not exited")

    def test_transcribe_raises_runtime_error_on_model_load_failure(self):
        import numpy as np
        fake_module = types.ModuleType("parakeet_mlx")
        fake_module.from_pretrained = MagicMock(side_effect=RuntimeError("disk full"))

        adapter = ParakeetSTTAdapter()
        audio = np.zeros(16000, dtype="float32")

        with patch("core.pipeline.stt_parakeet._try_import_parakeet", return_value=fake_module):
            with patch("core.mlx_lock.mlx_lock", contextlib.nullcontext, create=True):
                with self.assertRaises(RuntimeError):
                    adapter.transcribe(audio)

    def test_model_cached_across_calls(self):
        """from_pretrained called only once across multiple transcribe() calls."""
        import numpy as np
        adapter, fake_module, _ = self._make_adapter()
        audio = np.zeros(16000, dtype="float32")

        with patch("core.pipeline.stt_parakeet._try_import_parakeet", return_value=fake_module):
            with patch("core.mlx_lock.mlx_lock", contextlib.nullcontext, create=True):
                adapter.transcribe(audio)
                adapter.transcribe(audio)

        self.assertEqual(fake_module.from_pretrained.call_count, 1)

    def test_language_field_always_en(self):
        """STTResult.language is always 'en' regardless of kwargs."""
        import numpy as np
        adapter, fake_module, _ = self._make_adapter()
        audio = np.zeros(16000, dtype="float32")

        with patch("core.pipeline.stt_parakeet._try_import_parakeet", return_value=fake_module):
            with patch("core.mlx_lock.mlx_lock", contextlib.nullcontext, create=True):
                result = adapter.transcribe(audio, language="ru")  # ignored

        self.assertEqual(result.language, "en")


# ---------------------------------------------------------------------------
# Warmup / unload lifecycle
# ---------------------------------------------------------------------------

class TestParakeetMLXLifecycle(unittest.TestCase):

    def test_warmup_returns_false_when_lib_missing(self):
        with patch("core.pipeline.stt_parakeet._try_import_parakeet", return_value=None):
            adapter = ParakeetSTTAdapter()
            self.assertFalse(adapter.warmup())

    def test_warmup_returns_true_when_lib_present(self):
        fake_module, _ = _make_fake_parakeet_module()
        with patch("core.pipeline.stt_parakeet._try_import_parakeet", return_value=fake_module):
            adapter = ParakeetSTTAdapter()
            self.assertTrue(adapter.warmup())

    def test_unload_clears_model(self):
        fake_module, _ = _make_fake_parakeet_module()
        with patch("core.pipeline.stt_parakeet._try_import_parakeet", return_value=fake_module):
            adapter = ParakeetSTTAdapter()
            adapter.warmup()
            self.assertIsNotNone(adapter._model)
            adapter.unload()
            self.assertIsNone(adapter._model)

    def test_unload_resets_load_failed_flag(self):
        fake_module = types.ModuleType("parakeet_mlx")
        fake_module.from_pretrained = MagicMock(side_effect=RuntimeError("boom"))
        adapter = ParakeetSTTAdapter()
        with patch("core.pipeline.stt_parakeet._try_import_parakeet", return_value=fake_module):
            adapter.warmup()  # fails, sets _load_failed=True

        self.assertTrue(adapter._load_failed)
        adapter.unload()
        self.assertFalse(adapter._load_failed)


# ---------------------------------------------------------------------------
# Router factory tests
# ---------------------------------------------------------------------------

class TestRouterFactoryParakeet(unittest.TestCase):

    def _build_router(self, parakeet_enabled=True, parakeet_available=True):
        from core.pipeline.stt_router_factory import build_router

        fake_module = _make_fake_parakeet_module()[0] if parakeet_available else None

        with patch("core.pipeline.stt_parakeet._try_import_parakeet", return_value=fake_module):
            with patch(
                "core.pipeline.stt_router_factory.WhisperMLXAdapter.is_available",
                return_value=False,
            ):
                router = build_router(settings_dict={
                    "stt_parakeet_enabled": parakeet_enabled,
                    "stt_gigaam_enabled": False,
                })
        return router

    def test_factory_excludes_parakeet_when_disabled(self):
        router = self._build_router(parakeet_enabled=False, parakeet_available=True)
        names = [type(a).__name__ for a in router._adapters]
        self.assertNotIn("ParakeetSTTAdapter", names)

    def test_factory_includes_parakeet_when_enabled_and_available(self):
        router = self._build_router(parakeet_enabled=True, parakeet_available=True)
        names = [type(a).__name__ for a in router._adapters]
        self.assertIn("ParakeetSTTAdapter", names)

    def test_factory_excludes_parakeet_when_enabled_but_unavailable(self):
        router = self._build_router(parakeet_enabled=True, parakeet_available=False)
        names = [type(a).__name__ for a in router._adapters]
        self.assertNotIn("ParakeetSTTAdapter", names)


# ---------------------------------------------------------------------------
# Lazy-load thread-safety + MLX GPU serialization (W1218 parity for Parakeet)
# ---------------------------------------------------------------------------

class TestParakeetMLXLoadThreadSafe(unittest.TestCase):
    """Concurrent transcribe() must not double-load the MLX model, and the load
    must run under mlx_lock() (Metal GPU serialization).

    Before the W1218-parity fix ParakeetSTTAdapter had no _load_lock and loaded
    the model inline outside mlx_lock(): two threads could both pass the
    ``if self._model is None`` check and call from_pretrained twice, and the GPU
    load could race an in-flight inference on another thread → SIGSEGV (the race
    this module's header warns about; see PR #71).
    """

    def test_concurrent_transcribe_loads_model_once(self):
        import threading
        import time
        import numpy as np

        calls = []  # list.append is atomic under the GIL
        load_event = threading.Event()  # lets threads pile up before model appears

        fake_module = types.ModuleType("parakeet_mlx")
        fake_model = MagicMock()
        fake_model.transcribe.return_value = _make_fake_result("hi")

        def counting_from_pretrained(path):
            calls.append(path)
            load_event.wait(timeout=2.0)  # block so racing threads stack up
            return fake_model

        fake_module.from_pretrained = counting_from_pretrained

        adapter = ParakeetSTTAdapter()
        errors: list = []

        def worker():
            try:
                audio = np.zeros(1600, dtype=np.float32)
                with patch(
                    "core.pipeline.stt_parakeet._try_import_parakeet",
                    return_value=fake_module,
                ):
                    adapter.transcribe(audio)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.05)  # let all four enter transcribe() before the load finishes
        load_event.set()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(errors, [], f"Threads raised: {errors}")
        self.assertEqual(
            len(calls),
            1,
            f"model loaded {len(calls)}× — double-load race (missing _load_lock)",
        )

    def test_model_load_runs_inside_mlx_lock(self):
        import numpy as np

        depth = {"cur": 0, "at_load": None}

        @contextlib.contextmanager
        def recording_lock():
            depth["cur"] += 1
            try:
                yield
            finally:
                depth["cur"] -= 1

        fake_module = types.ModuleType("parakeet_mlx")
        fake_model = MagicMock()
        fake_model.transcribe.return_value = _make_fake_result("hi")

        def recording_from_pretrained(path):
            depth["at_load"] = depth["cur"]
            return fake_model

        fake_module.from_pretrained = recording_from_pretrained

        adapter = ParakeetSTTAdapter()
        audio = np.zeros(1600, dtype=np.float32)
        with patch(
            "core.pipeline.stt_parakeet._try_import_parakeet",
            return_value=fake_module,
        ), patch("core.mlx_lock.mlx_lock", recording_lock):
            adapter.transcribe(audio)

        self.assertIsNotNone(depth["at_load"], "from_pretrained was never called")
        self.assertGreaterEqual(
            depth["at_load"],
            1,
            "model load ran outside mlx_lock() — GPU load not serialized vs inference",
        )


if __name__ == "__main__":
    unittest.main()
