"""add/remove_stt_hotword берут итог из того, что РЕАЛЬНО сохранил set_settings.

Раньше хендлеры игнорировали результат `handle_set_settings` и отвечали локально
собранным списком: если валидатор/нормализация изменили или выбросили слово, клиент
получал «добавлено», хотя в настройках слова нет (follow-up гейта #2229).
Реальный set_settings возвращает словарь настроек, включая `stt_hotwords`.
"""

from __future__ import annotations

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KRAB_EAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRAB_EAR_ROOT not in sys.path:
    sys.path.insert(0, KRAB_EAR_ROOT)

from backend.stt_management_service import STTManagementService


class _Settings:
    """set_settings с настраиваемым «сохранённым» результатом."""

    def __init__(self, current, saved):
        self._current = current
        self._saved = saved  # что вернёт set_settings в ключе stt_hotwords (None — ключа нет)
        self.calls: list[dict] = []

    def cached_settings(self) -> dict:
        return {"stt_hotwords": list(self._current)}

    def handle_set_settings(self, params: dict) -> dict:
        self.calls.append(dict(params))
        result: dict = {"stt_hotwords_enabled": True}
        if self._saved is not None:
            result["stt_hotwords"] = list(self._saved)
        return result


def _svc(settings: _Settings) -> STTManagementService:
    return STTManagementService(settings_svc=settings, transcriber=None)


class HotwordPersistedTruthTests(unittest.TestCase):
    def test_add_returns_list_actually_saved(self) -> None:
        # нормализация при сохранении поменяла список (дедуп/порядок) — клиенту нужен он
        s = _Settings(["один"], saved=["Краб", "один", "второй"])
        res = _svc(s).handle_add_stt_hotword({"word": "Краб"})
        self.assertEqual(res["hotwords"], ["Краб", "один", "второй"])

    def test_add_raises_when_word_not_in_saved_list(self) -> None:
        s = _Settings(["один"], saved=["один"])  # слово потеряно при сохранении
        with self.assertRaises(RuntimeError):
            _svc(s).handle_add_stt_hotword({"word": "Краб"})

    def test_remove_raises_when_word_still_in_saved_list(self) -> None:
        s = _Settings(["один", "Краб"], saved=["один", "Краб"])  # удаление не применилось
        with self.assertRaises(RuntimeError):
            _svc(s).handle_remove_stt_hotword({"word": "Краб"})

    def test_remove_returns_saved_list(self) -> None:
        s = _Settings(["один", "Краб"], saved=["второй"])
        res = _svc(s).handle_remove_stt_hotword({"word": "Краб"})
        self.assertEqual(res["hotwords"], ["второй"])

    def test_result_without_hotwords_key_keeps_local_view(self) -> None:
        # заглушки/старые реализации без ключа: прежнее поведение (нет данных для сверки)
        s = _Settings(["один"], saved=None)
        res = _svc(s).handle_add_stt_hotword({"word": "Краб"})
        self.assertIn("Краб", res["hotwords"])


if __name__ == "__main__":
    unittest.main()
