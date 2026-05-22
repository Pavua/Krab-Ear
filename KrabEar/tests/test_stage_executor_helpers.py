"""Unit tests for executor.py private helper functions and _STAGE_FIELDS mapping.

Covers:
- _extract_stage_result: known stages, unknown stage, numpy array skipped
- _apply_cached_result: fields written to ctx, unknown keys ignored
- _stage_had_error: prefix matching, no false positives
- _STAGE_FIELDS: all expected stage names present with correct fields
- PipelineExecutor.to_legacy_dict: all fields, diarization, llm flags
"""

from __future__ import annotations

import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.pipeline.context import PipelineContext
from core.pipeline.executor import (
    _extract_stage_result,
    _apply_cached_result,
    _stage_had_error,
    _STAGE_FIELDS,
    PipelineExecutor,
)


# ---------------------------------------------------------------------------
# _STAGE_FIELDS mapping tests
# ---------------------------------------------------------------------------

class TestStageFieldsMapping(unittest.TestCase):
    """_STAGE_FIELDS defines which ctx fields each stage owns."""

    def test_stt_fields_present(self):
        fields = _STAGE_FIELDS["stt"]
        for name in ("raw_text", "language_detected", "model_used", "confidence", "segments"):
            self.assertIn(name, fields, f"stt should own {name!r}")

    def test_text_cleanup_fields_present(self):
        fields = _STAGE_FIELDS["text_cleanup"]
        self.assertIn("cleaned_text", fields)

    def test_llm_rewrite_fields_present(self):
        fields = _STAGE_FIELDS["llm_rewrite"]
        for name in ("rewritten_text", "llm_applied", "llm_fallback_reason", "llm_latency_ms"):
            self.assertIn(name, fields)

    def test_translation_fields_present(self):
        fields = _STAGE_FIELDS["translation"]
        self.assertIn("translation", fields)
        self.assertIn("translation_engine", fields)

    def test_diarization_fields_present(self):
        fields = _STAGE_FIELDS["diarization"]
        for name in ("diarization", "speaker_segments", "num_speakers"):
            self.assertIn(name, fields)

    def test_audio_normalization_fields_present(self):
        fields = _STAGE_FIELDS["audio_normalization"]
        self.assertIn("normalized_audio", fields)

    def test_all_six_stages_in_mapping(self):
        expected = {
            "stt",
            "text_cleanup",
            "llm_rewrite",
            "translation",
            "diarization",
            "audio_normalization",
        }
        self.assertEqual(expected, set(_STAGE_FIELDS.keys()))


# ---------------------------------------------------------------------------
# _extract_stage_result tests
# ---------------------------------------------------------------------------

