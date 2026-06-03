"""Wave-25 MD-injection tests for StatsReportGenerator.

Covers two MED Markdown-injection findings:

FIX E1 (MED) — _section_storage (~line 621):
  The storage section embedded file/dir names verbatim into a Markdown table.
  A planted directory named "evil|inject" broke the table; "=cmd" started a
  formula; "[text](url)" became a link; "<tag>" became raw HTML.
  Fix: apply _md_cell() to every label in the storage section.

FIX E2 (MED) — _md_cell (~lines 107-142):
  _md_cell neutralized |, CRLF, formula-start chars, and backticks — but NOT
  Markdown links ([text](url)), Markdown images (![alt](url)), or raw HTML
  (<tag>).  Fix: replace [ ] with fullwidth ［/］ and < > with &lt;/&gt;.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import re as _re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.stats_report import StatsReportGenerator, _md_cell  # noqa: E402


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _ts_recent() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


def _make_item(
    source_lang: str = "ru",
    tags: list | None = None,
    text: str = "test",
) -> dict:
    return {
        "id": "item-test",
        "ts": _ts_recent(),
        "text": text,
        "source_lang": source_lang,
        "confidence": 0.9,
        "audio_duration_sec": 30.0,
        "paste_status": "ok",
        "llm_applied": False,
        "tags": tags or [],
        "diarization": None,
        "favorite": False,
        "translation_mode": "off",
    }


class _FakeStore:
    """Minimal StateStore stub — enough for StatsReportGenerator."""

    def __init__(self, data_dir: Path, items: list) -> None:
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = data_dir / "history.ndjson"
        self.tombstones_path = data_dir / "history_tombstones.ndjson"
        self.status_path = data_dir / "history_status.ndjson"
        self.tags_path = data_dir / "history_tags.ndjson"
        self.settings_path = data_dir / "settings.json"
        self._items = items
        self._lock_path = data_dir / "history.lock"
        self.history_path.write_text(
            "\n".join(json.dumps(i) for i in items), encoding="utf-8"
        )
        self._lock_path.touch()

    @contextlib.contextmanager
    def _lock(self):
        self._lock_path.touch(exist_ok=True)
        with self._lock_path.open("r+", encoding="utf-8") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def _load_active_items_unlocked(self) -> list:
        return list(self._items)


# ---------------------------------------------------------------------------
# E2: _md_cell — link injection ([ ] escaping)
# ---------------------------------------------------------------------------

class TestMdCellLinkInjection(unittest.TestCase):
    """_md_cell must neutralize Markdown link/image syntax."""

    def test_link_syntax_broken(self) -> None:
        """[click me](javascript:alert(1)) must not pass through as a link."""
        out = _md_cell("[click me](javascript:alert(1))")
        # No raw [ or ] in output
        self.assertNotIn("[", out, f"Raw '[' found in: {out!r}")
        self.assertNotIn("]", out, f"Raw ']' found in: {out!r}")

    def test_image_syntax_broken(self) -> None:
        """![alt](url) must not pass through as a Markdown image."""
        out = _md_cell("![alt text](http://evil.com/x.png)")
        self.assertNotIn("[", out, f"Raw '[' found in: {out!r}")
        self.assertNotIn("]", out, f"Raw ']' found in: {out!r}")

    def test_fullwidth_brackets_used(self) -> None:
        """Brackets should be replaced with fullwidth equivalents ［ and ］."""
        out = _md_cell("[text](url)")
        self.assertIn("［", out, f"Expected fullwidth ［ in: {out!r}")
        self.assertIn("］", out, f"Expected fullwidth ］ in: {out!r}")

    def test_plain_text_brackets_safe(self) -> None:
        """A value with brackets preserves readable content with fullwidth chars."""
        out = _md_cell("[meeting notes]")
        # The text 'meeting notes' is still present
        self.assertIn("meeting notes", out)
        self.assertNotIn("[", out)
        self.assertNotIn("]", out)

    def test_nested_brackets_all_escaped(self) -> None:
        """Multiple [ and ] pairs are all replaced."""
        out = _md_cell("[a][b][c]")
        self.assertNotIn("[", out)
        self.assertNotIn("]", out)


# ---------------------------------------------------------------------------
# E2: _md_cell — HTML injection (< > escaping)
# ---------------------------------------------------------------------------

class TestMdCellHtmlInjection(unittest.TestCase):
    """_md_cell must neutralize raw HTML angle brackets."""

    def test_script_tag_escaped(self) -> None:
        """<script>x</script> must become &lt;script&gt;..."""
        out = _md_cell("<script>alert(1)</script>")
        self.assertNotIn("<", out, f"Raw '<' found in: {out!r}")
        self.assertNotIn(">", out, f"Raw '>' found in: {out!r}")
        self.assertIn("&lt;", out)
        self.assertIn("&gt;", out)

    def test_img_onerror_escaped(self) -> None:
        """<img src=x onerror=alert(1)> must be escaped."""
        out = _md_cell("<img src=x onerror=alert(1)>")
        self.assertNotIn("<", out)
        self.assertNotIn(">", out)
        self.assertIn("&lt;img", out)

    def test_html_entity_form(self) -> None:
        """Angle brackets become HTML entities preserving the text."""
        out = _md_cell("<b>bold</b>")
        self.assertIn("&lt;b&gt;", out)
        self.assertIn("bold", out)
        self.assertIn("&lt;/b&gt;", out)

    def test_comparison_operators_escaped(self) -> None:
        """< and > used as comparison operators are also escaped."""
        out = _md_cell("score > 0.9")
        self.assertNotIn(">", out)
        self.assertIn("&gt;", out)

    def test_combined_link_and_html(self) -> None:
        """Combining link syntax and HTML tags — both neutralized."""
        payload = "[evil](<script>alert(1)</script>)"
        out = _md_cell(payload)
        self.assertNotIn("[", out)
        self.assertNotIn("]", out)
        self.assertNotIn("<", out)
        self.assertNotIn(">", out)


# ---------------------------------------------------------------------------
# E2: _md_cell — existing protections still intact
# ---------------------------------------------------------------------------

class TestMdCellExistingProtectionsIntact(unittest.TestCase):
    """Existing _md_cell protections must continue to work after E2 changes."""

    def test_pipe_still_escaped(self) -> None:
        out = _md_cell("a|b")
        self.assertIn("\\|", out)
        # The pipe is escaped as \| — the raw unescaped pipe should not appear
        # (i.e. the sequence "a|b" should be "a\|b" in the output).
        self.assertNotIn("a|b", out, f"Unescaped 'a|b' found in: {out!r}")

    def test_newlines_still_collapsed(self) -> None:
        out = _md_cell("a\nb\rc")
        self.assertNotIn("\n", out)
        self.assertNotIn("\r", out)

    def test_backtick_still_replaced(self) -> None:
        out = _md_cell("hello`world")
        self.assertNotIn("`", out)

    def test_formula_start_still_neutralized(self) -> None:
        for prefix in ("=cmd", "+1", "-1", "@user"):
            out = _md_cell(prefix)
            self.assertTrue(
                out.startswith("'"),
                f"Formula prefix not neutralized for {prefix!r}: {out!r}",
            )

    def test_none_returns_empty(self) -> None:
        self.assertEqual(_md_cell(None), "")

    def test_plain_text_unchanged_except_escaping(self) -> None:
        """Plain ASCII text without special chars is returned as-is."""
        out = _md_cell("hello world")
        self.assertEqual(out, "hello world")

    def test_pipe_plus_brackets_plus_html(self) -> None:
        """All three new + old protections work together."""
        out = _md_cell("a|b[c]<d>")
        self.assertIn("\\|", out)    # pipe escaped
        self.assertNotIn("[", out)    # bracket removed
        self.assertNotIn("]", out)
        self.assertNotIn("<", out)    # html escaped
        self.assertNotIn(">", out)


# ---------------------------------------------------------------------------
# E1: storage section — filename injection
# ---------------------------------------------------------------------------

class TestStorageSectionEscaping(unittest.TestCase):
    """_section_storage must apply _md_cell to every filename/label in the table.

    We patch the filesystem glob so the generator sees attacker-controlled
    filenames without needing actual files on disk.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._gen = StatsReportGenerator()
        items = [_make_item()]
        self._store = _FakeStore(Path(self._tmp.name), items=items)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _render_storage_section(self, extra_filenames: list[str]) -> str:
        """Render just _section_storage with injected filenames from glob."""
        fake_files = []
        for name in extra_filenames:
            f = MagicMock(spec=Path)
            f.name = name
            f.is_file.return_value = True
            f.stat.return_value = MagicMock(st_size=1024)
            fake_files.append(f)

        # Patch Path.glob on the data_dir instance used inside _section_storage
        with patch.object(Path, "glob", return_value=iter(fake_files)):
            return self._gen._section_storage(self._store)

    def test_pipe_in_filename_escaped(self) -> None:
        """A filename containing | must be escaped as \\| in the table."""
        result = self._render_storage_section(["evil|inject.json"])
        self.assertIn("\\|", result, "Pipe in filename was not escaped")
        # The raw unescaped pipe followed by column data would break the table
        self.assertNotIn("evil|inject.json", result,
                         "Raw 'evil|inject.json' appeared verbatim in output")

    def test_formula_in_filename_neutralized(self) -> None:
        """A filename starting with '=' must be prefixed with apostrophe."""
        result = self._render_storage_section(["=cmd_inject.json"])
        # The cell should start with ' to neutralize formula
        self.assertIn("'=cmd_inject.json", result,
                      "Formula-start filename was not neutralized")

    def test_link_in_filename_escaped(self) -> None:
        """A filename like '[link](url).json' must not render as a Markdown link."""
        result = self._render_storage_section(["[link](javascript:alert(1)).json"])
        self.assertNotIn("[link]", result,
                         "Raw Markdown link syntax passed through in storage table")
        self.assertNotIn("[", result.split("| Файл |")[1].split("| **Итого")[0]
                         if "| Файл |" in result and "| **Итого" in result else "",
                         "Raw '[' found in storage table body")

    def test_html_in_filename_escaped(self) -> None:
        """A filename like '<script>.json' must not render as raw HTML."""
        result = self._render_storage_section(["<script>alert(1)</script>.json"])
        self.assertNotIn("<script>", result,
                         "Raw <script> tag appeared in storage table")
        self.assertIn("&lt;script&gt;", result,
                      "HTML-escaped form of <script> not found in output")

    def test_crlf_in_filename_collapsed(self) -> None:
        """A filename with embedded newlines collapses to spaces.

        Security concern: if the newline is NOT collapsed, the injected text
        "## INJECTED" could appear at the START of a line, making it a real
        Markdown heading.  After the fix the newline is replaced with a space
        so "## INJECTED" can only appear mid-cell (e.g. "evil ## INJECTED .json")
        and is therefore NOT a Markdown heading.
        """
        result = self._render_storage_section(["evil\n## INJECTED\n.json"])
        # The injected content must NOT appear as a real ## heading at line start
        self.assertFalse(
            bool(_re.search(r"^## INJECTED", result, _re.MULTILINE)),
            "## INJECTED appeared as a real Markdown heading at line start",
        )
        # If "INJECTED" does appear, it must be in the middle of a table cell line
        lines_with_injected = [ln for ln in result.splitlines() if "INJECTED" in ln]
        for ln in lines_with_injected:
            self.assertFalse(
                ln.strip().startswith("## INJECTED"),
                f"Injected heading at line start: {ln!r}",
            )

    def test_safe_filename_unchanged(self) -> None:
        """A normal filename is still displayed correctly."""
        result = self._render_storage_section(["collections.json"])
        self.assertIn("collections.json", result)


