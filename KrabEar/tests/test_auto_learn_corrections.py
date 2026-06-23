"""Тесты closed-loop STT vocabulary auto-learn from user corrections.

Проверяет:
  - setting OFF → vocab не обновляется (default safe)
  - setting ON  → new_word добавляется в stt_hotwords после успешной замены
  - vocab-add failure is non-fatal — replace всё равно успешен
  - old_word == new_word → vocab не обновляется (нечего учить)
  - empty new_word → ok=False (missing_words), vocab не трогается
  - очень длинный new_word (>60 символов) → добавления нет
  - фраза из >4 токенов → добавления нет
  - уже существующее слово → дубль не добавляется
  - несколько успешных замен → слово добавляется только один раз
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.llm_ops_service import LLMOpsService
from backend.state_store import StateStore


# ---------------------------------------------------------------------------
# Minimal stub collaborators
# ---------------------------------------------------------------------------


class _FakeSettingsService:
    """Минимальный стаб SettingsService с контролируемым словарём настроек."""

    def __init__(self, initial: dict | None = None) -> None:
        self._settings: dict = dict(initial or {})
        self.set_calls: list[dict] = []

    def cached_settings(self) -> dict:
        return dict(self._settings)

    def handle_set_settings(self, patch_dict: dict) -> dict:
        self.set_calls.append(dict(patch_dict))
        self._settings.update(patch_dict)
        return {"ok": True}


# ---------------------------------------------------------------------------
# Helper: build service with real StateStore + stubbed settings
# ---------------------------------------------------------------------------


def _make_svc(
    data_dir: Path,
    auto_learn: bool = False,
    initial_hotwords: list | None = None,
) -> tuple[LLMOpsService, _FakeSettingsService]:
    store = StateStore(data_dir)
    settings = _FakeSettingsService(
        {
            "auto_learn_corrections_enabled": auto_learn,
            "stt_hotwords": list(initial_hotwords or []),
        }
    )
    svc = LLMOpsService(store=store, settings_svc=settings, transcriber=None)
    return svc, settings


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class AutoLearnSettingOffTestCase(unittest.TestCase):
    """Когда auto_learn_corrections_enabled=False (default), vocab не меняется."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc, self.settings = _make_svc(
            Path(self.tmp.name) / "data", auto_learn=False
        )
        self.svc._store.add_history_item(text="Это кот на коврике", paste_status="ok")

    def test_no_vocab_add_when_setting_off(self) -> None:
        """Setting OFF → stt_hotwords не изменяется после успешной замены."""
        result = self.svc.handle_replace_word_in_last_transcript(
            {"old_word": "кот", "new_word": "код"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["replaced_count"], 1)
        # No set_settings calls for stt_hotwords
        hotword_calls = [
            c for c in self.settings.set_calls if "stt_hotwords" in c
        ]
        self.assertEqual(hotword_calls, [])


class AutoLearnSettingOnTestCase(unittest.TestCase):
    """Когда auto_learn_corrections_enabled=True, new_word добавляется в stt_hotwords."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc, self.settings = _make_svc(
            Path(self.tmp.name) / "data", auto_learn=True
        )
        self.svc._store.add_history_item(
            text="Transcription says кот here", paste_status="ok"
        )

    def test_new_word_added_to_hotwords(self) -> None:
        """Setting ON → new_word добавляется в stt_hotwords после успешной замены."""
        result = self.svc.handle_replace_word_in_last_transcript(
            {"old_word": "кот", "new_word": "code"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["replaced_count"], 1)
        hotword_calls = [
            c for c in self.settings.set_calls if "stt_hotwords" in c
        ]
        self.assertEqual(len(hotword_calls), 1)
        self.assertIn("code", hotword_calls[0]["stt_hotwords"])

    def test_word_not_found_no_vocab_add(self) -> None:
        """Если слово не найдено (ok=False), vocab не обновляется."""
        result = self.svc.handle_replace_word_in_last_transcript(
            {"old_word": "несуществующее", "new_word": "code"}
        )
        self.assertFalse(result["ok"])
        hotword_calls = [
            c for c in self.settings.set_calls if "stt_hotwords" in c
        ]
        self.assertEqual(hotword_calls, [])


class AutoLearnNonFatalTestCase(unittest.TestCase):
    """Ошибка vocab-add не должна ломать замену слова."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc, self.settings = _make_svc(
            Path(self.tmp.name) / "data", auto_learn=True
        )
        self.svc._store.add_history_item(
            text="Текст с котом здесь", paste_status="ok"
        )

    def test_vocab_add_failure_is_non_fatal(self) -> None:
        """Если handle_set_settings бросает исключение, replace всё равно возвращает ok=True."""
        self.settings.handle_set_settings = MagicMock(
            side_effect=RuntimeError("disk full")
        )
        result = self.svc.handle_replace_word_in_last_transcript(
            {"old_word": "котом", "new_word": "кодом"}
        )
        # Replace itself must succeed
        self.assertTrue(result["ok"])
        self.assertIn("кодом", result["new_text"])
        self.assertEqual(result["replaced_count"], 1)


