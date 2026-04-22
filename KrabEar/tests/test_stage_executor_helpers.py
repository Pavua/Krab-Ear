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


if __name__ == "__main__":
    unittest.main()
