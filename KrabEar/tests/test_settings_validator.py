"""Тесты для SettingsValidator."""

from backend.settings_validator import SettingsValidator, ValidationResult, CURRENT_SCHEMA_VERSION
import sys
import os
import unittest

# Настройка PYTHONPATH
_PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


class TestValidationResult(unittest.TestCase):
    def test_dataclass_defaults(self):
        vr = ValidationResult(valid=True)
        self.assertTrue(vr.valid)
        self.assertEqual(vr.errors, [])
        self.assertEqual(vr.warnings, [])
        self.assertEqual(vr.fixed, {})

    def test_dataclass_with_values(self):
        vr = ValidationResult(valid=False, errors=["err"], warnings=["warn"], fixed={"k": "v"})
        self.assertFalse(vr.valid)
        self.assertEqual(vr.errors, ["err"])
        self.assertEqual(vr.warnings, ["warn"])
        self.assertEqual(vr.fixed, {"k": "v"})


class TestValidateEnumFields(unittest.TestCase):
    def setUp(self):
        self.v = SettingsValidator()

    def test_valid_quality_profile(self):
        result = self.v.validate({"quality_profile": "balanced"})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["quality_profile"], "balanced")
        self.assertEqual(result.warnings, [])

    def test_invalid_quality_profile_auto_fixed(self):
        result = self.v.validate({"quality_profile": "ultra"})
        self.assertTrue(result.valid)  # no hard error, just warning
        self.assertEqual(result.fixed["quality_profile"], "balanced")
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("quality_profile", result.warnings[0])

    def test_invalid_cleanup_profile_auto_fixed(self):
        result = self.v.validate({"cleanup_profile": "extreme"})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["cleanup_profile"], "soft")

    def test_valid_translation_mode(self):
        result = self.v.validate({"translation_mode": "ru_to_es"})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["translation_mode"], "ru_to_es")

    def test_invalid_translation_mode(self):
        result = self.v.validate({"translation_mode": "en_to_es"})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["translation_mode"], "off")

    def test_invalid_update_channel(self):
        result = self.v.validate({"update_channel": "nightly"})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["update_channel"], "stable")

    def test_multiple_invalid_enums(self):
        result = self.v.validate({
            "quality_profile": "xxx",
            "cleanup_profile": "yyy",
            "translation_mode": "zzz",
        })
        self.assertTrue(result.valid)
        self.assertEqual(len(result.warnings), 3)

    def test_valid_live_subs_language_ru(self):
        """2026-08-12 live-subs-language-routing: enum, не диапазон (не в _RANGE_FIELDS)."""
        result = self.v.validate({"live_subs_language": "ru"})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["live_subs_language"], "ru")
        self.assertEqual(result.warnings, [])

    def test_valid_live_subs_language_all_allowed(self):
        for lang in ("ru", "es", "en", "auto"):
            with self.subTest(lang=lang):
                result = self.v.validate({"live_subs_language": lang})
                self.assertTrue(result.valid)
                self.assertEqual(result.fixed["live_subs_language"], lang)
                self.assertEqual(result.warnings, [])

    def test_invalid_live_subs_language_auto_fixed_to_ru(self):
        result = self.v.validate({"live_subs_language": "de"})
        self.assertTrue(result.valid)  # no hard error, just warning
        self.assertEqual(result.fixed["live_subs_language"], "ru")
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("live_subs_language", result.warnings[0])