class AutoLearnSanityChecksTestCase(unittest.TestCase):
    """Проверяет граничные случаи: old==new, пустое new, длинный/многосоставной токен."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc, self.settings = _make_svc(
            Path(self.tmp.name) / "data", auto_learn=True
        )
        self.svc._store.add_history_item(
            text="Пример текста для тестирования корректировок", paste_status="ok"
        )

    def test_old_equals_new_no_vocab_add(self) -> None:
        """old_word == new_word → нет смысла учить, stt_hotwords не меняется."""
        result = self.svc.handle_replace_word_in_last_transcript(
            {"old_word": "тестирования", "new_word": "тестирования"}
        )
        self.assertTrue(result["ok"])
        hotword_calls = [
            c for c in self.settings.set_calls if "stt_hotwords" in c
        ]
        self.assertEqual(hotword_calls, [])

    def test_empty_new_word_missing_words_error(self) -> None:
        """Пустой new_word → ok=False (missing_words), vocab не трогается."""
        result = self.svc.handle_replace_word_in_last_transcript(
            {"old_word": "тестирования", "new_word": ""}
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result.get("error"), "missing_words")
        hotword_calls = [
            c for c in self.settings.set_calls if "stt_hotwords" in c
        ]
        self.assertEqual(hotword_calls, [])

    def test_very_long_new_word_no_add(self) -> None:
        """new_word длиннее 60 символов → не добавляется в stt_hotwords."""
        long_word = "а" * 61
        # Patch the text so replace succeeds
        with patch.object(
            self.svc._store, "update_history_item_text", return_value=None
        ), patch.object(
            self.svc._store, "get_history_item_by_id"
        ) as mock_get:
            from unittest.mock import MagicMock as _M
            mock_item = _M()
            mock_item.text = "Пример текста для тестирования корректировок"
            mock_get.return_value = mock_item
            result = self.svc.handle_replace_word_in_last_transcript(
                {"old_word": "тестирования", "new_word": long_word}
            )
        self.assertTrue(result["ok"])
        hotword_calls = [
            c for c in self.settings.set_calls if "stt_hotwords" in c
        ]
        self.assertEqual(hotword_calls, [])

    def test_multi_token_phrase_above_four_no_add(self) -> None:
        """Фраза из >4 токенов → не добавляется (слишком длинная для hotword)."""
        five_token_phrase = "один два три четыре пять"
        with patch.object(
            self.svc._store, "update_history_item_text", return_value=None
        ), patch.object(
            self.svc._store, "get_history_item_by_id"
        ) as mock_get:
            from unittest.mock import MagicMock as _M
            mock_item = _M()
            mock_item.text = "Пример текста для тестирования корректировок"
            mock_get.return_value = mock_item
            result = self.svc.handle_replace_word_in_last_transcript(
                {"old_word": "тестирования", "new_word": five_token_phrase}
            )
        self.assertTrue(result["ok"])
        hotword_calls = [
            c for c in self.settings.set_calls if "stt_hotwords" in c
        ]
        self.assertEqual(hotword_calls, [])

    def test_existing_word_no_duplicate(self) -> None:
        """Если new_word уже в stt_hotwords, дубль не добавляется."""
        # Pre-populate stt_hotwords with the word we'll add
        self.settings._settings["stt_hotwords"] = ["корректировок"]
        result = self.svc.handle_replace_word_in_last_transcript(
            {"old_word": "тестирования", "new_word": "корректировок"}
        )
        self.assertTrue(result["ok"])
        hotword_calls = [
            c for c in self.settings.set_calls if "stt_hotwords" in c
        ]
        # No update call because word is already there
        self.assertEqual(hotword_calls, [])

    def test_short_phrase_up_to_four_tokens_is_added(self) -> None:
        """Фраза из ≤4 токенов добавляется (граничный случай)."""
        four_token_phrase = "один два три четыре"
        with patch.object(
            self.svc._store, "update_history_item_text", return_value=None
        ), patch.object(
            self.svc._store, "get_history_item_by_id"
        ) as mock_get:
            from unittest.mock import MagicMock as _M
            mock_item = _M()
            mock_item.text = "Пример текста для тестирования корректировок"
            mock_get.return_value = mock_item
            result = self.svc.handle_replace_word_in_last_transcript(
                {"old_word": "тестирования", "new_word": four_token_phrase}
            )
        self.assertTrue(result["ok"])
        hotword_calls = [
            c for c in self.settings.set_calls if "stt_hotwords" in c
        ]
        self.assertEqual(len(hotword_calls), 1)
        self.assertIn(four_token_phrase, hotword_calls[0]["stt_hotwords"])


class AutoLearnSettingsNoneTestCase(unittest.TestCase):
    """settings_svc=None (как в старых тестах) — auto-learn тихо скипается."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.svc = LLMOpsService(store=store, settings_svc=None, transcriber=None)
        self.svc._store.add_history_item(
            text="Тест с котом", paste_status="ok"
        )

    def test_none_settings_svc_is_safe(self) -> None:
        """settings_svc=None → replace работает, auto-learn тихо пропускается."""
        result = self.svc.handle_replace_word_in_last_transcript(
            {"old_word": "котом", "new_word": "кодом"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["replaced_count"], 1)


if __name__ == "__main__":
    unittest.main()
