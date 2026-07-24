"""Живой баг (2026-07-24): Swift-агент открывает WS-соединение К VG НАПРЯМУЮ

(``ConversationViewController+WebSocket.swift`` — ``URLSession``, минуя Python-бэкенд
целиком), поэтому ему нужно РЕАЛЬНОЕ значение ``voice_gateway_api_key`` для заголовка
``Authorization: Bearer``. Единственный канал, которым Swift получает настройки — IPC
``get_settings`` — с wave-35 CRIT редактирует ВСЕ ``_SENSITIVE_FIELDS`` (включая этот
ключ) в литеральную строку ``"REDACTED"``. Итог: Swift ВСЕГДА шлёт VG заголовок
``Authorization: Bearer REDACTED`` → VG отвечает 403 на КАЖДОЙ попытке разговора,
независимо от того, насколько свежий и верный ключ реально лежит в settings.json.
Поймано живьём владельцем (double-tap Right Option → «голосовой шлюз недоступен»).

Редактирование в ``get_settings`` — правильное поведение для ВСЕХ прочих полей
(``_SENSITIVE_FIELDS``, wave-35 CRIT: сокет технически доступен любому процессу под
тем же UNIX-пользователем). Фикс — НЕ снятие редактирования с общего ``get_settings``
(это откатило бы защиту для всех клиентов), а отдельный узкоскоуповый internal-only
метод ``get_voice_gateway_credential``, возвращающий ТОЛЬКО ``voice_gateway_url`` +
``voice_gateway_api_key`` НЕредактированными — используется исключительно
Swift-стороной для построения WS-заголовка разговора, не расширяет отображение этого
секрета ни в UI, ни в бэкапах (``settings_backup.py`` продолжает редактировать поле
в файлах на диске — эта волна её не трогает).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from backend.settings_service import SettingsService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _make_store(voice_gateway_api_key: str = "real-secret-key-43-chars-aaaaaaaaaaaaaaaaaa") -> MagicMock:
    store = MagicMock()
    current = {
        "voice_gateway_url": "http://127.0.0.1:8090",
        "voice_gateway_api_key": voice_gateway_api_key,
    }
    store.load_settings.return_value = dict(current)
    return store


class GetSettingsStillRedactsTest(unittest.TestCase):
    """Регрессия-гард: общий get_settings НЕ должен раскрывать ключ (wave-35 CRIT не отменяется)."""

    def test_handle_get_settings_still_redacts_voice_gateway_api_key(self):
        svc = SettingsService(store=_make_store())
        result = svc.handle_get_settings({})
        self.assertEqual(result["voice_gateway_api_key"], "REDACTED")


class GetVoiceGatewayCredentialTest(unittest.TestCase):
    """RED до фикса: метод не существует. GREEN после: возвращает сырой ключ."""

    def test_returns_unredacted_key_and_url(self):
        svc = SettingsService(store=_make_store("real-secret-key-43-chars-aaaaaaaaaaaaaaaaaa"))
        result = svc.handle_get_voice_gateway_credential({})
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["voice_gateway_url"], "http://127.0.0.1:8090")
        self.assertEqual(result["voice_gateway_api_key"], "real-secret-key-43-chars-aaaaaaaaaaaaaaaaaa")

    def test_returns_only_the_two_scoped_fields(self):
        """Узкий скоуп: метод не должен превращаться в get_settings-дубликат."""
        svc = SettingsService(store=_make_store())
        result = svc.handle_get_voice_gateway_credential({})
        self.assertEqual(set(result.keys()), {"ok", "voice_gateway_url", "voice_gateway_api_key"})

    def test_empty_key_returns_empty_string_not_redacted_placeholder(self):
        svc = SettingsService(store=_make_store(voice_gateway_api_key=""))
        result = svc.handle_get_voice_gateway_credential({})
        self.assertEqual(result["voice_gateway_api_key"], "")


if __name__ == "__main__":
    unittest.main()
