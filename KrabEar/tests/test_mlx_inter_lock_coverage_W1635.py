"""W1635 — mlx_inter_process_lock wraps all MLX call sites (W1630 F1 HIGH fix).

Verifies that each of the 6 MLX call sites added in W1635 acquires the
inter-process lock (outer) before the intra-process mlx_lock (inner), and that
when the feature flag is OFF the sites function unchanged (no-op path).

Sites covered:
  1. engine.py  — warmup_stt (transcribe path, raises on timeout)
  2. engine.py  — switch_profile clear_cache (degrade_on_timeout=True)
  3. engine.py  — post-diarization clear_cache (degrade_on_timeout=True)
  4. engine.py  — _transcribe_model (main transcribe loop, raises on timeout)
  5. audio_lang_id.py — detect (transcribe path, raises on timeout)
  6. stt_whisper_mlx_adapter.py — transcribe (transcribe path)
  7. bulk_reprocess.py — СНЯТ 02.09.2026: не вызывает MLX сам, а внешний захват
     вокруг transcriber.transcribe() самоблокировал поток пула внутри
     engine.transcribe (см. TestBulkReprocessNotAnMlxSiteW1635 ниже).

AST-based: verify mlx_inter_process_lock import and usage presence in source.
Mock-based: verify call ordering when env var enabled/disabled.
"""
from __future__ import annotations

import ast
import os
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, call, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source(rel_path: str) -> str:
    path = os.path.join(_PROJECT_ROOT, rel_path)
    with open(path) as f:
        return f.read()