class TestValidateUiLastTabField(unittest.TestCase):
    """ui_last_tab allowlist must cover every PanelTab rawValue the Swift agent
    actually sends (native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift
    enum PanelTab), not just the three tabs that shipped first.  A value missing
    from the allowlist is silently rewritten to 'dictation' every time the user
    switches to that tab, spamming a WARNING on every launch of a mismatched
    feature (e.g. every "Разговор с AI" / wake-word start) and permanently
    breaking tab-restore-on-relaunch for that tab.
    """

    def setUp(self):
        self.v = SettingsValidator()

    def test_valid_ui_last_tab_conversation_not_rewritten(self):
        result = self.v.validate({"ui_last_tab": "conversation"})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["ui_last_tab"], "conversation")
        self.assertEqual(result.warnings, [])

    def test_valid_ui_last_tab_call_automation_not_rewritten(self):
        result = self.v.validate({"ui_last_tab": "call_automation"})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["ui_last_tab"], "call_automation")
        self.assertEqual(result.warnings, [])

    def test_valid_ui_last_tab_diagnostics_not_rewritten(self):
        result = self.v.validate({"ui_last_tab": "diagnostics"})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["ui_last_tab"], "diagnostics")
        self.assertEqual(result.warnings, [])

    def test_valid_ui_last_tab_archive_not_rewritten(self):
        result = self.v.validate({"ui_last_tab": "archive"})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["ui_last_tab"], "archive")
        self.assertEqual(result.warnings, [])

    def test_valid_ui_last_tab_dictation_still_valid(self):
        """Pre-existing allowed value must keep working after the allowlist grows."""
        result = self.v.validate({"ui_last_tab": "dictation"})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["ui_last_tab"], "dictation")
        self.assertEqual(result.warnings, [])

    def test_valid_ui_last_tab_live_translation_still_valid(self):
        result = self.v.validate({"ui_last_tab": "live_translation"})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["ui_last_tab"], "live_translation")
        self.assertEqual(result.warnings, [])

    def test_valid_ui_last_tab_history_still_valid(self):
        result = self.v.validate({"ui_last_tab": "history"})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["ui_last_tab"], "history")
        self.assertEqual(result.warnings, [])

    def test_invalid_ui_last_tab_still_rewritten_to_dictation(self):
        """A genuinely unknown tab id must still be auto-fixed (regression guard)."""
        result = self.v.validate({"ui_last_tab": "not_a_real_tab"})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["ui_last_tab"], "dictation")
        self.assertEqual(len(result.warnings), 1)


class TestValidateRangeFields(unittest.TestCase):
    def setUp(self):
        self.v = SettingsValidator()

    def test_valid_history_page_size(self):
        result = self.v.validate({"history_page_size": 50})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["history_page_size"], 50)

    def test_history_page_size_clamped_high(self):
        result = self.v.validate({"history_page_size": 9999})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["history_page_size"], 500)
        self.assertIn("history_page_size", result.warnings[0])

    def test_history_page_size_clamped_low(self):
        result = self.v.validate({"history_page_size": 1})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["history_page_size"], 10)

    def test_audio_ducking_percent_valid(self):
        result = self.v.validate({"audio_ducking_percent": 75})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["audio_ducking_percent"], 75)

    def test_audio_ducking_percent_out_of_range(self):
        result = self.v.validate({"audio_ducking_percent": 150})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["audio_ducking_percent"], 100)

    def test_notify_confidence_threshold_valid(self):
        result = self.v.validate({"notify_confidence_threshold": 0.7})
        self.assertTrue(result.valid)
        self.assertAlmostEqual(result.fixed["notify_confidence_threshold"], 0.7)

    def test_notify_confidence_threshold_clamped(self):
        result = self.v.validate({"notify_confidence_threshold": 1.5})
        self.assertTrue(result.valid)
        self.assertAlmostEqual(result.fixed["notify_confidence_threshold"], 1.0)

    def test_invalid_non_numeric_range_field(self):
        result = self.v.validate({"history_page_size": "abc"})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["history_page_size"], 50)  # default
        self.assertEqual(len(result.warnings), 1)

    def test_range_type_coercion_float_string(self):
        result = self.v.validate({"notify_confidence_threshold": "0.8"})
        self.assertTrue(result.valid)
        self.assertAlmostEqual(result.fixed["notify_confidence_threshold"], 0.8)

    def test_overlay_opacity_clamped(self):
        result = self.v.validate({"overlay_opacity_percent": 5})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["overlay_opacity_percent"], 15)


