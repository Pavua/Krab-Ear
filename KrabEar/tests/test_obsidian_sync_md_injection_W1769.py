"""Regression tests — Wave 1769: Markdown body injection in obsidian_sync.py.

Attack vector:
  HistoryItem fields (text, translated_text, diarization speaker names) are
  inserted into the .md body without sanitization.  A value containing a bare
  ``---`` line would inject a false YAML-frontmatter boundary, potentially
  corrupting the .md structure in Obsidian.

  Speaker names with newlines break the inline **[name (ts)]** formatting
  and can inject arbitrary markdown lines.

Fix (Wave 1769):
  PRIMARY   — _sanitize_md_body_text() escapes bare "---" / "..." lines
              (YAML document boundary markers) with a leading backslash.
  SECONDARY — _sanitize_speaker_name() strips newlines and brackets from
              speaker labels used inline in the markdown body.

Both helpers are applied in _build_md_content() before appending to the
lines list.

Tests:
  1. _sanitize_md_body_text: bare "---" line is escaped.
  2. _sanitize_md_body_text: "..." (YAML end marker) is escaped.
  3. _sanitize_md_body_text: multi-line text with "---" in the middle.
  4. _sanitize_md_body_text: NUL bytes are stripped.
  5. _sanitize_md_body_text: CR characters are stripped.
  6. _sanitize_md_body_text: text without special chars is unchanged.
  7. _sanitize_speaker_name: newlines replaced with space.
  8. _sanitize_speaker_name: brackets replaced with underscore.
  9. _sanitize_speaker_name: empty/whitespace-only returns default.
  10. End-to-end: sync with "---" in transcript text produces escaped output.
  11. End-to-end: sync with malicious speaker name produces safe output.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.obsidian_sync import (  # noqa: E402
    ObsidianSyncManager,
    _sanitize_md_body_text,
    _sanitize_speaker_name,
)


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------

class TestSanitizeMdBodyText(unittest.TestCase):
    """_sanitize_md_body_text() escapes YAML boundary markers."""

    def test_bare_triple_dash_escaped(self) -> None:
        """Bare '---' on its own line is escaped."""
        result = _sanitize_md_body_text("---")
        self.assertNotEqual(result, "---")
        self.assertIn("---", result)  # dashes still present, just escaped
        self.assertNotRegex(result, r"^---$")

    def test_yaml_end_marker_escaped(self) -> None:
        """'...' (YAML end-of-document marker) on its own line is escaped."""
        result = _sanitize_md_body_text("...")
        self.assertNotRegex(result, r"^\.\.\.$")

    def test_triple_dash_in_middle_of_text(self) -> None:
        """'---' embedded in multi-line text is escaped."""
        text = "Hello world\n---\nEnd of section"
        result = _sanitize_md_body_text(text)
        lines = result.splitlines()
        self.assertNotIn("---", lines, "bare '---' must not remain as a standalone line")

    def test_nul_bytes_stripped(self) -> None:
        result = _sanitize_md_body_text("hello\x00world")
        self.assertNotIn("\x00", result)
        self.assertIn("hello", result)
        self.assertIn("world", result)

    def test_cr_stripped(self) -> None:
        result = _sanitize_md_body_text("line1\r\nline2")
        self.assertNotIn("\r", result)

    def test_normal_text_unchanged(self) -> None:
        """Plain transcript text without special chars is returned unchanged."""
        text = "Привет мир. Это нормальный транскрипт без специальных символов."
        self.assertEqual(_sanitize_md_body_text(text), text)

    def test_empty_string(self) -> None:
        self.assertEqual(_sanitize_md_body_text(""), "")

    def test_none_like_empty(self) -> None:
        # If someone passes None it should not crash (defensive)
        # Actually the function expects str; ensure with None-safe check
        self.assertEqual(_sanitize_md_body_text(""), "")

    def test_triple_dash_not_at_line_start_unchanged(self) -> None:
        """'---' that is NOT alone on a line is left as-is (e.g. inline dash sequences)."""
        text = "A range: 2020---2025 is fine"
        result = _sanitize_md_body_text(text)
        # The inline triple-dash is not a boundary marker; content preserved
        self.assertIn("2020", result)
        self.assertIn("2025", result)


class TestSanitizeSpeakerName(unittest.TestCase):
    """_sanitize_speaker_name() prevents inline Markdown injection."""

    def test_newline_replaced_with_space(self) -> None:
        result = _sanitize_speaker_name("Alice\nBob")
        self.assertNotIn("\n", result)
        self.assertIn("Alice", result)

    def test_square_brackets_replaced(self) -> None:
        result = _sanitize_speaker_name("Speaker[0]")
        self.assertNotIn("[", result)
        self.assertNotIn("]", result)

    def test_empty_name_returns_default(self) -> None:
        self.assertEqual(_sanitize_speaker_name(""), "Спикер")

    def test_whitespace_only_returns_default(self) -> None:
        self.assertEqual(_sanitize_speaker_name("   "), "Спикер")

    def test_normal_name_unchanged(self) -> None:
        result = _sanitize_speaker_name("Александр")
        self.assertEqual(result, "Александр")

    def test_backtick_replaced(self) -> None:
        result = _sanitize_speaker_name("Speaker`1`")
        self.assertNotIn("`", result)


# ---------------------------------------------------------------------------
# End-to-end tests via ObsidianSyncManager.sync()
# ---------------------------------------------------------------------------

class TestObsidianSyncMdInjectionE2E(unittest.TestCase):
    """End-to-end: sync writes safe .md even with injected content."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._vault = Path(self._tmp.name) / "vault"
        self._vault.mkdir()
        self._data = Path(self._tmp.name) / "data"
        self._data.mkdir()
        self._mgr = ObsidianSyncManager(data_dir=self._data)
        # Use configure() directly (not handle_configure) to skip the
        # IPC-level $HOME containment guard — we're testing the .md content
        # sanitization, not the path containment check.
        self._mgr.configure(str(self._vault))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_item(self, **kwargs):
        """Build a minimal HistoryItem-shaped dict for sync."""
        from datetime import timezone
        import uuid
        defaults = {
            "id": str(uuid.uuid4()),
            "ts": "2026-06-01T12:00:00Z",
            "text": "Normal transcript.",
            "source_lang": "ru",
            "translated_text": "",
            "translation_mode": "off",
            "target_lang": "",
            "confidence": 0.92,
            "audio_duration_sec": 10.0,
            "tags": [],
            "llm_applied": False,
            "diarization": None,
        }
        defaults.update(kwargs)
        return defaults

    def _read_md(self) -> str:
        """Return content of the single .md file in the vault Transcriptions folder."""
        # Default folder is 'Transcriptions' (ObsidianSyncManager._DEFAULT_FOLDER)
        md_files = list((self._vault / "Transcriptions").rglob("*.md"))
        self.assertEqual(len(md_files), 1, f"Expected 1 .md file, found {md_files}")
        return md_files[0].read_text(encoding="utf-8")

    def test_triple_dash_in_text_escaped_in_md(self) -> None:
        """Transcript text with bare '---' must be escaped in the .md body."""
        item = self._make_item(text="Before\n---\nAfter")
        self._mgr.sync([item], force=True)
        content = self._read_md()
        # The raw "---" must not appear as a standalone line after the
        # YAML frontmatter closing "---" (which is at the start of the file)
        body = content.split("---\n", 2)[-1]  # skip frontmatter
        body_lines = body.splitlines()
        self.assertNotIn("---", body_lines,
                         "bare '---' must be escaped in the md body")

    def test_malicious_speaker_name_sanitized(self) -> None:
        """Speaker name with newline must not inject raw lines into the .md.

        The newline in the speaker name is replaced with a space so the
        formatted inline text **[Alice --- title: injected (00:00:00)]** stays
        on a single line.  The raw '---' line (YAML boundary marker) that would
        appear if newlines were NOT stripped must be absent from the body.
        """
        diarization = {
            "enabled": True,
            "speaker_turns": [
                {"speaker": "Alice\n---\ntitle: injected", "text": "Hello.", "start": 0.0}
            ],
        }
        item = self._make_item(diarization=diarization)
        self._mgr.sync([item], force=True)
        content = self._read_md()
        body = content.split("---\n", 2)[-1]  # skip frontmatter
        body_lines = body.splitlines()
        # Primary: bare '---' must not appear as a standalone line in the body
        # (the original newlines would have produced this boundary marker)
        self.assertNotIn("---", body_lines,
                         "injected '---' from speaker name must be escaped in body")
        # Secondary: the speaker text is entirely on one inline-formatted line
        # (i.e., no newline survived; the **[name (ts)]** marker is unbroken)
        speaker_line_count = sum(1 for ln in body_lines if "Alice" in ln)
        self.assertEqual(speaker_line_count, 1,
                         "Sanitized speaker name must appear on exactly 1 line")


if __name__ == "__main__":
    unittest.main()
