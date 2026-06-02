"""Wave-19 injection tests for StatsReportGenerator.

Covers two MED Markdown-injection findings:

FINDING 1 (code-fence break): _section_language_distribution emitted raw
source_lang values INSIDE a fenced code block without ISO-639 validation.
A source_lang containing "\n```\n## X" closes the fence and injects a heading.

FIX 1: validate source_lang at render time with re.fullmatch(r'[A-Za-z]{2,8}...').
Any non-conforming value is replaced with 'unknown' (and skipped from chart).

FINDING 2 (inline code-span break): _md_cell neutralized | CR/LF and formula
chars but NOT backticks. Tags were wrapped in `...` at line ~534, so a tag
containing a backtick closes the span and the remainder renders as raw Markdown.

FIX 2: tags are rendered without the `...` wrapper; _md_cell now also replaces
backtick (U+0060) with MODIFIER LETTER GRAVE (U+02CB) as defence-in-depth.

Security model: exploitation requires that injected content appears at the START
of a line as a Markdown heading (##) or raw HTML block.  Newlines collapsed to
spaces + the fence-break guard together prevent this.  The tests verify the two
conditions that make injection exploitable:

  a) ## HEADING at start of a line  (Markdown heading injection)
  b) Raw HTML block at start of a line  (HTML injection)
  c) Raw backtick in output lines  (code-span re-opening)
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


def _has_real_heading(text: str, keyword: str) -> bool:
    """Return True if 'keyword' appears as a Markdown heading (## at line start)."""
    return bool(_re.search(r"^##\s+" + _re.escape(keyword), text, _re.MULTILINE))


def _has_html_block(text: str, tag_prefix: str) -> bool:
    """Return True if a raw HTML block starts with tag_prefix at line start."""
    return bool(_re.search(r"^" + _re.escape(tag_prefix), text, _re.MULTILINE))


# ---------------------------------------------------------------------------
# Finding 1: source_lang code-fence break
# ---------------------------------------------------------------------------

class TestSourceLangFenceBreak(unittest.TestCase):
    """FINDING 1: source_lang with embedded newline + fence must not break the
    fenced code block in _section_language_distribution.

    After fix: non-ISO-639 source_lang values are replaced with 'unknown' and
    skipped from the language chart entirely.  So the injected content simply
    does not appear in the report at all.
    """

    # A crafted source_lang that would close the fence and inject a heading
    # plus a raw HTML tag if the value were used verbatim.
    _EVIL_LANG = "\n```\n## INJECTED\n<img src=x onerror=alert(1)>"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._gen = StatsReportGenerator()
        items = [_make_item(source_lang=self._EVIL_LANG)]
        self._store = _FakeStore(Path(self._tmp.name), items=items)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_no_fence_break_real_heading(self) -> None:
        """The injected ## INJECTED must NOT appear as a real Markdown heading
        (i.e. ## at start of a line).  Non-ISO-639 lang → replaced with
        'unknown' → skipped → not emitted at all."""
        result = self._gen.generate_report(self._store, days=30)
        self.assertFalse(
            _has_real_heading(result, "INJECTED"),
            "Injected '## INJECTED' appeared as a real Markdown heading.",
        )

    def test_no_injected_content_in_output(self) -> None:
        """Non-conforming source_lang is entirely excluded (unknown + skipped).
        None of the payload text should appear anywhere in the report."""
        result = self._gen.generate_report(self._store, days=30)
        self.assertNotIn("INJECTED", result,
                         "Payload text from evil source_lang leaked into output.")

    def test_no_raw_html_at_line_start(self) -> None:
        """The <img onerror=...> must not appear at the start of any line."""
        result = self._gen.generate_report(self._store, days=30)
        self.assertFalse(
            _has_html_block(result, "<img src=x onerror=alert(1)>"),
            "Raw HTML block from evil source_lang found at line start.",
        )

    def test_valid_lang_codes_still_render(self) -> None:
        """Legitimate ISO-639 codes (ru, es, en, zh-Hant) still appear."""
        items = [
            _make_item(source_lang="ru"),
            _make_item(source_lang="es"),
            _make_item(source_lang="zh-Hant"),
        ]
        store = _FakeStore(Path(self._tmp.name) / "valid", items=items)
        result = self._gen.generate_report(store, days=30)
        self.assertIn("ru", result)
        self.assertIn("es", result)
        self.assertIn("zh-Hant", result)

    def test_lang_with_newline_excluded(self) -> None:
        """source_lang with embedded newline is sanitized away entirely."""
        items = [_make_item(source_lang="ru\n## BAD")]
        store = _FakeStore(Path(self._tmp.name) / "nl", items=items)
        result = self._gen.generate_report(store, days=30)
        self.assertFalse(_has_real_heading(result, "BAD"),
                         "Injected ## BAD appeared as Markdown heading.")
        self.assertNotIn("## BAD", result)

    def test_lang_with_backtick_excluded(self) -> None:
        """source_lang containing backtick/fence characters is excluded."""
        items = [_make_item(source_lang="ru`x")]
        store = _FakeStore(Path(self._tmp.name) / "bt", items=items)
        result = self._gen.generate_report(store, days=30)
        # ru`x is not a valid ISO-639 token → must not appear verbatim inside
        # the language code block (would corrupt the fence)
        self.assertNotIn("ru`x", result)

    def test_fence_structure_remains_valid(self) -> None:
        """The fenced code block for the language section is well-formed.
        After injecting an evil lang, the code fence count is even (opened and
        closed symmetrically) — no orphaned ``` that would corrupt the render."""
        result = self._gen.generate_report(self._store, days=30)
        fences = _re.findall(r"^```", result, _re.MULTILINE)
        self.assertEqual(
            len(fences) % 2, 0,
            f"Odd number of ``` fences ({len(fences)}) — fence structure broken.",
        )


# ---------------------------------------------------------------------------
# Finding 2: tag backtick inline code-span break
# ---------------------------------------------------------------------------

class TestTagBacktickCodeSpanBreak(unittest.TestCase):
    """FINDING 2: a backtick in a tag value must not close an inline code span
    and inject raw Markdown/HTML into the report.

    After fix:
    - Tags are NOT wrapped in `...` anymore (code-span wrapper removed).
    - _md_cell replaces backtick (U+0060) with MODIFIER LETTER GRAVE (U+02CB).
    - Newlines in tags are still collapsed to spaces by _md_cell.

    Exploitability requires the injected ## heading OR <img> to appear at the
    START of a line.  With newlines collapsed and backtick replaced, this
    cannot happen.
    """

    # Crafted tag: backtick would have broken the code span in the old code,
    # then the embedded newline + ## heading would inject a heading.
    _EVIL_TAG = "normal`\n## INJECTED\n<img src=x onerror=alert(2)>"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._gen = StatsReportGenerator()
        items = [_make_item(tags=[self._EVIL_TAG])]
        self._store = _FakeStore(Path(self._tmp.name), items=items)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_no_real_heading_from_tag(self) -> None:
        """## INJECTED must NOT appear as a real Markdown heading (## at line
        start).  Newlines are collapsed to spaces → cannot be at line start."""
        result = self._gen.generate_report(self._store, days=30)
        self.assertFalse(
            _has_real_heading(result, "INJECTED"),
            "Tag payload produced a real ## INJECTED heading at line start.",
        )

    def test_no_html_block_from_tag(self) -> None:
        """<img onerror=...> must NOT appear at the start of a line.
        Newlines collapsed → the img tag is in the middle of a list item."""
        result = self._gen.generate_report(self._store, days=30)
        self.assertFalse(
            _has_html_block(result, "<img src=x onerror=alert(2)>"),
            "Tag payload produced raw HTML block at line start.",
        )

    def test_no_raw_backtick_in_tag_lines(self) -> None:
        """The output line for the evil tag must not contain a raw backtick.
        Backtick in tag → replaced with ˋ (U+02CB) by _md_cell."""
        result = self._gen.generate_report(self._store, days=30)
        # Find lines that contain the 'normal' prefix of the tag value.
        tag_lines = [ln for ln in result.splitlines() if "normal" in ln]
        self.assertTrue(tag_lines, "Expected at least one output line with 'normal' tag prefix")
        for ln in tag_lines:
            self.assertNotIn("`", ln,
                             f"Raw backtick found in tag output line: {ln!r}")

    def test_tag_prefix_still_visible(self) -> None:
        """The 'normal' prefix of the tag is still visible in the output."""
        result = self._gen.generate_report(self._store, days=30)
        self.assertIn("normal", result)

    def test_entire_payload_on_single_line(self) -> None:
        """Newlines collapsed → entire sanitized tag content is on one line."""
        result = self._gen.generate_report(self._store, days=30)
        # The word 'INJECTED' (from the payload) still appears in output but
        # must be on the same line as 'normal' (not injected as separate heading).
        normal_lines = [ln for ln in result.splitlines() if "normal" in ln]
        self.assertTrue(normal_lines, "Expected at least one line with 'normal' in it")
        # The line containing 'normal' must also contain 'INJECTED' (same line,
        # not a separate heading line) — this proves newlines were collapsed.
        for ln in normal_lines:
            # INJECTED text should be on the same line as 'normal' after collapse
            self.assertIn("INJECTED", ln,
                          f"INJECTED text not on same line as 'normal': {ln!r}")

    def test_backtick_only_tag(self) -> None:
        """A tag that is just a backtick is sanitized without error."""
        items = [_make_item(tags=["`"])]
        store = _FakeStore(Path(self._tmp.name) / "bt_only", items=items)
        result = self._gen.generate_report(store, days=30)
        self.assertIsInstance(result, str)
        # Lines with "— 1" are tag count lines; none should have raw backtick
        tag_lines = [ln for ln in result.splitlines() if "— 1" in ln]
        for ln in tag_lines:
            self.assertNotIn("`", ln, f"Raw backtick in tag line: {ln!r}")

    def test_multiple_backticks_in_tag(self) -> None:
        """Multiple backticks in a tag are all neutralized."""
        items = [_make_item(tags=["a`b`c"])]
        store = _FakeStore(Path(self._tmp.name) / "multi_bt", items=items)
        result = self._gen.generate_report(store, days=30)
        abc_lines = [ln for ln in result.splitlines() if "a" in ln and "b" in ln and "c" in ln]
        for ln in abc_lines:
            self.assertNotIn("`", ln, f"Raw backtick in multi-backtick tag line: {ln!r}")


# ---------------------------------------------------------------------------
# _md_cell helper: backtick neutralization (defence-in-depth)
# ---------------------------------------------------------------------------

class TestMdCellBacktickNeutralization(unittest.TestCase):
    """_md_cell must neutralize backticks as defence-in-depth for code-span use."""

    def test_backtick_replaced(self) -> None:
        out = _md_cell("hello`world")
        self.assertNotIn("`", out)
        self.assertIn("hello", out)
        self.assertIn("world", out)

    def test_leading_backtick_replaced(self) -> None:
        out = _md_cell("`danger")
        self.assertFalse(out.startswith("`"), f"Output starts with backtick: {out!r}")

    def test_multiple_backticks_all_replaced(self) -> None:
        out = _md_cell("a`b`c`d")
        self.assertNotIn("`", out)

    def test_backtick_plus_newline_both_neutralized(self) -> None:
        out = _md_cell("tag`\n## X")
        self.assertNotIn("`", out)
        self.assertNotIn("\n", out)

    def test_plain_text_unchanged(self) -> None:
        self.assertEqual(_md_cell("meeting"), "meeting")
        self.assertEqual(_md_cell("Работа"), "Работа")

    def test_pipe_still_escaped(self) -> None:
        """Pipe escaping must still work alongside backtick neutralization."""
        out = _md_cell("a|b`c")
        self.assertIn("\\|", out)
        self.assertNotIn("`", out)

    def test_none_still_empty(self) -> None:
        self.assertEqual(_md_cell(None), "")

    def test_formula_lead_still_neutralized(self) -> None:
        out = _md_cell("=cmd`x")
        self.assertTrue(out.startswith("'"), f"Formula lead not neutralized: {out!r}")
        self.assertNotIn("`", out)

    def test_newlines_still_collapsed(self) -> None:
        out = _md_cell("a\nb\rc")
        self.assertNotIn("\n", out)
        self.assertNotIn("\r", out)


# ---------------------------------------------------------------------------
# Combined: both vectors in a single report
# ---------------------------------------------------------------------------

class TestCombinedInjectionVectors(unittest.TestCase):
    """Both vectors together must not produce an exploitable report."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._gen = StatsReportGenerator()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fence_break_and_code_span_break_together(self) -> None:
        evil_lang = "\n```\n## FENCE_BREAK\n<script>alert(1)</script>"
        evil_tag = "ok`\n## TAG_BREAK\n<img src=x>"
        items = [_make_item(source_lang=evil_lang, tags=[evil_tag])]
        store = _FakeStore(Path(self._tmp.name), items=items)
        result = self._gen.generate_report(store, days=30)

        # Neither payload should produce a real ## heading at line start
        self.assertFalse(_has_real_heading(result, "FENCE_BREAK"),
                         "## FENCE_BREAK appeared as real heading")
        self.assertFalse(_has_real_heading(result, "TAG_BREAK"),
                         "## TAG_BREAK appeared as real heading")

        # Neither payload should produce raw HTML blocks at line start
        self.assertFalse(_has_html_block(result, "<script>"),
                         "<script> appeared at line start as HTML block")
        self.assertFalse(_has_html_block(result, "<img src=x>"),
                         "<img src=x> appeared at line start as HTML block")

    def test_report_still_renders_valid_markdown_structure(self) -> None:
        """Even with malicious input the report has correct ## section count."""
        evil_lang = "\n```\n## FAKE_SECTION\n"
        evil_tag = "tag`broken"
        items = [_make_item(source_lang=evil_lang, tags=[evil_tag])]
        store = _FakeStore(Path(self._tmp.name) / "struct", items=items)
        result = self._gen.generate_report(store, days=30)
        h2_sections = _re.findall(r"^## \d+\.", result, _re.MULTILINE)
        self.assertEqual(len(h2_sections), 8,
                         f"Expected 8 ## sections, got {len(h2_sections)}: {h2_sections}")

    def test_fences_balanced_with_combined_attack(self) -> None:
        """Code fence count must remain even after both injection attempts."""
        evil_lang = "\n```\n## FAKE\n"
        evil_tag = "ok`\n## X\n"
        items = [_make_item(source_lang=evil_lang, tags=[evil_tag])]
        store = _FakeStore(Path(self._tmp.name) / "fences", items=items)
        result = self._gen.generate_report(store, days=30)
        fences = _re.findall(r"^```", result, _re.MULTILINE)
        self.assertEqual(
            len(fences) % 2, 0,
            f"Odd number of ``` fences ({len(fences)}) after combined attack.",
        )


if __name__ == "__main__":
    unittest.main()
