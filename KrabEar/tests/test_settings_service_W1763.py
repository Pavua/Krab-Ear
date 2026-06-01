"""W1763 — два исправления безопасности в SettingsService.

MED 1 — voice_gateway_url: точная проверка хоста через urlparse вместо startswith.
  - http://localhost.evil.com/ должен быть ОТКЛОНЁН (sibling-prefix bypass).
  - http://localhost:8090/ должен быть ПРИНЯТ.
  - http://127.0.0.1:8090/ должен быть ПРИНЯТ.
  - http://::1:8090/ должен быть ПРИНЯТ.
  - https://gateway.example.com/ должен быть ПРИНЯТ.
  - http://127.0.0.1.evil.com/ должен быть ОТКЛОНЁН.

MED 2 — privacy_mode kill-switch через _maybe_disable_sentry_for_privacy.
  - set_settings(privacy_mode_enabled=True) → kill-switch срабатывает.
  - import_settings с privacy_mode_enabled=True → kill-switch срабатывает.
  - restore_settings_backup с privacy_mode_enabled=True → kill-switch срабатывает.
  - повторное включение уже включённого privacy_mode → kill-switch НЕ срабатывает (идемпотентность).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.settings_service import SettingsService  # noqa: E402


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------

_BASE_SETTINGS: dict = {
    "quality_profile": "balanced",
    "cleanup_profile": "soft",
    "translation_mode": "off",
    "auto_paste": True,
    "translate_and_paste": False,
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
    "stt_hotwords": [],
    "stt_hotwords_enabled": True,
    "onboarding_completed": False,
    "privacy_mode_enabled": False,
}


def _make_store(extra: dict | None = None) -> MagicMock:
    """Фиктивный store с in-memory сохранением настроек."""
    store = MagicMock()
    current: dict = dict(_BASE_SETTINGS)
    if extra:
        current.update(extra)

    store.load_settings.return_value = dict(current)

    def _save(s: dict) -> dict:
        current.clear()
        current.update(s)
        store.load_settings.return_value = dict(current)
        return dict(s)

    store.save_settings.side_effect = _save
    return store


def _make_backup(restore_data: dict | None = None) -> MagicMock:
    """Фиктивный SettingsBackup, возвращающий restore_data из restore_backup()."""
    backup = MagicMock()
    backup.restore_backup.return_value = dict(restore_data or _BASE_SETTINGS)
    backup.create_backup.return_value = "backup-001"
    return backup


# ---------------------------------------------------------------------------
# MED 1 — voice_gateway_url exact hostname check
# ---------------------------------------------------------------------------

class TestVoiceGatewayUrlHostCheck(unittest.TestCase):
    """W1763 MED 1 — urlparse-based точная проверка хоста для voice_gateway_url."""

    def _svc(self) -> SettingsService:
        return SettingsService(store=_make_store())

    # ---- Легитимные URL (должны проходить) ----

    def test_localhost_url_accepted(self):
        """http://localhost:8090/ должен быть принят."""
        svc = self._svc()
        # Не должно бросать исключение
        svc.handle_set_settings({"voice_gateway_url": "http://localhost:8090"})

    def test_localhost_no_port_accepted(self):
        """http://localhost/ без порта должен быть принят."""
        svc = self._svc()
        svc.handle_set_settings({"voice_gateway_url": "http://localhost"})

    def test_127_0_0_1_url_accepted(self):
        """http://127.0.0.1:8090/ должен быть принят."""
        svc = self._svc()
        svc.handle_set_settings({"voice_gateway_url": "http://127.0.0.1:8090"})

    def test_https_remote_url_accepted(self):
        """https://gateway.example.com/ должен быть принят (HTTPS)."""
        svc = self._svc()
        svc.handle_set_settings({"voice_gateway_url": "https://gateway.example.com/api"})

    def test_https_any_host_accepted(self):
        """Любой HTTPS URL должен проходить валидацию."""
        svc = self._svc()
        svc.handle_set_settings({"voice_gateway_url": "https://evil.example.com"})

    # ---- Атакующие URL (должны ОТКЛОНЯТЬСЯ) ----

    def test_localhost_evil_sibling_prefix_rejected(self):
        """http://localhost.evil.com/ ДОЛЖЕН быть отклонён — sibling-prefix bypass.

        Старый код startswith("http://localhost") пропускал этот URL.
        Новый код urlparse().hostname проверяет точный хост.
        """
        svc = self._svc()
        with self.assertRaises(ValueError) as ctx:
            svc.handle_set_settings({"voice_gateway_url": "http://localhost.evil.com/"})
        self.assertIn("Voice Gateway URL", str(ctx.exception))

    def test_127_0_0_1_evil_sibling_prefix_rejected(self):
        """http://127.0.0.1.evil.com/ ДОЛЖЕН быть отклонён — sibling-prefix bypass."""
        svc = self._svc()
        with self.assertRaises(ValueError) as ctx:
            svc.handle_set_settings({"voice_gateway_url": "http://127.0.0.1.evil.com/"})
        self.assertIn("Voice Gateway URL", str(ctx.exception))

    def test_http_remote_non_localhost_rejected(self):
        """http://example.com/ должен быть отклонён (HTTP + не localhost)."""
        svc = self._svc()
        with self.assertRaises(ValueError):
            svc.handle_set_settings({"voice_gateway_url": "http://example.com"})

    def test_ftp_localhost_rejected(self):
        """ftp://localhost/ должен быть отклонён (неверная схема)."""
        svc = self._svc()
        with self.assertRaises(ValueError):
            svc.handle_set_settings({"voice_gateway_url": "ftp://localhost/path"})

    def test_localhost_path_traversal_rejected(self):
        """http://localhost@evil.com/ должен быть отклонён (userinfo bypass)."""
        svc = self._svc()
        with self.assertRaises(ValueError):
            svc.handle_set_settings({"voice_gateway_url": "http://localhost@evil.com"})