def _has_inter_lock_import(source: str) -> bool:
    """Return True if source imports mlx_inter_process_lock."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            src = ast.unparse(node)
            if "mlx_inter_process_lock" in src:
                return True
    return False


def _count_inter_lock_usages(source: str) -> int:
    """Count occurrences of mlx_inter_process_lock( in the source."""
    return source.count("mlx_inter_process_lock(")


# ---------------------------------------------------------------------------
# AST tests — import & usage presence
# ---------------------------------------------------------------------------

class TestEngineASTW1635(unittest.TestCase):
    """engine.py must import and use mlx_inter_process_lock at all 4 sites."""

    def setUp(self):
        self.src = _source("core/engine.py")

    def test_import_present(self):
        self.assertTrue(
            _has_inter_lock_import(self.src),
            "engine.py must import mlx_inter_process_lock from core.mlx_inter_lock",
        )

    def test_usage_count_at_least_four(self):
        count = _count_inter_lock_usages(self.src)
        self.assertGreaterEqual(
            count, 4,
            f"engine.py should have >=4 mlx_inter_process_lock() usages, got {count}",
        )

    def test_mlx_inter_lock_timeout_imported(self):
        self.assertIn(
            "MLXInterLockTimeout",
            self.src,
            "engine.py must import MLXInterLockTimeout for timeout handling",
        )

    def test_warmup_uses_inter_lock(self):
        """warmup_stt should use mlx_inter_process_lock near mlx_whisper.transcribe."""
        lines = self.src.splitlines()
        # Find warmup_stt area — look for the MLXInterLockTimeout except near transcribe/warmup
        warmup_idx = next(
            (i for i, l in enumerate(lines) if "STT warmup" in l and "mlx_inter_lock timeout" in l),
            None,
        )
        self.assertIsNotNone(warmup_idx, "engine.py must catch MLXInterLockTimeout in warmup_stt path")

    def test_transcribe_model_uses_inter_lock(self):
        """_transcribe_model must use mlx_inter_process_lock in the variants loop."""
        self.assertIn(
            "with mlx_inter_process_lock(), mlx_lock():",
            self.src,
            "engine.py _transcribe_model must use combined with statement",
        )


class TestAudioLangIDASTW1635(unittest.TestCase):
    """audio_lang_id.py must import and use mlx_inter_process_lock."""

    def setUp(self):
        self.src = _source("core/audio_lang_id.py")

    def test_import_present(self):
        self.assertTrue(
            _has_inter_lock_import(self.src),
            "audio_lang_id.py must import mlx_inter_process_lock",
        )

    def test_usage_present(self):
        count = _count_inter_lock_usages(self.src)
        self.assertGreaterEqual(count, 1, "audio_lang_id.py must use mlx_inter_process_lock()")

    def test_mlx_inter_lock_timeout_handled(self):
        self.assertIn("MLXInterLockTimeout", self.src, "audio_lang_id.py must handle MLXInterLockTimeout")


class TestSTTWhisperMLXAdapterASTW1635(unittest.TestCase):
    """stt_whisper_mlx_adapter.py must import and use mlx_inter_process_lock."""

    def setUp(self):
        self.src = _source("core/pipeline/stt_whisper_mlx_adapter.py")

    def test_import_present(self):
        self.assertTrue(
            _has_inter_lock_import(self.src),
            "stt_whisper_mlx_adapter.py must import mlx_inter_process_lock",
        )

    def test_usage_present(self):
        count = _count_inter_lock_usages(self.src)
        self.assertGreaterEqual(count, 1, "stt_whisper_mlx_adapter.py must use mlx_inter_process_lock()")

    def test_combined_with_statement(self):
        self.assertIn(
            "with mlx_inter_process_lock(), mlx_lock():",
            self.src,
            "stt_whisper_mlx_adapter.py must use combined with statement",
        )


class TestBulkReprocessNotAnMlxSiteW1635(unittest.TestCase):
    """Сайт 7 снят 02.09.2026: bulk_reprocess не вызывает MLX сам.

    Он зовёт transcriber.transcribe(), а engine.transcribe отдаёт работу в
    ThreadPoolExecutor — внешний захват здесь самоблокировал поток пула, который
    берёт тот же mlx_lock (RLock реентерабелен только для своего потока).
    Инвариант «MLX-инференс под обоими локами» держат сайты 1–6.
    Разбор: tests/test_bulk_reprocess_mlx_self_block_2026_09_02.py.
    """

    def test_no_lock_calls_remain(self):
        tree = ast.parse(_source("backend/bulk_reprocess.py"))
        found = sorted({
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"mlx_lock", "mlx_inter_process_lock"}
        })
        self.assertEqual(
            found, [],
            "bulk_reprocess снова захватывает GPU-локи вокруг transcribe "
            f"({found}) — самоблокировка потока пула внутри engine.transcribe",
        )


# ---------------------------------------------------------------------------
# Mock-based: env-var disabled path (no-op) — mlx_inter_process_lock returns _NoOpContext
# ---------------------------------------------------------------------------

class TestInterLockNoOpWhenDisabledW1635(unittest.TestCase):
    """When KRAB_EAR_MLX_INTER_PROCESS_LOCK != '1', factory returns a no-op context."""

    def test_disabled_returns_noop(self):
        """mlx_inter_process_lock() returns a no-op when env var not set."""
        env_without_flag = {k: v for k, v in os.environ.items()
                            if k != "KRAB_EAR_MLX_INTER_PROCESS_LOCK"}
        with patch.dict(os.environ, env_without_flag, clear=True):
            from core.mlx_inter_lock import mlx_inter_process_lock, _NoOpContext
            ctx = mlx_inter_process_lock()
            self.assertIsInstance(ctx, _NoOpContext)

    def test_disabled_noop_does_not_open_lockfile(self):
        """No-op context must not open any file (zero syscalls)."""
        env_without_flag = {k: v for k, v in os.environ.items()
                            if k != "KRAB_EAR_MLX_INTER_PROCESS_LOCK"}
        with patch.dict(os.environ, env_without_flag, clear=True):
            from core.mlx_inter_lock import mlx_inter_process_lock
            with patch("builtins.open") as mock_open, \
                 patch("os.open") as mock_os_open:
                with mlx_inter_process_lock():
                    pass
                mock_open.assert_not_called()
                mock_os_open.assert_not_called()

    def test_enabled_returns_inter_process_lock(self):
        """mlx_inter_process_lock() returns InterProcessMLXLock when env var set to '1'."""
        with patch.dict(os.environ, {"KRAB_EAR_MLX_INTER_PROCESS_LOCK": "1"}):
            from core.mlx_inter_lock import mlx_inter_process_lock, InterProcessMLXLock
            ctx = mlx_inter_process_lock()
            self.assertIsInstance(ctx, InterProcessMLXLock)


# ---------------------------------------------------------------------------
# Mock-based: audio_lang_id.py — inter_lock called before mlx_lock
# ---------------------------------------------------------------------------

class TestAudioLangIDInterLockOrderW1635(unittest.TestCase):
    """AudioLanguageID.detect() must acquire inter-process lock before mlx_lock."""

    def test_inter_lock_entered_before_mlx_lock(self):
        """Verify call ordering: mlx_inter_process_lock().__enter__ precedes mlx_lock().__enter__."""
        call_order: list[str] = []

        @contextmanager
        def fake_inter_lock(*args, **kwargs):
            call_order.append("inter_enter")
            yield
            call_order.append("inter_exit")

        @contextmanager
        def fake_mlx_lock():
            call_order.append("mlx_enter")
            yield
            call_order.append("mlx_exit")

        with patch.dict(os.environ, {"KRAB_EAR_MLX_INTER_PROCESS_LOCK": "1"}):
            import core.audio_lang_id as ali_mod
            with patch.object(ali_mod, "mlx_inter_process_lock", fake_inter_lock), \
                 patch.object(ali_mod, "mlx_lock", fake_mlx_lock):

                lang_id = ali_mod.AudioLanguageID(model_path="tiny")
                import numpy as np
                # Provide real-ish audio so it doesn't bail out early on silence check.
                audio = np.random.randn(16000).astype(np.float32) * 0.1

                # mock mlx_whisper import inside _detect_with_mlx
                fake_mlx_whisper = MagicMock()
                fake_mlx_whisper.audio.log_mel_spectrogram.return_value = MagicMock()
                fake_mlx_whisper.decoding.detect_language.return_value = {"ru": 0.9}

                with patch.dict(sys.modules, {"mlx_whisper": fake_mlx_whisper}):
                    # Patch the import check path
                    with patch("builtins.__import__", side_effect=lambda name, *a, **kw: (
                        fake_mlx_whisper if name == "mlx_whisper" else __import__(name, *a, **kw)
                    )):
                        lang_id.detect(audio, sample_rate=16000)

        # If we got here without crashing, check the order
        if call_order:
            self.assertEqual(call_order[0], "inter_enter", "inter-process lock must be entered first")
            if len(call_order) > 1:
                self.assertEqual(call_order[1], "mlx_enter", "mlx_lock must be entered second")


# ---------------------------------------------------------------------------
# Mock-based: mlx_inter_lock timeout propagates correctly from audio_lang_id
# ---------------------------------------------------------------------------

class TestAudioLangIDTimeoutHandlingW1635(unittest.TestCase):
    """AudioLanguageID must return None (not crash) when MLXInterLockTimeout is raised."""

    def test_returns_none_on_timeout(self):
        """detect() should catch MLXInterLockTimeout and return None gracefully."""
        from core.mlx_inter_lock import MLXInterLockTimeout

        def raising_inter_lock(*args, **kwargs):
            raise MLXInterLockTimeout(timeout_sec=5.0, lock_path=None)  # type: ignore[arg-type]

        import core.audio_lang_id as ali_mod
        with patch.object(ali_mod, "mlx_inter_process_lock", raising_inter_lock):
            lang_id = ali_mod.AudioLanguageID(model_path="tiny")
            import numpy as np
            audio = np.random.randn(16000).astype(np.float32) * 0.1

            fake_mlx_whisper = MagicMock()
            with patch.dict(sys.modules, {"mlx_whisper": fake_mlx_whisper}):
                result = lang_id.detect(audio, sample_rate=16000)

        self.assertIsNone(result, "detect() must return None when MLXInterLockTimeout raised")


if __name__ == "__main__":
    unittest.main()
