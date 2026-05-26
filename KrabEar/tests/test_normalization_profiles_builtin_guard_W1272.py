"""Tests for W1264 N1+N2: builtin profile override guard and rule dedup in add_profile.

W1264 N1 (MED): _load_custom() must skip disk entries whose name is in _BUILTIN_NAMES.
W1264 N2 (LOW): add_profile() must deduplicate the rules list.
"""

import json
import sys
import os
import tempfile
import unittest

# ---------------------------------------------------------------------------
# Path setup — resolve core.* from the KrabEar package root
# ---------------------------------------------------------------------------
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_TESTS_DIR)  # KrabEar/
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.normalization_profiles import (  # noqa: E402
    NormalizationProfileRegistry,
    _BUILTIN_NAMES,
)


class BuiltinProfileGuardTests(unittest.TestCase):
    """N1 — disk JSON with a builtin name must NOT replace the builtin profile."""

    def _make_registry_with_disk_entry(self, disk_entries: list[dict]) -> tuple[NormalizationProfileRegistry, str]:
        """Create a temp data_dir, write disk_entries to normalization_profiles.json,
        return (registry, data_dir_path)."""
        tmp = tempfile.mkdtemp()
        profiles_path = os.path.join(tmp, "normalization_profiles.json")
        with open(profiles_path, "w", encoding="utf-8") as f:
            json.dump(disk_entries, f)
        reg = NormalizationProfileRegistry(data_dir=__import__("pathlib").Path(tmp))
        return reg, tmp

    def test_disk_profile_named_verbatim_does_not_override_builtin(self):
        """Writing {"name": "verbatim", "rules": ["cleanup_soft"]} to disk must NOT
        replace the builtin verbatim profile (which only has strip_hallucinations)."""
        reg, _ = self._make_registry_with_disk_entry([
            {"name": "verbatim", "description": "evil override", "rules": ["cleanup_soft"]},
        ])
        profile = reg.get_profile("verbatim")
        self.assertIsNotNone(profile, "verbatim profile should still exist")
        self.assertTrue(profile.builtin, "verbatim must remain marked as builtin")
        # Builtin verbatim has exactly ["strip_hallucinations"], NOT ["cleanup_soft"]
        self.assertEqual(profile.rules, ["strip_hallucinations"],
                         "Disk entry must not override builtin verbatim rules")

    def test_disk_profile_named_clean_does_not_override_builtin(self):
        """Same guard applies to other builtin names (clean, formal, telegram, subtitles)."""
        for builtin_name in _BUILTIN_NAMES:
            with self.subTest(name=builtin_name):
                reg, _ = self._make_registry_with_disk_entry([
                    {"name": builtin_name, "rules": ["__injected__"]},
                ])
                profile = reg.get_profile(builtin_name)
                self.assertIsNotNone(profile)
                self.assertTrue(profile.builtin)
                self.assertNotIn("__injected__", profile.rules,
                                 f"Disk must not inject rules into builtin {builtin_name!r}")

    def test_disk_profile_with_unknown_name_loaded_normally(self):
        """A custom profile whose name is NOT in _BUILTIN_NAMES must be loaded normally."""
        reg, _ = self._make_registry_with_disk_entry([
            {"name": "my_custom_profile", "description": "OK", "rules": ["cleanup_soft"]},
        ])
        profile = reg.get_profile("my_custom_profile")
        self.assertIsNotNone(profile, "Custom profile should be loaded")
        self.assertFalse(profile.builtin)
        self.assertEqual(profile.rules, ["cleanup_soft"])

    def test_disk_multiple_entries_skips_builtins_loads_custom(self):
        """Mixed list: builtin entries are skipped, custom entries are loaded."""
        reg, _ = self._make_registry_with_disk_entry([
            {"name": "verbatim", "rules": ["evil"]},
            {"name": "my_new_profile", "rules": ["wrap_lines_42"]},
            {"name": "formal", "rules": ["evil2"]},
        ])
        # Builtins untouched
        self.assertTrue(reg.get_profile("verbatim").builtin)
        self.assertNotIn("evil", reg.get_profile("verbatim").rules)
        self.assertTrue(reg.get_profile("formal").builtin)
        # Custom loaded
        custom = reg.get_profile("my_new_profile")
        self.assertIsNotNone(custom)
        self.assertEqual(custom.rules, ["wrap_lines_42"])

    def test_builtin_names_frozenset_contains_all_builtin_profiles(self):
        """_BUILTIN_NAMES must cover every name that appears in _BUILTIN_PROFILES."""
        from core.normalization_profiles import _BUILTIN_PROFILES
        for bp in _BUILTIN_PROFILES:
            self.assertIn(bp["name"], _BUILTIN_NAMES,
                          f"Builtin profile {bp['name']!r} missing from _BUILTIN_NAMES")


class AddProfileRuleDedupTests(unittest.TestCase):
    """N2 — add_profile() must deduplicate the rules list while preserving order."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.reg = NormalizationProfileRegistry(
            data_dir=__import__("pathlib").Path(self.tmp)
        )

    def test_add_profile_dedupes_rules(self):
        """Duplicate rule entries in add_profile() are reduced to one occurrence."""
        profile = self.reg.add_profile(
            "dedup_test",
            rules=["strip_hallucinations", "cleanup_soft", "strip_hallucinations"],
            description="test",
        )
        self.assertEqual(profile.rules, ["strip_hallucinations", "cleanup_soft"],
                         "Duplicate rules must be removed, order preserved")

    def test_add_profile_dedupes_preserves_order(self):
        """First occurrence wins; order of unique rules is preserved."""
        profile = self.reg.add_profile(
            "order_test",
            rules=["b", "a", "b", "c", "a"],
        )
        self.assertEqual(profile.rules, ["b", "a", "c"])

    def test_add_profile_no_duplicates_unchanged(self):
        """If no duplicates, rules list is unchanged."""
        original = ["strip_hallucinations", "cleanup_soft", "normalize_entities"]
        profile = self.reg.add_profile("no_dup", rules=original)
        self.assertEqual(profile.rules, original)

    def test_add_profile_empty_rules_unchanged(self):
        """Empty rules list stays empty."""
        profile = self.reg.add_profile("empty", rules=[])
        self.assertEqual(profile.rules, [])

    def test_add_profile_single_rule_unchanged(self):
        """Single rule list is unchanged."""
        profile = self.reg.add_profile("single", rules=["cleanup_soft"])
        self.assertEqual(profile.rules, ["cleanup_soft"])

    def test_add_profile_deduped_rules_persisted(self):
        """Deduped rules are what gets saved to disk and reloaded."""
        self.reg.add_profile(
            "persisted_dedup",
            rules=["cleanup_soft", "cleanup_soft", "normalize_entities"],
        )
        # Reload from disk
        reg2 = NormalizationProfileRegistry(
            data_dir=__import__("pathlib").Path(self.tmp)
        )
        profile = reg2.get_profile("persisted_dedup")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.rules, ["cleanup_soft", "normalize_entities"])


if __name__ == "__main__":
    unittest.main()
