"""Tests for timeline_export output-injection hardening (W1770).

Findings (REAL MED — output injection via transcript-derived text):
  - SVG XSS: user title/topic/label text embedded into <text>/<title> SVG nodes
    must be XML-escaped (stdlib xml.sax.saxutils.escape) — opening the .svg in a
    browser must NOT execute injected markup/script.
  - iCal (.ics) injection (RFC 5545): CR/LF or special chars in SUMMARY/DESCRIPTION
    must be escaped per §3.3.11 — a raw newline or unescaped ';'/',' could inject a
    brand-new iCal property/line (e.g. a forged VEVENT).

Each test fails before the W1770 fix and passes after. Normal exports stay
well-formed (the SVG parses as XML; the ics property lines stay intact).

Wave: W1770
"""

from __future__ import annotations

import os
import sys
import unittest
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# Path setup — required pattern for this project's test suite
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.timeline_export import TimelineExporter  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_block(
    start_time: str = "2026-04-10T14:00:00+00:00",
    end_time: str = "2026-04-10T15:00:00+00:00",
    items_count: int = 5,
    summary_text: str = "тест запись аудио",
    languages=None,
):
    return {
        "start_time": start_time,
        "end_time": end_time,
        "items_count": items_count,
        "total_duration_sec": 120.0,
        "total_words": 80,
        "languages": languages if languages is not None else ["ru"],
        "summary_text": summary_text,
    }


def _ics_property_lines(ical_text: str):
    """Unfold ics lines (RFC 5545 §3.1) and return logical content lines.

    Continuation lines start with a single space — they belong to the previous
    line and are joined back, so injected raw newlines surface as brand-new
    logical lines (which is exactly what we assert against).
    """
    logical: list[str] = []
    for raw in ical_text.split("\r\n"):
        if raw.startswith(" ") and logical:
            logical[-1] += raw[1:]
        else:
            logical.append(raw)
    return logical


# ---------------------------------------------------------------------------
# SVG XSS
# ---------------------------------------------------------------------------

class SvgInjectionTestCase(unittest.TestCase):
    """SVG output must neutralise transcript-derived markup/script."""

    def setUp(self) -> None:
        self.exporter = TimelineExporter()

    def test_svg_script_payload_in_summary_escaped(self) -> None:
        """A title closing </text> then injecting <script> must be escaped."""
        payload = "</text><script>alert(1)</script>"
        svg = self.exporter.export_svg([_make_block(summary_text=payload)])
        # No raw script tag / no raw closing </text> from the payload survives.
        self.assertNotIn("<script>", svg)
        self.assertNotIn("</script>", svg)
        self.assertNotIn("<script>alert(1)</script>", svg)
        # The escaped form is present instead.
        self.assertIn("&lt;script&gt;", svg)

    def test_svg_script_payload_in_label_escaped(self) -> None:
        """Injection via the X-axis label (start_time string) is also escaped."""
        payload = '"><script>alert(2)</script>'
        svg = self.exporter.export_svg([_make_block(start_time=payload)])
        self.assertNotIn("<script>", svg)
        self.assertNotIn("</script>", svg)

    def test_svg_with_payload_parses_as_xml(self) -> None:
        """Even with a hostile title the SVG remains well-formed XML."""
        payload = "</text><script>alert(1)</script>&<>\"'"
        svg = self.exporter.export_svg([_make_block(summary_text=payload)])
        # Strip the XML declaration; ElementTree.fromstring rejects raw '<script>'
        # had escaping failed, so a clean parse proves the markup was neutralised.
        body = svg.split("?>", 1)[-1].lstrip()
        root = ET.fromstring(body)
        self.assertTrue(root.tag.endswith("svg"))

    def test_normal_svg_still_well_formed(self) -> None:
        """A benign export still parses as XML and keeps its title text."""
        svg = self.exporter.export_svg([_make_block(summary_text="meeting notes")])
        body = svg.split("?>", 1)[-1].lstrip()
        root = ET.fromstring(body)
        self.assertTrue(root.tag.endswith("svg"))


