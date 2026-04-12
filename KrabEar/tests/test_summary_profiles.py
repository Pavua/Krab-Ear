"""Тесты SummaryProfileManager и IPC-методов для профилей резюмирования."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.summary_profiles import SummaryProfile, SummaryProfileManager, _BUILTIN_MAP
from backend.history_service import HistoryService
from backend.state_store import StateStore
from backend.llm_rewriter import LLMRewriteResult


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

def _make_history_service(tmp_dir: Path, llm_rewriter=None) -> HistoryService:
    store = StateStore(data_dir=tmp_dir)
    return HistoryService(store=store, llm_rewriter=llm_rewriter)


def _add_items(svc: HistoryService, texts: list[str]) -> list[str]:
    ids = []
    for text in texts:
        item = svc.handle_add_history_item({"text": text, "paste_status": "ok"})
        ids.append(item["id"])
    return ids


def _ok_rewriter(summary_text: str) -> MagicMock:
    rw = MagicMock()
    rw._circuit = MagicMock()
    rw._circuit.state = "closed"
    rw.summarize.return_value = LLMRewriteResult(
        ok=True, text=summary_text, fallback_reason=None, latency_ms=10
    )
    return rw


# ===========================================================================
# Тесты SummaryProfileManager
# ===========================================================================

class TestSummaryProfileManagerBuiltin(unittest.TestCase):
    """Проверяем встроенные профили."""

    def setUp(self):
        self.mgr = SummaryProfileManager(data_dir=None)

    def test_all_builtin_profiles_present(self):
        profiles = self.mgr.list_profiles()
        names = {p["name"] for p in profiles}
        for expected in ("brief", "detailed", "bullet_points", "meeting_notes", "telegram"):
            self.assertIn(expected, names, f"Профиль {expected!r} отсутствует")

    def test_get_profile_brief(self):
        p = self.mgr.get_profile("brief")
        self.assertIsInstance(p, SummaryProfile)
        self.assertEqual(p.name, "brief")
        self.assertTrue(p.builtin)
        self.assertGreater(len(p.system_prompt), 10)
        self.assertGreater(p.max_tokens, 0)

    def test_get_profile_meeting_notes(self):
        p = self.mgr.get_profile("meeting_notes")
        self.assertEqual(p.name, "meeting_notes")
        self.assertIn("УЧАСТНИКИ", p.system_prompt)

    def test_get_profile_unknown_raises(self):
        with self.assertRaises(KeyError):
            self.mgr.get_profile("nonexistent_profile_xyz")

    def test_list_profiles_returns_dicts(self):
        profiles = self.mgr.list_profiles()
        self.assertIsInstance(profiles, list)
        for p in profiles:
            self.assertIn("name", p)
            self.assertIn("system_prompt", p)
            self.assertIn("max_tokens", p)
            self.assertIn("builtin", p)

    def test_builtin_profile_to_dict(self):
        p = self.mgr.get_profile("telegram")
        d = p.to_dict()
        self.assertEqual(d["name"], "telegram")
        self.assertTrue(d["builtin"])


class TestSummaryProfileManagerCustom(unittest.TestCase):
    """Проверяем кастомные профили и персистентность."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.mgr = SummaryProfileManager(data_dir=self.tmp)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_add_custom_profile(self):
        p = self.mgr.add_custom_profile(
            name="my_style",
            prompt="Summarize in one line.",
            max_tokens=100,
            format_instructions="One line.",
        )
        self.assertEqual(p.name, "my_style")
        self.assertFalse(p.builtin)
        self.assertEqual(p.max_tokens, 100)

    def test_custom_profile_persists(self):
        self.mgr.add_custom_profile("persistent", "My prompt.", 200)
        # Создаём новый менеджер из той же директории
        mgr2 = SummaryProfileManager(data_dir=self.tmp)
        p = mgr2.get_profile("persistent")
        self.assertEqual(p.name, "persistent")
        self.assertEqual(p.system_prompt, "My prompt.")

    def test_add_custom_overrides_existing(self):
        self.mgr.add_custom_profile("dupe", "First.", 100)
        self.mgr.add_custom_profile("dupe", "Second.", 200)
        p = self.mgr.get_profile("dupe")
        self.assertEqual(p.system_prompt, "Second.")

    def test_add_custom_builtin_name_raises(self):
        with self.assertRaises(ValueError):
            self.mgr.add_custom_profile("brief", "Override.", 100)

    def test_add_custom_empty_name_raises(self):
        with self.assertRaises(ValueError):
            self.mgr.add_custom_profile("", "Prompt.", 100)

    def test_add_custom_empty_prompt_raises(self):
        with self.assertRaises(ValueError):
            self.mgr.add_custom_profile("valid_name", "", 100)

    def test_remove_custom_profile(self):
        self.mgr.add_custom_profile("temp_profile", "Temp.", 50)
        removed = self.mgr.remove_custom_profile("temp_profile")
        self.assertTrue(removed)
        with self.assertRaises(KeyError):
            self.mgr.get_profile("temp_profile")

    def test_remove_builtin_raises(self):
        with self.assertRaises(ValueError):
            self.mgr.remove_custom_profile("brief")

    def test_custom_appears_in_list_profiles(self):
        self.mgr.add_custom_profile("listed_custom", "My prompt.", 150)
        names = {p["name"] for p in self.mgr.list_profiles()}
        self.assertIn("listed_custom", names)

    def test_profiles_json_written(self):
        self.mgr.add_custom_profile("json_test", "Prompt.", 100)
        path = self.tmp / "summary_profiles.json"
        self.assertTrue(path.exists())
        data = json.loads(path.read_text())
        self.assertEqual(data[0]["name"], "json_test")


