"""Языковые профили словаря подсказок (03.09.2026).

Запрос владельца: медико-юридический словарь на испанском полезен для звонков
и импорта испанских записей, но в русской диктовке он бесполезен — а место в
`initial_prompt` занимает. Бюджет Whisper (224 токена) на живых диктовках уже
режется на 41%, и каждый лишний термин выкупает место у контекста истории.

Поэтому термины разложены по языкам: общий список действует всегда, языковой
подключается только когда распознан этот язык.
"""
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.transcript_context import merge_language_hotwords  # noqa: E402


class MergeLanguageHotwordsTest(unittest.TestCase):
    PER_LANG = {
        "es": ["oxicodona", "pregabalina", "receta electrónica"],
        "ru": ["оксикодон"],
    }

    def test_language_terms_are_added_for_matching_language(self):
        merged = merge_language_hotwords(["Krab Ear"], "es", self.PER_LANG)
        self.assertIn("oxicodona", merged)
        self.assertIn("Krab Ear", merged)

    def test_other_languages_do_not_leak(self):
        """Главное свойство: испанские термины не занимают место в русской диктовке."""
        merged = merge_language_hotwords(["Krab Ear"], "ru", self.PER_LANG)
        self.assertNotIn("oxicodona", merged)
        self.assertIn("оксикодон", merged)

    def test_unknown_language_falls_back_to_common_list_only(self):
        merged = merge_language_hotwords(["Krab Ear"], "de", self.PER_LANG)
        self.assertEqual(merged, ["Krab Ear"])

    def test_missing_language_is_not_an_error(self):
        """Язык не определился — это норма, а не повод потерять общий список."""
        self.assertEqual(merge_language_hotwords(["Krab Ear"], None, self.PER_LANG), ["Krab Ear"])
        self.assertEqual(merge_language_hotwords(["Krab Ear"], "", self.PER_LANG), ["Krab Ear"])

    def test_order_is_stable_and_duplicates_collapse(self):
        """Порядок важен: обрезка промпта идёт с конца, и общий список должен
        стоять первым — он относится ко всем диктовкам, а языковой к одной."""
        merged = merge_language_hotwords(
            ["Krab Ear", "oxicodona"], "es", self.PER_LANG
        )
        self.assertEqual(merged[0], "Krab Ear")
        self.assertEqual(merged.count("oxicodona"), 1)

    def test_case_and_region_suffix_are_tolerated(self):
        """Движки отдают язык по-разному: `ES`, `es-ES`, `es`."""
        for lang in ("ES", "es-ES", "es_ES"):
            with self.subTest(lang=lang):
                self.assertIn("oxicodona", merge_language_hotwords([], lang, self.PER_LANG))

    def test_empty_inputs_give_empty_result(self):
        self.assertEqual(merge_language_hotwords([], "es", {}), [])
        self.assertEqual(merge_language_hotwords(None, "es", None), [])


if __name__ == "__main__":
    unittest.main()