class TestExtractStageResult(unittest.TestCase):
    """_extract_stage_result snapshots only the fields owned by a stage."""

    def _make_ctx(self, **kwargs):
        ctx = PipelineContext(audio_input=None)
        for k, v in kwargs.items():
            setattr(ctx, k, v)
        return ctx

    def test_stt_extracts_correct_fields(self):
        ctx = self._make_ctx(
            raw_text="Привет",
            confidence=0.88,
            model_used="balanced",
            language_detected="ru",
            segments=[{"start": 0.0, "end": 1.0, "text": "Привет"}],
        )
        snapshot = _extract_stage_result("stt", ctx)
        self.assertEqual(snapshot["raw_text"], "Привет")
        self.assertAlmostEqual(snapshot["confidence"], 0.88)
        self.assertEqual(snapshot["model_used"], "balanced")
        self.assertEqual(snapshot["language_detected"], "ru")
        self.assertIsInstance(snapshot["segments"], list)

    def test_text_cleanup_extracts_cleaned_text(self):
        ctx = self._make_ctx(cleaned_text="Clean sentence.")
        snapshot = _extract_stage_result("text_cleanup", ctx)
        self.assertEqual(snapshot["cleaned_text"], "Clean sentence.")
        # Should not contain fields from other stages
        self.assertNotIn("raw_text", snapshot)

    def test_llm_rewrite_extracts_llm_fields(self):
        ctx = self._make_ctx(
            rewritten_text="Better text",
            llm_applied=True,
            llm_fallback_reason=None,
            llm_latency_ms=320,
        )
        snapshot = _extract_stage_result("llm_rewrite", ctx)
        self.assertEqual(snapshot["rewritten_text"], "Better text")
        self.assertTrue(snapshot["llm_applied"])
        self.assertIsNone(snapshot["llm_fallback_reason"])
        self.assertEqual(snapshot["llm_latency_ms"], 320)

    def test_translation_extracts_translation_fields(self):
        ctx = self._make_ctx(translation="Hola mundo", translation_engine="offline")
        snapshot = _extract_stage_result("translation", ctx)
        self.assertEqual(snapshot["translation"], "Hola mundo")
        self.assertEqual(snapshot["translation_engine"], "offline")

    def test_diarization_extracts_speaker_fields(self):
        ctx = self._make_ctx(
            diarization={"segments": []},
            speaker_segments=[{"speaker": "A"}],
            num_speakers=2,
        )
        snapshot = _extract_stage_result("diarization", ctx)
        self.assertEqual(snapshot["num_speakers"], 2)
        self.assertEqual(len(snapshot["speaker_segments"]), 1)

    def test_unknown_stage_returns_empty_dict(self):
        ctx = self._make_ctx(raw_text="anything")
        snapshot = _extract_stage_result("nonexistent_stage", ctx)
        self.assertEqual(snapshot, {})

    def test_numpy_array_skipped_from_audio_normalization(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        ctx = PipelineContext(audio_input=None)
        ctx.normalized_audio = np.zeros(1000, dtype="float32")
        snapshot = _extract_stage_result("audio_normalization", ctx)
        # numpy arrays are too large — they must NOT appear in the snapshot
        self.assertNotIn("normalized_audio", snapshot)

    def test_none_value_included_in_snapshot(self):
        """None values (e.g. translation=None) are valid cache entries."""
        ctx = self._make_ctx(translation=None, translation_engine=None)
        snapshot = _extract_stage_result("translation", ctx)
        self.assertIn("translation", snapshot)
        self.assertIsNone(snapshot["translation"])

    def test_snapshot_does_not_contain_unrelated_fields(self):
        """stt snapshot must not bleed into text_cleanup fields."""
        ctx = self._make_ctx(raw_text="hello", cleaned_text="Hello.")
        stt_snap = _extract_stage_result("stt", ctx)
        self.assertNotIn("cleaned_text", stt_snap)


# ---------------------------------------------------------------------------
# _apply_cached_result tests
# ---------------------------------------------------------------------------

class TestApplyCachedResult(unittest.TestCase):
    """_apply_cached_result writes cached fields back onto ctx."""

    def test_known_fields_written_to_ctx(self):
        ctx = PipelineContext(audio_input=None)
        cached = {
            "raw_text": "Cached text",
            "confidence": 0.77,
            "model_used": "max",
            "language_detected": "es",
            "segments": [{"start": 0.0, "end": 2.0}],
        }
        result = _apply_cached_result("stt", cached, ctx)
        self.assertEqual(result.raw_text, "Cached text")
        self.assertAlmostEqual(result.confidence, 0.77)
        self.assertEqual(result.model_used, "max")
        self.assertEqual(result.language_detected, "es")

    def test_returns_same_ctx_object(self):
        ctx = PipelineContext(audio_input=None)
        result = _apply_cached_result("stt", {"raw_text": "x"}, ctx)
        self.assertIs(result, ctx)

    def test_unknown_keys_silently_ignored(self):
        """Keys not on PipelineContext must not raise AttributeError."""
        ctx = PipelineContext(audio_input=None)
        cached = {"_nonexistent_field_xyz": "value", "raw_text": "ok"}
        # Should not raise
        result = _apply_cached_result("stt", cached, ctx)
        self.assertEqual(result.raw_text, "ok")
        self.assertFalse(hasattr(ctx, "_nonexistent_field_xyz"))

    def test_empty_cached_dict_leaves_ctx_unchanged(self):
        ctx = PipelineContext(audio_input=None)
        ctx.raw_text = "original"
        result = _apply_cached_result("stt", {}, ctx)
        self.assertEqual(result.raw_text, "original")

    def test_llm_fields_restored(self):
        ctx = PipelineContext(audio_input=None)
        cached = {
            "rewritten_text": "Rewritten",
            "llm_applied": True,
            "llm_fallback_reason": None,
            "llm_latency_ms": 450,
        }
        result = _apply_cached_result("llm_rewrite", cached, ctx)
        self.assertEqual(result.rewritten_text, "Rewritten")
        self.assertTrue(result.llm_applied)
        self.assertEqual(result.llm_latency_ms, 450)

    def test_translation_fields_restored(self):
        ctx = PipelineContext(audio_input=None)
        cached = {"translation": "Hola", "translation_engine": "offline"}
        result = _apply_cached_result("translation", cached, ctx)
        self.assertEqual(result.translation, "Hola")
        self.assertEqual(result.translation_engine, "offline")


# ---------------------------------------------------------------------------
# _stage_had_error tests
# ---------------------------------------------------------------------------

class TestStageHadError(unittest.TestCase):
    """_stage_had_error checks if a stage prefix appears in ctx.errors."""

    def test_no_errors_returns_false(self):
        ctx = PipelineContext(audio_input=None)
        self.assertFalse(_stage_had_error("stt", ctx))

    def test_matching_error_prefix_returns_true(self):
        ctx = PipelineContext(audio_input=None)
        ctx.errors.append("stt: timeout while loading model")
        self.assertTrue(_stage_had_error("stt", ctx))

    def test_other_stage_error_does_not_trigger(self):
        ctx = PipelineContext(audio_input=None)
        ctx.errors.append("text_cleanup: normalization failed")
        self.assertFalse(_stage_had_error("stt", ctx))

    def test_multiple_errors_only_matching_counted(self):
        ctx = PipelineContext(audio_input=None)
        ctx.errors.append("diarization: model not found")
        ctx.errors.append("text_cleanup: stripped empty")
        self.assertFalse(_stage_had_error("stt", ctx))
        self.assertTrue(_stage_had_error("diarization", ctx))
        self.assertTrue(_stage_had_error("text_cleanup", ctx))

    def test_stage_exception_error_format_matched(self):
        """PipelineExecutor appends '{stage}_exception: {msg}' on unhandled exc."""
        ctx = PipelineContext(audio_input=None)
        ctx.errors.append("stt_exception: RuntimeError boom")
        # _stage_had_error uses prefix "{stage_name}:", but the executor
        # appends "{stage_name}_exception: {exc}". Verify the prefix detection
        # correctly identifies this as NOT the 'stt:' prefix.
        self.assertFalse(_stage_had_error("stt", ctx))
        # The actual prefix format the executor uses for exceptions:
        # ctx.errors.append(f"{stage.name}_exception: {exc}")
        # so stt_exception should match stage_name "stt_exception"
        self.assertTrue(_stage_had_error("stt_exception", ctx))

    def test_partial_prefix_not_matched(self):
        """'stt' prefix must not match 'stt_extra:' stage."""
        ctx = PipelineContext(audio_input=None)
        ctx.errors.append("stt_extra: something")
        # prefix is "stt:" — "stt_extra:" does not start with "stt:"
        self.assertFalse(_stage_had_error("stt", ctx))

    def test_empty_errors_list(self):
        ctx = PipelineContext(audio_input=None)
        ctx.errors = []
        self.assertFalse(_stage_had_error("translation", ctx))


# ---------------------------------------------------------------------------
# PipelineExecutor.to_legacy_dict additional edge cases
# ---------------------------------------------------------------------------

class TestToLegacyDictEdgeCases(unittest.TestCase):
    """Additional to_legacy_dict coverage beyond test_pipeline_core.py."""

    def _executor_run(self, **ctx_kwargs):
        ctx = PipelineContext(audio_input=None)
        for k, v in ctx_kwargs.items():
            setattr(ctx, k, v)
        executor = PipelineExecutor([])
        result = executor.run(ctx)
        return executor.to_legacy_dict(result)

    def test_diarization_dict_in_result(self):
        d = self._executor_run(
            diarization={"segments": [{"speaker": "A", "start": 0.0, "end": 1.0}]}
        )
        self.assertIsInstance(d["diarization"], dict)
        self.assertIn("segments", d["diarization"])

    def test_llm_applied_false_by_default(self):
        d = self._executor_run()
        self.assertFalse(d["llm_applied"])

    def test_llm_applied_true_when_set(self):
        d = self._executor_run(llm_applied=True)
        self.assertTrue(d["llm_applied"])

    def test_llm_latency_ms_none_by_default(self):
        d = self._executor_run()
        self.assertIsNone(d["llm_latency_ms"])

    def test_llm_latency_ms_value_propagated(self):
        d = self._executor_run(llm_latency_ms=512)
        self.assertEqual(d["llm_latency_ms"], 512)

    def test_llm_fallback_reason_none_by_default(self):
        d = self._executor_run()
        self.assertIsNone(d["llm_fallback_reason"])

    def test_llm_fallback_reason_propagated(self):
        d = self._executor_run(llm_fallback_reason="length_ratio")
        self.assertEqual(d["llm_fallback_reason"], "length_ratio")

    def test_model_field_propagated(self):
        d = self._executor_run(model_used="whisper-large-v3")
        self.assertEqual(d["model"], "whisper-large-v3")

    def test_language_field_propagated(self):
        d = self._executor_run(language_detected="ru")
        self.assertEqual(d["language"], "ru")

    def test_language_none_by_default(self):
        d = self._executor_run()
        self.assertIsNone(d["language"])

    def test_confidence_zero_by_default(self):
        d = self._executor_run()
        self.assertEqual(d["confidence"], 0.0)

    def test_duration_ms_is_int(self):
        d = self._executor_run()
        self.assertIsInstance(d["duration_ms"], int)

    def test_segments_empty_in_preview_mode(self):
        ctx = PipelineContext(audio_input=None, is_preview=True)
        ctx.segments = [{"start": 0.0, "end": 1.0, "text": "hi"}]
        executor = PipelineExecutor([])
        result = executor.run(ctx)
        d = executor.to_legacy_dict(result)
        self.assertEqual(d["segments"], [])

    def test_segments_returned_when_not_preview(self):
        ctx = PipelineContext(audio_input=None, is_preview=False)
        ctx.segments = [{"start": 0.0, "end": 1.0, "text": "hello"}]
        executor = PipelineExecutor([])
        result = executor.run(ctx)
        d = executor.to_legacy_dict(result)
        self.assertEqual(len(d["segments"]), 1)


# ---------------------------------------------------------------------------
# PipelineContext edge cases not in test_pipeline_core.py
# ---------------------------------------------------------------------------

class TestPipelineContextEdgeCases(unittest.TestCase):
    """Additional PipelineContext coverage."""

    def test_speaker_segments_default_empty(self):
        ctx = PipelineContext(audio_input=None)
        self.assertEqual(ctx.speaker_segments, [])

    def test_num_speakers_default_zero(self):
        ctx = PipelineContext(audio_input=None)
        self.assertEqual(ctx.num_speakers, 0)

    def test_rewritten_text_default_empty(self):
        ctx = PipelineContext(audio_input=None)
        self.assertEqual(ctx.rewritten_text, "")

    def test_model_used_default_empty(self):
        ctx = PipelineContext(audio_input=None)
        self.assertEqual(ctx.model_used, "")

    def test_language_detected_default_none(self):
        ctx = PipelineContext(audio_input=None)
        self.assertIsNone(ctx.language_detected)

    def test_translation_engine_default_none(self):
        ctx = PipelineContext(audio_input=None)
        self.assertIsNone(ctx.translation_engine)

    def test_cleaned_text_default_empty(self):
        ctx = PipelineContext(audio_input=None)
        self.assertEqual(ctx.cleaned_text, "")

    def test_normalized_audio_default_none(self):
        ctx = PipelineContext(audio_input=None)
        self.assertIsNone(ctx.normalized_audio)

    def test_final_text_precedence_rewritten_over_cleaned(self):
        """Executor sets final_text = rewritten > cleaned > raw."""
        ctx = PipelineContext(audio_input=None)
        ctx.raw_text = "raw"
        ctx.cleaned_text = "cleaned"
        ctx.rewritten_text = "rewritten"
        result = PipelineExecutor([]).run(ctx)
        self.assertEqual(result.final_text, "rewritten")

    def test_final_text_falls_back_to_cleaned(self):
        ctx = PipelineContext(audio_input=None)
        ctx.raw_text = "raw"
        ctx.cleaned_text = "cleaned"
        ctx.rewritten_text = ""
        result = PipelineExecutor([]).run(ctx)
        self.assertEqual(result.final_text, "cleaned")

    def test_final_text_falls_back_to_raw(self):
        ctx = PipelineContext(audio_input=None)
        ctx.raw_text = "raw"
        ctx.cleaned_text = ""
        ctx.rewritten_text = ""
        result = PipelineExecutor([]).run(ctx)
        self.assertEqual(result.final_text, "raw")

    def test_session_id_format_valid_uuid(self):
        import uuid
        ctx = PipelineContext(audio_input=None)
        parsed = uuid.UUID(ctx.session_id)
        self.assertIsNotNone(parsed)

    def test_extra_vocabulary_mutation_independent(self):
        ctx1 = PipelineContext(audio_input=None)
        ctx2 = PipelineContext(audio_input=None)
        ctx1.extra_vocabulary.append("термин")
        self.assertEqual(ctx2.extra_vocabulary, [])


# ---------------------------------------------------------------------------
# Stub stage helpers used by executor integration tests
# ---------------------------------------------------------------------------

class _StubStage:
    """Minimal stage that sets raw_text and is always cacheable."""

    name = "stt"
    cacheable = True

    def __init__(self, text="hello", should_run=True, raise_exc=False):
        self._text = text
        self._should_run = should_run
        self._raise_exc = raise_exc

    def should_run(self, ctx):
        return self._should_run

    def process(self, ctx):
        if self._raise_exc:
            raise RuntimeError("stage boom")
        ctx.raw_text = self._text
        ctx.model_used = "stub_model"
        ctx.confidence = 0.9
        ctx.language_detected = "ru"
        ctx.segments = [{"start": 0.0, "end": 1.0, "text": self._text}]
        return ctx


class _SideEffectStage:
    """Appends a marker to ctx.errors to simulate error reporting."""

    name = "text_cleanup"
    cacheable = False

    def should_run(self, ctx):
        return True

    def process(self, ctx):
        ctx.errors.append("text_cleanup: normalizer skipped empty")
        return ctx


# ---------------------------------------------------------------------------
# PipelineExecutor.run() with real stages
# ---------------------------------------------------------------------------

class TestExecutorRunWithStages(unittest.TestCase):
    """Cover executor.run() with stages (skipped, executed, exception)."""

    def test_stage_executed_sets_raw_text(self):
        stage = _StubStage(text="Привет мир")
        executor = PipelineExecutor([stage])
        ctx = PipelineContext(audio_input=None)
        result = executor.run(ctx)
        self.assertEqual(result.raw_text, "Привет мир")

    def test_stage_metrics_recorded(self):
        stage = _StubStage(text="test")
        executor = PipelineExecutor([stage])
        ctx = PipelineContext(audio_input=None)
        result = executor.run(ctx)
        self.assertEqual(len(result.stage_metrics), 1)
        self.assertEqual(result.stage_metrics[0].stage, "stt")
        self.assertIsNone(result.stage_metrics[0].error)

    def test_skipped_stage_records_skipped_metric(self):
        stage = _StubStage(should_run=False)
        executor = PipelineExecutor([stage])
        ctx = PipelineContext(audio_input=None)
        result = executor.run(ctx)
        self.assertEqual(len(result.stage_metrics), 1)
        metric = result.stage_metrics[0]
        self.assertTrue(metric.skipped)
        self.assertEqual(metric.duration_ms, 0)
        # Stage was skipped — raw_text should remain empty
        self.assertEqual(result.raw_text, "")

    def test_exception_in_stage_recorded_and_continues(self):
        """A stage that raises must record the error but not abort the run."""
        failing = _StubStage(raise_exc=True)
        # Second stage runs after the first fails
        ok_stage = _SideEffectStage()
        executor = PipelineExecutor([failing, ok_stage])
        ctx = PipelineContext(audio_input=None)
        result = executor.run(ctx)
        # Error from failing stage is captured
        self.assertTrue(any("stt_exception" in e for e in result.errors))
        # Second stage ran (it appended to errors with its prefix)
        self.assertTrue(any("text_cleanup:" in e for e in result.errors))
        # Metrics from both stages
        self.assertEqual(len(result.stage_metrics), 2)
        self.assertIsNotNone(result.stage_metrics[0].error)

    def test_multiple_stages_final_text_chain(self):
        """final_text should reflect full processing chain."""
        class _CleanupStage:
            name = "text_cleanup"
            cacheable = False
            def should_run(self, ctx): return True

            def process(self, ctx):
                ctx.cleaned_text = ctx.raw_text.strip().capitalize()
                return ctx

        executor = PipelineExecutor([_StubStage(text="  hi  "), _CleanupStage()])
        result = executor.run(PipelineContext(audio_input=None))
        # cleaned_text takes priority over raw_text when rewritten_text is empty
        self.assertEqual(result.final_text, "Hi")

    def test_empty_stages_list_run_returns_ctx(self):
        executor = PipelineExecutor([])
        ctx = PipelineContext(audio_input=None)
        ctx.raw_text = "original"
        result = executor.run(ctx)
        self.assertEqual(result.raw_text, "original")

    def test_exception_metric_has_error_message(self):
        stage = _StubStage(raise_exc=True)
        executor = PipelineExecutor([stage])
        result = executor.run(PipelineContext(audio_input=None))
        metric = result.stage_metrics[0]
        self.assertIsNotNone(metric.error)
        self.assertIn("boom", metric.error)


# ---------------------------------------------------------------------------
# PipelineExecutor._cleanup (temp file removal)
# ---------------------------------------------------------------------------

class TestExecutorCleanup(unittest.TestCase):
    """_cleanup removes ctx._temp_path if set."""

    def test_temp_path_deleted_after_run(self):
        import tempfile
        # Create a real temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
            tmp = f.name

        ctx = PipelineContext(audio_input=None)
        ctx._temp_path = tmp
        executor = PipelineExecutor([])
        executor.run(ctx)
        # _cleanup should have removed the file
        self.assertFalse(os.path.exists(tmp), "Temp file should be deleted by _cleanup")
        self.assertIsNone(ctx._temp_path)

    def test_cleanup_tolerates_missing_file(self):
        ctx = PipelineContext(audio_input=None)
        ctx._temp_path = "/nonexistent/path/file_xyz.wav"
        executor = PipelineExecutor([])
        # Must NOT raise even if file is already gone
        try:
            executor.run(ctx)
        except OSError:
            self.fail("_cleanup should not propagate OSError for missing temp file")

    def test_cleanup_no_temp_path_is_noop(self):
        ctx = PipelineContext(audio_input=None)
        self.assertIsNone(ctx._temp_path)
        executor = PipelineExecutor([])
        executor.run(ctx)  # Must not raise


# ---------------------------------------------------------------------------
# PipelineExecutor with StageCache integration
# ---------------------------------------------------------------------------

class TestExecutorWithCache(unittest.TestCase):
    """Cover cache hit / cache save paths in executor.run()."""

    def test_cache_miss_executes_stage_and_saves(self):
        from core.pipeline.stage_cache import StageCache
        cache = StageCache()
        stage = _StubStage(text="cached result")
        executor = PipelineExecutor([stage], cache=cache)
        ctx = PipelineContext(audio_input=b"fake_audio_bytes")
        result = executor.run(ctx)
        self.assertEqual(result.raw_text, "cached result")
        # Cache should now have an entry for 'stt'
        stats = cache.get_stats()
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["total_entries"], 1)

    def test_cache_hit_skips_stage_execution(self):
        from core.pipeline.stage_cache import StageCache
        cache = StageCache()
        audio = b"deterministic_bytes"
        audio_hash = StageCache.compute_hash(audio)

        # Pre-populate cache with a known result
        cached_data = {
            "raw_text": "from cache",
            "language_detected": "es",
            "model_used": "cached_model",
            "confidence": 0.99,
            "segments": [],
        }
        cache.put("stt", audio_hash, cached_data)

        class _NeverRunStage(_StubStage):
            def process(self, ctx):
                raise AssertionError("process() must not be called on cache hit")

        stage = _NeverRunStage()
        executor = PipelineExecutor([stage], cache=cache)
        ctx = PipelineContext(audio_input=audio)
        result = executor.run(ctx)

        self.assertEqual(result.raw_text, "from cache")
        self.assertEqual(result.language_detected, "es")
        self.assertEqual(result.model_used, "cached_model")
        stats = cache.get_stats()
        self.assertEqual(stats["hits"], 1)

    def test_stage_with_error_not_cached(self):
        """If a stage appends an error, its result must NOT be saved to cache."""
        from core.pipeline.stage_cache import StageCache

        class _ErrorStage:
            name = "stt"
            cacheable = True
            def should_run(self, ctx): return True

            def process(self, ctx):
                ctx.raw_text = "partial"
                ctx.errors.append("stt: model failed")
                return ctx

        cache = StageCache()
        executor = PipelineExecutor([_ErrorStage()], cache=cache)
        ctx = PipelineContext(audio_input=b"audio")
        executor.run(ctx)
        stats = cache.get_stats()
        # Nothing should be saved
        self.assertEqual(stats["total_entries"], 0)

    def test_no_cache_still_runs_normally(self):
        """executor without cache must run stages as before."""
        stage = _StubStage(text="no cache")
        executor = PipelineExecutor([stage])
        result = executor.run(PipelineContext(audio_input=b"bytes"))
        self.assertEqual(result.raw_text, "no cache")


