"""Тесты для TranscriptWriter."""
from __future__ import annotations
from backend.transcript_writer import TranscriptWriter

import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KRABEAR_ROOT = PROJECT_ROOT / "KrabEar"
if str(KRABEAR_ROOT) not in sys.path:
    sys.path.insert(0, str(KRABEAR_ROOT))


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


class TestTranscriptWriterAtomicWrite(unittest.TestCase):
    """Тесты W923 H1 + H2: атомарная запись и collision-resolution без TOCTOU."""

    def _make_item(self, ts="2026-05-26T12:00:00", **kwargs):
        base = {
            "text": "Атомарный тест.",
            "ts": ts,
            "audio_duration_sec": 2.0,
            "confidence": 0.95,
            "translated_text": "",
            "translation_status": "not_requested",
            "diarization": None,
        }
        base.update(kwargs)
        return base

    def test_atomic_write_leaves_no_tmp_on_success(self):
        """H1: после успешной записи .md.tmp файлов в директории быть не должно."""
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            item = self._make_item()
            path = TranscriptWriter.write_transcript(item, out_dir)

            # Основной файл должен существовать
            self.assertTrue(path.exists(), f"Файл не создан: {path}")
            # Временных .tmp файлов быть не должно
            tmp_files = list(out_dir.glob("*.tmp"))
            self.assertEqual(tmp_files, [], f"Найдены .tmp файлы: {tmp_files}")
            # Файл должен содержать корректный контент (не пустой и не truncated)
            content = path.read_text(encoding="utf-8")
            self.assertIn("# Транскрибация", content)
            self.assertIn("Атомарный тест.", content)

    def test_concurrent_same_second_writes_no_clobber(self):
        """H2: два потока с одним timestamp не должны перезаписать друг друга (TOCTOU race).

        Оба файла должны существовать с разными именами и содержать своё содержимое.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            # Оба элемента имеют один и тот же timestamp — провоцируем collision
            ts = "2026-05-26T12:00:00"
            item_a = self._make_item(ts=ts, text="Содержимое потока A")
            item_b = self._make_item(ts=ts, text="Содержимое потока B")

            results: list[Path] = []
            errors: list[Exception] = []
            barrier = threading.Barrier(2)

            def write_a():
                try:
                    barrier.wait()  # синхронизируем старт обоих потоков
                    p = TranscriptWriter.write_transcript(item_a, out_dir)
                    results.append(p)
                except Exception as exc:
                    errors.append(exc)

            def write_b():
                try:
                    barrier.wait()
                    p = TranscriptWriter.write_transcript(item_b, out_dir)
                    results.append(p)
                except Exception as exc:
                    errors.append(exc)

            t1 = threading.Thread(target=write_a)
            t2 = threading.Thread(target=write_b)
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

            self.assertEqual(errors, [], f"Ошибки в потоках: {errors}")
            self.assertEqual(len(results), 2, "Ожидались пути от обоих потоков")

            path_a, path_b = results[0], results[1]
            # Имена должны быть разными — второй получил уникальный суффикс
            self.assertNotEqual(
                path_a, path_b,
                "Оба потока получили одинаковый путь — коллизия не разрешена"
            )
            # Оба файла должны существовать
            self.assertTrue(path_a.exists(), f"Файл A не существует: {path_a}")
            self.assertTrue(path_b.exists(), f"Файл B не существует: {path_b}")
            # Ни один файл не должен быть пустым (не truncated/clobbered)
            content_a = path_a.read_text(encoding="utf-8")
            content_b = path_b.read_text(encoding="utf-8")
            self.assertIn("# Транскрибация", content_a)
            self.assertIn("# Транскрибация", content_b)
            # Каждый файл должен содержать именно своё содержимое (не перезаписан)
            combined = content_a + content_b
            self.assertIn("Содержимое потока A", combined)
            self.assertIn("Содержимое потока B", combined)


if __name__ == "__main__":
    unittest.main()
