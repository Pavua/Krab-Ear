"""Wave-26 MED fixes:
  A1 — LLMRewriteStage.should_run() privacy gate
  A2 — MAX_AUDIO_MB guard before pipeline_v2 branch in AudioEngine.transcribe()
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.pipeline.stages.llm_rewrite import LLMRewriteStage
from core.pipeline.context import PipelineContext


# ---------------------------------------------------------------------------
# A1: LLMRewriteStage privacy gate
# ---------------------------------------------------------------------------

def _mock_rewriter():
    rw = MagicMock()
    circuit = MagicMock()
    circuit.state = "closed"
    rw._circuit = circuit
    return rw


def _make_ctx(text="Привет"):
    ctx = PipelineContext(audio_input=None)
    ctx.cleaned_text = text
    ctx.raw_text = ""
    return ctx


class TestLLMRewritePrivacyGate(unittest.TestCase):
    """A1: privacy_mode_enabled=True must suppress the LLM rewrite stage."""

    def test_should_run_false_when_privacy_mode_enabled(self):
        """Privacy mode ON → should_run returns False regardless of llm_rewrite_enabled."""
        def settings(key, default=None):
            if key == "privacy_mode_enabled":
                return True
            if key == "llm_rewrite_enabled":
                return True
            return default

        stage = LLMRewriteStage(rewriter=_mock_rewriter(), settings_get=settings)
        ctx = _make_ctx()
        self.assertFalse(stage.should_run(ctx))

    def test_should_run_true_when_privacy_mode_disabled(self):
        """Privacy mode OFF + llm_rewrite_enabled=True → should_run returns True."""
        def settings(key, default=None):
            if key == "privacy_mode_enabled":
                return False
            if key == "llm_rewrite_enabled":
                return True
            return default

        stage = LLMRewriteStage(rewriter=_mock_rewriter(), settings_get=settings)
        ctx = _make_ctx()
        self.assertTrue(stage.should_run(ctx))

    def test_privacy_gate_checked_before_llm_enabled_flag(self):
        """Privacy gate is evaluated first; settings_get call order is privacy → llm_rewrite."""
        call_order: list[str] = []

        def settings(key, default=None):
            call_order.append(key)
            if key == "privacy_mode_enabled":
                return True
            return default

        stage = LLMRewriteStage(rewriter=_mock_rewriter(), settings_get=settings)
        ctx = _make_ctx()
        result = stage.should_run(ctx)
        self.assertFalse(result)
        # privacy_mode_enabled must appear BEFORE llm_rewrite_enabled in call order
        self.assertIn("privacy_mode_enabled", call_order)
        pm_idx = call_order.index("privacy_mode_enabled")
        if "llm_rewrite_enabled" in call_order:
            lr_idx = call_order.index("llm_rewrite_enabled")
            self.assertLess(pm_idx, lr_idx,
                            "privacy_mode_enabled check must precede llm_rewrite_enabled")

    def test_privacy_mode_false_string_is_falsy(self):
        """privacy_mode_enabled=False (bool) does not block."""
        def settings(key, default=None):
            return {"privacy_mode_enabled": False, "llm_rewrite_enabled": True}.get(key, default)

        stage = LLMRewriteStage(rewriter=_mock_rewriter(), settings_get=settings)
        self.assertTrue(stage.should_run(_make_ctx()))

    def test_no_rewriter_still_returns_false_in_privacy_mode(self):
        """None rewriter → should_run=False even before privacy check."""
        def settings(key, default=None):
            return True

        stage = LLMRewriteStage(rewriter=None, settings_get=settings)
        self.assertFalse(stage.should_run(_make_ctx()))

    def test_privacy_mode_key_queried(self):
        """Verify 'privacy_mode_enabled' is the exact key checked."""
        queried: list[str] = []

        def settings(key, default=None):
            queried.append(key)
            return False

        stage = LLMRewriteStage(rewriter=_mock_rewriter(), settings_get=settings)
        stage.should_run(_make_ctx())
        self.assertIn("privacy_mode_enabled", queried)


# ---------------------------------------------------------------------------
# A2: MAX_AUDIO_MB guard before pipeline_v2 branch
# ---------------------------------------------------------------------------

class TestMaxAudioMBGuardBeforePipelineV2(unittest.TestCase):
    """A2: Oversized files must be rejected before pipeline_v2 early-return."""

    def _make_engine(self, max_audio_mb=10):
        """Create a minimal AudioEngine stub that exercises only the size-guard path."""
        # We import the real AudioEngine but stub out heavy dependencies.
        # The test writes a real temp file so os.path.getsize() works.
        try:
            from core.engine import AudioEngine
        except Exception:
            self.skipTest("AudioEngine import failed (missing native deps)")
            return None

        engine = object.__new__(AudioEngine)
        # Minimal attributes required by transcribe() up to the size-guard point
        engine._settings_get = lambda k, d=None: max_audio_mb if k == "max_audio_mb" else d
        engine._llm_rewriter = None
        engine._translator = None
        return engine

    def _write_temp_file(self, size_bytes: int) -> str:
        """Write a temp file of the given size and return its path."""
        fd, path = tempfile.mkstemp(suffix=".wav")
        try:
            os.write(fd, b"\x00" * size_bytes)
        finally:
            os.close(fd)
        return path

    def test_oversized_file_raises_before_pipeline_v2(self):
        """File > MAX_AUDIO_MB → ValueError raised even with PIPELINE_V2_ENABLED=True."""
        engine = self._make_engine(max_audio_mb=1)
        if engine is None:
            return

        # 2 MB file → exceeds 1 MB limit
        path = self._write_temp_file(2 * 1024 * 1024)
        try:
            # Patch PIPELINE_V2_ENABLED so we would take that branch if guard wasn't there
            with patch("core.engine.settings") as mock_settings:
                mock_settings.PIPELINE_V2_ENABLED = True
                mock_settings.PIPELINE_V2 = True
                mock_settings.MAX_AUDIO_MB = 1
                with self.assertRaises(ValueError) as ctx:
                    engine.transcribe(path)
                self.assertIn("Файл слишком большой", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_within_limit_does_not_raise_size_error(self):
        """File within MAX_AUDIO_MB → no ValueError from size guard (may fail for other reasons)."""
        engine = self._make_engine(max_audio_mb=100)
        if engine is None:
            return

        # 1 KB file → well within 100 MB limit
        path = self._write_temp_file(1024)
        try:
            with patch("core.engine.settings") as mock_settings:
                mock_settings.PIPELINE_V2_ENABLED = False
                mock_settings.PIPELINE_V2 = False
                mock_settings.MAX_AUDIO_MB = 100
                mock_settings.TRANSCRIBE_LANGUAGE = "ru"
                mock_settings.STT_STREAMING_ENABLED = False
                mock_settings.NUMBER_NORMALIZATION_ENABLED = False
                mock_settings.DATETIME_NORMALIZATION_ENABLED = False
                # The legacy path will fail on STT model absence — that's OK; we just
                # must NOT get a size-related ValueError.
                try:
                    engine.transcribe(path)
                except ValueError as exc:
                    self.assertNotIn("слишком большой", str(exc),
                                     "Should not raise size error for file within limit")
                except Exception:
                    pass  # Other exceptions (STT unavailable etc.) are fine
        finally:
            os.unlink(path)

    def test_size_guard_uses_settings_get_for_max(self):
        """The guard must read max_audio_mb via self._settings_get, not a hardcoded constant."""
        engine = self._make_engine(max_audio_mb=5)
        if engine is None:
            return

        # 6 MB file → exceeds 5 MB from settings_get
        path = self._write_temp_file(6 * 1024 * 1024)
        try:
            with patch("core.engine.settings") as mock_settings:
                mock_settings.PIPELINE_V2_ENABLED = False
                mock_settings.PIPELINE_V2 = False
                mock_settings.MAX_AUDIO_MB = 1000  # global settings say 1000
                # But engine._settings_get returns 5 → the guard must use _settings_get
                with self.assertRaises(ValueError) as ctx:
                    engine.transcribe(path)
                self.assertIn("Файл слишком большой", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_numpy_array_bypasses_size_guard(self):
        """numpy array input skips the file-size guard (no path → no os.path.getsize)."""
        engine = self._make_engine(max_audio_mb=0)  # 0 MB limit → any file would fail
        if engine is None:
            return

        import numpy as np
        audio = np.zeros(16000, dtype=np.float32)  # 1 s of silence

        with patch("core.engine.settings") as mock_settings:
            mock_settings.PIPELINE_V2_ENABLED = False
            mock_settings.PIPELINE_V2 = False
            mock_settings.MAX_AUDIO_MB = 0
            mock_settings.STT_STREAMING_ENABLED = False
            mock_settings.TRANSCRIBE_LANGUAGE = "ru"
            mock_settings.NUMBER_NORMALIZATION_ENABLED = False
            mock_settings.DATETIME_NORMALIZATION_ENABLED = False
            # Should NOT raise ValueError("Файл слишком большой")
            try:
                engine.transcribe(audio)
            except ValueError as exc:
                self.assertNotIn("слишком большой", str(exc))
            except Exception:
                pass  # STT model missing is fine


class TestLLMRewritePrivacyGateDefaultSettings(unittest.TestCase):
    """Default settings_get (no callable provided) must not expose privacy bypass."""

    def test_default_settings_get_does_not_bypass_privacy(self):
        """With no settings_get, defaults return falsy → stage disabled by llm_rewrite_enabled."""
        stage = LLMRewriteStage(rewriter=_mock_rewriter())
        ctx = _make_ctx()
        # Default: llm_rewrite_enabled=False → disabled; privacy mode not even reached
        self.assertFalse(stage.should_run(ctx))


if __name__ == "__main__":
    unittest.main()
