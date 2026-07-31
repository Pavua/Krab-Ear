"""W-C волны gigaam-mlx-diar: диаризованный конвейер (чистые функции + оркестратор).

Без HF-моделей и без AudioEngine: оркестратор тестируется на fake-движке
(паттерн test_engine_diarization — литеральные списки сегментов).
"""
import unittest

import numpy as np

from core.diarized_transcription import (
    format_diarized_transcript,
    format_timestamp,
    merge_speaker_turns,
    run_diarized_transcription,
)


class TestMergeSpeakerTurns(unittest.TestCase):
    def test_same_speaker_merged_within_gap(self):
        turns = [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
            {"start": 5.5, "end": 9.0, "speaker": "SPEAKER_00"},
        ]
        merged = merge_speaker_turns(turns)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["end"], 9.0)

    def test_different_speakers_never_merged(self):
        turns = [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
            {"start": 5.1, "end": 9.0, "speaker": "SPEAKER_01"},
        ]
        self.assertEqual(len(merge_speaker_turns(turns)), 2)

    def test_merge_respects_20s_hard_limit(self):
        turns = [
            {"start": 0.0, "end": 12.0, "speaker": "SPEAKER_00"},
            {"start": 12.2, "end": 24.0, "speaker": "SPEAKER_00"},
        ]
        merged = merge_speaker_turns(turns)
        self.assertEqual(len(merged), 2, "склейка сверх 20 c запрещена (лимит GigaAM)")
        for seg in merged:
            self.assertLessEqual(seg["end"] - seg["start"], 20.0)

    def test_long_gap_breaks_merge(self):
        turns = [
            {"start": 0.0, "end": 3.0, "speaker": "SPEAKER_00"},
            {"start": 7.0, "end": 9.0, "speaker": "SPEAKER_00"},
        ]
        self.assertEqual(len(merge_speaker_turns(turns)), 2)

    def test_unsorted_input_is_sorted(self):
        turns = [
            {"start": 5.5, "end": 9.0, "speaker": "SPEAKER_00"},
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
        ]
        merged = merge_speaker_turns(turns)
        self.assertEqual(len(merged), 1)


class TestFormatting(unittest.TestCase):
    def test_timestamp(self):
        self.assertEqual(format_timestamp(0), "00:00")
        self.assertEqual(format_timestamp(75.9), "01:15")

    def test_transcript_lines_and_empty_skip(self):
        pieces = [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00", "text": "Привет."},
            {"start": 5.0, "end": 6.0, "speaker": "SPEAKER_01", "text": "  "},
            {"start": 6.0, "end": 9.0, "speaker": "SPEAKER_01", "text": "Здравствуй."},
        ]
        out = format_diarized_transcript(pieces)
        self.assertEqual(
            out, "[00:00] SPEAKER_00: Привет.\n[00:06] SPEAKER_01: Здравствуй."
        )


class _FakeEngine:
    """Минимальный движок: два спикера, распознавание = имя спикера + номер."""

    def __init__(self):
        self.calls = []

    def _run_diarization(self, path):
        return [
            {"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00"},
            {"start": 4.0, "end": 8.0, "speaker": "SPEAKER_01"},
            {"start": 8.0, "end": 8.2, "speaker": "SPEAKER_00"},  # < 0.4 c — пропуск
        ]

    def _resample_audio_to_mono_16k(self, audio, sample_rate):
        return audio

    def _transcribe_with_fallback(self, audio, prompt, language=None, audio_sample_rate=None):
        self.calls.append(len(audio))
        return {
            "text": f"реплика {len(self.calls)}",
            "engine": "gigaam-mlx-rnnt",
            "confidence": 0.9,
        }


class TestRunPipeline(unittest.TestCase):
    def test_contract_and_flow(self):
        import soundfile as sf
        import tempfile
        import os

        eng = _FakeEngine()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            path = tmp.name
        try:
            sf.write(path, np.zeros(16000 * 10, dtype=np.float32), 16000)
            result = run_diarized_transcription(eng, path, language="ru")
        finally:
            os.unlink(path)

        # Два содержательных сегмента (третий короче 0.4 c — пропущен).
        self.assertEqual(len(eng.calls), 2)
        self.assertEqual(
            result["text"],
            "[00:00] SPEAKER_00: реплика 1\n[00:04] SPEAKER_01: реплика 2",
        )
        # Полный контракт раннего return.
        for key in ("language", "confidence", "engine", "model",
                    "llm_applied", "segments", "diarization", "emotion"):
            self.assertIn(key, result)
        self.assertFalse(result["llm_applied"])
        self.assertEqual(result["engine"], "diarized+gigaam-mlx-rnnt")
        self.assertEqual(len(result["diarization"]), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