class TestValidateBoolFields(unittest.TestCase):
    def setUp(self):
        self.v = SettingsValidator()

    def test_bool_true(self):
        result = self.v.validate({"auto_paste": True})
        self.assertTrue(result.valid)
        self.assertIs(result.fixed["auto_paste"], True)

    def test_bool_false(self):
        result = self.v.validate({"auto_paste": False})
        self.assertTrue(result.valid)
        self.assertIs(result.fixed["auto_paste"], False)

    def test_bool_string_true(self):
        result = self.v.validate({"auto_paste": "true"})
        self.assertTrue(result.valid)
        self.assertIs(result.fixed["auto_paste"], True)

    def test_bool_string_false(self):
        result = self.v.validate({"auto_paste": "false"})
        self.assertTrue(result.valid)
        self.assertIs(result.fixed["auto_paste"], False)

    def test_bool_int_1(self):
        result = self.v.validate({"auto_paste": 1})
        self.assertTrue(result.valid)
        self.assertIs(result.fixed["auto_paste"], True)

    def test_bool_int_0(self):
        result = self.v.validate({"auto_paste": 0})
        self.assertTrue(result.valid)
        self.assertIs(result.fixed["auto_paste"], False)

    def test_bool_invalid_uses_default(self):
        result = self.v.validate({"auto_paste": "maybe"})
        self.assertTrue(result.valid)
        self.assertIs(result.fixed["auto_paste"], True)  # default for auto_paste
        self.assertEqual(len(result.warnings), 1)

    def test_bool_none_uses_default(self):
        result = self.v.validate({"silence_guard_enabled": None})
        self.assertTrue(result.valid)
        self.assertIs(result.fixed["silence_guard_enabled"], True)


class TestLLMProbeSettingRegistered(unittest.TestCase):
    """2026-09-03: llm_probe_enabled/llm_probe_interval_sec были read-only

    inline defaults в service.py (`.get("llm_probe_enabled", True)`), не
    зарегистрированы ни в DEFAULT_SETTINGS, ни в _BOOL_FIELDS/_RANGE_FIELDS.
    Строковое "false" из settings.json/set_settings оставалось truthy
    (тот же класс бага, что recording_owner_enforce), а get_settings не
    отдавал ключ вовсе, пока кто-то не поставит его вручную через сырой IPC.
    """

    def setUp(self):
        self.v = SettingsValidator()

    def test_llm_probe_enabled_registered_in_default_settings(self):
        from core.config import DEFAULT_SETTINGS
        self.assertIn("llm_probe_enabled", DEFAULT_SETTINGS)
        self.assertIs(DEFAULT_SETTINGS["llm_probe_enabled"], True)

    def test_llm_probe_interval_sec_registered_in_default_settings(self):
        from core.config import DEFAULT_SETTINGS
        self.assertIn("llm_probe_interval_sec", DEFAULT_SETTINGS)
        self.assertEqual(DEFAULT_SETTINGS["llm_probe_interval_sec"], 30.0)

    def test_llm_probe_enabled_string_false_coerced_to_bool(self):
        result = self.v.validate({"llm_probe_enabled": "false"})
        self.assertTrue(result.valid)
        self.assertIs(result.fixed["llm_probe_enabled"], False)

    def test_llm_probe_interval_sec_clamped_to_range(self):
        result = self.v.validate({"llm_probe_interval_sec": 9999.0})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["llm_probe_interval_sec"], 300.0)