# ===========================================================================
# Тесты IPC-методов в HistoryService
# ===========================================================================

class TestHistoryServiceSummaryProfileIPC(unittest.TestCase):
    """Тесты handle_list_summary_profiles и handle_add_summary_profile."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.svc = _make_history_service(self.tmp)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_list_summary_profiles_returns_all_builtin(self):
        result = self.svc.handle_list_summary_profiles({})
        names = {p["name"] for p in result["profiles"]}
        for n in ("brief", "detailed", "bullet_points", "meeting_notes", "telegram"):
            self.assertIn(n, names)

    def test_add_summary_profile_creates_custom(self):
        result = self.svc.handle_add_summary_profile({
            "name": "ipc_custom",
            "prompt": "My IPC prompt.",
            "max_tokens": 150,
            "format_instructions": "Short.",
        })
        p = result["profile"]
        self.assertEqual(p["name"], "ipc_custom")
        self.assertFalse(p["builtin"])

    def test_add_summary_profile_persists_across_calls(self):
        self.svc.handle_add_summary_profile({
            "name": "ipc_persist",
            "prompt": "Persist me.",
            "max_tokens": 200,
        })
        profiles_result = self.svc.handle_list_summary_profiles({})
        names = {p["name"] for p in profiles_result["profiles"]}
        self.assertIn("ipc_persist", names)

    def test_add_summary_profile_missing_name_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_add_summary_profile({"prompt": "No name."})

    def test_add_summary_profile_missing_prompt_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_add_summary_profile({"name": "no_prompt"})


# ===========================================================================
# Тесты auto_summarize_batch с параметром profile
# ===========================================================================

class TestAutoSummarizeBatchWithProfile(unittest.TestCase):
    """Проверяем что profile передаётся в промпт и возвращается в ответе."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_profile_in_response_when_llm_ok(self):
        rw = _ok_rewriter("РЕЗЮМЕ: Краткий итог.\nТЕЗИСЫ:\n- Тезис 1")
        svc = _make_history_service(self.tmp, llm_rewriter=rw)
        ids = _add_items(svc, ["Первая транскрипция.", "Вторая транскрипция."])
        result = svc.handle_auto_summarize_batch({"ids": ids, "profile": "detailed"})
        self.assertEqual(result.get("profile"), "detailed")
        self.assertTrue(result["llm"])

    def test_profile_prompt_uses_system_prompt(self):
        """Промпт, переданный в rewriter, должен содержать system_prompt профиля."""
        rw = _ok_rewriter("РЕЗЮМЕ: Тест.\nТЕЗИСЫ:\n- Пункт 1")
        svc = _make_history_service(self.tmp, llm_rewriter=rw)
        ids = _add_items(svc, ["Текст один.", "Текст два."])
        svc.handle_auto_summarize_batch({"ids": ids, "profile": "telegram"})
        call_args = rw.summarize.call_args
        prompt_used = call_args[0][0]
        # Промпт должен содержать часть system_prompt профиля "telegram"
        self.assertIn("Telegram", prompt_used)

    def test_unknown_profile_falls_back_to_brief(self):
        rw = _ok_rewriter("РЕЗЮМЕ: Упрощённый.\nТЕЗИСЫ:\n- Тезис")
        svc = _make_history_service(self.tmp, llm_rewriter=rw)
        ids = _add_items(svc, ["Тест текст."])
        # Профиль не существует — должен использоваться "brief"
        result = svc.handle_auto_summarize_batch({"ids": ids, "profile": "does_not_exist"})
        self.assertEqual(result.get("profile"), "brief")

    def test_no_profile_param_uses_brief(self):
        rw = _ok_rewriter("РЕЗЮМЕ: Дефолтный.\nТЕЗИСЫ:\n- Тезис")
        svc = _make_history_service(self.tmp, llm_rewriter=rw)
        ids = _add_items(svc, ["Дефолтный текст."])
        result = svc.handle_auto_summarize_batch({"ids": ids})
        self.assertEqual(result.get("profile"), "brief")

    def test_custom_profile_used_in_prompt(self):
        """Кастомный профиль должен подставить свой промпт в LLM-запрос."""
        rw = _ok_rewriter("РЕЗЮМЕ: Custom.\nТЕЗИСЫ:\n- Кастом 1")
        svc = _make_history_service(self.tmp, llm_rewriter=rw)
        # Добавляем кастомный профиль через IPC
        svc.handle_add_summary_profile({
            "name": "my_custom",
            "prompt": "UniquePromptXYZ: summarize this.",
            "max_tokens": 100,
        })
        ids = _add_items(svc, ["Тест кастомного профиля."])
        svc.handle_auto_summarize_batch({"ids": ids, "profile": "my_custom"})
        call_args = rw.summarize.call_args
        prompt_used = call_args[0][0]
        self.assertIn("UniquePromptXYZ", prompt_used)


