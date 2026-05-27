"""Tests for W1242 F4 LOW — constant-time token comparison in SharingManager.

Verifies that get_shared and revoke_share use hmac.compare_digest via
_find_share_by_token_constant_time instead of plain dict lookup.
"""

from __future__ import annotations

import ast
import hmac
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.sharing_manager import SharingManager, SharePackage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    def __init__(self, item_id: str, text: str = "hello") -> None:
        self.id = item_id
        self.text = text
        self.ts = "2026-01-01T00:00:00+00:00"
        self.translated_text = ""
        self.source_lang = "ru"
        self.target_lang = "es"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "ts": self.ts,
            "translated_text": self.translated_text,
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
        }


class FakeStore:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict[str, FakeHistoryItem] = {}

    def add_fake_item(self, item_id: str, text: str = "hello") -> FakeHistoryItem:
        item = FakeHistoryItem(item_id, text)
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str) -> FakeHistoryItem | None:
        return self._items.get(item_id)


def _make_manager(data_dir: str) -> SharingManager:
    store = FakeStore(data_dir)
    store.add_fake_item("item-1", "Test transcript")
    return SharingManager(store, share_no_default_ttl=True), store


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestConstantTimeTokenComparison(unittest.TestCase):
    """Verify hmac.compare_digest is used for all token lookups."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._mgr, self._store = _make_manager(self._tmpdir.name)
        # Create a share to test against
        self._pkg = self._mgr.prepare_share(["item-1"], format="text")
        self._token = self._pkg.share_id

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    # ------------------------------------------------------------------
    # test_get_shared_uses_constant_time_compare
    # ------------------------------------------------------------------

    def test_get_shared_uses_constant_time_compare(self) -> None:
        """get_shared must route token lookup through hmac.compare_digest."""
        calls: list[tuple[str, str]] = []

        original_compare_digest = hmac.compare_digest

        def spy_compare_digest(a: str, b: str) -> bool:
            calls.append((a, b))
            return original_compare_digest(a, b)

        with patch("backend.sharing_manager.hmac.compare_digest", side_effect=spy_compare_digest):
            result = self._mgr.get_shared(self._token)

        self.assertIsNotNone(result, "get_shared should find the package")
        self.assertIsInstance(result, SharePackage)
        # hmac.compare_digest must have been called at least once
        self.assertGreater(len(calls), 0, "hmac.compare_digest was never called by get_shared")
        # The token must appear in one of the comparison calls
        involved_tokens = {a for a, b in calls} | {b for a, b in calls}
        self.assertIn(self._token, involved_tokens, "Token was not passed to hmac.compare_digest")

    def test_get_shared_iterates_all_entries(self) -> None:
        """_find_share_by_token_constant_time must not short-circuit — it keeps
        iterating even after finding the match (constant-time property)."""
        # Add more shares so there are multiple entries
        self._mgr.prepare_share(["item-1"], format="text")
        self._mgr.prepare_share(["item-1"], format="text")

        calls: list[tuple[str, str]] = []
        original_compare_digest = hmac.compare_digest

        def spy_compare_digest(a: str, b: str) -> bool:
            calls.append((a, b))
            return original_compare_digest(a, b)

        with patch("backend.sharing_manager.hmac.compare_digest", side_effect=spy_compare_digest):
            self._mgr.get_shared(self._token)

        index_size = len(self._mgr._index)
        self.assertGreaterEqual(
            len(calls),
            index_size,
            f"Expected at least {index_size} compare_digest calls (one per entry), got {len(calls)}",
        )

    # ------------------------------------------------------------------
    # test_revoke_share_uses_constant_time_compare
    # ------------------------------------------------------------------

    def test_revoke_share_uses_constant_time_compare(self) -> None:
        """revoke_share must route token lookup through hmac.compare_digest."""
        calls: list[tuple[str, str]] = []

        original_compare_digest = hmac.compare_digest

        def spy_compare_digest(a: str, b: str) -> bool:
            calls.append((a, b))
            return original_compare_digest(a, b)

        with patch("backend.sharing_manager.hmac.compare_digest", side_effect=spy_compare_digest):
            result = self._mgr.revoke_share(self._token)

        self.assertTrue(result, "revoke_share should return True for known token")
        self.assertGreater(len(calls), 0, "hmac.compare_digest was never called by revoke_share")
        involved_tokens = {a for a, b in calls} | {b for a, b in calls}
        self.assertIn(self._token, involved_tokens, "Token was not passed to hmac.compare_digest")

    def test_revoke_share_iterates_all_entries(self) -> None:
        """revoke_share must not short-circuit — iterates all entries for constant-time."""
        self._mgr.prepare_share(["item-1"], format="text")
        self._mgr.prepare_share(["item-1"], format="text")

        calls: list[tuple[str, str]] = []
        original_compare_digest = hmac.compare_digest

        def spy_compare_digest(a: str, b: str) -> bool:
            calls.append((a, b))
            return original_compare_digest(a, b)

        with patch("backend.sharing_manager.hmac.compare_digest", side_effect=spy_compare_digest):
            self._mgr.revoke_share(self._token)

        index_size = len(self._mgr._index)
        self.assertGreaterEqual(
            len(calls),
            index_size,
            f"Expected at least {index_size} compare_digest calls, got {len(calls)}",
        )

    # ------------------------------------------------------------------
    # test_unknown_token_returns_none
    # ------------------------------------------------------------------

    def test_unknown_token_returns_none(self) -> None:
        """get_shared with an unknown token must return None (not raise)."""
        result = self._mgr.get_shared("nonexistent-token-xyz")
        self.assertIsNone(result)

    def test_unknown_token_revoke_returns_false(self) -> None:
        """revoke_share with an unknown token must return False (not raise)."""
        result = self._mgr.revoke_share("nonexistent-token-xyz")
        self.assertFalse(result)

    # ------------------------------------------------------------------
    # AST check — no plain dict[token] or token-in-dict in get_shared/revoke_share
    # ------------------------------------------------------------------

    def test_no_plain_dict_lookup_in_get_shared_or_revoke_share(self) -> None:
        """AST-level check: get_shared and revoke_share must not use
        `token in self._index` (timing oracle) or `self._index.get(token)`
        where `token` is the raw incoming parameter.

        Note: `self._index[entry["share_id"]]` for write-back after a
        constant-time lookup is intentional and allowed — only raw
        incoming-parameter lookups are forbidden.
        """
        src_path = Path(__file__).resolve().parents[1] / "backend" / "sharing_manager.py"
        src = src_path.read_text(encoding="utf-8")
        tree = ast.parse(src)

        target_methods = {"get_shared", "revoke_share"}

        violations: list[str] = []

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in target_methods:
                continue

            # Determine the name of the first non-self param (the incoming token)
            args = node.args.args
            param_names = {a.arg for a in args if a.arg != "self"}

            for child in ast.walk(node):
                # `token in self._index` → Compare with In + self._index comparator
                if isinstance(child, ast.Compare):
                    for comp, cmp_val in zip(child.ops, child.comparators):
                        if isinstance(comp, ast.In):
                            if isinstance(cmp_val, ast.Attribute) and cmp_val.attr == "_index":
                                violations.append(
                                    f"{node.name}: `in self._index` at line {child.lineno}"
                                )

                # self._index.get(token) → Call where func is Attribute(value=self._index, attr='get')
                # and first arg is a Name matching a param
                if isinstance(child, ast.Call):
                    func = child.func
                    if (
                        isinstance(func, ast.Attribute)
                        and func.attr == "get"
                        and isinstance(func.value, ast.Attribute)
                        and func.value.attr == "_index"
                    ):
                        if child.args:
                            first_arg = child.args[0]
                            if isinstance(first_arg, ast.Name) and first_arg.id in param_names:
                                violations.append(
                                    f"{node.name}: self._index.get({first_arg.id}) at line {child.lineno}"
                                )

        self.assertEqual(
            violations,
            [],
            "Timing-oracle dict lookups found:\n" + "\n".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
