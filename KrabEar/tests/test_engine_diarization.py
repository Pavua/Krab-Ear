"""Тесты diarization-логики AudioEngine.

Проверяем только локальные helper-методы, чтобы тесты не зависели от загрузки
моделей Hugging Face и оставались быстрыми.
"""

from __future__ import annotations
from core.engine import AudioEngine

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class AudioEngineDiarizationTests(unittest.TestCase):
    """Покрытие назначения спикеров и агрегации реплик."""

    def setUp(self) -> None:
        self.engine = AudioEngine()

    def test_assigns_speaker_by_max_overlap(self) -> None:
        whisper_segments = [
            {"start": 0.0, "end": 1.8, "text": "Привет."},
            {"start": 1.8, "end": 4.0, "text": "Как дела?"},
        ]
        speaker_segments = [
            {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"},
            {"start": 2.0, "end": 4.5, "speaker": "SPEAKER_01"},
        ]

        annotated = self.engine._annotate_segments_with_speakers(whisper_segments, speaker_segments)

        self.assertEqual(annotated[0]["speaker"], "SPEAKER_00")
        self.assertEqual(annotated[1]["speaker"], "SPEAKER_01")

    def test_groups_neighbor_segments_into_turns(self) -> None:
        annotated_segments = [
            {"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00", "text": "Добрый день"},
            {"start": 1.0, "end": 2.1, "speaker": "SPEAKER_00", "text": "меня слышно?"},
            {"start": 2.1, "end": 3.5, "speaker": "SPEAKER_01", "text": "Да, слышно."},
        ]

        turns = self.engine._merge_speaker_turns(annotated_segments)

        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["speaker"], "SPEAKER_00")
        self.assertEqual(turns[0]["text"], "Добрый день меня слышно?")
        self.assertEqual(turns[1]["speaker"], "SPEAKER_01")


if __name__ == "__main__":
    unittest.main()
