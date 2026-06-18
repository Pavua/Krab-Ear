"""Тесты для IPC-метода list_voice_commands (STTManagementService).

Проверяет:
  - Запись "list_voice_commands" присутствует в dispatch-таблице BackendService.
  - Хендлер возвращает ok=True, непустые commands и languages.
  - Каждая команда имеет все 4 ключа: language, phrase, action, description.
  - Фильтр по language работает корректно.
  - В RU-командах присутствует "новая строка".
  - Хендлер никогда не выбрасывает исключение (деградирует до ok=False).

🔴 Нет import mlx_whisper — совместим с ubuntu-CI (Python 3.12, без MLX wheels).
"""

from __future__ import annotations

import os
import sys
import types
import unittest

# ---------------------------------------------------------------------------
# sys.path setup — стандартный паттерн тестов Krab Ear
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

KRAB_EAR_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if KRAB_EAR_ROOT not in sys.path:
    sys.path.insert(0, KRAB_EAR_ROOT)

# ---------------------------------------------------------------------------
# Stub тяжёлых зависимостей (mlx_whisper, sounddevice, pyannote…)
# до импорта backend-модулей, чтобы CI на ubuntu не падал.
# ---------------------------------------------------------------------------


def _stub_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules.setdefault(name, mod)
    return mod


for _heavy in [
    "mlx_whisper", "mlx", "mlx.core", "mlx.nn",
    "sounddevice", "pyannote", "pyannote.audio",
    "torch", "torchaudio", "transformers",
    "sentry_sdk",
]:
    _stub_module(_heavy)

# ---------------------------------------------------------------------------
# Импорт тестируемых модулей
# ---------------------------------------------------------------------------

from backend.stt_management_service import (  # noqa: E402
    STTManagementService,
    _clean_pattern,
    _describe_command,
)


# ---------------------------------------------------------------------------
# Минимальные заглушки для STTManagementService
# ---------------------------------------------------------------------------

class _FakeSettingsSvc:
    """Минимальная заглушка SettingsService для тестов."""

    def cached_settings(self) -> dict:
        return {}

    def handle_set_settings(self, patch: dict) -> dict:
        return {"ok": True}


def _make_svc() -> STTManagementService:
    return STTManagementService(
        settings_svc=_FakeSettingsSvc(),
        transcriber=None,
    )


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------

class TestListVoiceCommandsDispatch(unittest.TestCase):
    """Запись list_voice_commands должна быть в dispatch-таблице BackendService."""

    def test_dispatch_entry_present(self) -> None:
        """Строка 'list_voice_commands' присутствует в service.py dispatch-таблице."""
        import pathlib
        service_path = pathlib.Path(KRAB_EAR_ROOT) / "backend" / "service.py"
        source = service_path.read_text(encoding="utf-8")
        self.assertIn('"list_voice_commands"', source,
                      "Запись list_voice_commands отсутствует в service.py")