# ===========================================================================
# Тест _build_batch_summary_prompt с профилем
# ===========================================================================

class TestBuildBatchSummaryPrompt(unittest.TestCase):
    """Unit-тесты для статического метода _build_batch_summary_prompt."""

    def test_without_profile_legacy_format(self):
        prompt = HistoryService._build_batch_summary_prompt(["Текст 1", "Текст 2"])
        self.assertIn("РЕЗЮМЕ", prompt)
        self.assertIn("ТЕЗИСЫ", prompt)

    def test_with_profile_uses_system_prompt(self):
        profile = SummaryProfile(
            name="test",
            system_prompt="TestSystemPrompt",
            max_tokens=100,
            format_instructions="One line.",
        )
        prompt = HistoryService._build_batch_summary_prompt(["Текст"], profile=profile)
        self.assertIn("TestSystemPrompt", prompt)
        self.assertIn("One line.", prompt)
        self.assertIn("Текст", prompt)

    def test_with_profile_no_format_instructions(self):
        profile = SummaryProfile(
            name="no_fmt",
            system_prompt="NoFmtPrompt",
            max_tokens=100,
            format_instructions="",
        )
        prompt = HistoryService._build_batch_summary_prompt(["A", "B"], profile=profile)
        self.assertIn("NoFmtPrompt", prompt)
        self.assertNotIn("Формат ответа:", prompt)


if __name__ == "__main__":
    unittest.main()
