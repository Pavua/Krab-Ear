"""Tests for PhoneticVocabulary (core) and PhoneticVocabService (backend).

Coverage:
- PhoneticVocabulary.correct():
    - variant replaced with canonical
    - many variants → one canonical (many-to-one)
    - longest variant wins over shorter prefix
    - case-insensitive match
    - no replacement inside larger words (word-boundary)
    - ReDoS-safe: regex-special variant treated literally (re.escape)
    - setting OFF → no change
- PhoneticVocabService CRUD round-trip (add / list / remove)
- Dedupe by canonical (add same canonical twice = merge variants)
- Empty canonical rejected (ValueError)
- Empty variants list rejected (ValueError)
- BackendService dispatch entries for all 3 methods
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.phonetic_vocabulary import PhoneticVocabulary
from backend.phonetic_vocab_service import PhoneticVocabService


# ---------------------------------------------------------------------------
# PhoneticVocabulary tests
# ---------------------------------------------------------------------------

class TestPhoneticVocabularyBasic(unittest.TestCase):
    """Basic correction logic."""

    def _make_vocab(self, entries, enabled=True):
        def _settings_get(key, default=None):
            if key == "phonetic_vocab_enabled":
                return enabled
            return default
        return PhoneticVocabulary(
            settings_get=_settings_get,
            entries_provider=lambda: entries,
        )

    def test_variant_replaced_with_canonical(self):
        """Misheard variant in text is replaced with canonical spelling."""
        vocab = self._make_vocab([{"canonical": "Павел", "variants": ["пашел"]}])
        result = vocab.correct("привет пашел как дела")
        self.assertIn("Павел", result)
        self.assertNotIn("пашел", result)

    def test_many_variants_to_one_canonical(self):
        """Multiple variants all map to the same canonical."""
        vocab = self._make_vocab([
            {"canonical": "Павел", "variants": ["пашел", "павэл", "павэль"]}
        ])
        result1 = vocab.correct("привет пашел")
        self.assertIn("Павел", result1)
        result2 = vocab.correct("это павэл написал")
        self.assertIn("Павел", result2)
        result3 = vocab.correct("а павэль пришёл")
        self.assertIn("Павел", result3)

    def test_longest_variant_wins(self):
        """Longest variant preferred over shorter prefix — no partial shadowing."""
        vocab = self._make_vocab([
            {"canonical": "Павел", "variants": ["пашел", "пашела"]}
        ])
        result = vocab.correct("пашела нет дома")
        self.assertIn("Павел", result)
        # 'пашел' must not have partially matched 'пашела' — only one canonical
        self.assertEqual(result.count("Павел"), 1)

    def test_case_insensitive_match(self):
        """Matching is case-insensitive."""
        vocab = self._make_vocab([{"canonical": "Демо", "variants": ["ДЕММА"]}])
        result = vocab.correct("это демма версия")
        self.assertIn("Демо", result)

    def test_no_replacement_inside_larger_word(self):
        """Variant 'пашел' must not match inside 'пашелов' (if that were a word)."""
        vocab = self._make_vocab([{"canonical": "Павел", "variants": ["demo"]}])
        # 'demo' should NOT be replaced inside 'demos'
        result = vocab.correct("look at demos here")
        self.assertNotIn("Павел", result)
        # standalone 'demo' SHOULD be replaced
        result2 = vocab.correct("look at demo here")
        self.assertIn("Павел", result2)

    def test_redos_safe_regex_special_variant(self):
        """Variant with regex-special chars like 'a(b' is treated literally."""
        vocab = self._make_vocab([{"canonical": "safe", "variants": ["a(b"]}])
        # Exact literal match should work
        result = vocab.correct("prefix a(b suffix")
        self.assertIn("safe", result)
        # No crash on the regex-special variant
        result2 = vocab.correct("no match here")
        self.assertEqual(result2, "no match here")

    def test_setting_off_no_change(self):
        """When phonetic_vocab_enabled=False, no replacement is performed."""
        vocab = self._make_vocab(
            [{"canonical": "Павел", "variants": ["пашел"]}],
            enabled=False,
        )
        text = "привет пашел как дела"
        result = vocab.correct(text)
        self.assertEqual(result, text)

    def test_empty_text_passthrough(self):
        """Empty string returns unchanged."""
        vocab = self._make_vocab([{"canonical": "Павел", "variants": ["пашел"]}])
        self.assertEqual(vocab.correct(""), "")

    def test_no_entries_passthrough(self):
        """No entries → original text returned unchanged."""
        vocab = self._make_vocab([])
        text = "hello world"
        self.assertEqual(vocab.correct(text), text)

    def test_multiple_entries_in_one_text(self):
        """Multiple distinct entries corrected in one pass."""
        vocab = self._make_vocab([
            {"canonical": "Павел", "variants": ["пашел"]},
            {"canonical": "демо", "variants": ["демма"]},
        ])
        text = "пашел показал демма продукт"
        result = vocab.correct(text)
        self.assertIn("Павел", result)
        self.assertIn("демо", result)

    def test_invalid_entry_skipped(self):
        """Entry with non-list variants is silently skipped."""
        vocab = self._make_vocab([
            {"canonical": "ok", "variants": "not-a-list"},
            {"canonical": "Павел", "variants": ["пашел"]},
        ])
        result = vocab.correct("привет пашел")
        self.assertIn("Павел", result)

    def test_empty_variant_in_list_skipped(self):
        """Empty string variants are silently skipped."""
        vocab = self._make_vocab([
            {"canonical": "Павел", "variants": ["", "пашел"]}
        ])
        result = vocab.correct("привет пашел")
        self.assertIn("Павел", result)


# ---------------------------------------------------------------------------
# PhoneticVocabService CRUD tests
# ---------------------------------------------------------------------------

class TestPhoneticVocabServiceCRUD(unittest.TestCase):
    """CRUD round-trip tests for PhoneticVocabService."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.svc = PhoneticVocabService(data_dir=Path(self._tmp))

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_add_and_list(self):
        """add_phonetic_entry → list_phonetic_entries returns the entry."""
        result = self.svc.handle_add_phonetic_entry(
            {"canonical": "Павел", "variants": ["пашел", "павэл"]}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["canonical"], "Павел")
        listing = self.svc.handle_list_phonetic_entries({})
        self.assertTrue(listing["ok"])
        canonicals = [e["canonical"] for e in listing["entries"]]
        self.assertIn("Павел", canonicals)

    def test_variants_stored(self):
        """Variants are stored and retrievable."""
        self.svc.handle_add_phonetic_entry(
            {"canonical": "демо", "variants": ["демма", "дэмо"]}
        )
        listing = self.svc.handle_list_phonetic_entries({})
        entry = next(e for e in listing["entries"] if e["canonical"] == "демо")
        self.assertIn("демма", entry["variants"])
        self.assertIn("дэмо", entry["variants"])

    def test_remove(self):
        """add → remove → list gives empty."""
        self.svc.handle_add_phonetic_entry(
            {"canonical": "Павел", "variants": ["пашел"]}
        )
        remove_result = self.svc.handle_remove_phonetic_entry({"canonical": "Павел"})
        self.assertTrue(remove_result["ok"])
        self.assertTrue(remove_result["removed"])
        listing = self.svc.handle_list_phonetic_entries({})
        self.assertEqual(listing["entries"], [])

    def test_remove_not_found_raises(self):
        """Removing a canonical that doesn't exist raises RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_remove_phonetic_entry({"canonical": "nonexistent"})

    def test_dedupe_by_canonical_merges_variants(self):
        """Adding same canonical twice merges variants (no duplicate entry)."""
        self.svc.handle_add_phonetic_entry(
            {"canonical": "Павел", "variants": ["пашел"]}
        )
        self.svc.handle_add_phonetic_entry(
            {"canonical": "Павел", "variants": ["павэл"]}
        )
        listing = self.svc.handle_list_phonetic_entries({})
        pavels = [e for e in listing["entries"] if e["canonical"] == "Павел"]
        self.assertEqual(len(pavels), 1)
        self.assertIn("пашел", pavels[0]["variants"])
        self.assertIn("павэл", pavels[0]["variants"])

    def test_dedupe_canonical_case_insensitive(self):
        """Canonical dedup is case-insensitive: 'павел' and 'Павел' → one entry."""
        self.svc.handle_add_phonetic_entry(
            {"canonical": "павел", "variants": ["пашел"]}
        )
        self.svc.handle_add_phonetic_entry(
            {"canonical": "Павел", "variants": ["павэл"]}
        )
        listing = self.svc.handle_list_phonetic_entries({})
        self.assertEqual(len(listing["entries"]), 1)

    def test_empty_canonical_rejected(self):
        """Empty canonical raises ValueError."""
        with self.assertRaises(ValueError):
            self.svc.handle_add_phonetic_entry(
                {"canonical": "", "variants": ["пашел"]}
            )

    def test_whitespace_only_canonical_rejected(self):
        """Whitespace-only canonical raises ValueError."""
        with self.assertRaises(ValueError):
            self.svc.handle_add_phonetic_entry(
                {"canonical": "   ", "variants": ["пашел"]}
            )

    def test_missing_canonical_rejected(self):
        """Missing canonical key raises ValueError."""
        with self.assertRaises(ValueError):
            self.svc.handle_add_phonetic_entry({"variants": ["пашел"]})

    def test_missing_variants_rejected(self):
        """Missing variants key raises ValueError."""
        with self.assertRaises(ValueError):
            self.svc.handle_add_phonetic_entry({"canonical": "Павел"})

    def test_non_list_variants_rejected(self):
        """Non-list variants raises ValueError."""
        with self.assertRaises(ValueError):
            self.svc.handle_add_phonetic_entry(
                {"canonical": "Павел", "variants": "пашел"}
            )

    def test_empty_variants_list_rejected(self):
        """Empty variants list raises ValueError."""
        with self.assertRaises(ValueError):
            self.svc.handle_add_phonetic_entry(
                {"canonical": "Павел", "variants": []}
            )

    def test_variant_too_long_rejected(self):
        """Variant exceeding max length raises ValueError."""
        long_variant = "x" * 201
        with self.assertRaises(ValueError):
            self.svc.handle_add_phonetic_entry(
                {"canonical": "Павел", "variants": [long_variant]}
            )

    def test_canonical_too_long_rejected(self):
        """Canonical exceeding max length raises ValueError."""
        long_canonical = "x" * 201
        with self.assertRaises(ValueError):
            self.svc.handle_add_phonetic_entry(
                {"canonical": long_canonical, "variants": ["пашел"]}
            )

    def test_persistence_across_reload(self):
        """Entries persist to disk and survive a service re-instantiation."""
        self.svc.handle_add_phonetic_entry(
            {"canonical": "persisted", "variants": ["variant1"]}
        )
        svc2 = PhoneticVocabService(data_dir=Path(self._tmp))
        listing = svc2.handle_list_phonetic_entries({})
        canonicals = [e["canonical"] for e in listing["entries"]]
        self.assertIn("persisted", canonicals)

    def test_clear_all(self):
        """clear_all() removes all entries from disk."""
        self.svc.handle_add_phonetic_entry(
            {"canonical": "Павел", "variants": ["пашел"]}
        )
        self.svc.clear_all()
        listing = self.svc.handle_list_phonetic_entries({})
        self.assertEqual(listing["entries"], [])

    def test_get_entries_returns_list(self):
        """get_entries() returns current list for provider callback."""
        self.svc.handle_add_phonetic_entry(
            {"canonical": "Павел", "variants": ["пашел"]}
        )
        entries = self.svc.get_entries()
        self.assertIsInstance(entries, list)
        self.assertTrue(any(e["canonical"] == "Павел" for e in entries))

    def test_empty_canonical_remove_rejected(self):
        """Removing with empty canonical raises ValueError."""
        with self.assertRaises(ValueError):
            self.svc.handle_remove_phonetic_entry({"canonical": ""})


# ---------------------------------------------------------------------------
# BackendService dispatch integration (no BackendService instantiation)
# ---------------------------------------------------------------------------

class TestPhoneticVocabDispatchEntries(unittest.TestCase):
    """Verify dispatch table wires the 3 handlers without instantiating BackendService."""

    def test_dispatch_table_has_all_three_methods(self):
        """The _build_dispatch_table source contains all 3 phonetic_entry keys."""
        import inspect
        from backend import service as svc_mod
        src = inspect.getsource(svc_mod.BackendService._build_dispatch_table)
        self.assertIn('"add_phonetic_entry"', src)
        self.assertIn('"list_phonetic_entries"', src)
        self.assertIn('"remove_phonetic_entry"', src)

    def test_phonetic_vocab_service_imported_in_service_module(self):
        """PhoneticVocabService is imported at module level in service.py."""
        from backend import service as svc_mod
        self.assertTrue(hasattr(svc_mod, "PhoneticVocabService"))

    def test_default_settings_has_phonetic_vocab_enabled(self):
        """DEFAULT_SETTINGS in config.py includes phonetic_vocab_enabled=False."""
        from core.config import DEFAULT_SETTINGS
        self.assertIn("phonetic_vocab_enabled", DEFAULT_SETTINGS)
        self.assertIs(DEFAULT_SETTINGS["phonetic_vocab_enabled"], False)


if __name__ == "__main__":
    unittest.main()
