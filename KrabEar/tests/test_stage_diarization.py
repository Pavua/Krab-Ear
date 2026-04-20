"""Tests for DiarizationStage."""

from core.pipeline.context import PipelineContext
from core.pipeline.stages.diarization import DiarizationStage
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Path setup
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))


def _ctx(**kwargs) -> PipelineContext:
    """Создаёт минимальный контекст для тестов."""
    defaults = {"audio_input": "/tmp/test_audio.wav"}
    defaults.update(kwargs)
    return PipelineContext(**defaults)


def _fake_diarization(segments):
    """Возвращает функцию-заглушку диаризации."""
    def fn(audio_path):
        return segments
    return fn


SAMPLE_SEGMENTS = [
    {"start": 0.0, "end": 2.5, "speaker": "SPEAKER_00"},
    {"start": 2.5, "end": 5.0, "speaker": "SPEAKER_01"},
    {"start": 5.0, "end": 8.0, "speaker": "SPEAKER_00"},
]


class TestDiarizationStageBasic(unittest.TestCase):

    def test_name(self):
        stage = DiarizationStage()
        self.assertEqual(stage.name, "diarization")

    def test_should_run_false_when_no_fn(self):
        stage = DiarizationStage(diarization_fn=None)
        ctx = _ctx(audio_input="/tmp/audio.wav")
        with patch("core.pipeline.stages.diarization._app_settings", create=True) as mock_settings:
            mock_settings.DIARIZATION_ENABLED = True
            # Even with settings enabled, no fn → False
            self.assertFalse(stage.should_run(ctx))

    def test_should_run_false_in_preview_mode(self):
        stage = DiarizationStage(diarization_fn=_fake_diarization([]))
        ctx = _ctx(audio_input="/tmp/audio.wav", is_preview=True)
        self.assertFalse(stage.should_run(ctx))

    def test_should_run_false_when_audio_is_ndarray(self):
        import numpy as np
        stage = DiarizationStage(diarization_fn=_fake_diarization([]))
        ctx = _ctx(audio_input=np.zeros(1000, dtype="float32"))
        # ndarray — путь не определить, should_run = False
        with patch("core.pipeline.stages.diarization._app_settings", create=True) as mock_settings:
            mock_settings.DIARIZATION_ENABLED = True
            self.assertFalse(stage.should_run(ctx))

    def test_should_run_true_with_file_path(self):
        stage = DiarizationStage(diarization_fn=_fake_diarization([]))
        ctx = _ctx(audio_input="/tmp/audio.wav")
        with patch("core.pipeline.stages.diarization._app_settings", create=True) as mock_settings:
            mock_settings.DIARIZATION_ENABLED = True
            self.assertTrue(stage.should_run(ctx))

    def test_should_run_true_uses_normalized_audio_first(self):
        stage = DiarizationStage(diarization_fn=_fake_diarization([]))
        ctx = _ctx(audio_input=None, normalized_audio="/tmp/normalized.wav")
        with patch("core.pipeline.stages.diarization._app_settings", create=True) as mock_settings:
            mock_settings.DIARIZATION_ENABLED = True
            self.assertTrue(stage.should_run(ctx))

    def test_should_run_false_when_settings_disabled(self):
        stage = DiarizationStage(diarization_fn=_fake_diarization([]))
        ctx = _ctx(audio_input="/tmp/audio.wav")
        with patch("core.pipeline.stages.diarization._app_settings", create=True) as mock_settings:
            mock_settings.DIARIZATION_ENABLED = False
            self.assertFalse(stage.should_run(ctx))


class TestDiarizationStageProcess(unittest.TestCase):

    def test_process_fills_speaker_segments(self):
        stage = DiarizationStage(diarization_fn=_fake_diarization(SAMPLE_SEGMENTS))
        ctx = _ctx(audio_input="/tmp/audio.wav")
        result = stage.process(ctx)
        self.assertEqual(result.speaker_segments, SAMPLE_SEGMENTS)

    def test_process_fills_num_speakers(self):
        stage = DiarizationStage(diarization_fn=_fake_diarization(SAMPLE_SEGMENTS))
        ctx = _ctx(audio_input="/tmp/audio.wav")
        result = stage.process(ctx)
        # SPEAKER_00 and SPEAKER_01
        self.assertEqual(result.num_speakers, 2)

    def test_process_fills_diarization_dict(self):
        stage = DiarizationStage(diarization_fn=_fake_diarization(SAMPLE_SEGMENTS))
        ctx = _ctx(audio_input="/tmp/audio.wav")
        result = stage.process(ctx)
        self.assertTrue(result.diarization.get("enabled"))
        self.assertEqual(result.diarization["speaker_segments"], SAMPLE_SEGMENTS)
        self.assertEqual(result.diarization["num_speakers"], 2)

    def test_process_empty_segments(self):
        stage = DiarizationStage(diarization_fn=_fake_diarization([]))
        ctx = _ctx(audio_input="/tmp/audio.wav")
        result = stage.process(ctx)
        self.assertEqual(result.speaker_segments, [])
        self.assertEqual(result.num_speakers, 0)
        self.assertTrue(result.diarization.get("enabled"))

    def test_process_graceful_on_error(self):
        def failing_fn(path):
            raise RuntimeError("pyannote unavailable")

        stage = DiarizationStage(diarization_fn=failing_fn)
        ctx = _ctx(audio_input="/tmp/audio.wav")
        # Should NOT raise
        result = stage.process(ctx)
        self.assertFalse(result.diarization.get("enabled"))
        self.assertTrue(any("diarization" in e for e in result.errors))
        self.assertEqual(result.speaker_segments, [])

    def test_process_uses_normalized_audio_path(self):
        called_with = []

        def recording_fn(path):
            called_with.append(path)
            return []

        stage = DiarizationStage(diarization_fn=recording_fn)
        ctx = _ctx(audio_input="/tmp/raw.wav", normalized_audio="/tmp/normalized.wav")
        stage.process(ctx)
        self.assertTrue(len(called_with) == 1)
        self.assertIn("normalized", called_with[0])

    def test_process_no_audio_path_appends_error(self):
        stage = DiarizationStage(diarization_fn=_fake_diarization([]))
        ctx = _ctx(audio_input=None, normalized_audio=None)
        result = stage.process(ctx)
        self.assertTrue(any("путь" in e or "diarization" in e for e in result.errors))

    def test_implements_pipeline_stage_protocol(self):
        from core.pipeline.base import PipelineStage
        stage = DiarizationStage(diarization_fn=_fake_diarization([]))
        self.assertIsInstance(stage, PipelineStage)


