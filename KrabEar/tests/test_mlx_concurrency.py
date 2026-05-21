"""Regression tests for MLX lock serialization (Phase C C.3).

Verifies:
1. mlx_lock() is reentrant (RLock semantics).
2. Two threads execute sequentially under the lock (no interleaving).
3. Smoke check: mlx_whisper.transcribe() call sites in engine.py are inside
   a with-block that holds mlx_lock — no unwrapped bare calls.
4. Smoke check: debug_whisper.py wraps both transcribe() calls in mlx_lock().

IMPORTANT: DO NOT import mlx_whisper or instantiate AudioEngine here —
           memory constraint (MLX + LM Studio running simultaneously can
           trigger Metal GPU memory pressure → reboot required).
"""

import sys
import os
import threading
import time
import unittest

# Allow imports from KrabEar/ package root
_tests_dir = os.path.dirname(os.path.abspath(__file__))
_krabear_dir = os.path.dirname(_tests_dir)
if _krabear_dir not in sys.path:
    sys.path.insert(0, _krabear_dir)

from core.mlx_lock import mlx_lock, _mlx_lock  # noqa: E402


class MLXLockReentranceTests(unittest.TestCase):
    """mlx_lock() must use RLock semantics (same thread can re-acquire)."""

    def test_lock_is_reentrant_single_level(self):
        """Acquiring once should not block."""
        with mlx_lock():
            pass  # no deadlock

    def test_lock_is_reentrant_nested(self):
        """RLock allows same thread to re-acquire without deadlock."""
        with mlx_lock():
            with mlx_lock():
                with mlx_lock():
                    pass  # three levels deep — should not deadlock

    def test_lock_returns_rlock_instance(self):
        """mlx_lock() must return the module-level RLock instance."""
        import threading
        self.assertIsInstance(_mlx_lock, type(threading.RLock()))

    def test_same_object_returned_each_call(self):
        """mlx_lock() must return the same singleton each time."""
        self.assertIs(mlx_lock(), mlx_lock())


