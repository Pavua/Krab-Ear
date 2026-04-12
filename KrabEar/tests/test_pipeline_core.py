"""Tests for PipelineContext, PipelineStage protocol, and PipelineExecutor."""

import sys
import os
import unittest
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.pipeline.context import PipelineContext, StageMetric
from core.pipeline.base import PipelineStage
from core.pipeline.executor import PipelineExecutor


# ---------------------------------------------------------------------------
# Helpers / fake stages
# ---------------------------------------------------------------------------

class AppendStage:
    """Appends a word to raw_text."""
    def __init__(self, name: str, word: str):
        self._name = name
        self._word = word

    @property
    def name(self) -> str:
        return self._name

    def should_run(self, ctx: PipelineContext) -> bool:
        return True

    def process(self, ctx: PipelineContext) -> PipelineContext:
        ctx.raw_text = (ctx.raw_text + " " + self._word).strip()
        return ctx


class SkipAlwaysStage:
    """A stage that always returns False from should_run."""
    @property
    def name(self) -> str:
        return "skip_always"

    def should_run(self, ctx: PipelineContext) -> bool:
        return False

    def process(self, ctx: PipelineContext) -> PipelineContext:
        ctx.raw_text = "SHOULD_NOT_BE_WRITTEN"
        return ctx


class ConditionalStage:
    """Runs only when is_preview is False."""
    @property
    def name(self) -> str:
        return "conditional"

    def should_run(self, ctx: PipelineContext) -> bool:
        return not ctx.is_preview

    def process(self, ctx: PipelineContext) -> PipelineContext:
        ctx.raw_text = "ran"
        return ctx


class ExplodingStage:
    """Raises an exception during process()."""
    @property
    def name(self) -> str:
        return "exploding"

    def should_run(self, ctx: PipelineContext) -> bool:
        return True

    def process(self, ctx: PipelineContext) -> PipelineContext:
        raise RuntimeError("boom")


class SetCleanedStage:
    """Sets cleaned_text from raw_text (simulate text cleanup)."""
    @property
    def name(self) -> str:
        return "set_cleaned"

    def should_run(self, ctx: PipelineContext) -> bool:
        return True

    def process(self, ctx: PipelineContext) -> PipelineContext:
        ctx.cleaned_text = ctx.raw_text.upper()
        return ctx


class SetRewrittenStage:
    """Sets rewritten_text."""
    @property
    def name(self) -> str:
        return "set_rewritten"

    def should_run(self, ctx: PipelineContext) -> bool:
        return True

    def process(self, ctx: PipelineContext) -> PipelineContext:
        ctx.rewritten_text = "rewritten"
        return ctx


class TempPathStage:
    """Sets _temp_path to a temp file."""
    def __init__(self, path: str):
        self._path = path

    @property
    def name(self) -> str:
        return "temp_path"

    def should_run(self, ctx: PipelineContext) -> bool:
        return True

    def process(self, ctx: PipelineContext) -> PipelineContext:
        ctx._temp_path = self._path
        return ctx


# ---------------------------------------------------------------------------
# PipelineContext tests
# ---------------------------------------------------------------------------

