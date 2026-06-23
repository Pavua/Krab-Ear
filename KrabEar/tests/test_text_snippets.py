"""Tests for TextSnippetExpander (core) and TextSnippetService (backend).

Coverage:
- TextSnippetExpander.expand():
    - trigger replaced in text
    - longest-match wins over shorter prefix
    - case-insensitive match
    - no replacement inside larger words (word-boundary)
    - ReDoS-safe: regex-special trigger like "a(b" treated literally
    - setting OFF → no expansion
- TextSnippetService CRUD round-trip (add / list / remove)
- Dedupe by trigger (add same trigger twice = update)
- Empty trigger / empty expansion rejected with ValueError
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

from core.text_snippet_expander import TextSnippetExpander
from backend.text_snippet_service import TextSnippetService


# ---------------------------------------------------------------------------
# TextSnippetExpander tests
# ---------------------------------------------------------------------------

class TestTextSnippetExpanderBasic(unittest.TestCase):
    """Basic expansion logic."""

    def _make_expander(self, snippets, enabled=True):
        def _settings_get(key, default=None):
            if key == "text_snippets_enabled":
                return enabled
            return default
        return TextSnippetExpander(
            settings_get=_settings_get,
            snippets_provider=lambda: snippets,
        )

    def test_trigger_replaced(self):
        """Trigger phrase in text is replaced with expansion."""
        exp = self._make_expander([{"trigger": "вставь подпись", "expansion": "С уважением,\nПавел"}])
        result = exp.expand("Привет, вставь подпись пожалуйста")
        self.assertIn("С уважением,\nПавел", result)
        self.assertNotIn("вставь подпись", result)

    def test_trigger_expansion_email(self):
        """Email trigger replaced correctly."""
        exp = self._make_expander([{"trigger": "мой имейл", "expansion": "pavelr7@gmail.com"}])
        result = exp.expand("отправь на мой имейл")
        self.assertIn("pavelr7@gmail.com", result)

    def test_longest_match_wins(self):
        """Longest trigger preferred over shorter prefix — no partial shadowing."""
        snippets = [
            {"trigger": "вставь", "expansion": "КОРОТКИЙ"},
            {"trigger": "вставь подпись", "expansion": "ДЛИННЫЙ"},
        ]
        exp = self._make_expander(snippets)
        result = exp.expand("вставь подпись в конце")
        # Longest match ("вставь подпись") must win
        self.assertIn("ДЛИННЫЙ", result)
        self.assertNotIn("КОРОТКИЙ", result)

    def test_case_insensitive(self):
        """Matching is case-insensitive."""
        exp = self._make_expander([{"trigger": "Мой Имейл", "expansion": "pavelr7@gmail.com"}])
        result = exp.expand("отправь на мой имейл")
        self.assertIn("pavelr7@gmail.com", result)

    def test_no_replacement_inside_larger_word(self):
        """Trigger 'email' must not match inside 'emails'."""
        exp = self._make_expander([{"trigger": "email", "expansion": "REPLACED"}])
        # 'emails' should NOT be replaced
        result = exp.expand("send to emails please")
        self.assertNotIn("REPLACED", result)
        # standalone 'email' SHOULD be replaced
        result2 = exp.expand("send to email please")
        self.assertIn("REPLACED", result2)

    def test_redos_safe_regex_special_trigger(self):
        """Trigger with regex-special chars like 'a(b' is treated literally (re.escape)."""
        exp = self._make_expander([{"trigger": "a(b", "expansion": "SAFE"}])
        # Exact literal match should work
        result = exp.expand("prefix a(b suffix")
        self.assertIn("SAFE", result)
        # No crash on the regex-special trigger
        result2 = exp.expand("no match here")
        self.assertEqual(result2, "no match here")

    def test_setting_off_no_expansion(self):
        """When text_snippets_enabled=False, no replacement is performed."""
        exp = self._make_expander(
            [{"trigger": "вставь подпись", "expansion": "С уважением,\nПавел"}],
            enabled=False,
        )
        text = "Привет, вставь подпись пожалуйста"
        result = exp.expand(text)
        self.assertEqual(result, text)

    def test_empty_text_passthrough(self):
        """Empty string returns unchanged."""
        exp = self._make_expander([{"trigger": "foo", "expansion": "bar"}])
        self.assertEqual(exp.expand(""), "")

    def test_no_snippets_passthrough(self):
        """No snippets → original text returned unchanged."""
        exp = self._make_expander([])
        text = "hello world"
        self.assertEqual(exp.expand(text), text)

    def test_multiple_triggers_in_one_text(self):
        """Multiple distinct triggers replaced in one pass."""
        snippets = [
            {"trigger": "мой имейл", "expansion": "pavelr7@gmail.com"},
            {"trigger": "вставь подпись", "expansion": "С уважением,\nПавел"},
        ]
        exp = self._make_expander(snippets)
        text = "отправь на мой имейл и вставь подпись"
        result = exp.expand(text)
        self.assertIn("pavelr7@gmail.com", result)
        self.assertIn("С уважением,\nПавел", result)

    def test_expansion_with_newlines(self):
        """Expansion may contain newlines."""
        exp = self._make_expander([{"trigger": "sig", "expansion": "line1\nline2"}])
        result = exp.expand("add sig here")
        self.assertIn("line1\nline2", result)


# ---------------------------------------------------------------------------
# TextSnippetService CRUD tests
# ---------------------------------------------------------------------------

class TestTextSnippetServiceCRUD(unittest.TestCase):
    """CRUD round-trip tests for TextSnippetService."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.svc = TextSnippetService(data_dir=Path(self._tmp))

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_add_and_list(self):
        """add_text_snippet → list_text_snippets returns the pair."""
        result = self.svc.handle_add_text_snippet(
            {"trigger": "мой имейл", "expansion": "pavelr7@gmail.com"}
        )
        self.assertTrue(result["ok"])
        listing = self.svc.handle_list_text_snippets({})
        self.assertTrue(listing["ok"])
        triggers = [s["trigger"] for s in listing["snippets"]]
        self.assertIn("мой имейл", triggers)

    def test_remove(self):
        """add → remove → list gives empty."""
        self.svc.handle_add_text_snippet({"trigger": "foo", "expansion": "bar"})
        remove_result = self.svc.handle_remove_text_snippet({"trigger": "foo"})
        self.assertTrue(remove_result["ok"])
        self.assertTrue(remove_result["removed"])
        listing = self.svc.handle_list_text_snippets({})
        self.assertEqual(listing["snippets"], [])

    def test_remove_not_found_raises(self):
        """Removing a trigger that doesn't exist raises RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_remove_text_snippet({"trigger": "nonexistent"})

    def test_dedupe_by_trigger(self):
        """Adding the same trigger twice → update (not duplicate)."""
        self.svc.handle_add_text_snippet({"trigger": "foo", "expansion": "first"})
        self.svc.handle_add_text_snippet({"trigger": "foo", "expansion": "second"})
        listing = self.svc.handle_list_text_snippets({})
        foos = [s for s in listing["snippets"] if s["trigger"] == "foo"]
        self.assertEqual(len(foos), 1)
        self.assertEqual(foos[0]["expansion"], "second")

    def test_dedupe_case_insensitive(self):
        """Trigger dedup is case-insensitive: 'Foo' and 'foo' are the same trigger."""
        self.svc.handle_add_text_snippet({"trigger": "Foo", "expansion": "first"})
        self.svc.handle_add_text_snippet({"trigger": "foo", "expansion": "second"})
        listing = self.svc.handle_list_text_snippets({})
        self.assertEqual(len(listing["snippets"]), 1)

    def test_empty_trigger_rejected(self):
        """Empty trigger raises ValueError."""
        with self.assertRaises(ValueError):
            self.svc.handle_add_text_snippet({"trigger": "", "expansion": "bar"})

    def test_whitespace_only_trigger_rejected(self):
        """Whitespace-only trigger raises ValueError."""
        with self.assertRaises(ValueError):
            self.svc.handle_add_text_snippet({"trigger": "   ", "expansion": "bar"})

    def test_missing_trigger_rejected(self):
        """Missing trigger key raises ValueError."""
        with self.assertRaises(ValueError):
            self.svc.handle_add_text_snippet({"expansion": "bar"})

    def test_missing_expansion_rejected(self):
        """Missing expansion key raises ValueError."""
        with self.assertRaises(ValueError):
            self.svc.handle_add_text_snippet({"trigger": "foo"})

    def test_non_string_expansion_rejected(self):
        """Non-string expansion raises ValueError."""
        with self.assertRaises(ValueError):
            self.svc.handle_add_text_snippet({"trigger": "foo", "expansion": 123})

    def test_trigger_too_long_rejected(self):
        """Trigger exceeding max length raises ValueError."""
        long_trigger = "x" * 201
        with self.assertRaises(ValueError):
            self.svc.handle_add_text_snippet({"trigger": long_trigger, "expansion": "bar"})

    def test_expansion_too_long_rejected(self):
        """Expansion exceeding max length raises ValueError."""
        long_exp = "x" * 2001
        with self.assertRaises(ValueError):
            self.svc.handle_add_text_snippet({"trigger": "foo", "expansion": long_exp})

    def test_persistence_across_reload(self):
        """Snippets persist to disk and survive a service re-instantiation."""
        self.svc.handle_add_text_snippet({"trigger": "persisted", "expansion": "yes"})
        svc2 = TextSnippetService(data_dir=Path(self._tmp))
        listing = svc2.handle_list_text_snippets({})
        triggers = [s["trigger"] for s in listing["snippets"]]
        self.assertIn("persisted", triggers)

    def test_clear_all(self):
        """clear_all() removes all snippets from disk."""
        self.svc.handle_add_text_snippet({"trigger": "foo", "expansion": "bar"})
        self.svc.clear_all()
        listing = self.svc.handle_list_text_snippets({})
        self.assertEqual(listing["snippets"], [])

    def test_get_snippets_returns_list(self):
        """get_snippets() returns current list for provider callback."""
        self.svc.handle_add_text_snippet({"trigger": "t1", "expansion": "e1"})
        snippets = self.svc.get_snippets()
        self.assertIsInstance(snippets, list)
        self.assertTrue(any(s["trigger"] == "t1" for s in snippets))

    def test_empty_trigger_remove_rejected(self):
        """Removing with empty trigger raises ValueError."""
        with self.assertRaises(ValueError):
            self.svc.handle_remove_text_snippet({"trigger": ""})


# ---------------------------------------------------------------------------
# BackendService dispatch integration (no BackendService instantiation)
# ---------------------------------------------------------------------------

class TestTextSnippetDispatchEntries(unittest.TestCase):
    """Verify dispatch table wires the 3 handlers without instantiating BackendService."""

    def test_dispatch_table_has_all_three_methods(self):
        """The _build_dispatch_table source contains all 3 text_snippet keys."""
        import inspect
        from backend import service as svc_mod
        src = inspect.getsource(svc_mod.BackendService._build_dispatch_table)
        self.assertIn('"add_text_snippet"', src)
        self.assertIn('"list_text_snippets"', src)
        self.assertIn('"remove_text_snippet"', src)

    def test_text_snippet_service_imported_in_service_module(self):
        """TextSnippetService is imported at module level in service.py."""
        from backend import service as svc_mod
        self.assertTrue(hasattr(svc_mod, "TextSnippetService"))

    def test_default_settings_has_text_snippets_enabled(self):
        """DEFAULT_SETTINGS in config.py includes text_snippets_enabled=False."""
        from core.config import DEFAULT_SETTINGS
        self.assertIn("text_snippets_enabled", DEFAULT_SETTINGS)
        self.assertIs(DEFAULT_SETTINGS["text_snippets_enabled"], False)


if __name__ == "__main__":
    unittest.main()
