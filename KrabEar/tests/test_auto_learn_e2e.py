"""E2E regression-тест closed-loop STT auto-learn (Cmd+Shift+R QuickReplace → stt_hotwords).

Существующий флоу (native/KrabEarAgent/Sources/KrabEarAgent/main+QuickReplace.swift ->
IPC replace_word_in_last_transcript -> backend/llm_ops_service.py) уже работает, но
раньше был покрыт только тестами, которые мокают либо `_maybe_auto_learn_word`
целиком, либо хранилище hotwords (см. test_auto_learn_corrections.py — там
`_FakeSettingsService` — стаб в памяти, не реальный SettingsService/StateStore).

Этот файл доказывает РЕАЛЬНУЮ цепочку без единого мока:
  StateStore (реальный, temp data_dir)
    -> SettingsService (реальный, читает/пишет settings.json на диске)
    -> LLMOpsService.handle_replace_word_in_last_transcript (реальный, вызывает
       _maybe_auto_learn_word без моков)
    -> STTManagementService.handle_list_stt_hotwords (реальный метод ЧТЕНИЯ
       словаря — тот же путь, которым GUI показывает «Словарь STT»)

Если wiring между "заменил слово" и "слово реально появилось в STT-словаре"
сломается (например переименуют ключ настройки, поменяют сигнатуру
handle_set_settings, или STTManagementService начнёт читать словарь из
другого места) — эти тесты упадут, а изолированные unit-тесты на
`_FakeSettingsService` — нет.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.llm_ops_service import LLMOpsService
from backend.settings_service import SettingsService
from backend.state_store import StateStore
from backend.stt_management_service import STTManagementService


class AutoLearnRealChainEnabledTestCase(unittest.TestCase):
    """auto_learn_corrections_enabled=True -> new_word реально доступен через
    РЕАЛЬНЫЙ метод чтения STT-словаря (handle_list_stt_hotwords), не через мок."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.settings_svc = SettingsService(store=self.store)
        self.llm_ops_svc = LLMOpsService(
            store=self.store, settings_svc=self.settings_svc, transcriber=None
        )
        self.stt_mgmt_svc = STTManagementService(
            settings_svc=self.settings_svc, transcriber=None
        )
        self.store.add_history_item(text="Запись содержит слово кот здесь", paste_status="ok")
        self.settings_svc.handle_set_settings({"auto_learn_corrections_enabled": True})

    def test_replaced_word_reaches_real_stt_vocabulary(self) -> None:
        """Замена "кот"->"код" реально добавляет "код" в stt_hotwords (проверено
        через handle_list_stt_hotwords — независимый от записи путь чтения)."""
        result = self.llm_ops_svc.handle_replace_word_in_last_transcript(
            {"old_word": "кот", "new_word": "код"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["replaced_count"], 1)
        self.assertIn("код", result["new_text"])

        hotwords_result = self.stt_mgmt_svc.handle_list_stt_hotwords({})
        self.assertTrue(hotwords_result["enabled"])
        self.assertIn("код", hotwords_result["hotwords"])

    def test_learned_word_survives_fresh_service_instances(self) -> None:
        """Слово переживает "рестарт бэкенда" — новый SettingsService/STTManagementService,
        читающие ТУ ЖЕ persisted settings.json, видят выученное слово."""
        result = self.llm_ops_svc.handle_replace_word_in_last_transcript(
            {"old_word": "кот", "new_word": "код"}
        )
        self.assertTrue(result["ok"])

        fresh_settings_svc = SettingsService(store=self.store)
        fresh_stt_mgmt_svc = STTManagementService(
            settings_svc=fresh_settings_svc, transcriber=None
        )
        hotwords_result = fresh_stt_mgmt_svc.handle_list_stt_hotwords({})
        self.assertIn("код", hotwords_result["hotwords"])

    def test_response_reports_auto_learned_true(self) -> None:
        """handle_replace_word_in_last_transcript возвращает auto_learned=True когда
        слово реально было добавлено в словарь (используется Swift-стороной для
        явного feedback пользователю)."""
        result = self.llm_ops_svc.handle_replace_word_in_last_transcript(
            {"old_word": "кот", "new_word": "код"}
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result.get("auto_learned"))


class AutoLearnRealChainDisabledTestCase(unittest.TestCase):
    """auto_learn_corrections_enabled=False (default, opt-in) -> new_word НЕ
    попадает в реальный STT-словарь после успешной замены."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.settings_svc = SettingsService(store=self.store)
        self.llm_ops_svc = LLMOpsService(
            store=self.store, settings_svc=self.settings_svc, transcriber=None
        )
        self.stt_mgmt_svc = STTManagementService(
            settings_svc=self.settings_svc, transcriber=None
        )
        self.store.add_history_item(text="Запись содержит слово кот здесь", paste_status="ok")
        # Явно фиксируем default=False для читаемости теста (не полагаемся молча
        # на DEFAULT_SETTINGS, чтобы тест не сломался тихо, если дефолт поменяют).
        self.settings_svc.handle_set_settings({"auto_learn_corrections_enabled": False})

    def test_replaced_word_does_not_reach_stt_vocabulary(self) -> None:
        result = self.llm_ops_svc.handle_replace_word_in_last_transcript(
            {"old_word": "кот", "new_word": "код"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["replaced_count"], 1)

        hotwords_result = self.stt_mgmt_svc.handle_list_stt_hotwords({})
        self.assertNotIn("код", hotwords_result["hotwords"])

    def test_response_reports_auto_learned_false(self) -> None:
        """Когда фича выключена, response должен явно сообщать auto_learned=False —
        Swift-сторона не должна показывать "слово выучено", если это неправда."""
        result = self.llm_ops_svc.handle_replace_word_in_last_transcript(
            {"old_word": "кот", "new_word": "код"}
        )
        self.assertTrue(result["ok"])
        self.assertIn("auto_learned", result)
        self.assertIs(result["auto_learned"], False)


if __name__ == "__main__":
    unittest.main()