class TestPipelineContextCreation(unittest.TestCase):

    def test_required_field_audio_input(self):
        ctx = PipelineContext(audio_input="audio.wav")
        self.assertEqual(ctx.audio_input, "audio.wav")

    def test_default_cleanup_profile(self):
        ctx = PipelineContext(audio_input=None)
        self.assertEqual(ctx.cleanup_profile, "soft")

    def test_default_is_preview_false(self):
        ctx = PipelineContext(audio_input=None)
        self.assertFalse(ctx.is_preview)

    def test_default_domain_casual(self):
        ctx = PipelineContext(audio_input=None)
        self.assertEqual(ctx.domain, "casual")

    def test_default_lang_hint_none(self):
        ctx = PipelineContext(audio_input=None)
        self.assertIsNone(ctx.lang_hint)

    def test_default_translation_mode_off(self):
        ctx = PipelineContext(audio_input=None)
        self.assertEqual(ctx.translation_mode, "off")

    def test_default_raw_text_empty(self):
        ctx = PipelineContext(audio_input=None)
        self.assertEqual(ctx.raw_text, "")

    def test_default_confidence_zero(self):
        ctx = PipelineContext(audio_input=None)
        self.assertEqual(ctx.confidence, 0.0)

    def test_default_final_text_empty(self):
        ctx = PipelineContext(audio_input=None)
        self.assertEqual(ctx.final_text, "")

    def test_session_id_is_uuid_string(self):
        ctx = PipelineContext(audio_input=None)
        import uuid
        # Should not raise
        uuid.UUID(ctx.session_id)

    def test_two_contexts_have_different_session_ids(self):
        ctx1 = PipelineContext(audio_input=None)
        ctx2 = PipelineContext(audio_input=None)
        self.assertNotEqual(ctx1.session_id, ctx2.session_id)

    def test_created_at_is_recent(self):
        before = time.time()
        ctx = PipelineContext(audio_input=None)
        after = time.time()
        self.assertGreaterEqual(ctx.created_at, before)
        self.assertLessEqual(ctx.created_at, after)

    def test_stage_metrics_default_empty_list(self):
        ctx = PipelineContext(audio_input=None)
        self.assertEqual(ctx.stage_metrics, [])

    def test_errors_default_empty_list(self):
        ctx = PipelineContext(audio_input=None)
        self.assertEqual(ctx.errors, [])

    def test_mutable_defaults_are_independent(self):
        ctx1 = PipelineContext(audio_input=None)
        ctx2 = PipelineContext(audio_input=None)
        ctx1.errors.append("err")
        self.assertEqual(ctx2.errors, [])

    def test_temp_path_default_none(self):
        ctx = PipelineContext(audio_input=None)
        self.assertIsNone(ctx._temp_path)

    def test_extra_vocabulary_default_empty(self):
        ctx = PipelineContext(audio_input=None)
        self.assertEqual(ctx.extra_vocabulary, [])

    def test_segments_default_empty(self):
        ctx = PipelineContext(audio_input=None)
        self.assertEqual(ctx.segments, [])

    def test_diarization_default_empty_dict(self):
        ctx = PipelineContext(audio_input=None)
        self.assertEqual(ctx.diarization, {})

    def test_llm_fields_defaults(self):
        ctx = PipelineContext(audio_input=None)
        self.assertFalse(ctx.llm_applied)
        self.assertIsNone(ctx.llm_fallback_reason)
        self.assertIsNone(ctx.llm_latency_ms)
        self.assertIsNone(ctx.translation)

    def test_override_fields(self):
        ctx = PipelineContext(
            audio_input="file.wav",
            cleanup_profile="strict",
            is_preview=True,
            domain="meeting",
            lang_hint="ru",
            translation_mode="auto",
        )
        self.assertEqual(ctx.cleanup_profile, "strict")
        self.assertTrue(ctx.is_preview)
        self.assertEqual(ctx.domain, "meeting")
        self.assertEqual(ctx.lang_hint, "ru")
        self.assertEqual(ctx.translation_mode, "auto")


# ---------------------------------------------------------------------------
# StageMetric tests
# ---------------------------------------------------------------------------

class TestStageMetric(unittest.TestCase):

    def test_basic_creation(self):
        m = StageMetric(stage="stt", duration_ms=150)
        self.assertEqual(m.stage, "stt")
        self.assertEqual(m.duration_ms, 150)
        self.assertFalse(m.skipped)
        self.assertIsNone(m.error)

    def test_skipped_metric(self):
        m = StageMetric(stage="diarization", duration_ms=0, skipped=True)
        self.assertTrue(m.skipped)

    def test_error_metric(self):
        m = StageMetric(stage="stt", duration_ms=10, error="timeout")
        self.assertEqual(m.error, "timeout")


