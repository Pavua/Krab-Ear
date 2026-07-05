"""Тесты генератора appcast-item (Sparkle auto-update, spec 2026-07-05).

Скрипт scripts/generate_appcast_item.py вставляет <item> в appcast.xml.
Требования: монотонность версии (новая строго > максимальной существующей),
валидный XML на выходе, все обязательные Sparkle-атрибуты enclosure.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_SCRIPT = _PROJECT_ROOT / "scripts" / "generate_appcast_item.py"
_spec = importlib.util.spec_from_file_location("generate_appcast_item", _SCRIPT)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

SKELETON = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
  <channel>
    <title>Krab Ear</title>
    <link>https://example.invalid/appcast.xml</link>
    <description>Krab Ear updates</description>
    <language>ru</language>
  </channel>
</rss>
"""


def _add(xml_text, version, url="https://example.invalid/a.zip",
         sig="EDSIG==", length=1234):
    return gen.add_item(xml_text, version=version, url=url,
                        ed_signature=sig, length=length)


class TestAddItem(unittest.TestCase):
    def test_insert_into_skeleton_is_valid_xml_with_required_attrs(self):
        out = _add(SKELETON, "2.4.0")
        root = ET.fromstring(out)  # парсится => валидный XML
        ns = {"sparkle": "http://www.andymatuschak.org/xml-namespaces/sparkle"}
        enclosures = root.findall(".//item/enclosure")
        self.assertEqual(len(enclosures), 1)
        e = enclosures[0]
        self.assertEqual(e.get("url"), "https://example.invalid/a.zip")
        self.assertEqual(
            e.get("{http://www.andymatuschak.org/xml-namespaces/sparkle}version"),
            "2.4.0")
        self.assertEqual(e.get("length"), "1234")
        self.assertEqual(
            e.get("{http://www.andymatuschak.org/xml-namespaces/sparkle}edSignature"),
            "EDSIG==")
        min_os = root.find(".//item/sparkle:minimumSystemVersion", ns)
        self.assertIsNotNone(min_os)
        self.assertEqual(min_os.text, "13.0")

    def test_second_item_appends_and_keeps_first(self):
        out = _add(_add(SKELETON, "2.4.0"), "2.4.1")
        root = ET.fromstring(out)
        versions = [
            e.get("{http://www.andymatuschak.org/xml-namespaces/sparkle}version")
            for e in root.findall(".//item/enclosure")
        ]
        self.assertEqual(sorted(versions), ["2.4.0", "2.4.1"])

    def test_non_monotonic_version_rejected(self):
        once = _add(SKELETON, "2.4.0")
        with self.assertRaises(ValueError):
            _add(once, "2.4.0")   # равная
        with self.assertRaises(ValueError):
            _add(once, "2.3.9")   # меньшая

    def test_bad_semver_rejected(self):
        with self.assertRaises(ValueError):
            _add(SKELETON, "v2.4.0")
        with self.assertRaises(ValueError):
            _add(SKELETON, "2.4")


if __name__ == "__main__":
    unittest.main()
