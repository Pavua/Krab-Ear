"""KE05C-12: backstop усечения текста доходит до TopicTracker.

``enrich`` усекал текст до 200 000 символов для regex-операций, но в ``_extract_topics_for_item``
передавал ИСХОДНУЮ запись — TopicTracker получал неограниченный текст, и верхняя граница дорогой
обработки не соблюдалась.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.metadata_enricher import _MAX_TEXT_LEN, MetadataEnricher  # noqa: E402


class TopicsReceiveClippedText(unittest.TestCase):
    def test_topic_extraction_never_sees_more_than_the_backstop(self):
        enricher = MetadataEnricher()
        seen: list[int] = []
        original = enricher._extract_topics_for_item

        def spy(item):
            seen.append(len(str(item.get("text") or "")))
            return original(item)

        enricher._extract_topics_for_item = spy
        huge = "слово " * (_MAX_TEXT_LEN // 6 + 5000)
        self.assertGreater(len(huge), _MAX_TEXT_LEN)
        out = enricher.enrich({"id": "1", "text": huge})
        self.assertEqual(len(seen), 1)
        self.assertLessEqual(seen[0], _MAX_TEXT_LEN)
        # исходная запись в ответе не усечена и не изменена
        self.assertEqual(len(out["text"]), len(huge))

    def test_short_text_is_passed_unchanged(self):
        enricher = MetadataEnricher()
        seen: list[str] = []
        original = enricher._extract_topics_for_item
        enricher._extract_topics_for_item = lambda item: (seen.append(item["text"]), original(item))[1]
        enricher.enrich({"id": "1", "text": "привет мир как дела"})
        self.assertEqual(seen, ["привет мир как дела"])

    def test_other_item_fields_are_still_passed(self):
        enricher = MetadataEnricher()
        keys: list[set] = []
        original = enricher._extract_topics_for_item
        enricher._extract_topics_for_item = lambda item: (keys.append(set(item)), original(item))[1]
        enricher.enrich({"id": "1", "text": "привет", "timestamp": "2026-01-01T00:00:00Z"})
        self.assertTrue({"id", "text", "timestamp"} <= keys[0])


if __name__ == "__main__":
    unittest.main()