# ---------------------------------------------------------------------------
# PipelineStage protocol tests
# ---------------------------------------------------------------------------

class TestPipelineStageProtocol(unittest.TestCase):

    def test_append_stage_is_protocol_instance(self):
        stage = AppendStage("a", "word")
        self.assertIsInstance(stage, PipelineStage)

    def test_skip_always_stage_is_protocol_instance(self):
        self.assertIsInstance(SkipAlwaysStage(), PipelineStage)

    def test_object_without_name_is_not_protocol(self):
        class BadStage:
            def should_run(self, ctx):
                return True
            def process(self, ctx):
                return ctx
        # Missing 'name' property — not a PipelineStage
        self.assertNotIsInstance(BadStage(), PipelineStage)

    def test_object_without_process_is_not_protocol(self):
        class BadStage:
            @property
            def name(self):
                return "bad"
            def should_run(self, ctx):
                return True
        self.assertNotIsInstance(BadStage(), PipelineStage)


# ---------------------------------------------------------------------------
# PipelineExecutor basic tests
# ---------------------------------------------------------------------------

class TestPipelineExecutorBasic(unittest.TestCase):

    def test_empty_stages_returns_context(self):
        ctx = PipelineContext(audio_input=None)
        result = PipelineExecutor([]).run(ctx)
        self.assertIsInstance(result, PipelineContext)

    def test_single_stage_modifies_context(self):
        ctx = PipelineContext(audio_input=None)
        result = PipelineExecutor([AppendStage("s1", "hello")]).run(ctx)
        self.assertEqual(result.raw_text, "hello")

    def test_multiple_stages_run_in_order(self):
        ctx = PipelineContext(audio_input=None)
        stages = [AppendStage("s1", "foo"), AppendStage("s2", "bar")]
        result = PipelineExecutor(stages).run(ctx)
        self.assertEqual(result.raw_text, "foo bar")

    def test_final_text_set_to_raw_when_no_cleanup(self):
        ctx = PipelineContext(audio_input=None)
        result = PipelineExecutor([AppendStage("s1", "hello")]).run(ctx)
        self.assertEqual(result.final_text, "hello")

    def test_final_text_prefers_cleaned_over_raw(self):
        ctx = PipelineContext(audio_input=None)
        stages = [AppendStage("s1", "hello"), SetCleanedStage()]
        result = PipelineExecutor(stages).run(ctx)
        self.assertEqual(result.final_text, "HELLO")

    def test_final_text_prefers_rewritten_over_cleaned(self):
        ctx = PipelineContext(audio_input=None)
        stages = [AppendStage("s1", "hello"), SetCleanedStage(), SetRewrittenStage()]
        result = PipelineExecutor(stages).run(ctx)
        self.assertEqual(result.final_text, "rewritten")


# ---------------------------------------------------------------------------
# PipelineExecutor timing tests
# ---------------------------------------------------------------------------

class TestPipelineExecutorTiming(unittest.TestCase):

    def test_metrics_recorded_for_each_stage(self):
        ctx = PipelineContext(audio_input=None)
        stages = [AppendStage("s1", "a"), AppendStage("s2", "b")]
        result = PipelineExecutor(stages).run(ctx)
        self.assertEqual(len(result.stage_metrics), 2)

    def test_metric_stage_names_correct(self):
        ctx = PipelineContext(audio_input=None)
        stages = [AppendStage("alpha", "a"), AppendStage("beta", "b")]
        result = PipelineExecutor(stages).run(ctx)
        names = [m.stage for m in result.stage_metrics]
        self.assertEqual(names, ["alpha", "beta"])

    def test_metric_duration_ms_non_negative(self):
        ctx = PipelineContext(audio_input=None)
        result = PipelineExecutor([AppendStage("s1", "x")]).run(ctx)
        self.assertGreaterEqual(result.stage_metrics[0].duration_ms, 0)

    def test_skipped_metric_duration_is_zero(self):
        ctx = PipelineContext(audio_input=None)
        result = PipelineExecutor([SkipAlwaysStage()]).run(ctx)
        self.assertEqual(result.stage_metrics[0].duration_ms, 0)
        self.assertTrue(result.stage_metrics[0].skipped)

    def test_to_legacy_dict_duration_ms_is_sum(self):
        ctx = PipelineContext(audio_input=None)
        stages = [AppendStage("s1", "a"), AppendStage("s2", "b")]
        executor = PipelineExecutor(stages)
        result = executor.run(ctx)
        legacy = executor.to_legacy_dict(result)
        expected_sum = sum(m.duration_ms for m in result.stage_metrics)
        self.assertEqual(legacy["duration_ms"], expected_sum)


