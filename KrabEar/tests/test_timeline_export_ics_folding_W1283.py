"""Tests for timeline_export ICS line-folding (RFC 5545) and privacy_mode PII guard.

W1279 findings:
  F1 MED — ICS line-folding violation (SUMMARY/DESCRIPTION exceed 75 octets).
  F2 MED — PII leak: raw transcript text in ICS SUMMARY when privacy_mode=True.
  F4 LOW — SVG tooltip embeds summary_text keywords without privacy guard.

Wave: W1283
"""

import ast
import os
import sys
import unittest

# ---------------------------------------------------------------------------
# Path setup — required pattern for this project's test suite
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.timeline_export import TimelineExporter, _fold_ics_line  # noqa: E402


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _max_octet_len(ical_text: str) -> int:
    """Return the maximum octet length of any unfolded content line in the iCal text."""
    max_len = 0
    for raw_line in ical_text.split("\r\n"):
        # Lines that start with a space are continuations — part of the previous line.
        # We measure the *physical* line length (i.e. after folding) against the RFC limit.
        max_len = max(max_len, len(raw_line.encode("utf-8")))
    return max_len


def _make_item(summary_text=None, text=None, start="2026-01-01T10:00:00"):
    item: dict = {"ts": start}
    if summary_text is not None:
        item["summary_text"] = summary_text
    if text is not None:
        item["text"] = text
    return item


# ---------------------------------------------------------------------------
# F1 — Line-folding tests
# ---------------------------------------------------------------------------


class TestIcsLineFolding(unittest.TestCase):
    """RFC 5545 §3.1: no content line may exceed 75 octets."""

    def setUp(self):
        self.exporter = TimelineExporter()

    # ── _fold_ics_line unit tests ──────────────────────────────────────────

    def test_short_line_unchanged(self):
        line = "SUMMARY:Hello"
        self.assertEqual(_fold_ics_line(line), line)

    def test_exactly_75_octets_unchanged(self):
        # 8-char property name + 1 colon = 9 chars; fill up to 75 with 'A'
        line = "SUMMARY:" + "A" * 67  # 8+67 = 75 bytes
        self.assertEqual(_fold_ics_line(line), line)

    def test_ics_summary_line_folded_at_75_octets(self):
        """F1: A SUMMARY value that pushes the line past 75 octets must be folded."""
        # 80-char ASCII value → line = "SUMMARY:" + 80 chars = 88 octets → must fold
        long_text = "A" * 80
        folded = _fold_ics_line(f"SUMMARY:{long_text}")
        for segment in folded.split("\r\n"):
            self.assertLessEqual(
                len(segment.encode("utf-8")),
                75,
                f"Segment exceeds 75 octets: {segment!r}",
            )
        # Re-assembling (remove CRLF SP) must recover original
        reassembled = folded.replace("\r\n ", "")
        self.assertEqual(reassembled, f"SUMMARY:{long_text}")

    def test_ics_description_line_folded(self):
        """F1: DESCRIPTION line that exceeds 75 octets must be folded."""
        long_desc = "Languages: ru, es, en | Recordings: 42 | Duration: 3600s"
        folded = _fold_ics_line(f"DESCRIPTION:{long_desc}")
        for segment in folded.split("\r\n"):
            self.assertLessEqual(
                len(segment.encode("utf-8")),
                75,
                f"Segment exceeds 75 octets: {segment!r}",
            )

    def test_ics_unicode_octet_count_correct(self):
        """F1: UTF-8 multi-byte characters count by octet, not by code-point."""
        # Each Cyrillic char = 2 bytes in UTF-8; 40 chars = 80 bytes for value
        # "SUMMARY:" = 8 bytes → total = 88 bytes → must fold
        cyrillic_val = "Привет мир! " * 4  # 48 chars, 48*2=96 bytes for value
        line = f"SUMMARY:{cyrillic_val}"
        folded = _fold_ics_line(line)
        for segment in folded.split("\r\n"):
            self.assertLessEqual(
                len(segment.encode("utf-8")),
                75,
                f"Segment exceeds 75 octets (Cyrillic): {segment!r}",
            )
        # Verify no broken sequences: re-decode must succeed and match original
        reassembled = folded.replace("\r\n ", "")
        self.assertEqual(reassembled, line)

    # ── Full export_ical integration ──────────────────────────────────────

    def test_export_ical_all_lines_within_75_octets(self):
        """All physical lines in the exported iCal must be ≤ 75 octets."""
        items = [
            _make_item(
                summary_text="Длинный текст транскрипции с кириллицей и english mixed content",
                start="2026-01-01T10:00:00",
            ),
            _make_item(
                summary_text="A" * 120,
                start="2026-01-02T10:00:00",
            ),
        ]
        ical = self.exporter.export_ical(items)
        max_len = _max_octet_len(ical)
        self.assertLessEqual(
            max_len,
            75,
            f"Found a line with {max_len} octets in iCal output",
        )


# ---------------------------------------------------------------------------
# F2 — Privacy mode SUMMARY tests
# ---------------------------------------------------------------------------