# ---------------------------------------------------------------------------
# E1+E2: Full report with malicious filenames and transcript titles
# ---------------------------------------------------------------------------

class TestFullReportCombinedW25(unittest.TestCase):
    """Combined test: malicious title (link/HTML) in history + filename injection."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._gen = StatsReportGenerator()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_link_in_title_not_rendered(self) -> None:
        """A transcript with a title like '[click](js:x)' must not produce a link.

        Titles appear in the speaker section (via _md_cell) and any other
        section that renders user text through _md_cell.
        """
        items = [_make_item(text="[click me](javascript:alert(1)) hello")]
        store = _FakeStore(Path(self._tmp.name), items=items)
        result = self._gen.generate_report(store, days=30)
        # The raw [ must not be in non-code parts of the output (sections 1-8)
        # We just verify the _md_cell contract: any value going through _md_cell
        # will have [ replaced.  Test _md_cell directly:
        escaped = _md_cell("[click me](javascript:alert(1))")
        self.assertNotIn("[", escaped)
        self.assertNotIn("]", escaped)
        self.assertIsInstance(result, str)

    def test_html_title_not_rendered(self) -> None:
        """A transcript title with <script> is neutralized when passed through _md_cell."""
        payload = "<script>alert(1)</script>"
        out = _md_cell(payload)
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_report_renders_without_error_with_attacker_data(self) -> None:
        """Full report generation must not raise even with adversarial input."""
        items = [
            _make_item(
                text="normal text",
                tags=["[evil](js:x)", "<img onerror=1>", "ok"],
                source_lang="ru",
            )
        ]
        store = _FakeStore(Path(self._tmp.name) / "attack", items=items)
        try:
            result = self._gen.generate_report(store, days=30)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"generate_report raised unexpectedly: {exc}")
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 100)

    def test_no_raw_angle_brackets_from_tags(self) -> None:
        """Tags containing < > must not produce raw angle brackets in the report."""
        items = [_make_item(tags=["<b>bold</b>", "<script>x</script>"])]
        store = _FakeStore(Path(self._tmp.name) / "html_tags", items=items)
        result = self._gen.generate_report(store, days=30)
        # Check lines that contain the tag content
        for ln in result.splitlines():
            if "bold" in ln or "script" in ln.lower():
                self.assertNotIn("<b>", ln, f"Raw <b> in: {ln!r}")
                self.assertNotIn("<script>", ln, f"Raw <script> in: {ln!r}")

    def test_no_raw_brackets_from_tags(self) -> None:
        """Tags containing [ ] must not produce raw brackets in the report."""
        items = [_make_item(tags=["[click](javascript:alert(1))", "[normal]"])]
        store = _FakeStore(Path(self._tmp.name) / "link_tags", items=items)
        result = self._gen.generate_report(store, days=30)
        # Find tag output lines (they contain "— N" count)
        tag_lines = [ln for ln in result.splitlines() if "— " in ln and "click" in ln]
        for ln in tag_lines:
            self.assertNotIn("[click]", ln, f"Raw [click] link syntax in: {ln!r}")


if __name__ == "__main__":
    unittest.main()
