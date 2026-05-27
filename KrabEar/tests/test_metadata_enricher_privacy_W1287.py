"""Tests for MetadataEnricher privacy_mode topic-skip (W1277 F5 LOW / W1287)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.metadata_enricher import MetadataEnricher


def _make_item(text: str = "Технологии программирование искусственный интеллект данные") -> dict:
    return {
        "text": text,
        "duration_sec": 10.0,
        "confidence": 0.85,
        "has_diarization": False,
        "has_llm_enhancement": False,
        "timestamp": "",
    }


class PrivacyModeTopicSkipTestCase(unittest.TestCase):
    """W1287: MetadataEnricher must skip topic enrichment in privacy_mode."""

    def _make_enricher(self, privacy_mode: bool) -> MetadataEnricher:
        settings = {"privacy_mode_enabled": privacy_mode}
        return MetadataEnricher(settings_provider=lambda: settings)

    # ── test_enrich_recording_skips_topics_in_privacy_mode ───────────────────

    def test_enrich_recording_skips_topics_in_privacy_mode(self) -> None:
        """When privacy_mode_enabled=True, topics must be an empty list."""
        enricher = self._make_enricher(privacy_mode=True)
        item = _make_item(
            "Технологии программирование искусственный интеллект данные алгоритмы"
        )
        result = enricher.enrich(item)
        topics = result["metadata"]["topics"]
        self.assertIsInstance(topics, list)
        self.assertEqual(
            topics,
            [],
            "topics must be empty when privacy_mode_enabled=True",
        )

    # ── test_enrich_recording_normal_when_privacy_disabled ───────────────────

    def test_enrich_recording_normal_when_privacy_disabled(self) -> None:
        """When privacy_mode_enabled=False, topics may be populated (non-empty list)."""
        enricher = self._make_enricher(privacy_mode=False)
        # Use a text with enough keyword-dense content to give TopicTracker words to extract
        item = _make_item(
            "Программирование Python искусственный интеллект технологии данные алгоритмы"
        )
        result = enricher.enrich(item)
        topics = result["metadata"]["topics"]
        self.assertIsInstance(topics, list, "topics must be a list")
        # Topics CAN be non-empty; the key invariant is that the list type is preserved
        # and no exception was raised.  We do not assert non-empty because the
        # TopicTracker may legitimately return [] for short inputs.
        for t in topics:
            self.assertIsInstance(t, str)

    # ── test_enrich_other_fields_still_populated_in_privacy_mode ────────────

    def test_enrich_other_fields_still_populated_in_privacy_mode(self) -> None:
        """Non-topic metadata fields must still be present and valid in privacy_mode."""
        enricher = self._make_enricher(privacy_mode=True)
        item = _make_item("Привет мир. Это тест для проверки.")
        result = enricher.enrich(item)
        meta = result["metadata"]

        # All other fields must be present
        self.assertIn("word_count", meta)
        self.assertIn("sentence_count", meta)
        self.assertIn("avg_word_length", meta)
        self.assertIn("language_detected", meta)
        self.assertIn("emotion", meta)
        self.assertIn("speech_pace_wpm", meta)
        self.assertIn("quality_grade", meta)
        self.assertIn("auto_title", meta)
        self.assertIn("enriched_at", meta)

        # Sanity-check non-topic values
        self.assertGreater(meta["word_count"], 0)
        self.assertIsInstance(meta["language_detected"], str)
        self.assertIn(meta["quality_grade"], ("A", "B", "C", "D", "F"))

    # ── additional guard: no settings_provider defaults to no privacy ────────

    def test_no_settings_provider_defaults_to_no_privacy_mode(self) -> None:
        """Without settings_provider, privacy_mode defaults to False (topics populated)."""
        enricher = MetadataEnricher()  # no settings_provider
        item = _make_item(
            "Программирование Python технологии данные алгоритмы обработка"
        )
        result = enricher.enrich(item)
        meta = result["metadata"]
        # topics key must always be present
        self.assertIn("topics", meta)
        self.assertIsInstance(meta["topics"], list)

    # ── privacy mode does not mutate original item ───────────────────────────

    def test_privacy_mode_does_not_mutate_original_item(self) -> None:
        """enrich() must not mutate the original item even in privacy_mode."""
        enricher = self._make_enricher(privacy_mode=True)
        item = _make_item("Тест приватного режима")
        enricher.enrich(item)
        self.assertNotIn("metadata", item)

    # ── topics key still present in metadata when privacy_mode=True ─────────

    def test_topics_key_present_in_metadata_when_privacy_mode(self) -> None:
        """metadata dict must still contain the 'topics' key (value=[]) in privacy_mode."""
        enricher = self._make_enricher(privacy_mode=True)
        result = enricher.enrich(_make_item())
        self.assertIn("topics", result["metadata"])

    # ── settings_provider raises: should default to no privacy ───────────────

    def test_faulty_settings_provider_defaults_to_no_privacy(self) -> None:
        """If settings_provider raises, privacy_mode defaults to False (safe fallback)."""
        def bad_provider():
            raise RuntimeError("provider broken")

        enricher = MetadataEnricher(settings_provider=bad_provider)
        # Should not raise; topics may be non-empty
        result = enricher.enrich(_make_item("Данные алгоритм программирование"))
        meta = result["metadata"]
        self.assertIn("topics", meta)
        self.assertIsInstance(meta["topics"], list)


class PrivacyModeRuntimeToggleTestCase(unittest.TestCase):
    """Verify that the privacy flag is read on each enrich() call (runtime toggle)."""

    def test_runtime_toggle_respected_per_call(self) -> None:
        """Toggling privacy_mode between calls affects topic enrichment dynamically."""
        state = {"privacy_mode_enabled": False}
        enricher = MetadataEnricher(settings_provider=lambda: dict(state))

        # With privacy OFF — topics list is a list (may or may not be non-empty)
        item = _make_item(
            "Программирование Python технологии данные алгоритмы обработка"
        )
        result_off = enricher.enrich(item)
        # No exception; topics is a list
        self.assertIsInstance(result_off["metadata"]["topics"], list)

        # Now enable privacy_mode
        state["privacy_mode_enabled"] = True
        result_on = enricher.enrich(item)
        self.assertEqual(result_on["metadata"]["topics"], [])

        # Disable again
        state["privacy_mode_enabled"] = False
        result_off2 = enricher.enrich(item)
        self.assertIsInstance(result_off2["metadata"]["topics"], list)


if __name__ == "__main__":
    unittest.main()