# ---------------------------------------------------------------------------
# StageCache unit tests
# ---------------------------------------------------------------------------

class TestStageCacheComputeHash(unittest.TestCase):
    """StageCache.compute_hash() for all input types."""

    def test_bytes_input(self):
        h = StageCache.compute_hash(b"hello")
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 64)  # SHA-256 hex

    def test_str_input(self):
        h = StageCache.compute_hash("hello")
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 64)

    def test_dict_input(self):
        h = StageCache.compute_hash({"key": "value"})
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 64)

    def test_list_input(self):
        h = StageCache.compute_hash([1, 2, 3])
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 64)

    def test_deterministic_for_same_bytes(self):
        h1 = StageCache.compute_hash(b"audio_data")
        h2 = StageCache.compute_hash(b"audio_data")
        self.assertEqual(h1, h2)

    def test_different_inputs_different_hashes(self):
        h1 = StageCache.compute_hash(b"audio_a")
        h2 = StageCache.compute_hash(b"audio_b")
        self.assertNotEqual(h1, h2)

    def test_fallback_for_int(self):
        # int is not bytes/str/dict/list — hits repr() fallback
        h = StageCache.compute_hash(42)
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 64)


# Import StageCache at module level for the hash tests above
from core.pipeline.stage_cache import StageCache  # noqa: E402


