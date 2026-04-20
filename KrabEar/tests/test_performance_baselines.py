"""Performance regression baselines for critical hot-path functions.

Each test runs the target function N times, takes the median over 3 rounds,
and asserts the result stays below BASELINE_MS * THRESHOLD_MULT.

BASELINE_MS values were measured on an M4 Max MacBook Pro (2025-04-20).
THRESHOLD_MULT = 3.0 provides headroom for slower CI runners.

To re-measure baselines after deliberate optimisation:
    python -m pytest KrabEar/tests/test_performance_baselines.py -v -s

Covered targets:
    1. core.utils.TextUtils.cleanup_transcript  — soft & strict profiles
    2. core.engine.AudioEngine.normalize_audio  — pre-STT normalisation
    3. backend.state_store.StateStore.load_settings — settings hot-read
    4. core.pipeline.executor.PipelineExecutor.run — end-to-end pipeline
"""

from __future__ import annotations

import os
import pathlib
import statistics
import sys
import tempfile
import time
import unittest
from typing import Callable, List
from unittest.mock import patch

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _median_ms(fn: Callable, rounds: int = 3, iters: int = 100) -> float:
    """Return median per-call latency in milliseconds across *rounds* batches."""
    per_round: List[float] = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        for _ in range(iters):
            fn()
        per_round.append((time.perf_counter() - t0) * 1000 / iters)
    return statistics.median(per_round)


# ---------------------------------------------------------------------------
# 1. TextUtils.cleanup_transcript
# ---------------------------------------------------------------------------

class CleanupTranscriptBaseline(unittest.TestCase):
    """Perf baselines for TextUtils.cleanup_transcript().

    Baseline measured: soft profile ~6.8 ms, strict profile ~3.1 ms
    for a realistic 4-sentence (~100-char) transcript on M4 Max.
    The 45 pre-compiled brand regexes dominate cost — linear in text length.
    """

    # Measured baselines (ms per call, M4 Max, 2025-04-20)
    SOFT_BASELINE_MS = 6.82
    STRICT_BASELINE_MS = 3.06
    # Allow 3× on slower CI runners
    THRESHOLD_MULT = 3.0

    # Realistic short transcript: 4 sentences, ~100 chars
    TYPICAL_TEXT = (
        "Привет, меня зовут Павел. "
        "Сегодня обсуждаем Krab Ear. "
        "Транскрипция через Whisper. "
        "Диаризация Pyannote."
    )

    def setUp(self) -> None:
        from core.utils import TextUtils  # noqa: PLC0415
        self.TextUtils = TextUtils

    def test_soft_profile_typical_transcript(self) -> None:
        """cleanup_transcript(soft) on ~100-char transcript must not regress."""
        elapsed_ms = _median_ms(
            lambda: self.TextUtils.cleanup_transcript(self.TYPICAL_TEXT, "soft"),
            rounds=3,
            iters=200,
        )
        limit_ms = self.SOFT_BASELINE_MS * self.THRESHOLD_MULT
        self.assertLess(
            elapsed_ms,
            limit_ms,
            f"Regression: soft cleanup took {elapsed_ms:.2f} ms "
            f"(baseline {self.SOFT_BASELINE_MS} ms × {self.THRESHOLD_MULT} = {limit_ms} ms)",
        )

    def test_strict_profile_typical_transcript(self) -> None:
        """cleanup_transcript(strict) on ~100-char transcript must not regress."""
        elapsed_ms = _median_ms(
            lambda: self.TextUtils.cleanup_transcript(self.TYPICAL_TEXT, "strict"),
            rounds=3,
            iters=200,
        )
        limit_ms = self.STRICT_BASELINE_MS * self.THRESHOLD_MULT
        self.assertLess(
            elapsed_ms,
            limit_ms,
            f"Regression: strict cleanup took {elapsed_ms:.2f} ms "
            f"(baseline {self.STRICT_BASELINE_MS} ms × {self.THRESHOLD_MULT} = {limit_ms} ms)",
        )


# ---------------------------------------------------------------------------
# 2. AudioEngine.normalize_audio
# ---------------------------------------------------------------------------