# ---------------------------------------------------------------------------
# MED 2 — privacy_mode kill-switch centralized in _maybe_disable_sentry_for_privacy
# ---------------------------------------------------------------------------

class TestPrivacyModeKillSwitchHelper(unittest.TestCase):
    """W1763 MED 2 — _maybe_disable_sentry_for_privacy вызывается из всех путей мутации."""

    def _svc(self, initial_privacy: bool = False) -> SettingsService:
        extra = {"privacy_mode_enabled": initial_privacy}
        return SettingsService(store=_make_store(extra=extra))

    # ---- Проверка что helper вызывается из каждого пути ----

    def test_set_settings_calls_privacy_kill_switch(self):
        """handle_set_settings(privacy_mode_enabled=True) должен вызвать kill-switch."""
        svc = self._svc(initial_privacy=False)
        with patch.object(svc, "_maybe_disable_sentry_for_privacy") as mock_kill:
            with patch("core.config.reload_settings_from_json", return_value=0):
                svc.handle_set_settings({"privacy_mode_enabled": True})
        mock_kill.assert_called_once()

    def test_import_settings_calls_privacy_kill_switch(self):
        """handle_import_settings с privacy_mode_enabled=True должен вызвать kill-switch."""
        svc = self._svc(initial_privacy=False)

        import_data = {"privacy_mode_enabled": True}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump(import_data, fh)
            tmp_path = fh.name

        try:
            with patch.object(svc, "_maybe_disable_sentry_for_privacy") as mock_kill:
                svc.handle_import_settings({"file": tmp_path})
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        mock_kill.assert_called_once()

    def test_restore_backup_calls_privacy_kill_switch(self):
        """handle_restore_settings_backup с privacy_mode_enabled=True должен вызвать kill-switch."""
        restore_data = dict(_BASE_SETTINGS)
        restore_data["privacy_mode_enabled"] = True
        backup = _make_backup(restore_data=restore_data)
        svc = SettingsService(store=_make_store(), backup=backup)

        with patch.object(svc, "_maybe_disable_sentry_for_privacy") as mock_kill:
            svc.handle_restore_settings_backup({"backup_id": "backup-001"})

        mock_kill.assert_called_once()

    # ---- Проверка реального side-effect: Sentry отключается ----

    def test_import_settings_with_privacy_true_disables_sentry(self):
        """import_settings с privacy_mode_enabled=True отключает Sentry SDK.

        Это ключевой тест MED 2: путь import_settings не вызывал kill-switch до исправления,
        поэтому при включении privacy через импорт Sentry продолжал захватывать данные.
        """
        svc = self._svc(initial_privacy=False)

        import_data = {"privacy_mode_enabled": True}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump(import_data, fh)
            tmp_path = fh.name

        # Симулируем инициализированный Sentry
        import backend.observability as _obs
        original_initialized = _obs._sentry_initialized
        _obs._sentry_initialized = True

        try:
            with patch("sentry_sdk.flush") as mock_flush:
                with patch("sentry_sdk.init") as mock_init:
                    svc.handle_import_settings({"file": tmp_path})

            # kill-switch должен был вызвать flush + init(dsn=None)
            mock_flush.assert_called_once()
            mock_init.assert_called_once_with(dsn=None)
            # _sentry_initialized должен быть сброшен
            self.assertFalse(_obs._sentry_initialized)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
            _obs._sentry_initialized = original_initialized

    def test_restore_backup_with_privacy_true_disables_sentry(self):
        """handle_restore_settings_backup с privacy_mode_enabled=True отключает Sentry SDK.

        Это ключевой тест MED 2: путь restore до исправления также не вызывал kill-switch.
        """
        restore_data = dict(_BASE_SETTINGS)
        restore_data["privacy_mode_enabled"] = True
        backup = _make_backup(restore_data=restore_data)
        svc = SettingsService(store=_make_store(), backup=backup)

        import backend.observability as _obs
        original_initialized = _obs._sentry_initialized
        _obs._sentry_initialized = True

        try:
            with patch("sentry_sdk.flush") as mock_flush:
                with patch("sentry_sdk.init") as mock_init:
                    svc.handle_restore_settings_backup({"backup_id": "backup-001"})

            mock_flush.assert_called_once()
            mock_init.assert_called_once_with(dsn=None)
            self.assertFalse(_obs._sentry_initialized)
        finally:
            _obs._sentry_initialized = original_initialized

    # ---- Идемпотентность: повторное включение уже включённого privacy ----

    def test_privacy_already_enabled_no_double_kill(self):
        """Если privacy_mode_enabled уже True, kill-switch НЕ должен срабатывать снова."""
        svc = self._svc(initial_privacy=True)  # Уже True

        import backend.observability as _obs
        original_initialized = _obs._sentry_initialized
        _obs._sentry_initialized = False  # Sentry уже выключен

        try:
            with patch("sentry_sdk.flush") as mock_flush:
                with patch("sentry_sdk.init") as mock_init:
                    with patch("core.config.reload_settings_from_json", return_value=0):
                        svc.handle_set_settings({"privacy_mode_enabled": True})

            # Sentry уже был выключен, не должен дёргаться снова
            mock_flush.assert_not_called()
            mock_init.assert_not_called()
        finally:
            _obs._sentry_initialized = original_initialized

    # ---- set_settings оригинальный путь тоже работает ----

    def test_set_settings_privacy_true_disables_sentry(self):
        """handle_set_settings(privacy_mode_enabled=True) — оригинальный путь всё ещё работает."""
        svc = self._svc(initial_privacy=False)

        import backend.observability as _obs
        original_initialized = _obs._sentry_initialized
        _obs._sentry_initialized = True

        try:
            with patch("sentry_sdk.flush") as mock_flush:
                with patch("sentry_sdk.init") as mock_init:
                    with patch("core.config.reload_settings_from_json", return_value=0):
                        svc.handle_set_settings({"privacy_mode_enabled": True})

            mock_flush.assert_called_once()
            mock_init.assert_called_once_with(dsn=None)
            self.assertFalse(_obs._sentry_initialized)
        finally:
            _obs._sentry_initialized = original_initialized


if __name__ == "__main__":
    unittest.main()
