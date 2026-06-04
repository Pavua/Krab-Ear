"""Regression tests — Wave 1732: XSS via source_lang in recap email HTML sink.

Attack vector:
  A history item stored with source_lang='<script>alert(1)</script>' (or any
  HTML payload) reaches the recap email HTML body through two paths:
    1. daily_digest.py aggregates languages_used[lang] from raw item.source_lang
       with only .strip() — no whitelist.
    2. recap_scheduler._build_html() interpolates lang_str (built from
       languages_used) directly into HTML with no html.escape().

Fix (Wave 1732):
  PRIMARY   — html.escape() at every user-derived HTML sink in _build_html().
  SECONDARY — RFC-5646 whitelist at daily_digest.DailyDigestGenerator aggregation
              point (defense-in-depth, consistent with recording_insights.py fix
              in Wave 1725).

Tests:
  1. _build_html escapes malicious lang in languages_used dict.
  2. _build_html escapes malicious highlight text.
  3. _build_html escapes malicious topic keyword.
  4. _build_html escapes malicious date string.
  5. DailyDigestGenerator drops malicious source_lang (whitelist gate).
  6. DailyDigestGenerator keeps valid RFC-5646 lang codes (ru, en, zh-Hant).
  7. End-to-end: stored malicious source_lang → digest → HTML → no raw <script>.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tests.test_helpers import make_test_item  # noqa: E402

from backend.recap_scheduler import _build_html
from backend.daily_digest import DailyDigestGenerator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_XSS_LANG = "<script>alert(1)</script>"
_XSS_TEXT = "<img src=x onerror=alert(2)>"
_XSS_TOPIC = '"><svg onload=alert(3)>'
_XSS_DATE = "<script>alert(4)</script>"


def _make_digest(**kwargs):
    """Build a minimal DailyDigest-compatible namespace."""
    defaults = dict(
        date="2026-05-31",
        total_recordings=1,
        total_duration_min=2.5,
        total_words=42,
        languages_used={"ru": 1},
        top_topics=["встреча"],
        highlights=["Нормальный текст."],
        formatted_markdown="# ok",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# 1–4: _build_html sink escaping
# ---------------------------------------------------------------------------

class TestBuildHtmlEscapesUserData(unittest.TestCase):
    """_build_html must not emit raw user-controlled HTML in any field."""

    def _assert_no_raw_script(self, html_str: str, payload: str, context: str):
        self.assertNotIn(payload, html_str, f"Raw payload leaked in {context}!")

    def test_escapes_malicious_lang_in_languages_used(self):
        """lang key from languages_used dict must be html-escaped at the sink."""
        digest = _make_digest(languages_used={_XSS_LANG: 1})
        result = _build_html(digest)
        self._assert_no_raw_script(result, _XSS_LANG, "lang_str")
        # Confirm it appears escaped instead
        self.assertIn("&lt;script&gt;", result)
        self.assertNotIn("<script>", result)

    def test_escapes_malicious_highlight_text(self):
        """Highlight snippets (raw transcript text) must be html-escaped."""
        digest = _make_digest(highlights=[_XSS_TEXT])
        result = _build_html(digest)
        self._assert_no_raw_script(result, _XSS_TEXT, "highlights")
        self.assertIn("&lt;img", result)

    def test_escapes_malicious_topic_keyword(self):
        """Top-topic keywords must be html-escaped."""
        digest = _make_digest(top_topics=[_XSS_TOPIC])
        result = _build_html(digest)
        self._assert_no_raw_script(result, _XSS_TOPIC, "top_topics")
        self.assertIn("&gt;&lt;svg", result)

    def test_escapes_malicious_date_string(self):
        """Date field must be html-escaped (defense-in-depth)."""
        digest = _make_digest(date=_XSS_DATE)
        result = _build_html(digest)
        self._assert_no_raw_script(result, _XSS_DATE, "date")
        self.assertIn("&lt;script&gt;", result)

    def test_multiple_malicious_languages_none_leak(self):
        """Multiple malicious lang codes — none should appear raw."""
        payloads = {
            "<b>bold</b>": 3,
            '"><script>x()</script>': 2,
            "<svg/onload=1>": 1,
        }
        digest = _make_digest(languages_used=payloads)
        result = _build_html(digest)
        for payload in payloads:
            self._assert_no_raw_script(result, payload, f"lang={payload!r}")
        # Raw < characters from payload must not appear unescaped
        # (we specifically check the attack vectors; &lt; is fine)
        self.assertNotIn("<b>bold</b>", result)
        self.assertNotIn("<svg/onload=1>", result)

    def test_benign_content_preserved(self):
        """Normal content must survive escaping intact (not double-escaped)."""
        digest = _make_digest(
            languages_used={"ru": 3, "en": 2},
            top_topics=["встреча", "задача"],
            highlights=["Обсудили квартальный план."],
        )
        result = _build_html(digest)
        self.assertIn("встреча", result)
        self.assertIn("задача", result)
        self.assertIn("Обсудили квартальный план.", result)
        # Numeric stats preserved
        self.assertIn("2.5", result)
        self.assertIn("42", result)


# ---------------------------------------------------------------------------
# 5–6: DailyDigestGenerator whitelist at aggregation point
# ---------------------------------------------------------------------------

class TestDailyDigestWhitelistsSourceLang(unittest.TestCase):
    """DailyDigestGenerator must reject malicious source_lang at aggregation."""

    def _make_item(self, source_lang: str, ts: str = "2026-05-31T10:00:00Z"):
        return make_test_item(
            source_lang=source_lang,
            ts=ts,
            audio_duration_sec=60.0,
            text="Hello world test text for digest",
            confidence=0.9,
        )

    def _run_with_items(self, items):
        gen = DailyDigestGenerator()
        store = MagicMock()
        store._load_active_items_with_lock.return_value = items
        return gen.generate_digest(date_str="2026-05-31", store=store)

    def test_malicious_source_lang_excluded(self):
        """<script> source_lang must be excluded from languages_used."""
        item = self._make_item(_XSS_LANG)
        digest = self._run_with_items([item])
        self.assertNotIn(_XSS_LANG, digest.languages_used,
                         "Malicious source_lang must not enter languages_used dict!")

    def test_xss_html_source_lang_excluded(self):
        """HTML injection in source_lang must be excluded."""
        item = self._make_item("<img src=x onerror=alert(1)>")
        digest = self._run_with_items([item])
        self.assertFalse(
            any("<" in k for k in digest.languages_used),
            "No HTML-containing key should exist in languages_used",
        )

    def test_valid_lang_codes_accepted(self):
        """Standard RFC-5646 language codes must be preserved."""
        items = [
            self._make_item("ru"),
            self._make_item("en"),
            self._make_item("zh-Hant"),
            self._make_item("es-419"),
        ]
        digest = self._run_with_items(items)
        self.assertIn("ru", digest.languages_used)
        self.assertIn("en", digest.languages_used)
        self.assertIn("zh-Hant", digest.languages_used)
        self.assertIn("es-419", digest.languages_used)

    def test_empty_lang_excluded(self):
        """Empty source_lang (None or '') must be silently ignored."""
        item_none = self._make_item("")
        digest = self._run_with_items([item_none])
        self.assertEqual({}, digest.languages_used)


# ---------------------------------------------------------------------------
# 7: End-to-end XSS chain
# ---------------------------------------------------------------------------

class TestEndToEndXSSChain(unittest.TestCase):
    """Full attack chain: malicious source_lang → digest → _build_html → no <script>."""

    def _make_item(self, source_lang: str):
        return make_test_item(
            source_lang=source_lang,
            ts="2026-05-31T10:00:00Z",
            audio_duration_sec=30.0,
            text="normal transcription text",
            confidence=0.85,
        )

    def test_stored_xss_lang_does_not_reach_html_output(self):
        """
        Simulate the full attack:
          1. Store item.source_lang = '<script>alert(1)</script>'
          2. Generate digest via DailyDigestGenerator
          3. Render HTML via _build_html
          4. Assert no raw <script> tag in final HTML
        """
        gen = DailyDigestGenerator()
        store = MagicMock()
        store._load_active_items_with_lock.return_value = [
            self._make_item(_XSS_LANG),
            self._make_item("ru"),  # one valid alongside malicious
        ]
        digest = gen.generate_digest(date_str="2026-05-31", store=store)
        html_output = _build_html(digest)

        # Primary assertion: no raw <script> tag in email HTML
        self.assertNotIn("<script>", html_output,
                         "XSS payload must not appear raw in email HTML!")

        # Whitelist gate: malicious lang also excluded from dict
        self.assertNotIn(_XSS_LANG, digest.languages_used)
        # Valid lang kept
        self.assertIn("ru", digest.languages_used)

    def test_malicious_transcript_text_escaped_in_highlights(self):
        """
        Transcript text itself (stored via STT) could contain HTML.
        Highlights are excerpts of item.text — must be escaped in the email.
        """
        gen = DailyDigestGenerator()
        store = MagicMock()
        store._load_active_items_with_lock.return_value = [
            self._make_item_with_text('<script>document.cookie</script>'),
        ]
        digest = gen.generate_digest(date_str="2026-05-31", store=store)
        html_output = _build_html(digest)
        self.assertNotIn("<script>", html_output)

    def _make_item_with_text(self, text: str):
        return make_test_item(
            source_lang="ru",
            ts="2026-05-31T10:00:00Z",
            audio_duration_sec=30.0,
            text=text,
            confidence=0.9,
        )


if __name__ == "__main__":
    unittest.main()
