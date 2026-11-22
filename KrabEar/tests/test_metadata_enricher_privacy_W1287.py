"""Tests for MetadataEnricher privacy_mode topic-skip (W1277 F5 LOW / W1287)."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch
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
        item = _make_item(
            "Программирование Python искусственный интеллект технологии данные алгоритмы"
        )
        # Детерминированный extractor: точное значение и сам факт вызова (раньше проверялся
        # только тип списка — постоянно пустые topics проходили).
        with patch.object(enricher, "_extract_topics_for_item",
                          return_value=["qa-topic"]) as extractor:
            result = enricher.enrich(item)
        self.assertEqual(result["metadata"]["topics"], ["qa-topic"])
        extractor.assert_called_once()

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

    def test_faulty_settings_provider_defaults_to_privacy_on(self) -> None:
        """If settings_provider raises, privacy_mode defaults to ON (fail-closed)."""
        def bad_provider():
            raise RuntimeError("provider broken")

        enricher = MetadataEnricher(settings_provider=bad_provider)
        with patch.object(enricher, "_extract_topics_for_item",
                          return_value=["qa-topic"]) as extractor:
            result = enricher.enrich(_make_item("Данные алгоритм программирование"))
        extractor.assert_not_called()
        meta = result["metadata"]
        self.assertIn("topics", meta)
        self.assertEqual(meta["topics"], [])


class PrivacyModeRuntimeToggleTestCase(unittest.TestCase):
    """Verify that the privacy flag is read on each enrich() call (runtime toggle)."""

    def test_runtime_toggle_respected_per_call(self) -> None:
        """Toggling privacy_mode between calls affects topic enrichment dynamically."""
        state = {"privacy_mode_enabled": False}
        provider = MagicMock(side_effect=lambda: dict(state))
        enricher = MetadataEnricher(settings_provider=provider)
        item = _make_item(
            "Программирование Python технологии данные алгоритмы обработка"
        )
        with patch.object(enricher, "_extract_topics_for_item",
                          return_value=["qa-topic"]) as extractor:
            # OFF → точное значение и вызов extractor
            self.assertEqual(enricher.enrich(item)["metadata"]["topics"], ["qa-topic"])
            self.assertEqual(extractor.call_count, 1)

            # ON → пусто, extractor НЕ вызывается (текст даже не попадает в извлечение)
            state["privacy_mode_enabled"] = True
            self.assertEqual(enricher.enrich(item)["metadata"]["topics"], [])
            self.assertEqual(extractor.call_count, 1)

            # снова OFF → результат восстановлен
            state["privacy_mode_enabled"] = False
            self.assertEqual(enricher.enrich(item)["metadata"]["topics"], ["qa-topic"])
            self.assertEqual(extractor.call_count, 2)
        # провайдер опрашивается на каждый enrich (флаг читается в момент вызова)
        self.assertGreaterEqual(provider.call_count, 3)


if __name__ == "__main__":
    unittest.main()