class TestIcsSummaryPrivacyMode(unittest.TestCase):
    """F2: ICS SUMMARY must not contain raw transcript when privacy_mode=True."""

    def setUp(self):
        self.exporter = TimelineExporter()

    def test_ics_summary_privacy_mode_generic(self):
        """When privacy_mode=True, SUMMARY is generic, NOT the transcript text."""
        item = _make_item(
            summary_text="Confidential transcript about private meeting",
            text="Raw text that must not appear",
            start="2026-01-01T10:00:00",
        )
        ical = self.exporter.export_ical([item], privacy_mode=True)
        self.assertIn("Krab Ear recording", ical)
        self.assertNotIn("Confidential", ical)
        self.assertNotIn("Raw text", ical)

    def test_ics_summary_privacy_mode_false_uses_summary_text(self):
        """When privacy_mode=False (default), SUMMARY uses summary_text."""
        item = _make_item(
            summary_text="Meeting summary notes",
            start="2026-01-01T10:00:00",
        )
        ical = self.exporter.export_ical([item], privacy_mode=False)
        self.assertIn("Meeting summary notes", ical)

    def test_ics_summary_no_privacy_falls_back_to_text(self):
        """When privacy_mode=False and summary_text absent, falls back to text[:80]."""
        item = _make_item(text="Raw fallback text", start="2026-01-01T10:00:00")
        ical = self.exporter.export_ical([item], privacy_mode=False)
        self.assertIn("Raw fallback text", ical)

    def test_ics_summary_default_is_privacy_off(self):
        """Default call (no privacy_mode arg) exposes summary_text (backward-compat)."""
        item = _make_item(
            summary_text="Should appear by default",
            start="2026-01-01T10:00:00",
        )
        ical = self.exporter.export_ical([item])
        self.assertIn("Should appear by default", ical)

    def test_ics_privacy_mode_generic_still_has_summary_property(self):
        """Even in privacy mode the SUMMARY property line must be present."""
        item = _make_item(start="2026-01-01T10:00:00")
        ical = self.exporter.export_ical([item], privacy_mode=True)
        # Find unfolded SUMMARY line
        unfolded = ical.replace("\r\n ", "")
        self.assertTrue(
            any(line.startswith("SUMMARY:") for line in unfolded.splitlines()),
            "SUMMARY property missing in privacy mode",
        )


# ---------------------------------------------------------------------------
# F4 — SVG tooltip privacy mode tests
# ---------------------------------------------------------------------------


class TestSvgTooltipPrivacyMode(unittest.TestCase):
    """F4: SVG tooltips must not embed summary_text keywords when privacy_mode=True."""

    def setUp(self):
        self.exporter = TimelineExporter()
        self.blocks = [
            {
                "start_time": "2026-01-01T10:00:00",
                "items_count": 5,
                "languages": ["ru"],
                "total_duration_sec": 300.0,
                "summary_text": "Secret meeting content",
            }
        ]

    def test_svg_tooltip_privacy_mode_no_keywords(self):
        """When privacy_mode=True, SVG <title> must not contain summary_text."""
        svg = self.exporter.export_svg(self.blocks, privacy_mode=True)
        self.assertNotIn("Secret meeting content", svg)

    def test_svg_tooltip_privacy_mode_false_includes_keywords(self):
        """When privacy_mode=False (default), SVG <title> includes summary_text."""
        svg = self.exporter.export_svg(self.blocks, privacy_mode=False)
        self.assertIn("Secret meeting content", svg)

    def test_svg_tooltip_default_includes_keywords(self):
        """Default export_svg (no privacy_mode) includes summary_text in tooltip."""
        svg = self.exporter.export_svg(self.blocks)
        self.assertIn("Secret meeting content", svg)

    def test_svg_tooltip_privacy_mode_keeps_metadata(self):
        """Privacy mode removes transcript text but keeps timestamp/count metadata."""
        svg = self.exporter.export_svg(self.blocks, privacy_mode=True)
        self.assertIn("5 items", svg)
        self.assertIn("ru", svg)


# ---------------------------------------------------------------------------
# AST smoke-test — verify _fold_ics_line is a module-level function
# ---------------------------------------------------------------------------


class TestAstSmokeTest(unittest.TestCase):
    """Verify _fold_ics_line exists as a module-level function via AST."""

    def test_fold_ics_line_is_module_level_function(self):
        module_path = os.path.join(
            os.path.dirname(__file__), "..", "backend", "timeline_export.py"
        )
        with open(module_path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        top_level_funcs = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and isinstance(node.col_offset, int) and node.col_offset == 0
        }
        self.assertIn(
            "_fold_ics_line",
            top_level_funcs,
            "_fold_ics_line must be a module-level function",
        )

    def test_export_ical_has_privacy_mode_param(self):
        """export_ical must accept a privacy_mode parameter."""
        import inspect
        sig = inspect.signature(TimelineExporter.export_ical)
        self.assertIn("privacy_mode", sig.parameters)

    def test_export_svg_has_privacy_mode_param(self):
        """export_svg must accept a privacy_mode parameter."""
        import inspect
        sig = inspect.signature(TimelineExporter.export_svg)
        self.assertIn("privacy_mode", sig.parameters)


if __name__ == "__main__":
    unittest.main()
