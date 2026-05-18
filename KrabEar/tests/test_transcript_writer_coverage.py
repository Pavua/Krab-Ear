"""Coverage tests for TranscriptWriter — Obsidian-compatible .md output.

Covers: file creation, YAML-style frontmatter markers, filename patterns,
unicode/special-char filenames, long filenames, dir auto-creation, overwrite
safety, speaker diarization, empty-text guard, concurrent writes, and
get_filename helper.

CONSTRAINT: no models loaded; pure unit + tmp_path via tempfile.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KRABEAR_ROOT = PROJECT_ROOT / "KrabEar"
if str(KRABEAR_ROOT) not in sys.path:
    sys.path.insert(0, str(KRABEAR_ROOT))

from backend.transcript_writer import TranscriptWriter  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _item(**kw):
    base = {
        "text": "Тестовая транскрибация.",
        "ts": "2026-05-18T12:00:00",
        "audio_duration_sec": 10.0,
        "confidence": 0.90,
        "translated_text": "",
        "translation_status": "not_requested",
        "diarization": None,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# test cases
# ---------------------------------------------------------------------------

class TestWriteCreatesMdFile(unittest.TestCase):
    """write_transcript creates a .md file on disk."""

    def test_write_creates_md_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            path = TranscriptWriter.write_transcript(_item(), out)
            self.assertTrue(path.exists(), "File should exist after write_transcript")
            self.assertEqual(path.suffix, ".md")


class TestWriteIncludesYamlFrontmatter(unittest.TestCase):
    """build_content includes recognisable Obsidian frontmatter markers."""

    def test_write_includes_yaml_frontmatter(self):
        # TranscriptWriter uses bold **field:** lines as Obsidian-style metadata.
        content = TranscriptWriter.build_content(_item())
        # Must contain the header + metadata block before the --- separator
        self.assertIn("**Дата:**", content)
        self.assertIn("**Длительность:**", content)
        self.assertIn("**Качество:**", content)
        self.assertIn("**Теги:**", content)
        self.assertIn("---", content)


class TestWriteFilenameUsesTimestamp(unittest.TestCase):
    """Filename starts with the date extracted from ts."""

    def test_write_filename_uses_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            path = TranscriptWriter.write_transcript(_item(ts="2026-05-18T09:45:00"), out)
            self.assertTrue(
                path.name.startswith("2026-05-18"),
                f"Expected date prefix, got: {path.name}",
            )


class TestWriteHandlesUnicodeFilename(unittest.TestCase):
    """Filenames may contain Cyrillic — file must be written and readable."""

    def test_write_handles_unicode_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            path = TranscriptWriter.write_transcript(_item(), out)
            # The default filename uses Cyrillic "Транскрибация"
            self.assertIn("Транскрибация", path.name)
            content = path.read_text(encoding="utf-8")
            self.assertTrue(len(content) > 0)


class TestWriteTruncatesTooLongFilename(unittest.TestCase):
    """If a very long text is used, the filename must not exceed OS limits.

    TranscriptWriter derives the filename from the date (fixed-width) and
    a fixed suffix so the name is inherently short.  This test asserts the
    resulting name stays well under 255 bytes regardless of item content.
    """

    def test_write_truncates_too_long_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            long_text = "А" * 1000
            path = TranscriptWriter.write_transcript(_item(text=long_text), out)
            # File must exist and name must be under the POSIX 255-byte limit
            self.assertTrue(path.exists())
            self.assertLessEqual(
                len(path.name.encode("utf-8")),
                255,
                f"Filename too long: {len(path.name.encode('utf-8'))} bytes",
            )


class TestWriteToNonexistentDirCreatesIt(unittest.TestCase):
    """write_transcript auto-creates the output directory tree."""

    def test_write_to_nonexistent_dir_creates_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "level1" / "level2" / "transcripts"
            self.assertFalse(out.exists())
            TranscriptWriter.write_transcript(_item(), out)
            self.assertTrue(out.exists())


class TestWriteOverwriteExistingSafe(unittest.TestCase):
    """Writing a second item for the same date appends a time suffix instead of overwriting."""

    def test_write_overwrite_existing_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            path1 = TranscriptWriter.write_transcript(_item(ts="2026-05-18T08:00:00"), out)
            path2 = TranscriptWriter.write_transcript(
                _item(text="Вторая запись.", ts="2026-05-18T09:00:00"), out
            )
            # Both files should exist; path2 should have a time suffix
            self.assertTrue(path1.exists())
            self.assertTrue(path2.exists())
            self.assertNotEqual(path1, path2)
            content1 = path1.read_text(encoding="utf-8")
            content2 = path2.read_text(encoding="utf-8")
            # Original file should be untouched
            self.assertIn("Тестовая транскрибация.", content1)
            self.assertIn("Вторая запись.", content2)


class TestWriteIncludesSpeakersIfPresent(unittest.TestCase):
    """Diarized items produce per-speaker lines in the text section."""

    def test_write_includes_speakers_if_present(self):
        diar = {
            "enabled": True,
            "speaker_turns": [
                {"speaker": "SPEAKER_00", "text": "Добрый день."},
                {"speaker": "SPEAKER_01", "text": "Здравствуйте."},
            ],
        }
        content = TranscriptWriter.build_content(_item(diarization=diar))
        self.assertIn("[SPEAKER_00]:", content)
        self.assertIn("[SPEAKER_01]:", content)
        self.assertIn("Добрый день.", content)
        self.assertIn("Здравствуйте.", content)


class TestWriteSkipsEmptyText(unittest.TestCase):
    """Items with empty text still produce a valid file, just with empty body."""

    def test_write_skips_empty_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            path = TranscriptWriter.write_transcript(_item(text=""), out)
            self.assertTrue(path.exists())
            content = path.read_text(encoding="utf-8")
            # Header and metadata must still be present
            self.assertIn("# Транскрибация", content)
            self.assertIn("## Текст", content)


class TestWriteHandlesSpecialChars(unittest.TestCase):
    """Text body with special Markdown/XML chars is preserved verbatim."""

    def test_write_handles_special_chars(self):
        special = "Test <br/> & \"quotes\" 'apos' | pipe # hash * asterisk"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            path = TranscriptWriter.write_transcript(_item(text=special), out)
            content = path.read_text(encoding="utf-8")
            self.assertIn(special, content)


class TestGetFilenameForHistoryItem(unittest.TestCase):
    """Filename derives from ts field; fallback to today when ts is absent/invalid."""

    def test_get_filename_for_history_item_with_ts(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            path = TranscriptWriter.write_transcript(_item(ts="2026-01-15T08:30:00"), out)
            self.assertTrue(path.name.startswith("2026-01-15"))

    def test_get_filename_for_history_item_without_ts(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            item = _item()
            item.pop("ts")  # remove ts entirely
            path = TranscriptWriter.write_transcript(item, out)
            # Should still create a valid .md file
            self.assertTrue(path.exists())
            self.assertEqual(path.suffix, ".md")

    def test_get_filename_for_history_item_bad_ts(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            path = TranscriptWriter.write_transcript(_item(ts="not-a-date"), out)
            self.assertTrue(path.exists())
            self.assertEqual(path.suffix, ".md")


class TestConcurrentWritesSafe(unittest.TestCase):
    """Multiple threads can write transcripts simultaneously without corruption."""

    def test_concurrent_writes_safe(self):
        errors: list[Exception] = []
        paths: list[Path] = []
        lock = threading.Lock()

        def write_one(idx: int, out: Path):
            try:
                # Use distinct timestamps to avoid the overwrite-suffix path racing
                ts = f"2026-05-18T{10 + idx:02d}:00:00"
                path = TranscriptWriter.write_transcript(
                    _item(text=f"Thread {idx}", ts=ts), out
                )
                with lock:
                    paths.append(path)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            threads = [threading.Thread(target=write_one, args=(i, out)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # Assertions must run while tmpdir is still alive
            self.assertEqual(errors, [], f"Concurrent write errors: {errors}")
            self.assertEqual(len(paths), 8)
            for p in paths:
                self.assertTrue(p.exists(), f"File gone: {p}")
                content = p.read_text(encoding="utf-8")
                self.assertIn("Thread", content)


if __name__ == "__main__":
    unittest.main()