class NormalizeAudioBaseline(unittest.TestCase):
    """Perf baselines for AudioEngine.normalize_audio().

    Baseline measured: ~10.5 ms for a 2-second 16 kHz mono WAV on M4 Max.
    Uses soundfile (libsndfile) + numpy for RMS normalisation.
    Mocks mlx_whisper to avoid model load.
    """

    BASELINE_MS = 10.49
    THRESHOLD_MULT = 3.0

    _tmp_wav: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import numpy as np  # noqa: PLC0415
            import soundfile as sf  # noqa: PLC0415
        except ImportError:
            raise unittest.SkipTest("soundfile/numpy not available")

        sr = 16000
        data = np.random.default_rng(42).standard_normal(sr * 2).astype(np.float32) * 0.1
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        sf.write(tmp.name, data, sr)
        cls._tmp_wav = tmp.name

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._tmp_wav and os.path.exists(cls._tmp_wav):
            os.unlink(cls._tmp_wav)

    def _make_engine(self):
        """Instantiate AudioEngine without loading any STT models."""
        with patch.dict("sys.modules", {"mlx_whisper": None}):
            from core.engine import AudioEngine  # noqa: PLC0415
        engine = AudioEngine.__new__(AudioEngine)
        return engine

    def test_normalize_audio_2s_wav(self) -> None:
        """normalize_audio on a 2-second WAV must not regress."""
        try:
            engine = self._make_engine()
        except Exception as exc:
            self.skipTest(f"AudioEngine unavailable: {exc}")

        wav = self._tmp_wav
        elapsed_ms = _median_ms(
            lambda: engine.normalize_audio(wav),
            rounds=3,
            iters=20,
        )
        limit_ms = self.BASELINE_MS * self.THRESHOLD_MULT
        self.assertLess(
            elapsed_ms,
            limit_ms,
            f"Regression: normalize_audio took {elapsed_ms:.2f} ms "
            f"(baseline {self.BASELINE_MS} ms × {self.THRESHOLD_MULT} = {limit_ms} ms)",
        )

    def test_normalize_audio_missing_file_fast(self) -> None:
        """normalize_audio on a missing path must return quickly (no I/O block)."""
        try:
            engine = self._make_engine()
        except Exception as exc:
            self.skipTest(f"AudioEngine unavailable: {exc}")

        elapsed_ms = _median_ms(
            lambda: engine.normalize_audio("/tmp/nonexistent_krabear_perf_test.wav"),
            rounds=3,
            iters=500,
        )
        # Missing-file path does only os.path.exists — must be sub-millisecond
        limit_ms = 1.0
        self.assertLess(
            elapsed_ms,
            limit_ms,
            f"Regression: missing-file normalize_audio took {elapsed_ms:.3f} ms > {limit_ms} ms",
        )


# ---------------------------------------------------------------------------
# 3. StateStore.load_settings
# ---------------------------------------------------------------------------

class LoadSettingsBaseline(unittest.TestCase):
    """Perf baselines for StateStore.load_settings().

    Baseline measured: ~0.33 ms (populated settings.json) and
    ~0.15 ms (empty file, returns defaults) on M4 Max.
    The function acquires a file-lock + JSON parse on every call.
    """

    POPULATED_BASELINE_MS = 0.325
    EMPTY_BASELINE_MS = 0.149
    # Allow 6× — file-lock latency can spike on loaded CI runners
    THRESHOLD_MULT = 6.0

    def setUp(self) -> None:
        from backend.state_store import StateStore  # noqa: PLC0415
        self.StateStore = StateStore

    def test_load_settings_populated(self) -> None:
        """load_settings with existing settings.json must not regress."""
        with tempfile.TemporaryDirectory() as tmp:
            store = self.StateStore(pathlib.Path(tmp))
            store.save_settings({"stt_model": "balanced", "language": "ru"})

            elapsed_ms = _median_ms(
                store.load_settings,
                rounds=3,
                iters=50,
            )
        limit_ms = self.POPULATED_BASELINE_MS * self.THRESHOLD_MULT
        self.assertLess(
            elapsed_ms,
            limit_ms,
            f"Regression: load_settings (populated) took {elapsed_ms:.3f} ms "
            f"(baseline {self.POPULATED_BASELINE_MS} ms × {self.THRESHOLD_MULT} = {limit_ms:.3f} ms)",
        )

    def test_load_settings_defaults_only(self) -> None:
        """load_settings on empty store (returns defaults) must not regress."""
        with tempfile.TemporaryDirectory() as tmp:
            store = self.StateStore(pathlib.Path(tmp))

            elapsed_ms = _median_ms(
                store.load_settings,
                rounds=3,
                iters=50,
            )
        limit_ms = self.EMPTY_BASELINE_MS * self.THRESHOLD_MULT
        self.assertLess(
            elapsed_ms,
            limit_ms,
            f"Regression: load_settings (empty) took {elapsed_ms:.3f} ms "
            f"(baseline {self.EMPTY_BASELINE_MS} ms × {self.THRESHOLD_MULT} = {limit_ms:.3f} ms)",
        )