# ---------------------------------------------------------------------------
# PipelineExecutor should_run / skip tests
# ---------------------------------------------------------------------------

class TestPipelineExecutorSkip(unittest.TestCase):

    def test_skipped_stage_not_modifying_context(self):
        ctx = PipelineContext(audio_input=None)
        ctx.raw_text = "original"
        result = PipelineExecutor([SkipAlwaysStage()]).run(ctx)
        self.assertEqual(result.raw_text, "original")

    def test_skipped_stage_recorded_in_metrics(self):
        ctx = PipelineContext(audio_input=None)
        result = PipelineExecutor([SkipAlwaysStage()]).run(ctx)
        self.assertEqual(len(result.stage_metrics), 1)
        self.assertTrue(result.stage_metrics[0].skipped)

    def test_conditional_stage_runs_when_not_preview(self):
        ctx = PipelineContext(audio_input=None, is_preview=False)
        result = PipelineExecutor([ConditionalStage()]).run(ctx)
        self.assertEqual(result.raw_text, "ran")

    def test_conditional_stage_skipped_when_preview(self):
        ctx = PipelineContext(audio_input=None, is_preview=True)
        result = PipelineExecutor([ConditionalStage()]).run(ctx)
        self.assertEqual(result.raw_text, "")
        self.assertTrue(result.stage_metrics[0].skipped)

    def test_mix_of_skipped_and_active_stages(self):
        ctx = PipelineContext(audio_input=None)
        stages = [SkipAlwaysStage(), AppendStage("s2", "hello")]
        result = PipelineExecutor(stages).run(ctx)
        self.assertEqual(result.raw_text, "hello")
        self.assertTrue(result.stage_metrics[0].skipped)
        self.assertFalse(result.stage_metrics[1].skipped)


# ---------------------------------------------------------------------------
# PipelineExecutor error handling tests
# ---------------------------------------------------------------------------

class TestPipelineExecutorErrorHandling(unittest.TestCase):

    def test_exploding_stage_appends_error(self):
        ctx = PipelineContext(audio_input=None)
        result = PipelineExecutor([ExplodingStage()]).run(ctx)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("exploding_exception", result.errors[0])
        self.assertIn("boom", result.errors[0])

    def test_exploding_stage_records_metric_with_error(self):
        ctx = PipelineContext(audio_input=None)
        result = PipelineExecutor([ExplodingStage()]).run(ctx)
        metric = result.stage_metrics[0]
        self.assertEqual(metric.stage, "exploding")
        self.assertIsNotNone(metric.error)
        self.assertIn("boom", metric.error)

    def test_pipeline_continues_after_exploding_stage(self):
        ctx = PipelineContext(audio_input=None)
        stages = [ExplodingStage(), AppendStage("s2", "after_error")]
        result = PipelineExecutor(stages).run(ctx)
        self.assertEqual(result.raw_text, "after_error")

    def test_multiple_errors_accumulated(self):
        ctx = PipelineContext(audio_input=None)
        stages = [ExplodingStage(), ExplodingStage()]
        result = PipelineExecutor(stages).run(ctx)
        self.assertEqual(len(result.errors), 2)

    def test_no_error_on_clean_run(self):
        ctx = PipelineContext(audio_input=None)
        result = PipelineExecutor([AppendStage("s1", "ok")]).run(ctx)
        self.assertEqual(result.errors, [])


