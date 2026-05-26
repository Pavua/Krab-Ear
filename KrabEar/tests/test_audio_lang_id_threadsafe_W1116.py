"""Tests for AudioLanguageID._model_cache RLock thread-safety (W1109 F1 HIGH).

Tests:
    - test_model_cache_threadsafe: verifies _model_cache_lock is an RLock instance
      and that the class-level dict and lock are present as expected.
    - test_concurrent_detect_no_race: launches N threads simultaneously calling
      _detect_with_mlx (via a stub mlx_whisper) and verifies no data corruption
      or exceptions occur — validates the RLock prevents check/insert/clear races.
"""

import ast
import os
import sys
import threading
import unittest

# ---------------------------------------------------------------------------
# Path setup — same pattern used throughout Krab Ear test suite
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KRABEAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRABEAR_ROOT not in sys.path:
    sys.path.insert(0, KRABEAR_ROOT)

# ---------------------------------------------------------------------------
# Minimal stubs so the module loads without mlx_whisper / core.config
# ---------------------------------------------------------------------------
import types  # noqa: E402

# Stub core.config
_config_mod = types.ModuleType("core.config")
_settings_stub = types.SimpleNamespace(
    STT_AUDIO_LANG_ID_ENABLED=True,
    STT_AUDIO_LANG_ID_PREVIEW_SEC=5.0,
    MODEL_BALANCED="mlx-community/whisper-large-v3-turbo",
)
_config_mod.settings = _settings_stub
sys.modules.setdefault("core.config", _config_mod)

# Stub core.mlx_lock — provide a real threading.RLock-based context manager
_mlx_lock_mod = types.ModuleType("core.mlx_lock")
_GLOBAL_MLX_LOCK = threading.RLock()


def _mlx_lock():
    return _GLOBAL_MLX_LOCK


_mlx_lock_mod.mlx_lock = _mlx_lock
sys.modules.setdefault("core.mlx_lock", _mlx_lock_mod)

import numpy as np  # noqa: E402

from core.audio_lang_id import AudioLanguageID  # noqa: E402


# ---------------------------------------------------------------------------
# Helper: stub mlx_whisper module
# ---------------------------------------------------------------------------

def _make_stub_mlx_whisper(lang_result="ru", load_delay=0.0, load_side_effect=None):
    """Return a stub mlx_whisper module.

    Parameters
    ----------
    lang_result: language string returned by detect_language.
    load_delay:  simulated load delay in seconds.
    load_side_effect: if not None, raise this exception from load_model.
    """
    import time

    stub = types.SimpleNamespace()

    class _Audio:
        @staticmethod
        def log_mel_spectrogram(audio):
            return object()  # opaque mel object

    class _Decoding:
        @staticmethod
        def detect_language(model, mel):
            return lang_result

    class _LoadModels:
        @staticmethod
        def load_model(path):
            if load_delay:
                time.sleep(load_delay)
            if load_side_effect:
                raise load_side_effect
            return object()  # opaque model handle

    stub.audio = _Audio()
    stub.decoding = _Decoding()
    stub.load_models = _LoadModels()
    return stub


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestModelCacheThreadsafe(unittest.TestCase):
    """AST + instance-level attribute checks for the RLock fix."""

    def setUp(self):
        # Reset class-level cache before each test to avoid cross-test pollution.
        AudioLanguageID._model_cache.clear()

    def test_rlock_class_attribute_exists(self):
        """_model_cache_lock must be a class-level threading.RLock."""
        self.assertTrue(
            hasattr(AudioLanguageID, "_model_cache_lock"),
            "_model_cache_lock class attribute must exist",
        )
        lock = AudioLanguageID._model_cache_lock
        self.assertIsInstance(
            lock, type(threading.RLock()),
            "_model_cache_lock must be a threading.RLock instance",
        )

    def test_rlock_is_reentrant(self):
        """The same thread must be able to acquire _model_cache_lock twice without deadlock."""
        lock = AudioLanguageID._model_cache_lock
        acquired_outer = lock.acquire(blocking=False)
        self.assertTrue(acquired_outer, "First acquire must succeed")
        acquired_inner = lock.acquire(blocking=False)
        self.assertTrue(acquired_inner, "Second acquire by same thread must succeed (RLock reentrant)")
        lock.release()
        lock.release()

    def test_ast_with_lock_wraps_cache_ops(self):
        """AST check: _detect_with_mlx body contains 'with AudioLanguageID._model_cache_lock:'."""
        src_path = os.path.join(KRABEAR_ROOT, "core", "audio_lang_id.py")
        with open(src_path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())

        # Find the _detect_with_mlx method
        method_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_detect_with_mlx":
                method_node = node
                break

        self.assertIsNotNone(method_node, "_detect_with_mlx method must exist")

        # Check that the body has a With statement targeting _model_cache_lock
        found_with = False
        for stmt in ast.walk(method_node):
            if isinstance(stmt, ast.With):
                for item in stmt.items:
                    ctx = item.context_expr
                    # Looking for: AudioLanguageID._model_cache_lock
                    if (
                        isinstance(ctx, ast.Attribute)
                        and ctx.attr == "_model_cache_lock"
                        and isinstance(ctx.value, ast.Name)
                        and ctx.value.id == "AudioLanguageID"
                    ):
                        found_with = True
                        break
        self.assertTrue(
            found_with,
            "_detect_with_mlx must use 'with AudioLanguageID._model_cache_lock:' to guard cache ops",
        )

    def test_model_cache_dict_still_exists(self):
        """_model_cache must remain a dict (not broken by the fix)."""
        self.assertIsInstance(AudioLanguageID._model_cache, dict)