class TestListVoiceCommandsHandler(unittest.TestCase):
    """Проверка возвращаемых данных handle_list_voice_commands."""

    def setUp(self) -> None:
        self.svc = _make_svc()

    def test_returns_ok_true(self) -> None:
        result = self.svc.handle_list_voice_commands({})
        self.assertTrue(result.get("ok"), f"ok должно быть True, получено: {result}")

    def test_commands_non_empty(self) -> None:
        result = self.svc.handle_list_voice_commands({})
        cmds = result.get("commands", [])
        self.assertGreater(len(cmds), 0, "commands не должен быть пустым")

    def test_languages_non_empty(self) -> None:
        result = self.svc.handle_list_voice_commands({})
        langs = result.get("languages", [])
        self.assertIn("ru", langs)
        self.assertIn("es", langs)
        self.assertIn("en", langs)

    def test_each_command_has_all_keys(self) -> None:
        result = self.svc.handle_list_voice_commands({})
        required_keys = {"language", "phrase", "action", "description"}
        for cmd in result["commands"]:
            self.assertEqual(
                required_keys,
                set(cmd.keys()),
                f"Команда не имеет всех ключей: {cmd}",
            )

    def test_language_filter_ru(self) -> None:
        result = self.svc.handle_list_voice_commands({"language": "ru"})
        self.assertTrue(result.get("ok"))
        for cmd in result["commands"]:
            self.assertEqual(cmd["language"], "ru",
                             "При фильтре ru попала команда другого языка")

    def test_language_filter_es(self) -> None:
        result = self.svc.handle_list_voice_commands({"language": "es"})
        self.assertTrue(result.get("ok"))
        for cmd in result["commands"]:
            self.assertEqual(cmd["language"], "es")

    def test_language_filter_en(self) -> None:
        result = self.svc.handle_list_voice_commands({"language": "en"})
        self.assertTrue(result.get("ok"))
        for cmd in result["commands"]:
            self.assertEqual(cmd["language"], "en")

    def test_ru_contains_novaya_stroka(self) -> None:
        """RU-команды должны содержать «новая строка» (insert \\n)."""
        result = self.svc.handle_list_voice_commands({"language": "ru"})
        phrases = [cmd["phrase"] for cmd in result["commands"]]
        self.assertTrue(
            any("новая строка" in p for p in phrases),
            f"Фраза «новая строка» отсутствует в RU-командах. Найдено: {phrases}",
        )

    def test_unknown_language_filter_returns_empty_commands(self) -> None:
        result = self.svc.handle_list_voice_commands({"language": "zh"})
        self.assertTrue(result.get("ok"))
        self.assertEqual(result["commands"], [])

    def test_handler_never_raises(self) -> None:
        """Хендлер должен перехватывать все исключения и возвращать ok=False."""
        # Патчим _RU_COMMANDS чтобы вызвать исключение внутри хендлера.
        import backend.stt_management_service as m
        original = m._RU_COMMANDS
        try:
            m._RU_COMMANDS = None  # type: ignore[assignment]  # вызовет TypeError при итерации
            result = self.svc.handle_list_voice_commands({})
            # Должно деградировать без исключения
            self.assertFalse(result.get("ok"),
                             "При внутренней ошибке ok должно быть False")
        finally:
            m._RU_COMMANDS = original


class TestCleanPattern(unittest.TestCase):
    """Тесты вспомогательной функции _clean_pattern."""

    def test_plain_phrase(self) -> None:
        self.assertEqual(_clean_pattern("новая строка"), "новая строка")

    def test_strips_extra_spaces(self) -> None:
        self.assertEqual(_clean_pattern("  delete  last  word  "), "delete last word")

    def test_escapes_removed(self) -> None:
        # re.escape("punto") → "punto" (нет спецсимволов), но r"punto\.y" → "punto.y"
        self.assertEqual(_clean_pattern(r"punto\.y"), "punto.y")


class TestDescribeCommand(unittest.TestCase):
    """Тесты вспомогательной функции _describe_command."""

    def test_delete_last_word(self) -> None:
        desc = _describe_command("delete_last", "word")
        self.assertIn("слово", desc.lower())

    def test_delete_last_sentence(self) -> None:
        desc = _describe_command("delete_last", "sentence")
        self.assertIn("предложение", desc.lower())

    def test_delete_last_paragraph(self) -> None:
        desc = _describe_command("delete_last", "paragraph")
        self.assertIn("абзац", desc.lower())

    def test_insert_newline(self) -> None:
        desc = _describe_command("insert", "\n")
        self.assertIn("строк", desc.lower())

    def test_insert_comma(self) -> None:
        desc = _describe_command("insert", ",")
        self.assertIn(",", desc)

    def test_insert_period(self) -> None:
        desc = _describe_command("insert", ".")
        self.assertIn(".", desc)

    def test_capitalize_next(self) -> None:
        desc = _describe_command("capitalize_next", "")
        self.assertTrue(len(desc) > 0)

    def test_uppercase_sent(self) -> None:
        desc = _describe_command("uppercase_sent", "")
        self.assertTrue(len(desc) > 0)


if __name__ == "__main__":
    unittest.main()