# ---------------------------------------------------------------------------
# 4. PipelineExecutor.run
# ---------------------------------------------------------------------------

class PipelineExecutorBaseline(unittest.TestCase):
    """Perf baselines for PipelineExecutor.run().

    Baseline measured: ~0.05 ms for a 2-stage and 5-stage pipeline
    using lightweight stub stages (no I/O, no model inference).
    Measures the executor loop overhead itself, not stage logic.
    """

    TWO_STAGE_BASELINE_MS = 0.053
    FIVE_STAGE_BASELINE_MS = 0.054
    # Allow 20× — executor overhead is tiny, CI jitter dominates
    THRESHOLD_MULT = 20.0

    def setUp(self) -> None:
        from core.pipeline.executor import PipelineExecutor  # noqa: PLC0415
        from core.pipeline.context import PipelineContext    # noqa: PLC0415
        self.PipelineExecutor = PipelineExecutor
        self.PipelineContext = PipelineContext

    def _make_noop_stage(self, stage_name: str = "noop"):
        """Return a minimal stage that satisfies the PipelineStage protocol."""

        class _Noop:
            name = stage_name

            def should_run(self, ctx):  # noqa: ANN001
                return True

            def process(self, ctx):  # noqa: ANN001
                return ctx

        return _Noop()

    def _make_text_stage(self):
        """Stage that does a small CPU operation (upper-case cleaned_text)."""

        class _Text:
            name = "text_upper"

            def should_run(self, ctx):  # noqa: ANN001
                return True

            def process(self, ctx):  # noqa: ANN001
                ctx.cleaned_text = ctx.raw_text.upper()
                return ctx

        return _Text()

    def test_pipeline_two_stages(self) -> None:
        """PipelineExecutor with 2 stub stages must not regress."""
        executor = self.PipelineExecutor(
            [self._make_noop_stage("a"), self._make_text_stage()]
        )
        PCtx = self.PipelineContext

        elapsed_ms = _median_ms(
            lambda: executor.run(PCtx(audio_input=None, raw_text="hello world")),
            rounds=3,
            iters=1000,
        )
        limit_ms = self.TWO_STAGE_BASELINE_MS * self.THRESHOLD_MULT
        self.assertLess(
            elapsed_ms,
            limit_ms,
            f"Regression: 2-stage pipeline took {elapsed_ms:.4f} ms "
            f"(baseline {self.TWO_STAGE_BASELINE_MS} ms × {self.THRESHOLD_MULT} = {limit_ms} ms)",
        )

    def test_pipeline_five_stages(self) -> None:
        """PipelineExecutor with 5 stub stages must not regress."""
        executor = self.PipelineExecutor([
            self._make_noop_stage("pre"),
            self._make_text_stage(),
            self._make_noop_stage("mid"),
            self._make_text_stage(),
            self._make_noop_stage("post"),
        ])
        PCtx = self.PipelineContext
        payload = "hello world " * 50  # ~600 chars

        elapsed_ms = _median_ms(
            lambda: executor.run(PCtx(audio_input=None, raw_text=payload)),
            rounds=3,
            iters=1000,
        )
        limit_ms = self.FIVE_STAGE_BASELINE_MS * self.THRESHOLD_MULT
        self.assertLess(
            elapsed_ms,
            limit_ms,
            f"Regression: 5-stage pipeline took {elapsed_ms:.4f} ms "
            f"(baseline {self.FIVE_STAGE_BASELINE_MS} ms × {self.THRESHOLD_MULT} = {limit_ms} ms)",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
