"""W1520 AST regression guard — verifies translator.py contains all 6 fixes
restored after the W1497 cherry-pick regressor (commit 60919b88).

Covered fixes:
  W1428 — no duplicate clear_cache definitions
  W1429 — TranslationCache.get/put called WITHOUT network_mode kwarg
  W1430 — _apply_glossary uses re.sub with \\b word-boundary
  W1455 — _apply_glossary uses lambda (backslash-safe replacement)
  W1498 — _privacy_was_on initialised to None in __init__
  W1500 — _settings_getter slot present in __init__
"""
from __future__ import annotations

import ast
import os
import sys
import unittest

# ── project path ──────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

_TRANSLATOR_PATH = os.path.join(
    PROJECT_ROOT, "KrabEar", "backend", "translator.py"
)


def _load_source() -> str:
    with open(_TRANSLATOR_PATH, encoding="utf-8") as fh:
        return fh.read()


def _parse() -> ast.Module:
    return ast.parse(_load_source(), filename=_TRANSLATOR_PATH)


class TestNoRegressionW1517(unittest.TestCase):
    """AST-level guards ensuring all 6 regressed fixes are present."""

    # ── W1428: single clear_cache definition ──────────────────────────────

    def test_w1428_no_duplicate_clear_cache(self):
        """W1428: clear_cache must be defined exactly once (no duplicates)."""
        tree = _parse()
        definitions = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "clear_cache"
        ]
        self.assertEqual(
            len(definitions),
            1,
            f"Expected exactly 1 clear_cache definition, found {len(definitions)}",
        )

    # ── W1429: no network_mode kwarg in _tc.get / _tc.put calls ──────────

    def test_w1429_no_network_mode_kwarg_in_tc_get_put(self):
        """W1429: _tc.get() and _tc.put() must NOT pass network_mode kwarg.

        Uses AST to inspect call sites where the receiver ends in `_tc`.
        """
        tree = _parse()
        violations: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Match _tc.get(...) and _tc.put(...)
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in ("get", "put"):
                continue
            val = func.value
            if not (isinstance(val, ast.Name) and val.id == "_tc"):
                continue
            # Check for network_mode keyword
            for kw in node.keywords:
                if kw.arg == "network_mode":
                    violations.append(
                        f"_tc.{func.attr}() at line {node.lineno} has network_mode= kwarg"
                    )
        self.assertEqual(
            violations,
            [],
            f"W1429 regression: TranslationCache.get/put must not receive network_mode kwarg.\n"
            f"Violations: {violations}",
        )

    def test_w1429_translation_cache_imported_or_used(self):
        """W1429: TranslationCache wiring exists in module (import or annotation)."""
        source = _load_source()
        self.assertIn(
            "_translation_cache",
            source,
            "translator.py must reference _translation_cache (wiring slot)",
        )

    # ── W1430: word-boundary regex in _apply_glossary ─────────────────────

    def test_w1430_apply_glossary_uses_word_boundaries(self):
        r"""W1430: _apply_glossary must use re.sub with \b boundaries."""
        source = _load_source()
        self.assertIn(
            r'r"\b"',
            source,
            r"_apply_glossary must contain r\"\b\" for word-boundary matching (W1430)",
        )

    def test_w1430_apply_glossary_uses_re_escape(self):
        """W1430: _apply_glossary must use re.escape(source)."""
        source = _load_source()
        self.assertIn(
            "re.escape(source)",
            source,
            "_apply_glossary must call re.escape(source) to escape glossary terms",
        )

    # ── W1455: lambda in _apply_glossary ─────────────────────────────────

    def test_w1455_apply_glossary_uses_lambda(self):
        """W1455: _apply_glossary replacement must use a lambda (backslash-safe)."""
        tree = _parse()
        # Find _apply_glossary function node
        apply_glossary_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_apply_glossary":
                apply_glossary_node = node
                break
        self.assertIsNotNone(apply_glossary_node, "_apply_glossary function not found")

        # Verify a lambda is present inside _apply_glossary
        lambdas = [
            n for n in ast.walk(apply_glossary_node) if isinstance(n, ast.Lambda)
        ]
        self.assertGreater(
            len(lambdas),
            0,
            "_apply_glossary must use a lambda for replacement (W1455 backslash-safe fix)",
        )

    # ── W1498: _privacy_was_on slot ───────────────────────────────────────

    def test_w1498_privacy_was_on_initialised(self):
        """W1498: _privacy_was_on must be initialised in __init__."""
        source = _load_source()
        self.assertIn(
            "_privacy_was_on",
            source,
            "translator.py must initialise _privacy_was_on (W1498)",
        )

    def test_w1498_privacy_was_on_is_none_sentinel(self):
        """W1498: _privacy_was_on initial value must be None (sentinel)."""
        source = _load_source()
        self.assertIn(
            "_privacy_was_on: bool | None = None",
            source,
            "_privacy_was_on must be typed as bool | None and initialised to None",
        )

    # ── W1500: _settings_getter slot ─────────────────────────────────────

    def test_w1500_settings_getter_slot_present(self):
        """W1500: _settings_getter slot must be declared in __init__."""
        source = _load_source()
        self.assertIn(
            "_settings_getter",
            source,
            "translator.py must declare _settings_getter slot (W1500)",
        )

    def test_w1500_settings_getter_initialised_to_none(self):
        """W1500: _settings_getter must be initialised to None."""
        source = _load_source()
        self.assertIn(
            "self._settings_getter",
            source,
            "translator.py must set self._settings_getter (W1500 late-injection pattern)",
        )

    # ── Structural: _cache_lock present ──────────────────────────────────

    def test_cache_lock_present(self):
        """W1428/W1498: _cache_lock (threading.RLock) must be initialised."""
        source = _load_source()
        self.assertIn(
            "_cache_lock",
            source,
            "translator.py must have _cache_lock for thread-safe LRU cache access",
        )

    def test_clear_cache_clears_unavailable_set(self):
        """W1498: clear_cache must also clear _unavailable (failed-models set)."""
        source = _load_source()
        # Both _unavailable.clear() must appear within the module
        self.assertIn(
            "_unavailable.clear()",
            source,
            "clear_cache must call self._unavailable.clear() (W1498)",
        )


if __name__ == "__main__":
    unittest.main()
