"""Dedicated unit tests для SettingsService.

Проверяет:
- Кэширование с TTL (5 сек): cached_settings() вызывает load_settings только при необходимости
- Cache hit в пределах TTL: второй вызов не зовёт load_settings
- Cache miss после TTL: вызов через 5.1 сек должен снова вызвать load_settings
- Invalidate on set_settings: cache сбрасывается после save_settings
- Invalidate on apply_profile_preset: cache сбрасывается
- Invalidate on import_settings: cache сбрасывается
- Invalidate on set_notification_preferences: cache сбрасывается
- Cached value is a COPY: mutate returned dict не должен влиять на кэш
- Profile preset validation: невалидный preset raises ValueError
- Profile preset application: apply_profile_preset должен применять preset settings
- export_settings: возвращает dict с полными (кроме sensitive) настройками
- Coerce helpers: _coerce_bool и _coerce_bounded работают корректно
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.settings_service import SettingsService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _make_store(settings: dict | None = None) -> MagicMock:
    """Создаёт фиктивный store с сохранёнными настройками."""
    store = MagicMock()
    current: dict = dict(settings or {
        "quality_profile": "balanced",
        "cleanup_profile": "soft",
        "translation_mode": "off",
        "auto_paste": True,
        "realtime_preview_enabled": True,
        "mode": "headless",
        "translation_style": "neutral",
        "clipboard_mode": "always_copy",
        "update_channel": "stable",
        "translation_glossary": {},
        "text_templates": {},
        "network_mode": "offline_default",
        "hotkey_profile": "default",
        "history_policy": "unlimited",
        "history_text_density": "normal",
        "capture_source_mode": "mic",
        "ui_last_tab": "history",
        "auto_start_enabled": False,
        "show_dock_icon": True,
        "play_start_sound": True,
        "audio_ducking_enabled": True,
        "silence_guard_enabled": True,
        "background_guard_enabled": True,
        "call_notify_default": True,
        "call_auto_summary": True,
        "history_focus_mode": True,
        "voice_gateway_url": "http://127.0.0.1:8090",
        "voice_gateway_api_key": "",
        "history_page_size": 50,
        "audio_ducking_percent": 50,
        "stop_tail_trim_ms": 180,
        "silence_guard_rms_threshold": 0.0020,
        "silence_guard_peak_threshold": 0.0120,
        "silence_guard_active_ratio_threshold": 0.015,
        "background_guard_min_peak": 0.025,
        "background_guard_min_rms": 0.0040,
        "background_guard_uniform_frame_threshold": 0.0060,
        "background_guard_max_uniform_active_ratio": 0.92,
        "overlay_opacity_percent": 45,
        "notifications_enabled": True,
        "notify_on_low_confidence": True,
        "notify_confidence_threshold": 0.5,
        "notify_on_llm_failure": True,
        "notify_on_import_complete": True,
        "notify_sound_enabled": True,
    })
    store.load_settings.return_value = dict(current)
    saved_holder: list[dict] = []

    def _save(s: dict) -> dict:
        current.clear()
        current.update(s)
        store.load_settings.return_value = dict(current)
        saved_holder.clear()
        saved_holder.append(dict(s))
        return dict(s)

    store.save_settings.side_effect = _save
    store._saved = saved_holder
    store._current = current
    return store


class TestCachingBasics(unittest.TestCase):
    """Тесты базового кэширования настроек."""

    def test_cached_settings_calls_load_settings_on_fresh_start(self):
        """cached_settings() должен вызвать load_settings при инициализации."""
        store = _make_store()
        svc = SettingsService(store=store)

        result = svc.cached_settings()

        store.load_settings.assert_called_once()
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("quality_profile"), "balanced")

    def test_cached_settings_returns_dict_copy(self):
        """cached_settings() должен вернуть копию, а не сам кэш."""
        store = _make_store()
        svc = SettingsService(store=store)

        result1 = svc.cached_settings()
        result1["quality_profile"] = "max"  # мутируем копию

        result2 = svc.cached_settings()

        # Кэш должен остаться неизменённым
        self.assertEqual(result2.get("quality_profile"), "balanced")
        self.assertNotEqual(result1["quality_profile"], result2["quality_profile"])

    def test_cached_settings_with_mock_clock_hit_within_ttl(self):
        """Два вызова within 5s должны использовать кэш (second call не зовёт load_settings)."""
        store = _make_store()
        svc = SettingsService(store=store)

        with patch("time.monotonic") as mock_time:
            mock_time.return_value = 100.0
            svc.cached_settings()
            store.load_settings.reset_mock()

            # Второй вызов в пределах TTL
            mock_time.return_value = 103.0
            svc.cached_settings()

            store.load_settings.assert_not_called()

    def test_cached_settings_with_mock_clock_miss_after_ttl(self):
        """Вызов через 5.1s должен перезагрузить кэш."""
        store = _make_store()
        svc = SettingsService(store=store)

        with patch("time.monotonic") as mock_time:
            mock_time.return_value = 100.0
            svc.cached_settings()
            store.load_settings.reset_mock()

            # Третий вызов после истечения TTL
            mock_time.return_value = 105.2  # > 100 + 5.0
            svc.cached_settings()

            store.load_settings.assert_called_once()

    def test_cache_invalidate_resets_state(self):
        """invalidate_cache() должен сбросить _cache и _cache_ts."""
        store = _make_store()
        svc = SettingsService(store=store)

        svc.cached_settings()
        self.assertIsNotNone(svc._cache)
        self.assertGreater(svc._cache_ts, 0.0)

        svc.invalidate_cache()

        self.assertIsNone(svc._cache)
        self.assertEqual(svc._cache_ts, 0.0)


class TestHandleGetSettings(unittest.TestCase):
    """Тесты для handle_get_settings."""

    def test_handle_get_settings_returns_cached_settings(self):
        """handle_get_settings должен вернуть результат cached_settings()."""
        store = _make_store()
        svc = SettingsService(store=store)

        result = svc.handle_get_settings({})

        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("quality_profile"), "balanced")


class TestHandleSetSettings(unittest.TestCase):
    """Тесты для handle_set_settings."""

    def test_handle_set_settings_updates_and_invalidates_cache(self):
        """handle_set_settings должен сохранить настройки и инвалидировать кэш."""
        store = _make_store()
        svc = SettingsService(store=store)

        svc.cached_settings()
        svc.handle_set_settings({"quality_profile": "max"})

        self.assertEqual(store._current.get("quality_profile"), "max")
        self.assertIsNone(svc._cache)  # cache invalidated

    def test_handle_set_settings_normalizes_enum_fields(self):
        """handle_set_settings должен нормализовать enum-поля на значения по умолчанию."""
        store = _make_store()
        svc = SettingsService(store=store)

        svc.handle_set_settings({"quality_profile": "invalid_profile"})

        # Должен быть установлен default
        self.assertEqual(store._current.get("quality_profile"), "balanced")

    def test_handle_set_settings_normalizes_bool_fields(self):
        """handle_set_settings должен нормализовать bool-поля."""
        store = _make_store()
        svc = SettingsService(store=store)

        svc.handle_set_settings({"auto_paste": False})

        self.assertFalse(store._current.get("auto_paste"))

    def test_handle_set_settings_normalizes_int_fields(self):
        """handle_set_settings должен нормализовать int-поля (history_page_size)."""
        store = _make_store()
        svc = SettingsService(store=store)

        svc.handle_set_settings({"history_page_size": 100})

        self.assertEqual(store._current.get("history_page_size"), 100)

    def test_handle_set_settings_clamps_int_fields(self):
        """handle_set_settings должен зажимать int-поля в допустимый диапазон."""
        store = _make_store()
        svc = SettingsService(store=store)

        svc.handle_set_settings({"history_page_size": 1000})  # > 500 max

        self.assertEqual(store._current.get("history_page_size"), 500)

    def test_handle_set_settings_validates_voice_gateway_url(self):
        """handle_set_settings должен проверять localhost/HTTPS для voice_gateway_url."""
        store = _make_store()
        svc = SettingsService(store=store)

        with self.assertRaises(ValueError):
            svc.handle_set_settings({"voice_gateway_url": "http://example.com"})


class TestHandleApplyProfilePreset(unittest.TestCase):
    """Тесты для handle_apply_profile_preset."""

    def test_apply_profile_preset_valid_default(self):
        """apply_profile_preset с 'default' должен применить default preset."""
        store = _make_store()
        svc = SettingsService(store=store)

        svc.handle_apply_profile_preset({"profile": "default"})

        self.assertEqual(store._current.get("quality_profile"), "balanced")
        self.assertEqual(store._current.get("cleanup_profile"), "soft")
        self.assertTrue(store._current.get("auto_paste"))

    def test_apply_profile_preset_valid_meeting(self):
        """apply_profile_preset с 'meeting' должен применить meeting preset."""
        store = _make_store()
        svc = SettingsService(store=store)

        svc.handle_apply_profile_preset({"profile": "meeting"})

        self.assertEqual(store._current.get("quality_profile"), "max")
        self.assertEqual(store._current.get("cleanup_profile"), "strict")
        self.assertFalse(store._current.get("auto_paste"))

    def test_apply_profile_preset_valid_translation(self):
        """apply_profile_preset с 'translation' должен применить translation preset."""
        store = _make_store()
        svc = SettingsService(store=store)

        svc.handle_apply_profile_preset({"profile": "translation"})

        self.assertEqual(store._current.get("translation_mode"), "auto")
        self.assertTrue(store._current.get("translate_and_paste"))

    def test_apply_profile_preset_valid_call_recording(self):
        """apply_profile_preset с 'call_recording' должен применить preset."""
        store = _make_store()
        svc = SettingsService(store=store)

        svc.handle_apply_profile_preset({"profile": "call_recording"})

        self.assertEqual(store._current.get("quality_profile"), "max")
        self.assertFalse(store._current.get("realtime_preview_enabled"))

    def test_apply_profile_preset_invalid_raises_error(self):
        """apply_profile_preset с невалидным preset должен raise ValueError."""
        store = _make_store()
        svc = SettingsService(store=store)

        with self.assertRaises(ValueError) as ctx:
            svc.handle_apply_profile_preset({"profile": "nonexistent"})

        self.assertIn("Неизвестный пресет", str(ctx.exception))

    def test_apply_profile_preset_invalidates_cache(self):
        """apply_profile_preset должен инвалидировать кэш."""
        store = _make_store()
        svc = SettingsService(store=store)

        svc.cached_settings()
        svc.handle_apply_profile_preset({"profile": "meeting"})

        self.assertIsNone(svc._cache)

    def test_apply_profile_preset_list(self):
        """handle_list_profile_presets должен вернуть список всех пресетов."""
        store = _make_store()
        svc = SettingsService(store=store)

        result = svc.handle_list_profile_presets({})

        self.assertIn("presets", result)
        self.assertEqual(len(result["presets"]), 4)
        preset_names = {p["name"] for p in result["presets"]}
        self.assertEqual(preset_names, {"default", "meeting", "translation", "call_recording"})


class TestHandleNotificationPreferences(unittest.TestCase):
    """Тесты для handle_get_notification_preferences и handle_set_notification_preferences."""

    def test_get_notification_preferences_returns_all_fields(self):
        """get_notification_preferences должен вернуть все поля уведомлений."""
        store = _make_store()
        svc = SettingsService(store=store)

        result = svc.handle_get_notification_preferences({})

        expected_keys = {
            "notifications_enabled",
            "notify_on_low_confidence",
            "notify_confidence_threshold",
            "notify_on_llm_failure",
            "notify_on_import_complete",
            "notify_sound_enabled",
        }
        self.assertEqual(set(result.keys()), expected_keys)

    def test_set_notification_preferences_updates_bool_field(self):
        """set_notification_preferences должен обновить bool-поля."""
        store = _make_store()
        svc = SettingsService(store=store)

        svc.handle_set_notification_preferences({"notifications_enabled": False})

        self.assertFalse(store._current.get("notifications_enabled"))

    def test_set_notification_preferences_updates_threshold_field(self):
        """set_notification_preferences должен обновить notify_confidence_threshold."""
        store = _make_store()
        svc = SettingsService(store=store)

        svc.handle_set_notification_preferences({"notify_confidence_threshold": 0.7})

        self.assertEqual(store._current.get("notify_confidence_threshold"), 0.7)

    def test_set_notification_preferences_clamps_threshold(self):
        """set_notification_preferences должен зажимать threshold в [0.0, 1.0]."""
        store = _make_store()
        svc = SettingsService(store=store)

        svc.handle_set_notification_preferences({"notify_confidence_threshold": 1.5})

        self.assertEqual(store._current.get("notify_confidence_threshold"), 1.0)

    def test_set_notification_preferences_invalidates_cache(self):
        """set_notification_preferences должен инвалидировать кэш."""
        store = _make_store()
        svc = SettingsService(store=store)

        svc.cached_settings()
        svc.handle_set_notification_preferences({"notifications_enabled": False})

        self.assertIsNone(svc._cache)


class TestCoerceFunctions(unittest.TestCase):
    """Тесты для _coerce_bool и _coerce_bounded."""

    def test_coerce_bool_with_bool_value(self):
        """_coerce_bool с bool должен вернуть тот же bool."""
        self.assertTrue(SettingsService._coerce_bool(True, default=False))
        self.assertFalse(SettingsService._coerce_bool(False, default=True))

    def test_coerce_bool_with_string_true_variants(self):
        """_coerce_bool должен распознать '1', 'true', 'on', 'yes'."""
        for val in ["1", "true", "on", "yes"]:
            self.assertTrue(SettingsService._coerce_bool(val, default=False), f"Failed for '{val}'")

    def test_coerce_bool_with_string_false_variants(self):
        """_coerce_bool должен распознать '0', 'false', 'off', 'no'."""
        for val in ["0", "false", "off", "no"]:
            self.assertFalse(SettingsService._coerce_bool(val, default=True), f"Failed for '{val}'")

    def test_coerce_bool_with_none_returns_default(self):
        """_coerce_bool с None должен вернуть default."""
        self.assertTrue(SettingsService._coerce_bool(None, default=True))
        self.assertFalse(SettingsService._coerce_bool(None, default=False))

    def test_coerce_bool_with_int(self):
        """_coerce_bool с int должен вернуть bool(int)."""
        self.assertTrue(SettingsService._coerce_bool(1, default=False))
        self.assertFalse(SettingsService._coerce_bool(0, default=True))

    def test_coerce_bool_with_invalid_string_returns_default(self):
        """_coerce_bool с невалидной строкой должен вернуть default."""
        self.assertTrue(SettingsService._coerce_bool("invalid", default=True))
        self.assertFalse(SettingsService._coerce_bool("invalid", default=False))

    def test_coerce_bounded_with_int(self):
        """_coerce_bounded должен работать с int."""
        result = SettingsService._coerce_bounded(50, default=50, min_value=0, max_value=100)
        self.assertEqual(result, 50)
        self.assertIsInstance(result, int)

    def test_coerce_bounded_with_float(self):
        """_coerce_bounded должен работать с float."""
        result = SettingsService._coerce_bounded(0.5, default=0.5, min_value=0.0, max_value=1.0)
        self.assertAlmostEqual(result, 0.5)
        self.assertIsInstance(result, float)

    def test_coerce_bounded_clamps_min(self):
        """_coerce_bounded должен зажимать к минимуму."""
        result = SettingsService._coerce_bounded(-10, default=50, min_value=0, max_value=100)
        self.assertEqual(result, 0)

    def test_coerce_bounded_clamps_max(self):
        """_coerce_bounded должен зажимать к максимуму."""
        result = SettingsService._coerce_bounded(200, default=50, min_value=0, max_value=100)
        self.assertEqual(result, 100)

    def test_coerce_bounded_with_invalid_value_returns_default(self):
        """_coerce_bounded с невалидным значением должен вернуть default."""
        result = SettingsService._coerce_bounded("invalid", default=50, min_value=0, max_value=100)
        self.assertEqual(result, 50)

    def test_coerce_bounded_converts_string_int(self):
        """_coerce_bounded должен конвертировать строку в int."""
        result = SettingsService._coerce_bounded("75", default=50, min_value=0, max_value=100)
        self.assertEqual(result, 75)


class TestCacheMultipleCalls(unittest.TestCase):
    """Тесты кэширования при множественных вызовах операций."""

    def test_multiple_set_operations_invalidate_cache(self):
        """Каждый handle_set_settings должен инвалидировать кэш."""
        store = _make_store()
        svc = SettingsService(store=store)

        svc.cached_settings()
        svc.handle_set_settings({"quality_profile": "max"})
        self.assertIsNone(svc._cache)

        svc.cached_settings()
        svc.handle_set_settings({"auto_paste": False})
        self.assertIsNone(svc._cache)

    def test_cache_reloads_after_invalidation_via_mock_clock(self):
        """После инвалидации, cached_settings() должен перезагрузить."""
        store = _make_store()
        svc = SettingsService(store=store)

        with patch("time.monotonic") as mock_time:
            mock_time.return_value = 100.0
            svc.cached_settings()
            store.load_settings.reset_mock()

            # Инвалидируем кэш
            svc.invalidate_cache()

            # Следующий call должен загрузить, даже если время не прошло
            svc.cached_settings()

            store.load_settings.assert_called_once()


class TestPresetChangedEvent(unittest.TestCase):
    """Проверяет, что handle_apply_profile_preset эмитит preset.changed через EventBus."""

    def _make_service(self):
        store = _make_store()
        svc = SettingsService(store=store)
        return svc, store

    def test_apply_profile_preset_emits_preset_changed_event(self):
        svc, _ = self._make_service()
        with patch("backend.event_bus.bus") as mock_bus:
            svc.handle_apply_profile_preset({"profile": "meeting"})
            mock_bus.emit.assert_called_once()
            event_name, payload = mock_bus.emit.call_args[0]
            self.assertEqual(event_name, "preset.changed")
            self.assertEqual(payload["profile"], "meeting")

    def test_apply_profile_preset_saves_active_preset_field(self):
        svc, store = self._make_service()
        with patch("backend.event_bus.bus"):
            svc.handle_apply_profile_preset({"profile": "translation"})
        saved_settings = store.save_settings.call_args[0][0]
        self.assertEqual(saved_settings.get("active_preset"), "translation")

    def test_apply_profile_preset_event_emit_failure_does_not_raise(self):
        svc, _ = self._make_service()
        with patch("backend.event_bus.bus") as mock_bus:
            mock_bus.emit.side_effect = RuntimeError("bus down")
            result = svc.handle_apply_profile_preset({"profile": "default"})
            self.assertIsNotNone(result)

    def test_apply_profile_preset_active_preset_for_all_profiles(self):
        for preset_id in ["default", "meeting", "translation", "call_recording"]:
            with self.subTest(preset=preset_id):
                svc, store = self._make_service()
                with patch("backend.event_bus.bus"):
                    svc.handle_apply_profile_preset({"profile": preset_id})
                saved_settings = store.save_settings.call_args[0][0]
                self.assertEqual(saved_settings.get("active_preset"), preset_id)


class TestBreadcrumbs(unittest.TestCase):
    """Проверяет, что Sentry breadcrumbs отправляются из SettingsService."""

    def _make_service(self) -> tuple[SettingsService, MagicMock]:
        store = _make_store()
        svc = SettingsService(store=store)
        return svc, store

    def test_set_settings_calls_add_breadcrumb(self):
        """handle_set_settings должен вызвать add_breadcrumb с category='settings'."""
        svc, _ = self._make_service()
        with patch("backend.settings_service.add_breadcrumb") as mock_bc:
            svc.handle_set_settings({"quality_profile": "max"})
        mock_bc.assert_called_once()
        call_kwargs = mock_bc.call_args
        args, kwargs = call_kwargs
        # может быть positional или keyword
        category = kwargs.get("category") or (args[0] if args else None)
        message = kwargs.get("message") or (args[1] if len(args) > 1 else None)
        data = kwargs.get("data") or (args[3] if len(args) > 3 else {})
        self.assertEqual(category, "settings")
        self.assertEqual(message, "set_settings")
        self.assertIn("quality_profile", data.get("keys", []))

    def test_set_settings_breadcrumb_no_secret_values(self):
        """handle_set_settings breadcrumb не должен содержать значений настроек, только ключи."""
        svc, _ = self._make_service()
        with patch("backend.settings_service.add_breadcrumb") as mock_bc:
            svc.handle_set_settings({"voice_gateway_api_key": "super_secret_token"})
        _, kwargs = mock_bc.call_args
        data = kwargs.get("data", {})
        # значение не должно попасть в data
        for v in data.values():
            self.assertNotIn("super_secret_token", str(v))

    def test_import_settings_calls_add_breadcrumb(self):
        """handle_import_settings должен вызвать add_breadcrumb с imported/skipped counts."""
        import json
        import tempfile

        svc, _ = self._make_service()
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump({"quality_profile": "max", "auto_paste": False}, fh)
            tmp_path = fh.name

        with patch("backend.settings_service.add_breadcrumb") as mock_bc:
            svc.handle_import_settings({"file": tmp_path})

        mock_bc.assert_called_once()
        _, kwargs = mock_bc.call_args
        self.assertEqual(kwargs.get("category"), "settings")
        self.assertEqual(kwargs.get("message"), "import_settings")
        data = kwargs.get("data", {})
        self.assertIn("imported", data)
        self.assertIn("skipped", data)
        self.assertIn("error_count", data)

    def test_apply_profile_preset_calls_add_breadcrumb(self):
        """handle_apply_profile_preset должен вызвать add_breadcrumb с именем профиля."""
        svc, _ = self._make_service()
        with patch("backend.event_bus.bus"):
            with patch("backend.settings_service.add_breadcrumb") as mock_bc:
                svc.handle_apply_profile_preset({"profile": "meeting"})
        mock_bc.assert_called_once()
        _, kwargs = mock_bc.call_args
        self.assertEqual(kwargs.get("category"), "settings")
        self.assertEqual(kwargs.get("message"), "apply_profile_preset")
        data = kwargs.get("data", {})
        self.assertEqual(data.get("profile"), "meeting")
        self.assertIn("keys_changed", data)

    def test_apply_profile_preset_breadcrumb_contains_changed_keys(self):
        """Breadcrumb для apply_profile_preset должен содержать список изменённых ключей."""
        svc, _ = self._make_service()
        with patch("backend.event_bus.bus"):
            with patch("backend.settings_service.add_breadcrumb") as mock_bc:
                svc.handle_apply_profile_preset({"profile": "translation"})
        _, kwargs = mock_bc.call_args
        data = kwargs.get("data", {})
        keys_changed = data.get("keys_changed", [])
        self.assertIsInstance(keys_changed, list)
        self.assertGreater(len(keys_changed), 0)


class TestExportSettingsRedactsAllSensitiveFields(unittest.TestCase):
    """W929 F4 — handle_export_settings must redact all 9 SENSITIVE_FIELDS, not just 4."""

    def test_export_settings_redacts_all_9_sensitive_fields(self):
        """All 9 fields from SENSITIVE_FIELDS (imported from settings_backup)
        must be absent from the exported file after the F4 fix."""
        import json
        import os
        import tempfile

        from backend.settings_backup import SENSITIVE_FIELDS

        # Build a store that contains all 9 sensitive fields + a safe key.
        all_9_secret_values = {f: f"secret_{f}" for f in SENSITIVE_FIELDS}
        base = _make_store()
        # Inject the 9 sensitive values into the mocked store's current dict.
        base._current.update(all_9_secret_values)
        base.load_settings.return_value = dict(base._current)

        svc = SettingsService(store=base)
        svc.invalidate_cache()  # ensure fresh load picks up updated mock

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            tmp_path = fh.name

        try:
            result = svc.handle_export_settings({"file": tmp_path})
            with open(tmp_path, encoding="utf-8") as f:
                exported = json.load(f)
        finally:
            os.unlink(tmp_path)

        # None of the 9 sensitive keys should appear in the export.
        leaked = [k for k in SENSITIVE_FIELDS if k in exported]
        self.assertEqual(
            leaked,
            [],
            f"Sensitive fields leaked into export: {leaked}",
        )
        # The count reported should not include the sensitive keys.
        self.assertEqual(result["settings_count"], len(exported))

    def test_sensitive_fields_set_covers_all_known_secrets(self):
        """Canonical SENSITIVE_FIELDS must redact every known credential key.

        The set legitimately grows as new secret-bearing settings are added
        (Wave 20 added llm_api_key / smtp_password / ipc_signing_secret on top
        of the original 9). Asserting a magic count is brittle and broke CI;
        instead pin the security invariant — every known secret key MUST be in
        the redaction set, and nothing may silently leave it.
        """
        from backend.settings_backup import SENSITIVE_FIELDS

        required_secrets = {
            "voice_gateway_api_key",
            "hf_token",
            "rest_api_key",
            "lm_studio_api_key",
            "telnyx_api_key",
            "twilio_account_sid",
            "twilio_auth_token",
            "sentry_dsn",
            "stt_gigaam_hf_token",
            "llm_api_key",
            "smtp_password",
            "ipc_signing_secret",
        }
        missing = required_secrets - SENSITIVE_FIELDS
        self.assertEqual(
            missing,
            set(),
            f"Secret keys dropped from SENSITIVE_FIELDS redaction set: {missing}",
        )


class TestSmtpHostSsrfGuardW1770(unittest.TestCase):
    """wave-1770 HIGH: smtp_host must reject link-local (cloud metadata) IPs.

    169.254.169.254 is the AWS/GCP/Azure instance-metadata endpoint. If smtp_host
    is set to this IP and recap email fires, smtplib opens a TCP connection to the
    metadata service, potentially leaking instance credentials.
    """

    def _svc(self) -> SettingsService:
        return SettingsService(store=_make_store())

    def test_link_local_ip_rejected(self) -> None:
        """169.254.169.254 must raise ValueError."""
        svc = self._svc()
        with self.assertRaises(ValueError):
            svc.handle_set_settings({"smtp_host": "169.254.169.254"})

    def test_link_local_any_rejected(self) -> None:
        """169.254.0.1 (any link-local) must raise ValueError."""
        svc = self._svc()
        with self.assertRaises(ValueError):
            svc.handle_set_settings({"smtp_host": "169.254.0.1"})

    def test_multicast_rejected(self) -> None:
        """224.0.0.1 (multicast) must raise ValueError."""
        svc = self._svc()
        with self.assertRaises(ValueError):
            svc.handle_set_settings({"smtp_host": "224.0.0.1"})

    def test_empty_smtp_host_allowed(self) -> None:
        """Empty smtp_host (SMTP disabled) passes validation without exception."""
        svc = self._svc()
        result = svc.handle_set_settings({"smtp_host": ""})
        self.assertEqual(result.get("smtp_host", ""), "")

    def test_localhost_allowed(self) -> None:
        """localhost (local relay) is allowed."""
        svc = self._svc()
        result = svc.handle_set_settings({"smtp_host": "localhost"})
        self.assertEqual(result.get("smtp_host"), "localhost")

    def test_loopback_ip_allowed(self) -> None:
        """127.0.0.1 (loopback — local Postfix relay) is allowed."""
        svc = self._svc()
        result = svc.handle_set_settings({"smtp_host": "127.0.0.1"})
        self.assertEqual(result.get("smtp_host"), "127.0.0.1")

    def test_external_hostname_allowed(self) -> None:
        """smtp.gmail.com (external hostname) is allowed."""
        svc = self._svc()
        result = svc.handle_set_settings({"smtp_host": "smtp.gmail.com"})
        self.assertEqual(result.get("smtp_host"), "smtp.gmail.com")

    def test_private_ip_allowed(self) -> None:
        """RFC1918 (10.x.x.x) is allowed — corporate mail relays commonly use these."""
        svc = self._svc()
        result = svc.handle_set_settings({"smtp_host": "10.0.0.1"})
        self.assertEqual(result.get("smtp_host"), "10.0.0.1")


if __name__ == "__main__":
    unittest.main()
