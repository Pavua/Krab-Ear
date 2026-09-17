"""Тест парсера сценария R2 (scripts/record_golden.py).

Сценарий `docs/golden/r2-scenario.md` — источник фраз для записи
golden-набора (стенд качества STT, эпик R2). Парсер обязан выдавать
ровно 30 RU + 12 ES + 8 EN со сквозными id `ru_001…` и не тащить
markdown-обёртки в текст фразы.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "record_golden.py"
SCENARIO_PATH = REPO_ROOT / "docs" / "golden" / "r2-scenario.md"


def _load_module():
    spec = importlib.util.spec_from_file_location("record_golden_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    # dataclasses резолвит аннотации через sys.modules[cls.__module__] — до exec_module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ParseScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()
        cls.phrases = cls.mod.parse_scenario(SCENARIO_PATH.read_text(encoding="utf-8"))

    def test_counts_per_language(self) -> None:
        counts: dict[str, int] = {}
        for phrase in self.phrases:
            counts[phrase.lang] = counts.get(phrase.lang, 0) + 1
        self.assertEqual(counts, {"ru": 30, "es": 12, "en": 8})

    def test_ids_are_sequential_and_unique(self) -> None:
        for lang, total in (("ru", 30), ("es", 12), ("en", 8)):
            ids = [p.phrase_id for p in self.phrases if p.lang == lang]
            self.assertEqual(ids, [f"{lang}_{i:03d}" for i in range(1, total + 1)])
        all_ids = [p.phrase_id for p in self.phrases]
        self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_first_phrases_sane(self) -> None:
        by_id = {p.phrase_id: p.text for p in self.phrases}
        self.assertTrue(by_id["ru_001"].startswith("Напомни"))
        self.assertTrue(by_id["es_001"].startswith("Recuérdame"))
        self.assertTrue(by_id["en_001"].startswith("Remind"))
        # Парсер не тащит markdown-обёртки и не режет текст.
        for text in by_id.values():
            self.assertNotIn("`", text)
            self.assertGreater(len(text), 10)

    def test_numbered_lines_outside_phrase_sections_ignored(self) -> None:
        """«Как записывать» тоже нумерован (1–6) — в фразы попасть не должен."""
        self.assertGreater(len(self.phrases), 0)
        for phrase in self.phrases:
            self.assertNotIn("mkdir", phrase.text)
            self.assertNotIn("ffmpeg", phrase.text)

    def test_empty_text_yields_no_phrases(self) -> None:
        self.assertEqual(self.mod.parse_scenario(""), [])

    def test_text_without_phrase_sections_yields_nothing(self) -> None:
        self.assertEqual(self.mod.parse_scenario("# Заголовок\n\n1. не фраза\n"), [])


if __name__ == "__main__":
    unittest.main()
