"""Тесты IPC-методов шифрования истории.

Покрывает:
  - get_encryption_status: форма ответа + поведение с keychain доступным/недоступным
  - set_history_encryption: round-trip включения/выключения,
    отказ при недоступном Keychain, «не трогает настройки» при ошибке Keychain
  - Записи в dispatch table существуют

🔴 Правила:
  - Никакого реального Keychain: ВСЕГДА патчим build_history_crypto / keychain_available
    / _run_security.
  - Нет assert import mlx_whisper — MLX-masking trap.
  - service.close() в tearDown — daemon-thread teardown (#1782).
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Лёгкие стаб-заглушки — без реального BackendService (нет MLX / аудио).
# ---------------------------------------------------------------------------


def _make_fake_settings_svc(initial_settings: dict | None = None):
    """Создаёт мок SettingsService с нужным поведением."""
    settings = dict(initial_settings or {})
    svc = MagicMock()
    svc.cached_settings.return_value = settings

    def set_settings_side_effect(patch_dict):
        settings.update(patch_dict)
        return {"ok": True, "settings": settings}

    svc.handle_set_settings.side_effect = set_settings_side_effect
    return svc, settings


class TestGetEncryptionStatus(unittest.TestCase):
    """Тесты get_encryption_status (без BackendService — inline unit)."""

    def _call_handler(self, enabled: bool, available: bool):
        """Вызывает _handle_get_encryption_status изолированно."""
        from backend.service import BackendService  # noqa: F401 — just to locate

        # Создаём минимальный объект, имитирующий нужную часть BackendService.
        obj = object.__new__(BackendService)
        settings_svc, _ = _make_fake_settings_svc(
            {"history_encryption_enabled": enabled}
        )
        object.__setattr__(obj, "_settings_svc", settings_svc)

        with patch(
            "backend.crypto_keystore.keychain_available",
            return_value=available,
        ):
            return obj._handle_get_encryption_status({})

    def test_shape_enabled_available(self):
        result = self._call_handler(enabled=True, available=True)
        self.assertTrue(result["ok"])
        self.assertIs(result["enabled"], True)
        self.assertIs(result["available"], True)

    def test_shape_disabled_unavailable(self):
        result = self._call_handler(enabled=False, available=False)
        self.assertTrue(result["ok"])
        self.assertIs(result["enabled"], False)
        self.assertIs(result["available"], False)

    def test_enabled_false_available_true(self):
        result = self._call_handler(enabled=False, available=True)
        self.assertTrue(result["ok"])
        self.assertIs(result["enabled"], False)
        self.assertIs(result["available"], True)

    def test_missing_setting_defaults_to_false(self):
        """Когда history_encryption_enabled отсутствует — должно вернуть False."""
        from backend.service import BackendService

        obj = object.__new__(BackendService)
        settings_svc, _ = _make_fake_settings_svc({})
        object.__setattr__(obj, "_settings_svc", settings_svc)

        with patch("backend.crypto_keystore.keychain_available", return_value=True):
            result = obj._handle_get_encryption_status({})
        self.assertIs(result["enabled"], False)


class TestSetHistoryEncryption(unittest.TestCase):
    """Тесты set_history_encryption (без BackendService — inline unit)."""

    def _make_obj(self, initial_settings: dict | None = None):
        from backend.service import BackendService

        obj = object.__new__(BackendService)
        svc, settings = _make_fake_settings_svc(initial_settings or {})
        object.__setattr__(obj, "_settings_svc", svc)
        return obj, settings

    def test_enable_when_keychain_available(self):
        """Включение шифрования при доступном Keychain → ok:True, enabled:True."""
        from backend.history_crypto import HistoryCrypto

        obj, settings = self._make_obj({"history_encryption_enabled": False})
        fake_crypto = HistoryCrypto(b"A" * 32)  # валидный экземпляр

        with patch("backend.history_crypto.build_history_crypto", return_value=fake_crypto):
            result = obj._handle_set_history_encryption({"enabled": True})

        self.assertTrue(result["ok"])
        self.assertIs(result["enabled"], True)
        self.assertIs(result["available"], True)
        # Настройка реально обновлена
        self.assertTrue(settings.get("history_encryption_enabled"))

    def test_disable_without_keychain_check(self):
        """Выключение шифрования — Keychain НЕ проверяется, всегда успех."""
        obj, settings = self._make_obj({"history_encryption_enabled": True})

        # build_history_crypto вообще не должен вызываться при disabled=False
        with patch(
            "backend.history_crypto.build_history_crypto", side_effect=AssertionError("не должен вызываться")
        ):
            result = obj._handle_set_history_encryption({"enabled": False})

        self.assertTrue(result["ok"])
        self.assertIs(result["enabled"], False)
        self.assertFalse(settings.get("history_encryption_enabled"))

    def test_enable_keychain_unavailable_returns_error(self):
        """При keychain_unavailable (build_history_crypto→None) — ok:False, настройка не меняется."""
        obj, settings = self._make_obj({"history_encryption_enabled": False})

        with patch("backend.history_crypto.build_history_crypto", return_value=None):
            result = obj._handle_set_history_encryption({"enabled": True})

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "keychain_unavailable")
        # Текущее значение настройки возвращается
        self.assertIn("enabled", result)
        # Настройка НЕ изменилась
        self.assertFalse(settings.get("history_encryption_enabled"))

    def test_round_trip_enable_then_disable(self):
        """Включить → выключить → статус False."""
        from backend.history_crypto import HistoryCrypto

        obj, settings = self._make_obj({"history_encryption_enabled": False})
        fake_crypto = HistoryCrypto(b"B" * 32)

        with patch("backend.history_crypto.build_history_crypto", return_value=fake_crypto):
            r1 = obj._handle_set_history_encryption({"enabled": True})
        self.assertTrue(r1["ok"])
        self.assertTrue(settings.get("history_encryption_enabled"))

        with patch(
            "backend.history_crypto.build_history_crypto",
            side_effect=AssertionError("не должен"),
        ):
            r2 = obj._handle_set_history_encryption({"enabled": False})
        self.assertTrue(r2["ok"])
        self.assertFalse(settings.get("history_encryption_enabled"))


class TestEncryptionDispatchTable(unittest.TestCase):
    """Проверяет, что методы зарегистрированы в dispatch table BackendService.

    Читаем исходник service.py как текст — не требует инициализации
    (нет MLX / аудио / StateStore).
    """

    def _get_dispatch_source(self) -> str:
        """Читает исходник _build_dispatch_table прямо из файла."""
        import pathlib
        service_py = pathlib.Path(PROJECT_ROOT) / "backend" / "service.py"
        return service_py.read_text(encoding="utf-8")

    def test_set_history_encryption_in_source(self):
        src = self._get_dispatch_source()
        self.assertIn('"set_history_encryption"', src)

    def test_get_encryption_status_in_source(self):
        src = self._get_dispatch_source()
        self.assertIn('"get_encryption_status"', src)


class TestKeychainAvailable(unittest.TestCase):
    """Тесты keychain_available() из crypto_keystore."""

    def test_available_on_darwin_with_security(self):
        import shutil
        with patch("sys.platform", "darwin"), \
             patch.object(shutil, "which", return_value="/usr/bin/security"):
            from backend.crypto_keystore import keychain_available
            self.assertTrue(keychain_available())

    def test_unavailable_on_linux(self):
        import shutil
        with patch("sys.platform", "linux"), \
             patch.object(shutil, "which", return_value="/usr/bin/security"):
            from backend.crypto_keystore import keychain_available
            self.assertFalse(keychain_available())

    def test_unavailable_when_no_security_binary(self):
        import shutil
        with patch("sys.platform", "darwin"), \
             patch.object(shutil, "which", return_value=None):
            from backend.crypto_keystore import keychain_available
            self.assertFalse(keychain_available())

    def test_no_key_created(self):
        """keychain_available НЕ должна вызывать security CLI."""
        from backend import crypto_keystore
        with patch.object(crypto_keystore, "_run_security") as mock_run:
            with patch("sys.platform", "darwin"):
                import shutil
                with patch.object(shutil, "which", return_value="/usr/bin/security"):
                    crypto_keystore.keychain_available()
            mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
