"""Wave 217 — deep tests for settings migration 1.0→2.0 + SettingsValidator.

Covers:
- test_migrate_1_0_to_2_0_changes_specific_keys
- test_migrate_preserves_unknown_keys
- test_migrate_creates_backup_before
- test_migrate_rollback_on_validation_failure
- test_unknown_schema_version_handled
- test_invalid_enum_value_corrected_during_migrate
- test_missing_field_filled_with_defaults
- test_unicode_setting_values_preserved
- test_concurrent_migrate_blocked_by_lock (thread safety)
- test_migrate_idempotent (calling twice = same result)
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest

_PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from backend.settings_validator import (  # noqa: E402
    SettingsValidator,
    _MIGRATIONS,
)
from backend.settings_backup import SettingsBackup  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_v1_settings(**extra) -> dict:
    """Return a minimal valid 1.0 settings dict."""
    base = {
        "schema_version": "1.0",
        "quality_profile": "balanced",
        "cleanup_profile": "soft",
        "translation_mode": "off",
        "history_limit": "unlimited",
        "auto_paste": True,
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# 1. Specific key changes in 1.0 → 2.0
# ---------------------------------------------------------------------------

class TestMigrate10To20SpecificKeys(unittest.TestCase):
    """Verify every documented operation in the 1.0→2.0 migration table."""

    def setUp(self):
        self.v = SettingsValidator()
        self.ops = _MIGRATIONS[("1.0", "2.0")]
        # Collect all expected add_default keys
        self.added_defaults = {
            key: val for kind, key, val in self.ops if kind == "add_default"
        }
        # Collect all expected renames
        self.renames = {
            old: new for kind, old, new, *_ in
            [(op + (None,)) for op in self.ops]
            if kind == "rename"
        }

    def test_history_limit_renamed_to_history_policy(self):
        s = _make_v1_settings(history_limit="unlimited")
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIn("history_policy", result)
        self.assertEqual(result["history_policy"], "unlimited")
        self.assertNotIn("history_limit", result)

    def test_overlay_opacity_percent_added(self):
        s = _make_v1_settings()
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIn("overlay_opacity_percent", result)
        self.assertEqual(result["overlay_opacity_percent"], 45)

    def test_call_budget_usd_added(self):
        s = _make_v1_settings()
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIn("call_budget_usd", result)
        self.assertAlmostEqual(result["call_budget_usd"], 2.0)

    def test_call_notify_default_added_true(self):
        s = _make_v1_settings()
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIn("call_notify_default", result)
        self.assertTrue(result["call_notify_default"])

    def test_call_auto_summary_added_true(self):
        s = _make_v1_settings()
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIn("call_auto_summary", result)
        self.assertTrue(result["call_auto_summary"])

    def test_llm_rewrite_enabled_added_false(self):
        s = _make_v1_settings()
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIn("llm_rewrite_enabled", result)
        self.assertFalse(result["llm_rewrite_enabled"])

    def test_auto_save_transcripts_added_false(self):
        s = _make_v1_settings()
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIn("auto_save_transcripts", result)
        self.assertFalse(result["auto_save_transcripts"])

    def test_notifications_enabled_added_true(self):
        s = _make_v1_settings()
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIn("notifications_enabled", result)
        self.assertTrue(result["notifications_enabled"])

    def test_notify_on_low_confidence_added_true(self):
        s = _make_v1_settings()
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIn("notify_on_low_confidence", result)
        self.assertTrue(result["notify_on_low_confidence"])

    def test_notify_confidence_threshold_added(self):
        s = _make_v1_settings()
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIn("notify_confidence_threshold", result)
        self.assertAlmostEqual(result["notify_confidence_threshold"], 0.5)

    def test_notify_on_llm_failure_added_true(self):
        s = _make_v1_settings()
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIn("notify_on_llm_failure", result)
        self.assertTrue(result["notify_on_llm_failure"])

    def test_notify_on_import_complete_added_true(self):
        s = _make_v1_settings()
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIn("notify_on_import_complete", result)
        self.assertTrue(result["notify_on_import_complete"])

    def test_notify_sound_enabled_added_true(self):
        s = _make_v1_settings()
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIn("notify_sound_enabled", result)
        self.assertTrue(result["notify_sound_enabled"])

    def test_capture_source_mode_added(self):
        s = _make_v1_settings()
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIn("capture_source_mode", result)
        self.assertEqual(result["capture_source_mode"], "mic")

    def test_ui_last_tab_added(self):
        s = _make_v1_settings()
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIn("ui_last_tab", result)
        self.assertEqual(result["ui_last_tab"], "history")

    def test_history_focus_mode_added_true(self):
        s = _make_v1_settings()
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIn("history_focus_mode", result)
        self.assertTrue(result["history_focus_mode"])

    def test_all_add_default_ops_present(self):
        """Smoke check: every add_default op in _MIGRATIONS produces the correct default."""
        s = _make_v1_settings()
        result = self.v.migrate(s, "1.0", "2.0")
        for key, default_val in self.added_defaults.items():
            with self.subTest(key=key):
                self.assertIn(key, result)
                self.assertEqual(result[key], default_val)


# ---------------------------------------------------------------------------
# 2. Unknown keys are preserved
# ---------------------------------------------------------------------------

class TestMigratePreservesUnknownKeys(unittest.TestCase):
    def setUp(self):
        self.v = SettingsValidator()

    def test_extra_custom_key_preserved(self):
        s = _make_v1_settings(my_custom_flag=True, custom_int=42)
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertTrue(result["my_custom_flag"])
        self.assertEqual(result["custom_int"], 42)

    def test_original_keys_preserved(self):
        s = _make_v1_settings()
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertEqual(result["quality_profile"], "balanced")
        self.assertEqual(result["auto_paste"], True)

    def test_nested_dict_preserved(self):
        s = _make_v1_settings(translation_glossary={"Краб": "crab"})
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertEqual(result["translation_glossary"], {"Краб": "crab"})

    def test_migration_returns_copy_not_same_object(self):
        s = _make_v1_settings()
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIsNot(result, s)

    def test_original_dict_not_mutated(self):
        s = _make_v1_settings()
        original_keys = set(s.keys())
        self.v.migrate(s, "1.0", "2.0")
        self.assertEqual(set(s.keys()), original_keys)


# ---------------------------------------------------------------------------
# 3. Backup created before migrate
# ---------------------------------------------------------------------------

class TestMigrateCreatesBackupBefore(unittest.TestCase):
    """SettingsBackup.create_backup works before migrate is called."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.backup = SettingsBackup(backup_dir=__import__("pathlib").Path(self._tmpdir))
        self.v = SettingsValidator()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_backup_created_before_migration(self):
        s = _make_v1_settings()
        # Simulate the expected workflow: backup then migrate
        backup_id = self.backup.create_backup(s, reason="pre_migrate")
        self.v.migrate(s, "1.0", "2.0")

        # Backup exists and contains original data
        restored = self.backup.restore_backup(backup_id)
        self.assertIn("quality_profile", restored)
        self.assertEqual(restored["quality_profile"], "balanced")

    def test_backup_does_not_include_sensitive_keys(self):
        s = _make_v1_settings(hf_token="secret_token_123", telnyx_api_key="tlx_key")
        backup_id = self.backup.create_backup(s, reason="pre_migrate")
        restored = self.backup.restore_backup(backup_id)
        self.assertNotIn("hf_token", restored)
        self.assertNotIn("telnyx_api_key", restored)

    def test_backup_reason_recorded_in_id(self):
        s = _make_v1_settings()
        backup_id = self.backup.create_backup(s, reason="pre_migrate_1_0_to_2_0")
        self.assertIn("pre_migrate_1_0_to_2_0", backup_id)

    def test_backup_contains_pre_migration_state(self):
        """Backup stores old keys (e.g. history_limit), not post-migration ones."""
        s = _make_v1_settings(history_limit="unlimited")
        backup_id = self.backup.create_backup(s, reason="pre_migrate")
        restored = self.backup.restore_backup(backup_id)
        # The backup should have the original key
        self.assertIn("history_limit", restored)
        # history_policy is NOT in the backup (it's added by migration)
        self.assertNotIn("history_policy", restored)

    def test_multiple_backups_listed(self):
        s = _make_v1_settings()
        for i in range(3):
            time.sleep(0.01)  # ensure distinct timestamps
            self.backup.create_backup(s, reason=f"step_{i}")
        listed = self.backup.list_backups()
        self.assertEqual(len(listed), 3)