# ---------------------------------------------------------------------------
# iCal (.ics) injection
# ---------------------------------------------------------------------------

class IcalInjectionTestCase(unittest.TestCase):
    """iCal SUMMARY/DESCRIPTION must not allow new-property injection."""

    def setUp(self) -> None:
        self.exporter = TimelineExporter()

    def test_summary_crlf_does_not_inject_property(self) -> None:
        """CRLF + 'BEGIN:VEVENT' in SUMMARY must not create a forged property line."""
        payload = "Lunch\r\nBEGIN:VEVENT\r\nSUMMARY:Injected"
        block = _make_block(summary_text=payload)
        ical = self.exporter.export_ical([block])

        logical = _ics_property_lines(ical)
        # The injected text must stay folded INSIDE the single SUMMARY value, never
        # surfacing as its own logical property line. We assert on unfolded logical
        # lines (not raw substring count, since the harmless escaped text "BEGIN:VEVENT"
        # legitimately remains inline inside the SUMMARY value).
        self.assertEqual(
            sum(1 for ln in logical if ln == "BEGIN:VEVENT"), 1,
            "payload must not inject a second BEGIN:VEVENT property line",
        )
        self.assertNotIn("SUMMARY:Injected", logical)
        # The newlines became literal "\n" inside the (single) SUMMARY value.
        summary_lines = [ln for ln in logical if ln.startswith("SUMMARY:")]
        self.assertEqual(len(summary_lines), 1)
        self.assertIn("\\n", summary_lines[0])
        # And the forged property text rode along inside that one value (escaped),
        # i.e. it did NOT become a real standalone line.
        self.assertIn("BEGIN:VEVENT", summary_lines[0])

    def test_summary_lone_cr_normalised(self) -> None:
        """A bare CR must be normalised to literal '\\n' (not silently dropped)."""
        block = _make_block(summary_text="line1\rline2")
        ical = self.exporter.export_ical([block])
        logical = _ics_property_lines(ical)
        summary_lines = [ln for ln in logical if ln.startswith("SUMMARY:")]
        self.assertEqual(len(summary_lines), 1)
        self.assertIn("\\n", summary_lines[0])
        self.assertIn("line1", summary_lines[0])
        self.assertIn("line2", summary_lines[0])

    def test_summary_semicolon_comma_escaped(self) -> None:
        """';' and ',' in SUMMARY must be backslash-escaped per RFC 5545."""
        escaped = self.exporter._ical_escape("a;b,c")
        self.assertIn("\\;", escaped)
        self.assertIn("\\,", escaped)
        self.assertNotIn("a;b", escaped)

    def test_no_raw_newline_in_any_property_value(self) -> None:
        """Hostile DESCRIPTION-derived languages must not emit a raw newline."""
        block = _make_block(
            summary_text="ok",
            languages=["ru\r\nX-PIRATE:1", "es"],
        )
        ical = self.exporter.export_ical([block])
        self.assertEqual(ical.count("BEGIN:VEVENT"), 1)
        logical = _ics_property_lines(ical)
        self.assertNotIn("X-PIRATE:1", logical)

    def test_normal_ical_lines_intact(self) -> None:
        """A benign export keeps the expected RFC 5545 property lines."""
        ical = self.exporter.export_ical([_make_block(summary_text="важная встреча")])
        logical = _ics_property_lines(ical)
        self.assertIn("BEGIN:VCALENDAR", logical)
        self.assertIn("END:VCALENDAR", logical)
        self.assertEqual(ical.count("BEGIN:VEVENT"), 1)
        # SUMMARY value preserved (Cyrillic survives, no escaping artefacts).
        summary_lines = [ln for ln in logical if ln.startswith("SUMMARY:")]
        self.assertEqual(len(summary_lines), 1)
        self.assertIn("важная встреча", summary_lines[0])


if __name__ == "__main__":
    unittest.main()