if __name__ == "__main__":
    unittest.main()


class TestDiarizationEdgeCases(unittest.TestCase):
    """Edge cases: single speaker, overlapping speakers, extreme timings."""

    def test_single_speaker_segment_entire_audio(self):
        # One speaker for entire duration
        segments = [{"start": 0.0, "end": 60.0, "speaker": "SPEAKER_00"}]
        stage = DiarizationStage(diarization_fn=_fake_diarization(segments))
        ctx = _ctx(audio_input="/tmp/audio.wav")
        result = stage.process(ctx)
        self.assertEqual(result.num_speakers, 1)
        self.assertEqual(len(result.speaker_segments), 1)

    def test_single_short_speaker_segment(self):
        # Very short segment
        segments = [{"start": 0.0, "end": 0.1, "speaker": "SPEAKER_00"}]
        stage = DiarizationStage(diarization_fn=_fake_diarization(segments))
        ctx = _ctx(audio_input="/tmp/audio.wav")
        result = stage.process(ctx)
        self.assertEqual(result.num_speakers, 1)
        self.assertAlmostEqual(result.speaker_segments[0]["end"] - result.speaker_segments[0]["start"], 0.1)

    def test_overlapping_speakers(self):
        # Multiple speakers with overlapping time ranges
        segments = [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
            {"start": 3.0, "end": 8.0, "speaker": "SPEAKER_01"},  # Overlaps
            {"start": 7.0, "end": 10.0, "speaker": "SPEAKER_00"},
        ]
        stage = DiarizationStage(diarization_fn=_fake_diarization(segments))
        ctx = _ctx(audio_input="/tmp/audio.wav")
        result = stage.process(ctx)
        # Should handle overlapping gracefully
        self.assertEqual(len(result.speaker_segments), 3)
        self.assertIn("SPEAKER_00", [s["speaker"] for s in result.speaker_segments])
        self.assertIn("SPEAKER_01", [s["speaker"] for s in result.speaker_segments])

    def test_many_speakers(self):
        # High speaker count
        segments = [
            {"start": i * 1.0, "end": (i + 1) * 1.0, "speaker": f"SPEAKER_{i:02d}"}
            for i in range(20)
        ]
        stage = DiarizationStage(diarization_fn=_fake_diarization(segments))
        ctx = _ctx(audio_input="/tmp/audio.wav")
        result = stage.process(ctx)
        self.assertEqual(result.num_speakers, 20)

    def test_zero_duration_segment(self):
        # start == end
        segments = [{"start": 5.0, "end": 5.0, "speaker": "SPEAKER_00"}]
        stage = DiarizationStage(diarization_fn=_fake_diarization(segments))
        ctx = _ctx(audio_input="/tmp/audio.wav")
        result = stage.process(ctx)
        # Should include but recognize as zero-duration
        self.assertEqual(result.num_speakers, 1)

    def test_negative_timestamps(self):
        # Start/end before zero (edge case)
        segments = [
            {"start": -1.0, "end": 2.0, "speaker": "SPEAKER_00"},
            {"start": 2.0, "end": 5.0, "speaker": "SPEAKER_01"},
        ]
        stage = DiarizationStage(diarization_fn=_fake_diarization(segments))
        ctx = _ctx(audio_input="/tmp/audio.wav")
        result = stage.process(ctx)
        # Should include or error gracefully
        self.assertIsNotNone(result.speaker_segments)

    def test_unsorted_segments(self):
        # Segments not in temporal order
        segments = [
            {"start": 5.0, "end": 10.0, "speaker": "SPEAKER_01"},
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
            {"start": 10.0, "end": 15.0, "speaker": "SPEAKER_02"},
        ]
        stage = DiarizationStage(diarization_fn=_fake_diarization(segments))
        ctx = _ctx(audio_input="/tmp/audio.wav")
        result = stage.process(ctx)
        # Should handle unsorted gracefully
        self.assertEqual(result.num_speakers, 3)