class TestValidateSpecialFields(unittest.TestCase):
    def setUp(self):
        self.v = SettingsValidator()

    def test_translation_glossary_valid(self):
        result = self.v.validate({"translation_glossary": {"hello": "привет"}})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["translation_glossary"], {"hello": "привет"})

    def test_translation_glossary_invalid_type(self):
        result = self.v.validate({"translation_glossary": ["hello"]})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["translation_glossary"], {})
        self.assertIn("translation_glossary", result.warnings[0])

    def test_text_templates_valid(self):
        result = self.v.validate({"text_templates": {"key": "value"}})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["text_templates"], {"key": "value"})

    def test_text_templates_invalid_type(self):
        result = self.v.validate({"text_templates": "not a dict"})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["text_templates"], {})

    def test_text_templates_strips_whitespace(self):
        result = self.v.validate({"text_templates": {"  key  ": "  value  "}})
        self.assertTrue(result.valid)
        self.assertIn("key", result.fixed["text_templates"])
        self.assertEqual(result.fixed["text_templates"]["key"], "value")

    def test_stt_hotwords_drops_non_str_and_strips(self):
        # W1768: крафтнутый stt_hotwords с не-строковыми элементами не должен
        # просачиваться через validate() (import_settings/restore_settings_backup),
        # иначе `_w.strip()` в recording_core_service / transcript_context падает
        # с AttributeError и тихо рушит поток транскрипции.
        result = self.v.validate({"stt_hotwords": [None, 123, {}, "ok ", " good"]})
        self.assertTrue(result.valid)
        # Не-строки отброшены; строки застрипаны; пустые удалены.
        self.assertEqual(result.fixed["stt_hotwords"], ["ok", "good"])

    def test_stt_hotwords_valid_list_passes_unchanged(self):
        result = self.v.validate({"stt_hotwords": ["Краб", "GigaAM"]})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["stt_hotwords"], ["Краб", "GigaAM"])
        self.assertEqual(result.warnings, [])

    def test_stt_hotwords_invalid_type_becomes_empty(self):
        result = self.v.validate({"stt_hotwords": "not a list"})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["stt_hotwords"], [])

    def test_voice_gateway_url_valid_localhost(self):
        result = self.v.validate({"voice_gateway_url": "http://127.0.0.1:8090"})
        self.assertTrue(result.valid)
        self.assertEqual(result.errors, [])

    def test_voice_gateway_url_valid_localhost_name(self):
        result = self.v.validate({"voice_gateway_url": "http://localhost:9000/ws"})
        self.assertTrue(result.valid)
        self.assertEqual(result.errors, [])

    def test_voice_gateway_url_valid_https(self):
        result = self.v.validate({"voice_gateway_url": "https://gateway.example.com"})
        self.assertTrue(result.valid)

    # W850 — IPv6 loopback variants
    def test_voice_gateway_url_ipv6_loopback_http(self):
        """http://[::1] must be allowed — IPv6 loopback for dual-stack VG installs."""
        result = self.v.validate({"voice_gateway_url": "http://[::1]:9000/ws"})
        self.assertTrue(result.valid)
        self.assertEqual(result.errors, [])

    def test_voice_gateway_url_ipv6_loopback_https(self):
        """https://[::1] must be allowed."""
        result = self.v.validate({"voice_gateway_url": "https://[::1]:9000/ws"})
        self.assertTrue(result.valid)
        self.assertEqual(result.errors, [])

    def test_voice_gateway_url_ipv6_loopback_no_port(self):
        """http://[::1] without explicit port must be allowed."""
        result = self.v.validate({"voice_gateway_url": "http://[::1]/ws"})
        self.assertTrue(result.valid)
        self.assertEqual(result.errors, [])

    def test_voice_gateway_url_invalid(self):
        result = self.v.validate({"voice_gateway_url": "http://evil.com/api"})
        self.assertFalse(result.valid)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("voice_gateway_url", result.errors[0])

    def test_voice_gateway_url_rfc1918_http_invalid(self):
        """http://192.168.x.x must be rejected (not loopback, not external HTTPS)."""
        result = self.v.validate({"voice_gateway_url": "http://192.168.1.100:9000/ws"})
        self.assertFalse(result.valid)
        self.assertEqual(len(result.errors), 1)

    def test_voice_gateway_url_ftp_scheme_invalid(self):
        """ftp:// must be rejected."""
        result = self.v.validate({"voice_gateway_url": "ftp://localhost/ws"})
        self.assertFalse(result.valid)
        self.assertEqual(len(result.errors), 1)

    def test_empty_settings_valid(self):
        result = self.v.validate({})
        self.assertTrue(result.valid)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.warnings, [])


class TestMigration(unittest.TestCase):
    def setUp(self):
        self.v = SettingsValidator()

    def test_migrate_same_version_noop(self):
        s = {"quality_profile": "balanced", "mode": "headless"}
        result = self.v.migrate(s, "2.0", "2.0")
        self.assertEqual(result, s)

    def test_migrate_1_0_to_2_0_adds_defaults(self):
        s = {"quality_profile": "balanced"}
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIn("overlay_opacity_percent", result)
        self.assertEqual(result["overlay_opacity_percent"], 45)
        self.assertIn("call_budget_usd", result)
        self.assertIn("llm_rewrite_enabled", result)
        self.assertFalse(result["llm_rewrite_enabled"])

    def test_migrate_1_0_to_2_0_renames_history_limit(self):
        s = {"history_limit": "unlimited"}
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIn("history_policy", result)
        self.assertEqual(result["history_policy"], "unlimited")
        self.assertNotIn("history_limit", result)

    def test_migrate_1_0_to_2_0_no_overwrite_existing(self):
        """Если ключ уже есть, add_default не должен перезаписывать."""
        s = {"overlay_opacity_percent": 60}
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertEqual(result["overlay_opacity_percent"], 60)

    def test_migrate_unknown_version_raises(self):
        with self.assertRaises(ValueError):
            self.v.migrate({}, "0.1", "2.0")

    def test_migrate_preserves_existing_keys(self):
        s = {"quality_profile": "max", "auto_paste": True}
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertEqual(result["quality_profile"], "max")
        self.assertTrue(result["auto_paste"])

    def test_current_schema_version_constant(self):
        self.assertEqual(CURRENT_SCHEMA_VERSION, "2.0")