class TestConcurrentDetectNoRace(unittest.TestCase):
    """Concurrent _detect_with_mlx calls via stub mlx_whisper — no exceptions or corrupted state."""

    def setUp(self):
        AudioLanguageID._model_cache.clear()

    def test_concurrent_detect_no_race(self):
        """N threads simultaneously calling _detect_with_mlx must not raise or corrupt cache."""
        stub = _make_stub_mlx_whisper(lang_result="ru")
        audio_16k = np.zeros(16000 * 3, dtype=np.float32)
        lid = AudioLanguageID(model_path="test-model-path")

        errors = []
        results = []
        lock = threading.Lock()

        def worker():
            try:
                # Call the internal method directly — it's what holds the race-prone code.
                result = lid._detect_with_mlx(stub, audio_16k)
                with lock:
                    results.append(result)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        n_threads = 20
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual([], errors, f"Concurrent detect raised exceptions: {errors}")
        self.assertEqual(n_threads, len(results), "All threads must produce a result")
        # All results must be the stub lang code
        for r in results:
            self.assertEqual("ru", r, f"Unexpected result: {r}")

        # Cache must contain exactly 1 entry (eviction policy: max 1)
        self.assertEqual(
            1, len(AudioLanguageID._model_cache),
            f"Cache must have exactly 1 entry, got {len(AudioLanguageID._model_cache)}",
        )

    def test_concurrent_model_eviction_no_race(self):
        """Threads calling detect with alternating model paths must not corrupt cache size."""
        audio_16k = np.zeros(16000 * 2, dtype=np.float32)
        errors = []
        lock = threading.Lock()

        def worker(model_path: str):
            stub = _make_stub_mlx_whisper(lang_result="en")
            lid = AudioLanguageID(model_path=model_path)
            try:
                lid._detect_with_mlx(stub, audio_16k)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        # Two different model paths — half the threads use each
        n = 20
        threads = (
            [threading.Thread(target=worker, args=("model-A",), daemon=True) for _ in range(n // 2)]
            + [threading.Thread(target=worker, args=("model-B",), daemon=True) for _ in range(n // 2)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual([], errors, f"Eviction race raised exceptions: {errors}")
        # After all threads finish, cache size must be exactly 1 (never > 1)
        self.assertLessEqual(
            len(AudioLanguageID._model_cache), 1,
            f"Cache size must be ≤1 after concurrent eviction, got {len(AudioLanguageID._model_cache)}",
        )

    def test_model_cache_threadsafe_basic(self):
        """Single-thread smoke: cache populated on first call, reused on second."""
        stub = _make_stub_mlx_whisper(lang_result="es")
        audio_16k = np.zeros(16000 * 2, dtype=np.float32)
        lid = AudioLanguageID(model_path="smoke-model")

        self.assertEqual(0, len(AudioLanguageID._model_cache))

        result1 = lid._detect_with_mlx(stub, audio_16k)
        self.assertEqual("es", result1)
        self.assertEqual(1, len(AudioLanguageID._model_cache))
        model_ref = AudioLanguageID._model_cache.get("smoke-model")
        self.assertIsNotNone(model_ref)

        # Second call — same model; no reload (object identity preserved)
        result2 = lid._detect_with_mlx(stub, audio_16k)
        self.assertEqual("es", result2)
        self.assertEqual(1, len(AudioLanguageID._model_cache))
        self.assertIs(model_ref, AudioLanguageID._model_cache.get("smoke-model"))


if __name__ == "__main__":
    unittest.main()