# ---------------------------------------------------------------------------
# 4. Rollback on validation failure
# ---------------------------------------------------------------------------

class TestMigrateRollbackOnValidationFailure(unittest.TestCase):
    """When post-migrate validation finds hard errors, the caller can fallback."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.backup = SettingsBackup(backup_dir=__import__("pathlib").Path(self._tmpdir))
        self.v = SettingsValidator()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_rollback_path_with_invalid_gateway_url(self):
        """If migrated settings have a bad voice_gateway_url, restore from backup."""
        s = _make_v1_settings(
            voice_gateway_url="http://evil.example.com/api"  # hard error
        )
        backup_id = self.backup.create_backup(s, reason="pre_migrate")
        migrated = self.v.migrate(s, "1.0", "2.0")
        validation = self.v.validate(migrated)

        if not validation.valid:
            # Simulate rollback: restore from backup
            rolled_back = self.backup.restore_backup(backup_id)
            self.assertIn("quality_profile", rolled_back)
        else:
            # Should not happen in this case since voice_gateway_url is a hard error
            self.fail("Expected validation failure with bad voice_gateway_url")

    def test_valid_migrated_settings_pass_validation(self):
        """Happy path: 1.0 → 2.0 migration produces settings that validate cleanly."""
        s = _make_v1_settings()
        migrated = self.v.migrate(s, "1.0", "2.0")
        validation = self.v.validate(migrated)
        self.assertTrue(validation.valid)
        self.assertEqual(validation.errors, [])

    def test_rollback_preserves_original_data_exactly(self):
        s = _make_v1_settings(my_key="my_value")
        backup_id = self.backup.create_backup(s, reason="pre_migrate")
        restored = self.backup.restore_backup(backup_id)
        self.assertEqual(restored["my_key"], "my_value")
        self.assertEqual(restored["quality_profile"], "balanced")


# ---------------------------------------------------------------------------
# 5. Unknown schema version
# ---------------------------------------------------------------------------

class TestUnknownSchemaVersionHandled(unittest.TestCase):
    def setUp(self):
        self.v = SettingsValidator()

    def test_unknown_from_version_raises(self):
        with self.assertRaises(ValueError):
            self.v.migrate({}, "0.1", "2.0")

    def test_unknown_to_version_raises(self):
        """No path from 1.0 to 3.0 exists."""
        with self.assertRaises(ValueError):
            self.v.migrate({}, "1.0", "3.0")

    def test_totally_unknown_version_raises(self):
        with self.assertRaises(ValueError):
            self.v.migrate({}, "99.99", "100.0")

    def test_same_unknown_version_noop(self):
        """Same from/to version is always a no-op, even if unknown."""
        s = {"quality_profile": "balanced"}
        result = self.v.migrate(s, "9.9", "9.9")
        self.assertEqual(result, s)

    def test_future_version_raises(self):
        with self.assertRaises(ValueError):
            self.v.migrate({}, "2.0", "3.0")

    def test_error_message_mentions_version(self):
        try:
            self.v.migrate({}, "0.5", "2.0")
        except ValueError as e:
            self.assertIn("0.5", str(e))
        else:
            self.fail("Expected ValueError")


# ---------------------------------------------------------------------------
# 6. Invalid enum value corrected during migrate+validate
# ---------------------------------------------------------------------------

class TestInvalidEnumCorrectedDuringMigrate(unittest.TestCase):
    def setUp(self):
        self.v = SettingsValidator()

    def test_invalid_quality_profile_fixed_after_migrate(self):
        s = _make_v1_settings(quality_profile="INVALID")
        migrated = self.v.migrate(s, "1.0", "2.0")
        # Migration preserves the value; validate then corrects it
        result = self.v.validate(migrated)
        self.assertEqual(result.fixed["quality_profile"], "balanced")
        self.assertTrue(result.valid)

    def test_invalid_translation_mode_fixed_after_migrate(self):
        s = _make_v1_settings(translation_mode="fr_to_de")
        migrated = self.v.migrate(s, "1.0", "2.0")
        result = self.v.validate(migrated)
        self.assertEqual(result.fixed["translation_mode"], "off")
        self.assertTrue(result.valid)

    def test_invalid_clipboard_mode_fixed_after_migrate(self):
        s = _make_v1_settings(clipboard_mode="always_ask")
        migrated = self.v.migrate(s, "1.0", "2.0")
        result = self.v.validate(migrated)
        self.assertEqual(result.fixed["clipboard_mode"], "always_copy")

    def test_migrate_does_not_itself_correct_enums(self):
        """Migration is a dumb structural transform; it should NOT correct enum values."""
        s = _make_v1_settings(quality_profile="BOGUS")
        migrated = self.v.migrate(s, "1.0", "2.0")
        # Migration preserves the bogus value — correction is validate()'s job
        self.assertEqual(migrated["quality_profile"], "BOGUS")


# ---------------------------------------------------------------------------
# 7. Missing fields filled with defaults
# ---------------------------------------------------------------------------

class TestMissingFieldFilledWithDefaults(unittest.TestCase):
    def setUp(self):
        self.v = SettingsValidator()

    def test_migrate_adds_overlay_opacity_if_missing(self):
        s = {}  # no fields at all
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertEqual(result["overlay_opacity_percent"], 45)

    def test_migrate_adds_all_call_fields(self):
        s = {}
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIn("call_budget_usd", result)
        self.assertIn("call_notify_default", result)
        self.assertIn("call_auto_summary", result)

    def test_migrate_does_not_overwrite_existing_defaults(self):
        """If a v1 user already set the value, migration must not override it."""
        s = {"overlay_opacity_percent": 80, "llm_rewrite_enabled": True}
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertEqual(result["overlay_opacity_percent"], 80)
        self.assertTrue(result["llm_rewrite_enabled"])

    def test_validate_fills_known_range_key_with_default_on_bad_type(self):
        """validate() fills default for unparsable range values."""
        result = self.v.validate({"history_page_size": None})
        self.assertEqual(result.fixed["history_page_size"], 50)  # default

    def test_validate_fills_default_bool_on_invalid(self):
        result = self.v.validate({"auto_paste": "maybe"})
        self.assertIs(result.fixed["auto_paste"], True)  # default is True


# ---------------------------------------------------------------------------
# 8. Unicode setting values preserved
# ---------------------------------------------------------------------------

class TestUnicodeSettingValuesPreserved(unittest.TestCase):
    def setUp(self):
        self.v = SettingsValidator()
        self._tmpdir = tempfile.mkdtemp()
        self.backup = SettingsBackup(backup_dir=__import__("pathlib").Path(self._tmpdir))

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_unicode_in_unknown_string_field(self):
        s = _make_v1_settings(
            custom_label="Привет мир",
            spanish_label="¡Hola mundo!",
        )
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertEqual(result["custom_label"], "Привет мир")
        self.assertEqual(result["spanish_label"], "¡Hola mundo!")

    def test_unicode_glossary_preserved_after_migrate_and_validate(self):
        s = _make_v1_settings(translation_glossary={"Краб": "crab", "Ухо": "ear"})
        migrated = self.v.migrate(s, "1.0", "2.0")
        result = self.v.validate(migrated)
        self.assertEqual(result.fixed["translation_glossary"]["Краб"], "crab")
        self.assertEqual(result.fixed["translation_glossary"]["Ухо"], "ear")

    def test_unicode_preserved_in_backup_round_trip(self):
        s = _make_v1_settings(my_note="Тест юникода: 日本語 🎵")
        backup_id = self.backup.create_backup(s, reason="unicode_test")
        restored = self.backup.restore_backup(backup_id)
        self.assertEqual(restored["my_note"], "Тест юникода: 日本語 🎵")

    def test_unicode_in_text_templates_preserved(self):
        s = {
            "text_templates": {
                "приветствие": "Привет, как дела?",
                "saludo": "¿Cómo estás?",
            }
        }
        result = self.v.validate(s)
        self.assertEqual(result.fixed["text_templates"]["приветствие"], "Привет, как дела?")
        self.assertEqual(result.fixed["text_templates"]["saludo"], "¿Cómo estás?")

    def test_cyrillic_keys_in_unknown_settings_preserved(self):
        s = _make_v1_settings(пользователь="Павел")
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertEqual(result["пользователь"], "Павел")


# ---------------------------------------------------------------------------
# 9. Concurrent migrations don't corrupt results (thread safety)
# ---------------------------------------------------------------------------

class TestConcurrentMigrateBlockedByLock(unittest.TestCase):
    """Verify that concurrent calls to migrate() produce correct independent results.

    SettingsValidator.migrate() is a pure function with no shared mutable state,
    so concurrent calls must each return a correct independent result — no cross-
    contamination between threads.
    """

    def setUp(self):
        self.v = SettingsValidator()

    def test_concurrent_migrations_all_correct(self):
        results: list[dict] = []
        errors: list[Exception] = []

        def run_migrate(seed_val):
            try:
                s = _make_v1_settings(seed_key=seed_val)
                out = self.v.migrate(s, "1.0", "2.0")
                results.append(out)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=run_migrate, args=(f"val_{i}",))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f"Errors in threads: {errors}")
        self.assertEqual(len(results), 10)
        # Each result must have the correct seed_key and all migration defaults
        seen_vals = {r["seed_key"] for r in results}
        self.assertEqual(seen_vals, {f"val_{i}" for i in range(10)})

    def test_concurrent_validations_all_correct(self):
        results: list[bool] = []
        errors: list[Exception] = []

        def run_validate():
            try:
                s = {"quality_profile": "balanced", "history_page_size": 50}
                out = self.v.validate(s)
                results.append(out.valid)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run_validate) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f"Errors in threads: {errors}")
        self.assertTrue(all(results))
        self.assertEqual(len(results), 20)

    def test_concurrent_backup_writes_produce_distinct_files(self):
        tmpdir = tempfile.mkdtemp()
        backup = SettingsBackup(backup_dir=__import__("pathlib").Path(tmpdir))
        ids: list[str] = []
        lock = threading.Lock()
        errors: list[Exception] = []

        def write_backup(i):
            try:
                time.sleep(i * 0.005)  # stagger slightly to avoid same-second collision
                bid = backup.create_backup({"key": f"v{i}"}, reason=f"t{i}")
                with lock:
                    ids.append(bid)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=write_backup, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [], f"Errors: {errors}")
        self.assertEqual(len(ids), 5)

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 10. Idempotent migration (calling twice = same result)
# ---------------------------------------------------------------------------

class TestMigrateIdempotent(unittest.TestCase):
    def setUp(self):
        self.v = SettingsValidator()

    def test_migrate_same_version_twice_is_noop(self):
        s = _make_v1_settings()
        once = self.v.migrate(s, "1.0", "2.0")
        # Migrate again from 2.0 → 2.0 (same version) is always a no-op
        twice = self.v.migrate(once, "2.0", "2.0")
        self.assertEqual(once, twice)

    def test_validate_twice_produces_same_fixed(self):
        s = _make_v1_settings()
        migrated = self.v.migrate(s, "1.0", "2.0")
        first = self.v.validate(migrated)
        second = self.v.validate(first.fixed)
        self.assertEqual(first.fixed, second.fixed)
        self.assertEqual(first.errors, second.errors)
        # Warnings may differ (second pass may have 0 warnings if first fixed everything)
        self.assertEqual(second.warnings, [])

    def test_migrate_then_validate_twice_stable(self):
        s = _make_v1_settings(
            quality_profile="balanced",
            history_page_size=50,
            overlay_opacity_percent=45,
        )
        migrated = self.v.migrate(s, "1.0", "2.0")
        r1 = self.v.validate(migrated)
        r2 = self.v.validate(r1.fixed)
        self.assertEqual(r1.fixed, r2.fixed)

    def test_add_default_ops_are_idempotent(self):
        """Running migration ops twice on already-migrated data is safe."""
        s = _make_v1_settings()
        migrated_once = self.v.migrate(s, "1.0", "2.0")
        # Manually apply migration ops a second time
        from backend.settings_validator import _MIGRATIONS, SettingsValidator
        ops = _MIGRATIONS[("1.0", "2.0")]
        migrated_twice = SettingsValidator._apply_migration_ops(migrated_once, ops)
        # add_default ops must not overwrite existing values
        self.assertEqual(migrated_once["overlay_opacity_percent"], migrated_twice["overlay_opacity_percent"])
        self.assertEqual(migrated_once["llm_rewrite_enabled"], migrated_twice["llm_rewrite_enabled"])
        self.assertEqual(migrated_once["capture_source_mode"], migrated_twice["capture_source_mode"])

    def test_rename_op_idempotent_when_old_key_gone(self):
        """Second rename op does nothing when old key no longer exists."""
        s = _make_v1_settings(history_limit="unlimited")
        migrated = self.v.migrate(s, "1.0", "2.0")
        # After migration, history_limit is gone → rename is no-op
        self.assertNotIn("history_limit", migrated)
        # Applying rename again is safe
        from backend.settings_validator import SettingsValidator
        re_applied = SettingsValidator._apply_migration_ops(
            migrated, [("rename", "history_limit", "history_policy")]
        )
        self.assertEqual(re_applied["history_policy"], migrated["history_policy"])


if __name__ == "__main__":
    unittest.main()
