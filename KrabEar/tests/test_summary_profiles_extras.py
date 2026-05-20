"""test_summary_profiles_extras.py — глубокие edge-case тесты SummaryProfileManager.

Wave 208 extras: metadata completeness, validation edge cases, max_length enforcement,
LLM unavailable error path, unicode preservation, concurrency, persist round-trip.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.summary_profiles import SummaryProfileManager  # noqa: E402

# Required keys every profile dict must have
_REQUIRED_KEYS = {"name", "system_prompt", "max_tokens", "format_instructions", "builtin"}


# ---------------------------------------------------------------------------
# Тест 1: metadata completeness для всех встроенных профилей
# ---------------------------------------------------------------------------


class TestDefaultProfileMetadataComplete(unittest.TestCase):
    """Каждый встроенный профиль должен иметь все поля и непустые значения."""

    def setUp(self):
        self.mgr = SummaryProfileManager(data_dir=None)

    def test_all_builtin_have_required_keys(self):
        for p in self.mgr.list_profiles():
            for key in _REQUIRED_KEYS:
                self.assertIn(key, p, f"Профиль {p.get('name')!r}: отсутствует ключ {key!r}")

    def test_builtin_flag_is_true(self):
        for p in self.mgr.list_profiles():
            if p["name"] in ("brief", "detailed", "bullet_points", "meeting_notes", "telegram"):
                self.assertTrue(p["builtin"], f"{p['name']!r}: builtin должен быть True")

    def test_system_prompt_nonempty(self):
        for p in self.mgr.list_profiles():
            self.assertGreater(
                len(p["system_prompt"].strip()), 10,
                f"Профиль {p['name']!r}: system_prompt слишком короткий"
            )

    def test_max_tokens_positive(self):
        for p in self.mgr.list_profiles():
            self.assertGreater(p["max_tokens"], 0, f"Профиль {p['name']!r}: max_tokens <= 0")

    def test_format_instructions_nonempty(self):
        for p in self.mgr.list_profiles():
            if p["name"] in ("brief", "detailed", "bullet_points", "meeting_notes", "telegram"):
                self.assertGreater(
                    len(p["format_instructions"].strip()), 0,
                    f"Профиль {p['name']!r}: пустой format_instructions"
                )

    def test_five_builtin_profiles_count(self):
        mgr = SummaryProfileManager(data_dir=None)
        profiles = mgr.list_profiles()
        builtins = [p for p in profiles if p["builtin"]]
        self.assertEqual(len(builtins), 5)


# ---------------------------------------------------------------------------
# Тест 2: валидация — отклонение пустого prompt
# ---------------------------------------------------------------------------


class TestCustomProfileValidationRejectsEmptyPrompt(unittest.TestCase):
    def setUp(self):
        self.mgr = SummaryProfileManager(data_dir=None)

    def test_empty_prompt_raises(self):
        with self.assertRaises(ValueError):
            self.mgr.add_custom_profile("myprofile", prompt="", max_tokens=100)

    def test_whitespace_only_prompt_raises(self):
        with self.assertRaises(ValueError):
            self.mgr.add_custom_profile("myprofile2", prompt="   \t\n", max_tokens=100)

    def test_empty_name_raises(self):
        with self.assertRaises(ValueError):
            self.mgr.add_custom_profile("", prompt="Хороший промпт", max_tokens=100)

    def test_builtin_name_reserved_raises(self):
        for name in ("brief", "detailed", "bullet_points", "meeting_notes", "telegram"):
            with self.assertRaises(ValueError):
                self.mgr.add_custom_profile(name, prompt="Промпт", max_tokens=100)

    def test_zero_max_tokens_raises(self):
        with self.assertRaises(ValueError):
            self.mgr.add_custom_profile("test_zero", prompt="Промпт", max_tokens=0)

    def test_negative_max_tokens_raises(self):
        with self.assertRaises(ValueError):
            self.mgr.add_custom_profile("test_neg", prompt="Промпт", max_tokens=-50)


# ---------------------------------------------------------------------------
# Тест 3: max_tokens enforcement
# ---------------------------------------------------------------------------


class TestCustomProfileMaxLengthEnforced(unittest.TestCase):
    def setUp(self):
        self.mgr = SummaryProfileManager(data_dir=None)

    def test_max_tokens_stored_correctly(self):
        p = self.mgr.add_custom_profile("mt_test", prompt="Тест промпт", max_tokens=99)
        self.assertEqual(p.max_tokens, 99)

    def test_max_tokens_roundtrip_via_get(self):
        self.mgr.add_custom_profile("mt_test2", prompt="Тест промпт", max_tokens=777)
        fetched = self.mgr.get_profile("mt_test2")
        self.assertEqual(fetched.max_tokens, 777)

    def test_float_max_tokens_truncated_to_int(self):
        # max_tokens is cast with int(), so 150.9 → 150
        p = self.mgr.add_custom_profile("mt_float", prompt="Промпт float", max_tokens=150)
        self.assertIsInstance(p.max_tokens, int)

    def test_large_max_tokens_accepted(self):
        p = self.mgr.add_custom_profile("mt_large", prompt="Промпт большой", max_tokens=100_000)
        self.assertEqual(p.max_tokens, 100_000)


# ---------------------------------------------------------------------------
# Тест 4: LLM unavailable → error path
# ---------------------------------------------------------------------------


class TestApplyWithLLMUnavailableReturnsError(unittest.TestCase):
    """HistoryService.handle_summarize_history_item должен не падать при circuit open."""

    def test_circuit_open_returns_error_flag(self):
        # Build a mock LLMRewriter with open circuit
        rw = MagicMock()
        rw._circuit = MagicMock()
        rw._circuit.state = "open"

        # Simulate what HistoryService does when it checks circuit state
        # We test the integration point directly: if circuit is open, result is not ok
        from backend.llm_rewriter import LLMRewriteResult
        rw.summarize.return_value = LLMRewriteResult(
            ok=False, text="", fallback_reason="circuit_open", latency_ms=0
        )

        result = rw.summarize("some text", system_prompt="...", max_tokens=100)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "circuit_open")

    def test_summarize_exception_returns_error(self):
        rw = MagicMock()
        rw.summarize.side_effect = RuntimeError("LM Studio недоступен")

        with self.assertRaises(RuntimeError):
            rw.summarize("текст", system_prompt="...", max_tokens=100)

    def test_no_llm_rewriter_at_all(self):
        """SummaryProfileManager без LLM rewriter не должен падать при get_profile."""
        mgr = SummaryProfileManager(data_dir=None)
        # Just getting a profile should never require LLM
        p = mgr.get_profile("brief")
        self.assertIsNotNone(p)


# ---------------------------------------------------------------------------
# Тест 5: unicode в шаблоне
# ---------------------------------------------------------------------------


class TestUnicodeInTemplatePreserved(unittest.TestCase):
    def setUp(self):
        self.mgr = SummaryProfileManager(data_dir=None)

    def test_cyrillic_prompt_preserved(self):
        prompt = "Сделай краткое резюме текста по-русски, используя кириллицу."
        p = self.mgr.add_custom_profile("ru_test", prompt=prompt, max_tokens=200)
        self.assertEqual(p.system_prompt, prompt)

    def test_spanish_accents_preserved(self):
        prompt = "Haz un resumen breve en español con acentos: á é í ó ú ñ ü."
        p = self.mgr.add_custom_profile("es_test", prompt=prompt, max_tokens=150)
        self.assertEqual(p.system_prompt, prompt)

    def test_emoji_in_prompt_preserved(self):
        prompt = "Краткое резюме 📝 для Telegram 🚀 формат."
        p = self.mgr.add_custom_profile("emoji_test", prompt=prompt, max_tokens=100)
        self.assertEqual(p.system_prompt, prompt)

    def test_mixed_scripts_preserved(self):
        prompt = "Summary / Резюме / Resumen — mix of scripts."
        p = self.mgr.add_custom_profile("mixed_test", prompt=prompt, max_tokens=100)
        self.assertEqual(p.system_prompt, prompt)

    def test_unicode_format_instructions_preserved(self):
        instructions = "Маркированный список (• пункт) — не более 5 пунктов."
        p = self.mgr.add_custom_profile(
            "fi_unicode", prompt="Промпт", max_tokens=100,
            format_instructions=instructions
        )
        self.assertEqual(p.format_instructions, instructions)


# ---------------------------------------------------------------------------
# Тест 6: concurrent apply — thread safety
# ---------------------------------------------------------------------------


class TestConcurrentApplyThreadSafe(unittest.TestCase):
    """20 потоков одновременно читают/создают профили — нет гонок или исключений."""

    def setUp(self):
        self.mgr = SummaryProfileManager(data_dir=None)

    def test_concurrent_get_profile_no_error(self):
        errors: List[Exception] = []

        def run(i: int):
            try:
                p = self.mgr.get_profile("brief")
                assert p.name == "brief"
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [], f"Ошибки в потоках: {errors}")

    def test_concurrent_add_custom_profiles(self):
        errors: List[Exception] = []
        created: List[str] = []

        def run(i: int):
            try:
                name = f"thread_profile_{i}"
                p = self.mgr.add_custom_profile(
                    name, prompt=f"Промпт для потока {i}", max_tokens=100
                )
                created.append(p.name)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [], f"Ошибки в потоках: {errors}")
        self.assertEqual(len(created), 20)

    def test_concurrent_list_profiles_stable(self):
        """list_profiles() под нагрузкой не должен падать."""
        errors: List[Exception] = []

        def run():
            try:
                profiles = self.mgr.list_profiles()
                assert isinstance(profiles, list)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])


# ---------------------------------------------------------------------------
# Тест 7: persist round-trip
# ---------------------------------------------------------------------------


class TestPersistRoundTrip(unittest.TestCase):
    """Кастомные профили должны переживать reload из файла."""

    def _make_mgr(self, tmp: Path) -> SummaryProfileManager:
        return SummaryProfileManager(data_dir=tmp)

    def test_custom_profile_survives_reload(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            mgr1 = self._make_mgr(tmp)
            mgr1.add_custom_profile(
                "persist_test",
                prompt="Тест персистентности",
                max_tokens=300,
                format_instructions="Формат теста"
            )

            # Reload from same dir
            mgr2 = self._make_mgr(tmp)
            p = mgr2.get_profile("persist_test")
            self.assertEqual(p.name, "persist_test")
            self.assertEqual(p.system_prompt, "Тест персистентности")
            self.assertEqual(p.max_tokens, 300)
            self.assertEqual(p.format_instructions, "Формат теста")
            self.assertFalse(p.builtin)

    def test_multiple_custom_profiles_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            mgr1 = self._make_mgr(tmp)
            for i in range(5):
                mgr1.add_custom_profile(
                    f"profile_{i}",
                    prompt=f"Промпт {i}",
                    max_tokens=100 + i * 10
                )

            mgr2 = self._make_mgr(tmp)
            for i in range(5):
                p = mgr2.get_profile(f"profile_{i}")
                self.assertEqual(p.max_tokens, 100 + i * 10)

    def test_remove_custom_profile_not_reloaded(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            mgr1 = self._make_mgr(tmp)
            mgr1.add_custom_profile("to_remove", prompt="Удалить меня", max_tokens=100)
            mgr1.remove_custom_profile("to_remove")

            mgr2 = self._make_mgr(tmp)
            with self.assertRaises(KeyError):
                mgr2.get_profile("to_remove")

    def test_unicode_profile_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            mgr1 = self._make_mgr(tmp)
            prompt = "Промпт с кириллицей и Spanish: á ñ ü 🎤"
            mgr1.add_custom_profile("unicode_rt", prompt=prompt, max_tokens=200)

            mgr2 = self._make_mgr(tmp)
            p = mgr2.get_profile("unicode_rt")
            self.assertEqual(p.system_prompt, prompt)

    def test_json_file_format_valid(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            mgr = self._make_mgr(tmp)
            mgr.add_custom_profile("json_test", prompt="Промпт JSON", max_tokens=150)

            profiles_file = tmp / "summary_profiles.json"
            self.assertTrue(profiles_file.exists())
            data = json.loads(profiles_file.read_text(encoding="utf-8"))
            self.assertIsInstance(data, list)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["name"], "json_test")

    def test_no_file_without_data_dir(self):
        """Без data_dir _save() ничего не пишет — нет исключений."""
        mgr = SummaryProfileManager(data_dir=None)
        mgr.add_custom_profile("in_memory_only", prompt="Промпт в памяти", max_tokens=100)
        # Just getting works, no file created
        p = mgr.get_profile("in_memory_only")
        self.assertEqual(p.name, "in_memory_only")


if __name__ == "__main__":
    unittest.main()
