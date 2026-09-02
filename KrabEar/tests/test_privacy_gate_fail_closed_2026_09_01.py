"""Privacy-гейты RecordingCoreService обязаны быть fail-CLOSED (2026-09-01).

ЧТО НАЙДЕНО
-----------
`_privacy_getter` починили fail-closed в W1768 (см.
`test_recording_core_privacy_getter_fail_closed_W1768.py`) — но три СОСЕДНИХ
гейта той же самой настройки `privacy_mode_enabled` остались на generic-обёртке

    def _get_runtime_setting(self, key, default):
        try:
            return self._settings_svc.cached_settings().get(key, default)
        except Exception:
            return default          # ← для privacy это fail-OPEN

Один из них стоит в ДЕСЯТИ строках от fail-closed сиблинга. Классическая
sibling-gate asymmetry: путь научен инцидентом, соседний — нет.

ПОЧЕМУ ПУТЬ ДОСТИЖИМ, А НЕ ТЕОРЕТИЧЕН
--------------------------------------
`SettingsService.cached_settings()` ловит ТОЛЬКО `StateStoreLockTimeout`;
всё прочее пробрасывается наверх. `StateStore._lock()` в этом же проекте
документирует `ENOSPC`/`EMFILE`/`EACCES` в фазе захвата (touch/open/flock) как
реалистичные. Такой `OSError` не является `StateStoreLockTimeout` → долетает
до generic-обёртки → privacy-гейт открывается.

ЧТО ТЕЧЁТ
---------
* `handle_get_recording_state` — `preview_text` уходит по IPC-поллингу.
  Собственный комментарий гейта: «wave-31 HIGH … the IPC poll path was leaking
  accumulated partial transcript text».
* `_replay_terminal_response` — отдаёт ПОЛНЫЙ кэшированный stop-ответ.
  Комментарий: «ни один replay-путь не должен выдать старый cleartext».

Третий сайт (старт RealtimePartialTranscriber) прикрыт вторым слоем —
fail-closed `_privacy_getter` подавит emit, — но открывать первый слой всё
равно нельзя: defense-in-depth не повод держать дырявый внешний гейт.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _make_service(settings_side_effect):
    from backend.recording_core_service import RecordingCoreService

    settings_svc = MagicMock()
    settings_svc.cached_settings.side_effect = settings_side_effect

    recorder = MagicMock()
    recorder.sample_rate = 16000
    recorder.is_recording = False

    return RecordingCoreService(
        recorder=recorder,
        transcriber=MagicMock(),
        translator=MagicMock(),
        store=MagicMock(),
        vocabulary=MagicMock(),
        settings_svc=settings_svc,
        llm_rewriter=MagicMock(),
        auto_glossary=MagicMock(),
        semantic_searcher=MagicMock(),
        context_memory=MagicMock(),
        clipboard_history=[],
        auto_backup=MagicMock(),
        session_tracker=MagicMock(),
        action_items_extractor=MagicMock(),
        transcription_counter_ref=[0],
        last_stt_engine_ref=[None],
    )


def _raises_oserror():
    # Именно OSError, а не StateStoreLockTimeout: cached_settings() ловит
    # только второй, а первый документирован как реалистичный в _lock().
    raise OSError(24, "Too many open files")


class PrivacyGateFailsClosedTests(unittest.TestCase):
    """Неизвестное состояние приватности обязано читаться как «privacy ON»."""

    def test_privacy_helper_returns_true_when_settings_raise(self) -> None:
        """Главный регресс: сбой чтения настроек ⇒ гейт закрыт."""
        svc = _make_service(_raises_oserror)
        self.assertTrue(
            svc._privacy_mode_enabled(),
            "сбой cached_settings() обязан читаться как privacy ON (fail-closed)",
        )

    def test_privacy_helper_returns_real_flag_on_normal_path(self) -> None:
        """Нормальный путь не искажается: отдаётся реальный флаг."""
        svc = _make_service(lambda *a, **k: {"privacy_mode_enabled": False})
        self.assertFalse(svc._privacy_mode_enabled())

        svc_on = _make_service(lambda *a, **k: {"privacy_mode_enabled": True})
        self.assertTrue(svc_on._privacy_mode_enabled())

    def test_missing_key_is_treated_as_privacy_off(self) -> None:
        """Отсутствие ключа ≠ сбой: настройки прочитаны, режим просто выключен.

        🔴 Разделение важно: иначе fail-closed выродится в «privacy всегда ON»
        и фича молча перестанет работать на чистом конфиге.
        """
        svc = _make_service(lambda *a, **k: {})
        self.assertFalse(svc._privacy_mode_enabled())

    def test_recording_state_hides_preview_text_when_settings_raise(self) -> None:
        """Утечка №1: preview_text по IPC-поллингу при неизвестной приватности."""
        svc = _make_service(_raises_oserror)
        svc._preview_text = "секретный транскрипт владельца"
        res = svc.handle_get_recording_state({})
        self.assertEqual(
            res.get("preview_text", ""), "",
            "preview_text обязан быть пуст, пока состояние приватности неизвестно",
        )

    def test_terminal_replay_suppressed_when_settings_raise(self) -> None:
        """Утечка №2: replay полного stop-ответа с cleartext."""
        svc = _make_service(_raises_oserror)
        self.assertIsNone(
            svc._replay_terminal_response("любой-токен"),
            "replay обязан молчать, пока состояние приватности неизвестно",
        )


if __name__ == "__main__":
    unittest.main()
