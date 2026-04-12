"""Тесты для TranscriptWriter."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KRABEAR_ROOT = PROJECT_ROOT / "KrabEar"
if str(KRABEAR_ROOT) not in sys.path:
    sys.path.insert(0, str(KRABEAR_ROOT))

from backend.transcript_writer import TranscriptWriter


class TestTranscriptWriterBuildContent(unittest.TestCase):
    """Тесты генерации содержимого .md файла."""

    def _make_item(self, **kwargs):
        base = {
            "text": "Привет, это тест.",
            "ts": "2026-04-12T15:30:00",
            "audio_duration_sec": 5.0,
            "confidence": 0.92,
            "translated_text": "",
            "translation_status": "not_requested",
            "diarization": None,
        }
        base.update(kwargs)
        return base

    def test_header_contains_date(self):
        item = self._make_item()
        content = TranscriptWriter.build_content(item)
        self.assertIn("# Транскрибация (2026-04-12)", content)

    def test_metadata_fields_present(self):
        item = self._make_item()
        content = TranscriptWriter.build_content(item)
        self.assertIn("**Дата:**", content)
        self.assertIn("**Длительность:**", content)
        self.assertIn("**Качество:**", content)
        self.assertIn("**Теги:** #transcription #krab-ear", content)

    def test_confidence_formatted_as_percent(self):
        item = self._make_item(confidence=0.85)
        content = TranscriptWriter.build_content(item)
        self.assertIn("85%", content)

    def test_text_section_present(self):
        item = self._make_item(text="Тестовый текст.")
        content = TranscriptWriter.build_content(item)
        self.assertIn("## Текст", content)
        self.assertIn("Тестовый текст.", content)

    def test_translation_section_present_when_ok(self):
        item = self._make_item(
            translated_text="Texto de prueba.",
            translation_status="ok",
        )
        content = TranscriptWriter.build_content(item)
        self.assertIn("## Перевод", content)
        self.assertIn("Texto de prueba.", content)

    def test_translation_section_absent_when_not_ok(self):
        item = self._make_item(
            translated_text="Texto de prueba.",
            translation_status="error",
        )
        content = TranscriptWriter.build_content(item)
        self.assertNotIn("## Перевод", content)

    def test_diarization_with_multiple_speakers(self):
        item = self._make_item(
            diarization={
                "enabled": True,
                "speaker_turns": [
                    {"speaker": "SPEAKER_00", "text": "Добрый день."},
                    {"speaker": "SPEAKER_01", "text": "Здравствуйте."},
                ],
            }
        )
        content = TranscriptWriter.build_content(item)
        self.assertIn("[SPEAKER_00]:", content)
        self.assertIn("[SPEAKER_01]:", content)

    def test_diarization_single_speaker_falls_back_to_text(self):
        item = self._make_item(
            text="Монолог спикера.",
            diarization={
                "enabled": True,
                "speaker_turns": [
                    {"speaker": "SPEAKER_00", "text": "Монолог спикера."},
                ],
            }
        )
        content = TranscriptWriter.build_content(item)
        # Single speaker — no speaker label, just plain text
        self.assertNotIn("[SPEAKER_00]:", content)
        self.assertIn("Монолог спикера.", content)

    def test_no_confidence_shows_dash(self):
        item = self._make_item(confidence=None)
        content = TranscriptWriter.build_content(item)
        self.assertIn("**Качество:** —", content)


class TestTranscriptWriterWriteFile(unittest.TestCase):
    """Тесты записи файлов на диск."""

    def _make_item(self, **kwargs):
        base = {
            "text": "Тест записи файла.",
            "ts": "2026-04-12T10:00:00",
            "audio_duration_sec": 3.0,
            "confidence": 0.80,
            "translated_text": "",
            "translation_status": "not_requested",
            "diarization": None,
        }
        base.update(kwargs)
        return base

    def test_creates_output_dir_if_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "nonexistent" / "transcripts"
            item = self._make_item()
            path = TranscriptWriter.write_transcript(item, out_dir)
            self.assertTrue(out_dir.exists())
            self.assertTrue(path.exists())

    def test_filename_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            item = self._make_item(ts="2026-04-12T10:00:00")
            path = TranscriptWriter.write_transcript(item, out_dir)
            self.assertTrue(path.name.startswith("2026-04-12-Транскрибация"))
            self.assertTrue(path.name.endswith(".md"))

    def test_file_content_is_valid_markdown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            item = self._make_item(text="Содержимое транскрипции.")
            path = TranscriptWriter.write_transcript(item, out_dir)
            content = path.read_text(encoding="utf-8")
            self.assertIn("# Транскрибация", content)
            self.assertIn("Содержимое транскрипции.", content)

    def test_duplicate_date_gets_time_suffix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            item1 = self._make_item(ts="2026-04-12T10:00:00")
            item2 = self._make_item(ts="2026-04-12T11:30:00")
            path1 = TranscriptWriter.write_transcript(item1, out_dir)
            path2 = TranscriptWriter.write_transcript(item2, out_dir)
            self.assertNotEqual(path1, path2)
            self.assertTrue(path2.exists())

    def test_returns_path_object(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            item = self._make_item()
            result = TranscriptWriter.write_transcript(item, out_dir)
            self.assertIsInstance(result, Path)


class TestTranscriptWriterFormatHelpers(unittest.TestCase):
    """Тесты вспомогательных методов форматирования."""

    def test_format_duration_seconds_only(self):
        result = TranscriptWriter._format_duration(45.0)
        self.assertEqual(result, "45с")

    def test_format_duration_minutes_and_seconds(self):
        result = TranscriptWriter._format_duration(125.0)
        self.assertEqual(result, "2м 5с")

    def test_format_duration_hours(self):
        result = TranscriptWriter._format_duration(3725.0)
        self.assertEqual(result, "1ч 2м 5с")

    def test_format_duration_zero_returns_dash(self):
        result = TranscriptWriter._format_duration(0)
        self.assertEqual(result, "—")

    def test_format_duration_none_returns_dash(self):
        result = TranscriptWriter._format_duration(None)
        self.assertEqual(result, "—")

    def test_format_date_human_valid_iso(self):
        result = TranscriptWriter._format_date_human("2026-04-12T15:30:00")
        self.assertIn("апреля", result)
        self.assertIn("2026", result)
        self.assertIn("15:30", result)

    def test_format_date_human_invalid_returns_original(self):
        result = TranscriptWriter._format_date_human("not-a-date")
        self.assertEqual(result, "not-a-date")


if __name__ == "__main__":
    unittest.main()