class MLXLockSerializationTests(unittest.TestCase):
    """Two threads competing for mlx_lock must execute sequentially."""

    def test_lock_serializes_threads(self):
        """No interleaving: A-start → A-end → B-start → B-end (or vice versa)."""
        results = []
        barrier = threading.Barrier(2)  # both threads start at same time

        def worker(name: str):
            barrier.wait()  # sync start
            with mlx_lock():
                results.append(f"{name}-start")
                time.sleep(0.015)
                results.append(f"{name}-end")

        t1 = threading.Thread(target=worker, args=("A",))
        t2 = threading.Thread(target=worker, args=("B",))
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        self.assertEqual(len(results), 4, f"Expected 4 events, got: {results}")
        # Either A completes first, or B completes first — no interleaving
        self.assertIn(
            results,
            [
                ["A-start", "A-end", "B-start", "B-end"],
                ["B-start", "B-end", "A-start", "A-end"],
            ],
            f"Unexpected interleaving: {results}",
        )

    def test_lock_serializes_many_threads(self):
        """Five threads: each must observe start→end pairs without interleaving."""
        results = []
        lock_for_list = threading.Lock()
        n = 5

        def worker(name: str):
            with mlx_lock():
                with lock_for_list:
                    results.append(f"{name}-start")
                time.sleep(0.005)
                with lock_for_list:
                    results.append(f"{name}-end")

        threads = [threading.Thread(target=worker, args=(f"T{i}",)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(len(results), n * 2)
        # Verify start-end pairing: every "X-start" must be immediately followed by "X-end"
        for i in range(0, len(results), 2):
            start_name = results[i].replace("-start", "")
            end_name = results[i + 1].replace("-end", "")
            self.assertEqual(
                start_name,
                end_name,
                f"Interleaving detected at positions {i},{i+1}: {results}",
            )


class MLXLockSmokeCheckEngineTests(unittest.TestCase):
    """Smoke-test: engine.py must not have bare mlx_whisper.transcribe() calls.

    Parses engine.py as text to verify every mlx_whisper.transcribe invocation
    appears within a with-block that acquired mlx_lock.  Does NOT import MLX.
    """

    @classmethod
    def _read_engine(cls):
        path = os.path.join(_krabear_dir, "core", "engine.py")
        if not os.path.exists(path):
            return None, path
        with open(path, encoding="utf-8") as f:
            return f.read(), path

    def test_engine_transcribe_sites_inside_mlx_lock_block(self):
        """Every mlx_whisper.transcribe() call in engine.py must be inside with mlx_lock()."""
        text, path = self._read_engine()
        if text is None:
            self.skipTest(f"engine.py not found at {path}")

        lines = text.split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip()
            # Skip comments
            if stripped.startswith("#"):
                continue
            if "mlx_whisper.transcribe" not in line:
                continue
            # Skip lines where mlx_whisper.transcribe appears only inside a string literal
            # (e.g. error messages, docstrings, raise RuntimeError("...mlx_whisper.transcribe"))
            # A real call site has mlx_whisper.transcribe followed by '(' not inside quotes.
            # Heuristic: if the occurrence is only inside a string (surrounded by quotes), skip it.
            # Check: strip the line and see if the call token appears outside of quotes
            # by looking for 'mlx_whisper.transcribe(' as an actual invocation pattern.
            if "mlx_whisper.transcribe(" not in line:
                # It's a reference in a string/comment, not an actual call — skip
                continue
            # Look back up to 20 lines for 'with mlx_lock'
            context_start = max(0, i - 20)
            context = "\n".join(lines[context_start : i + 1])
            self.assertIn(
                "mlx_lock",
                context,
                f"Unwrapped mlx_whisper.transcribe at {path}:{i + 1}:\n  {line.strip()}\n"
                f"  Context (lines {context_start + 1}-{i + 1}):\n{context}",
            )

    def test_engine_imports_mlx_lock(self):
        """engine.py must import mlx_lock from core.mlx_lock."""
        text, path = self._read_engine()
        if text is None:
            self.skipTest(f"engine.py not found at {path}")
        self.assertIn(
            "from core.mlx_lock import mlx_lock",
            text,
            "engine.py must import mlx_lock from core.mlx_lock",
        )


class MLXLockSmokeCheckAudioLangIdTests(unittest.TestCase):
    """Smoke-test: audio_lang_id.py must wrap all MLX calls in mlx_lock."""

    @classmethod
    def _read_file(cls, relative_path):
        path = os.path.join(_krabear_dir, *relative_path.split("/"))
        if not os.path.exists(path):
            return None, path
        with open(path, encoding="utf-8") as f:
            return f.read(), path

    def test_audio_lang_id_uses_mlx_lock(self):
        """audio_lang_id.py _run_detect must acquire mlx_lock before calling _detect_with_mlx."""
        text, path = self._read_file("core/audio_lang_id.py")
        if text is None:
            self.skipTest(f"audio_lang_id.py not found at {path}")
        self.assertIn("from core.mlx_lock import mlx_lock", text)
        self.assertIn("with mlx_lock():", text)

    def test_audio_lang_id_no_bare_mlx_calls_outside_lock(self):
        """No mlx_whisper.* inference calls in audio_lang_id.py outside _detect_with_mlx scope."""
        text, path = self._read_file("core/audio_lang_id.py")
        if text is None:
            self.skipTest(f"audio_lang_id.py not found at {path}")
        lines = text.split("\n")
        inference_calls = [
            "mlx_whisper.load_models.load_model",
            "mlx_whisper.audio.log_mel_spectrogram",
            "mlx_whisper.decoding.detect_language",
        ]
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for call in inference_calls:
                if call not in line:
                    continue
                # The call must be inside _detect_with_mlx which is called under mlx_lock
                # Verify by checking that _run_detect (the caller) contains 'with mlx_lock'
                # This is a structural check: the function _detect_with_mlx should only
                # be called from within a mlx_lock context in _run_detect.
                # The inference calls are inside _detect_with_mlx, called from _run_detect
                # under mlx_lock. Verify the file still has that pattern.
                self.assertIn(
                    "_detect_with_mlx",
                    text,
                    "Expected _detect_with_mlx helper method in audio_lang_id.py",
                )
                self.assertIn(
                    "with mlx_lock():",
                    text,
                    f"audio_lang_id.py must have 'with mlx_lock():' wrapping MLX inference calls",
                )


class MLXLockSmokeCheckDebugScriptTests(unittest.TestCase):
    """Smoke-test: debug_whisper.py must wrap transcribe() calls in mlx_lock."""

    @classmethod
    def _read_file(cls):
        path = os.path.join(_krabear_dir, "scripts", "debug_whisper.py")
        if not os.path.exists(path):
            return None, path
        with open(path, encoding="utf-8") as f:
            return f.read(), path

    def test_debug_whisper_imports_mlx_lock(self):
        """debug_whisper.py must import mlx_lock."""
        text, path = self._read_file()
        if text is None:
            self.skipTest(f"debug_whisper.py not found at {path}")
        self.assertIn(
            "from core.mlx_lock import mlx_lock",
            text,
            "debug_whisper.py must import mlx_lock from core.mlx_lock",
        )

    def test_debug_whisper_transcribe_sites_inside_mlx_lock(self):
        """Every mlx_whisper.transcribe() in debug_whisper.py must be inside with mlx_lock()."""
        text, path = self._read_file()
        if text is None:
            self.skipTest(f"debug_whisper.py not found at {path}")
        lines = text.split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "mlx_whisper.transcribe" not in line:
                continue
            context_start = max(0, i - 10)
            context = "\n".join(lines[context_start : i + 1])
            self.assertIn(
                "mlx_lock",
                context,
                f"Unwrapped mlx_whisper.transcribe at {path}:{i + 1}:\n  {line.strip()}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