class TestValidateFullSettings(unittest.TestCase):
    """Интеграционный тест: полный словарь настроек проходит валидацию."""

    def setUp(self):
        self.v = SettingsValidator()

    def test_default_settings_valid(self):
        from backend.models import DEFAULT_SETTINGS
        result = self.v.validate(DEFAULT_SETTINGS)
        self.assertTrue(result.valid)
        self.assertEqual(result.errors, [])

    def test_multiple_invalid_fields_collected(self):
        s = {
            "quality_profile": "bad",
            "cleanup_profile": "bad",
            "history_page_size": 9999,
            "audio_ducking_percent": -5,
        }
        result = self.v.validate(s)
        self.assertTrue(result.valid)  # warnings, not errors
        self.assertGreaterEqual(len(result.warnings), 4)

    def test_fixed_dict_independent_of_input(self):
        """Изменение fixed не должно влиять на оригинальный dict."""
        s = {"quality_profile": "balanced"}
        result = self.v.validate(s)
        result.fixed["quality_profile"] = "max"
        self.assertEqual(s["quality_profile"], "balanced")


# ---------------------------------------------------------------------------
# Wave 105: additional coverage tests
# ---------------------------------------------------------------------------

class TestValidateUnknownKeysPreserved(unittest.TestCase):
    """test_unknown_keys_preserved: extra keys pass through unchanged."""

    def setUp(self):
        self.v = SettingsValidator()

    def test_unknown_keys_preserved(self):
        s = {"unknown_future_key": "value123", "quality_profile": "balanced"}
        result = self.v.validate(s)
        self.assertTrue(result.valid)
        self.assertIn("unknown_future_key", result.fixed)
        self.assertEqual(result.fixed["unknown_future_key"], "value123")

    def test_unknown_keys_not_in_warnings(self):
        """Unknown keys should NOT generate warnings — forward-compatible."""
        s = {"totally_new_key": 42}
        result = self.v.validate(s)
        self.assertTrue(result.valid)
        self.assertEqual(result.errors, [])
        # No warning about the unknown key itself
        unknown_warnings = [w for w in result.warnings if "totally_new_key" in w]
        self.assertEqual(unknown_warnings, [])


class TestValidateDefaultFilledForMissing(unittest.TestCase):
    """test_default_filled_for_missing: absent known fields get defaults on migrate."""

    def setUp(self):
        self.v = SettingsValidator()

    def test_default_filled_for_missing_after_migrate(self):
        """After 1.0→2.0 migration, known new fields are populated with defaults."""
        s = {}
        result = self.v.migrate(s, "1.0", "2.0")
        self.assertIn("overlay_opacity_percent", result)
        self.assertEqual(result["overlay_opacity_percent"], 45)
        self.assertIn("call_notify_default", result)
        self.assertTrue(result["call_notify_default"])

    def test_missing_field_not_touched_by_validate(self):
        """validate() should NOT inject defaults for absent optional keys."""
        s = {"quality_profile": "balanced"}
        result = self.v.validate(s)
        # history_page_size is absent — validate() must NOT add it
        self.assertNotIn("history_page_size", result.fixed)


class TestValidateConcurrentThreadSafe(unittest.TestCase):
    """test_validate_concurrent_thread_safe: concurrent validate() calls are safe."""

    def setUp(self):
        self.v = SettingsValidator()

    def test_validate_concurrent_thread_safe(self):
        import threading
        errors: list[Exception] = []
        results: list = []

        def _run(i: int) -> None:
            try:
                s = {"quality_profile": "balanced" if i % 2 == 0 else "bad_value"}
                r = self.v.validate(s)
                results.append(r.valid)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_run, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent validate raised: {errors}")
        self.assertEqual(len(results), 20)
        for valid in results:
            self.assertIsInstance(valid, bool)

    def test_migrate_concurrent_thread_safe(self):
        import threading
        errors: list[Exception] = []

        def _run() -> None:
            try:
                s = {"history_limit": "unlimited"}
                self.v.migrate(s, "1.0", "2.0")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_run) for _ in range(15)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent migrate raised: {errors}")


if __name__ == "__main__":
    unittest.main()