# ---------------------------------------------------------------------------
# PipelineExecutor temp cleanup tests
# ---------------------------------------------------------------------------

class TestPipelineExecutorCleanup(unittest.TestCase):

    def test_temp_path_deleted_after_run(self):
        import tempfile
        # Create a real temp file
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp = f.name

        self.assertTrue(os.path.exists(tmp))
        ctx = PipelineContext(audio_input=None)
        result = PipelineExecutor([TempPathStage(tmp)]).run(ctx)
        self.assertFalse(os.path.exists(tmp))
        self.assertIsNone(result._temp_path)

    def test_missing_temp_path_does_not_raise(self):
        ctx = PipelineContext(audio_input=None)
        ctx._temp_path = "/nonexistent/path/xyz_12345.wav"
        # Should not raise — OSError is suppressed
        PipelineExecutor([]).run(ctx)

    def test_temp_path_cleaned_even_on_stage_exception(self):
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp = f.name

        ctx = PipelineContext(audio_input=None)
        # Set temp path first, then explode
        stages = [TempPathStage(tmp), ExplodingStage()]
        result = PipelineExecutor(stages).run(ctx)
        self.assertFalse(os.path.exists(tmp))


# ---------------------------------------------------------------------------
# to_legacy_dict tests
# ---------------------------------------------------------------------------

class TestToLegacyDict(unittest.TestCase):

    def _run_with_text(self, raw="hello", cleaned="Hello", rewritten=""):
        ctx = PipelineContext(audio_input=None)
        ctx.raw_text = raw
        ctx.cleaned_text = cleaned
        ctx.rewritten_text = rewritten
        executor = PipelineExecutor([])
        result = executor.run(ctx)
        return executor.to_legacy_dict(result), result

    def test_text_field_is_final_text(self):
        d, ctx = self._run_with_text(raw="hello", cleaned="Hello")
        self.assertEqual(d["text"], ctx.final_text)

    def test_raw_text_field_present(self):
        d, _ = self._run_with_text()
        self.assertEqual(d["raw_text"], "hello")

    def test_cleaned_text_field_present(self):
        d, _ = self._run_with_text(cleaned="Hello")
        self.assertEqual(d["cleaned_text"], "Hello")

    def test_engine_field_is_pipeline_v2(self):
        d, _ = self._run_with_text()
        self.assertEqual(d["engine"], "pipeline_v2")

    def test_confidence_rounded_to_3(self):
        ctx = PipelineContext(audio_input=None)
        ctx.confidence = 0.123456789
        executor = PipelineExecutor([])
        result = executor.run(ctx)
        d = executor.to_legacy_dict(result)
        self.assertEqual(d["confidence"], 0.123)

    def test_segments_empty_when_preview(self):
        ctx = PipelineContext(audio_input=None, is_preview=True)
        ctx.segments = [{"start": 0, "end": 1, "text": "hi"}]
        executor = PipelineExecutor([])
        result = executor.run(ctx)
        d = executor.to_legacy_dict(result)
        self.assertEqual(d["segments"], [])

    def test_segments_present_when_not_preview(self):
        ctx = PipelineContext(audio_input=None, is_preview=False)
        ctx.segments = [{"start": 0, "end": 1, "text": "hi"}]
        executor = PipelineExecutor([])
        result = executor.run(ctx)
        d = executor.to_legacy_dict(result)
        self.assertEqual(len(d["segments"]), 1)

    def test_all_required_keys_present(self):
        d, _ = self._run_with_text()
        required = {
            "text", "raw_text", "cleaned_text", "llm_applied",
            "llm_latency_ms", "llm_fallback_reason", "confidence",
            "duration_ms", "engine", "model", "language",
            "segments", "diarization",
        }
        self.assertEqual(required, set(d.keys()))


if __name__ == "__main__":
    unittest.main()