class TestStageCacheGetPut(unittest.TestCase):
    """StageCache.get() / .put() behaviour."""

    def _new_cache(self):
        return StageCache(max_entries=5)

    def test_get_on_empty_cache_returns_none(self):
        cache = self._new_cache()
        self.assertIsNone(cache.get("stt", "somehash"))

    def test_put_then_get_returns_result(self):
        cache = self._new_cache()
        cache.put("stt", "h1", {"raw_text": "hello"})
        result = cache.get("stt", "h1")
        self.assertIsNotNone(result)
        self.assertEqual(result["raw_text"], "hello")

    def test_get_wrong_stage_returns_none(self):
        cache = self._new_cache()
        cache.put("stt", "h1", {"raw_text": "hi"})
        self.assertIsNone(cache.get("text_cleanup", "h1"))

    def test_lru_eviction_when_full(self):
        cache = StageCache(max_entries=3)
        cache.put("stt", "h1", {"v": 1})
        cache.put("stt", "h2", {"v": 2})
        cache.put("stt", "h3", {"v": 3})
        # Adding 4th should evict h1 (LRU)
        cache.put("stt", "h4", {"v": 4})
        self.assertIsNone(cache.get("stt", "h1"), "h1 should have been evicted")
        self.assertIsNotNone(cache.get("stt", "h4"))

    def test_invalidate_single_stage(self):
        cache = self._new_cache()
        cache.put("stt", "h1", {"raw_text": "x"})
        cache.put("text_cleanup", "h2", {"cleaned_text": "y"})
        cache.invalidate("stt")
        self.assertIsNone(cache.get("stt", "h1"))
        self.assertIsNotNone(cache.get("text_cleanup", "h2"))

    def test_invalidate_all_stages(self):
        cache = self._new_cache()
        cache.put("stt", "h1", {"raw_text": "x"})
        cache.put("text_cleanup", "h2", {"cleaned_text": "y"})
        cache.invalidate()
        self.assertIsNone(cache.get("stt", "h1"))
        self.assertIsNone(cache.get("text_cleanup", "h2"))

    def test_get_stats_hit_rate(self):
        cache = self._new_cache()
        cache.put("stt", "h1", {"raw_text": "x"})
        cache.get("stt", "h1")   # hit
        cache.get("stt", "miss")  # miss
        stats = cache.get_stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)
        self.assertAlmostEqual(stats["hit_rate"], 0.5)
        self.assertEqual(stats["total_entries"], 1)

    def test_reset_stats(self):
        cache = self._new_cache()
        cache.put("stt", "h1", {"raw_text": "x"})
        cache.get("stt", "h1")
        cache.reset_stats()
        stats = cache.get_stats()
        self.assertEqual(stats["hits"], 0)
        self.assertEqual(stats["misses"], 0)

    def test_put_updates_existing_entry(self):
        cache = self._new_cache()
        cache.put("stt", "h1", {"raw_text": "old"})
        cache.put("stt", "h1", {"raw_text": "new"})
        result = cache.get("stt", "h1")
        self.assertEqual(result["raw_text"], "new")

    def test_expired_entry_returns_none(self):
        import time
        cache = self._new_cache()
        # Put with TTL=0 → instantly expired
        cache.put("stt", "h1", {"raw_text": "x"}, ttl_sec=0)
        time.sleep(0.01)  # ensure monotonic advances
        result = cache.get("stt", "h1")
        self.assertIsNone(result, "Expired entry must return None")
        # Miss should be counted
        stats = cache.get_stats()
        self.assertGreater(stats["misses"], 0)


if __name__ == "__main__":
    unittest.main()
