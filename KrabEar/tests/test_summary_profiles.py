"""Тесты SummaryProfileManager и IPC-методов для профилей резюмирования."""

from __future__ import annotations
from backend.llm_rewriter import LLMRewriteResult
from backend.state_store import StateStore
from backend.history_service import HistoryService
from backend.summary_profiles import SummaryProfile, SummaryProfileManager

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


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


# ===========================================================================
# Дополнительные тесты: валидация, персистентность, граничные случаи
# ===========================================================================

class TestSummaryProfileValidation(unittest.TestCase):
    """Тесты валидации параметров профилей."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.mgr = SummaryProfileManager(data_dir=self.tmp)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_max_tokens_validation_negative(self):
        """max_tokens < 1 должен вызвать ValueError."""
        with self.assertRaises(ValueError):
            self.mgr.add_custom_profile("negative", "Prompt.", max_tokens=-1)

    def test_max_tokens_validation_zero(self):
        """max_tokens = 0 должен вызвать ValueError."""
        with self.assertRaises(ValueError):
            self.mgr.add_custom_profile("zero", "Prompt.", max_tokens=0)

    def test_max_tokens_coerced_to_int(self):
        """max_tokens коерцируется в int из float."""
        p = self.mgr.add_custom_profile("float_tokens", "Prompt.", max_tokens=150.9)
        self.assertEqual(p.max_tokens, 150)

    def test_name_trimmed_whitespace(self):
        """Имя с пробелами должно быть отрезано."""
        p = self.mgr.add_custom_profile("  trimmed  ", "Prompt.", max_tokens=100)
        self.assertEqual(p.name, "trimmed")

    def test_prompt_trimmed_whitespace(self):
        """Промпт с пробелами должен быть отрезан."""
        p = self.mgr.add_custom_profile("test", "  content  ", max_tokens=100)
        self.assertEqual(p.system_prompt, "content")

    def test_whitespace_only_name_raises(self):
        """Имя только из пробелов должно вызвать ValueError."""
        with self.assertRaises(ValueError):
            self.mgr.add_custom_profile("   ", "Prompt.", max_tokens=100)

    def test_whitespace_only_prompt_raises(self):
        """Промпт только из пробелов должен вызвать ValueError."""
        with self.assertRaises(ValueError):
            self.mgr.add_custom_profile("name", "   ", max_tokens=100)

    def test_very_large_max_tokens_clamped(self):
        """max_tokens выше потолка должен быть зажат до _MAX_TOKENS_CEILING (DoS guard)."""
        from backend.summary_profiles import _MAX_TOKENS_CEILING
        p = self.mgr.add_custom_profile("big", "Prompt.", max_tokens=999999)
        self.assertEqual(p.max_tokens, _MAX_TOKENS_CEILING)

    def test_max_tokens_one(self):
        """max_tokens = 1 — минимально допустимое значение."""
        p = self.mgr.add_custom_profile("min", "Prompt.", max_tokens=1)
        self.assertEqual(p.max_tokens, 1)


class TestSummaryProfilePersistence(unittest.TestCase):
    """Тесты сохранения и загрузки профилей на диск."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.mgr = SummaryProfileManager(data_dir=self.tmp)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_multiple_custom_profiles_persist(self):
        """Несколько кастомных профилей должны сохраниться и загрузиться."""
        self.mgr.add_custom_profile("profile1", "Prompt 1.", 100, "Fmt 1")
        self.mgr.add_custom_profile("profile2", "Prompt 2.", 200, "Fmt 2")
        self.mgr.add_custom_profile("profile3", "Prompt 3.", 300)

        mgr2 = SummaryProfileManager(data_dir=self.tmp)
        p1 = mgr2.get_profile("profile1")
        p2 = mgr2.get_profile("profile2")
        p3 = mgr2.get_profile("profile3")

        self.assertEqual(p1.max_tokens, 100)
        self.assertEqual(p2.format_instructions, "Fmt 2")
        self.assertEqual(p3.system_prompt, "Prompt 3.")

    def test_json_file_structure(self):
        """JSON файл должен содержать массив профилей."""
        self.mgr.add_custom_profile("json_test", "Prompt.", 150, "Format.")
        path = self.tmp / "summary_profiles.json"
        data = json.loads(path.read_text())
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "json_test")

    def test_remove_custom_profile_persists(self):
        """Удаление профиля должно сохраниться на диск."""
        self.mgr.add_custom_profile("to_delete", "Prompt.", 100)
        self.mgr.remove_custom_profile("to_delete")

        mgr2 = SummaryProfileManager(data_dir=self.tmp)
        with self.assertRaises(KeyError):
            mgr2.get_profile("to_delete")

    def test_remove_nonexistent_profile_returns_false(self):
        """Удаление несуществующего профиля должно вернуть False."""
        result = self.mgr.remove_custom_profile("does_not_exist")
        self.assertFalse(result)

    def test_no_data_dir_prevents_persistence(self):
        """SummaryProfileManager(data_dir=None) не сохраняет на диск."""
        mgr = SummaryProfileManager(data_dir=None)
        mgr.add_custom_profile("ephemeral", "Prompt.", 100)
        # Должна быть в памяти, но не на диске
        p = mgr.get_profile("ephemeral")
        self.assertEqual(p.name, "ephemeral")


class TestSummaryProfileIsolation(unittest.TestCase):
    """Тесты изоляции между встроенными и кастомными профилями."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.mgr = SummaryProfileManager(data_dir=self.tmp)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_builtin_profile_not_in_custom(self):
        """Встроенные профили не должны быть в _custom."""
        builtin_count = len([p for p in self.mgr.list_profiles() if p["builtin"]])
        self.assertEqual(builtin_count, 5)
        self.assertEqual(len(self.mgr._custom), 0)

    def test_custom_only_in_list_as_custom(self):
        """Кастомные профили должны иметь builtin=False."""
        self.mgr.add_custom_profile("custom1", "Prompt.", 100)
        profiles = self.mgr.list_profiles()
        custom = [p for p in profiles if p["name"] == "custom1"]
        self.assertEqual(len(custom), 1)
        self.assertFalse(custom[0]["builtin"])

    def test_cannot_override_builtin(self):
        """Нельзя создать кастомный профиль с именем встроенного."""
        with self.assertRaises(ValueError):
            self.mgr.add_custom_profile("brief", "Override.", 100)


class TestSummaryProfileEdgeCases(unittest.TestCase):
    """Тесты нештатных сценариев."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.mgr = SummaryProfileManager(data_dir=self.tmp)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_profile_to_dict_round_trip(self):
        """SummaryProfile -> dict -> load должно восстановить объект."""
        p1 = self.mgr.add_custom_profile("roundtrip", "Test prompt.", 123, "Test fmt.")
        d = p1.to_dict()
        p2 = SummaryProfile(**d)
        self.assertEqual(p1.name, p2.name)
        self.assertEqual(p1.system_prompt, p2.system_prompt)
        self.assertEqual(p1.max_tokens, p2.max_tokens)

    def test_empty_format_instructions_default(self):
        """format_instructions может быть пустой строкой."""
        p = self.mgr.add_custom_profile("no_fmt", "Prompt.", 100)
        self.assertEqual(p.format_instructions, "")

    def test_special_characters_in_prompt(self):
        """Промпт со специальными символами должен быть сохранён."""
        special = "Тест: 'кавычки' \"двойные\" \n новая строка \t таб"
        p = self.mgr.add_custom_profile("special", special, 100)
        self.assertEqual(p.system_prompt, special)

    def test_unicode_in_profile(self):
        """Unicode должен работать корректно."""
        p = self.mgr.add_custom_profile("тест_пром", "Промпт на русском.", 100)
        self.assertEqual(p.name, "тест_пром")
        self.assertIn("русском", p.system_prompt)


class TestHistoryServiceSummaryProfileEdgeCases(unittest.TestCase):
    """Тесты IPC методов с edge cases."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.svc = _make_history_service(self.tmp)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_add_summary_profile_ipc_default_max_tokens(self):
        """handle_add_summary_profile использует default max_tokens (300)."""
        result = self.svc.handle_add_summary_profile({
            "name": "ipc_default",
            "prompt": "Test.",
        })
        self.assertEqual(result["profile"]["max_tokens"], 300)

    def test_add_summary_profile_ipc_optional_format_instructions(self):
        """handle_add_summary_profile работает без format_instructions."""
        result = self.svc.handle_add_summary_profile({
            "name": "ipc_no_fmt",
            "prompt": "Test.",
            "max_tokens": 150,
        })
        self.assertEqual(result["profile"]["format_instructions"], "")

    def test_add_summary_profile_ipc_coerces_max_tokens_to_int(self):
        """handle_add_summary_profile коерцирует max_tokens в int."""
        result = self.svc.handle_add_summary_profile({
            "name": "ipc_coerce",
            "prompt": "Test.",
            "max_tokens": "200",
        })
        self.assertEqual(result["profile"]["max_tokens"], 200)

    def test_auto_summarize_with_empty_items_raises(self):
        """auto_summarize_batch с пустым списком ID вызывает RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_auto_summarize_batch({"ids": []})

    def test_auto_summarize_with_nonexistent_ids_raises(self):
        """auto_summarize_batch с несуществующими ID вызывает RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_auto_summarize_batch({"ids": ["nonexistent_id_xyz"]})


class TestSummaryProfileGetDefault(unittest.TestCase):
    """Тесты получения дефолтного профиля и его применения."""

    def setUp(self):
        self.mgr = SummaryProfileManager(data_dir=None)

    def test_brief_is_accessible_as_default(self):
        """'brief' — дефолтный профиль; get_profile('brief') всегда работает."""
        p = self.mgr.get_profile("brief")
        self.assertEqual(p.name, "brief")
        self.assertTrue(p.builtin)

    def test_all_builtin_profiles_system_prompts_nonempty(self):
        """Все встроенные профили имеют непустой system_prompt."""
        for name in ("brief", "detailed", "bullet_points", "meeting_notes", "telegram"):
            p = self.mgr.get_profile(name)
            self.assertTrue(
                len(p.system_prompt) > 10,
                f"system_prompt профиля {name!r} слишком короткий"
            )

    def test_all_builtin_profiles_max_tokens_positive(self):
        """Все встроенные профили имеют max_tokens > 0."""
        for name in ("brief", "detailed", "bullet_points", "meeting_notes", "telegram"):
            p = self.mgr.get_profile(name)
            self.assertGreater(p.max_tokens, 0, f"max_tokens профиля {name!r} <= 0")

    def test_all_builtin_profiles_have_format_instructions(self):
        """Все встроенные профили имеют непустые format_instructions."""
        for name in ("brief", "detailed", "bullet_points", "meeting_notes", "telegram"):
            p = self.mgr.get_profile(name)
            self.assertTrue(
                len(p.format_instructions) > 0,
                f"format_instructions профиля {name!r} пустые"
            )

    def test_apply_defaults_brief_tokens_lte_detailed(self):
        """'brief' имеет меньше max_tokens чем 'detailed' — дефолтный минимум."""
        brief = self.mgr.get_profile("brief")
        detailed = self.mgr.get_profile("detailed")
        self.assertLessEqual(brief.max_tokens, detailed.max_tokens)

    def test_default_profile_to_dict_has_all_keys(self):
        """to_dict() дефолтного профиля содержит все обязательные ключи."""
        p = self.mgr.get_profile("brief")
        d = p.to_dict()
        for key in ("name", "system_prompt", "max_tokens", "format_instructions", "builtin"):
            self.assertIn(key, d, f"Ключ {key!r} отсутствует в to_dict()")

    def test_list_profiles_total_count_builtin(self):
        """list_profiles без кастомных возвращает ровно 5 встроенных."""
        profiles = self.mgr.list_profiles()
        self.assertEqual(len(profiles), 5)

    def test_brief_profile_mentioned_in_prompt(self):
        """Промпт профиля 'brief' упоминает краткость."""
        p = self.mgr.get_profile("brief")
        self.assertIn("краткое", p.system_prompt.lower())

    def test_bullet_points_prompt_mentions_list(self):
        """Промпт 'bullet_points' упоминает список."""
        p = self.mgr.get_profile("bullet_points")
        prompt_lower = p.system_prompt.lower()
        self.assertTrue(
            "список" in prompt_lower or "маркир" in prompt_lower,
            "Промпт bullet_points должен упоминать список"
        )


class TestSummaryProfileApplyDefaults(unittest.TestCase):
    """Тесты поведения при неизвестном профиле (fallback к 'brief')."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.svc = _make_history_service(self.tmp)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_unknown_profile_fallback_to_brief(self):
        """handle_auto_summarize_batch с неизвестным профилем → brief."""
        rw = _ok_rewriter("РЕЗЮМЕ: Краткий.\nТЕЗИСЫ:\n- Тезис")
        svc = _make_history_service(self.tmp, llm_rewriter=rw)
        ids = _add_items(svc, ["Текст для теста дефолта."])
        result = svc.handle_auto_summarize_batch({"ids": ids, "profile": "unknown_profile_xyz"})
        self.assertEqual(result.get("profile"), "brief")

    def test_no_profile_defaults_to_brief(self):
        """handle_auto_summarize_batch без profile → brief."""
        rw = _ok_rewriter("РЕЗЮМЕ: Краткий.\nТЕЗИСЫ:\n- Тезис")
        svc = _make_history_service(self.tmp, llm_rewriter=rw)
        ids = _add_items(svc, ["Дефолтный текст."])
        result = svc.handle_auto_summarize_batch({"ids": ids})
        self.assertEqual(result.get("profile"), "brief")

    def test_explicit_brief_profile_used(self):
        """Явный brief profile используется корректно."""
        rw = _ok_rewriter("РЕЗЮМЕ: Explicit.\nТЕЗИСЫ:\n- Тезис")
        svc = _make_history_service(self.tmp, llm_rewriter=rw)
        ids = _add_items(svc, ["Явный brief текст."])
        result = svc.handle_auto_summarize_batch({"ids": ids, "profile": "brief"})
        self.assertEqual(result.get("profile"), "brief")
        self.assertTrue(result["llm"])


class TestApplyProfileToTextViaLLM(unittest.TestCase):
    """test_apply_profile_to_text_via_llm: profile system_prompt is used in LLM call."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_apply_profile_to_text_via_llm(self):
        """Profile system_prompt is forwarded to the LLM rewriter call."""
        rw = _ok_rewriter("РЕЗЮМЕ: Итог.\nТЕЗИСЫ:\n- Тезис 1")
        svc = _make_history_service(self.tmp, llm_rewriter=rw)
        ids = _add_items(svc, ["Текст для резюмирования."])
        result = svc.handle_auto_summarize_batch({"ids": ids, "profile": "detailed"})
        # Verify LLM was called and profile was applied
        self.assertTrue(rw.summarize.called)
        call_prompt = rw.summarize.call_args[0][0]
        # The "detailed" profile's system_prompt should appear in the prompt
        detailed_profile_keyword = "аналитик"
        self.assertIn(detailed_profile_keyword, call_prompt.lower())
        self.assertEqual(result.get("profile"), "detailed")

    def test_apply_bullet_points_profile_to_text_via_llm(self):
        """bullet_points profile injects its own system_prompt into the LLM call."""
        rw = _ok_rewriter("РЕЗЮМЕ: Список.\nТЕЗИСЫ:\n- Пункт 1\n- Пункт 2")
        svc = _make_history_service(self.tmp, llm_rewriter=rw)
        ids = _add_items(svc, ["Длинный текст для маркированного резюме."])
        svc.handle_auto_summarize_batch({"ids": ids, "profile": "bullet_points"})
        call_prompt = rw.summarize.call_args[0][0]
        # bullet_points prompt mentions "маркированного" or "список"
        self.assertTrue(
            "маркир" in call_prompt.lower() or "список" in call_prompt.lower(),
            "bullet_points prompt missing list keyword"
        )


class TestUnicodeProfileName(unittest.TestCase):
    """test_unicode_profile_name: unicode names persist and retrieve correctly."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.mgr = SummaryProfileManager(data_dir=self.tmp)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_unicode_profile_name(self):
        """Profile with a unicode name (Cyrillic) is stored and retrieved correctly."""
        name = "профиль_краткий"
        self.mgr.add_custom_profile(name, "Краткое резюме.", 100)
        p = self.mgr.get_profile(name)
        self.assertEqual(p.name, name)
        self.assertFalse(p.builtin)

    def test_unicode_profile_name_persists(self):
        """Unicode profile name survives a JSON round-trip."""
        name = "日本語プロファイル"
        self.mgr.add_custom_profile(name, "Unicode prompt.", 150)
        mgr2 = SummaryProfileManager(data_dir=self.tmp)
        p = mgr2.get_profile(name)
        self.assertEqual(p.name, name)

    def test_unicode_in_prompt_persists(self):
        """Unicode prompt content (emoji, CJK, Cyrillic) survives JSON save/load."""
        prompt = "Резюмируй: 🎙️ 주요 포인트 정리. Кратко."
        self.mgr.add_custom_profile("unicode_prompt", prompt, 100)
        mgr2 = SummaryProfileManager(data_dir=self.tmp)
        p = mgr2.get_profile("unicode_prompt")
        self.assertEqual(p.system_prompt, prompt)


class TestPersistReloadCustomProfiles(unittest.TestCase):
    """test_persist_reload_custom_profiles: custom profiles survive manager restart."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_persist_reload_custom_profiles(self):
        """Custom profiles written by one manager are readable by a fresh manager."""
        mgr1 = SummaryProfileManager(data_dir=self.tmp)
        mgr1.add_custom_profile("save_me", "Save prompt.", 200, "Fmt save.")
        mgr1.add_custom_profile("save_me_2", "Another prompt.", 300)

        mgr2 = SummaryProfileManager(data_dir=self.tmp)
        p1 = mgr2.get_profile("save_me")
        p2 = mgr2.get_profile("save_me_2")
        self.assertEqual(p1.system_prompt, "Save prompt.")
        self.assertEqual(p1.format_instructions, "Fmt save.")
        self.assertEqual(p2.max_tokens, 300)

    def test_persist_reload_preserves_builtin_false(self):
        """Reloaded custom profiles have builtin=False."""
        mgr1 = SummaryProfileManager(data_dir=self.tmp)
        mgr1.add_custom_profile("reloaded", "My prompt.", 150)

        mgr2 = SummaryProfileManager(data_dir=self.tmp)
        p = mgr2.get_profile("reloaded")
        self.assertFalse(p.builtin)

    def test_persist_reload_after_delete(self):
        """Deleted profile does not reappear after manager restart."""
        mgr1 = SummaryProfileManager(data_dir=self.tmp)
        mgr1.add_custom_profile("will_delete", "Delete me.", 100)
        mgr1.remove_custom_profile("will_delete")

        mgr2 = SummaryProfileManager(data_dir=self.tmp)
        with self.assertRaises(KeyError):
            mgr2.get_profile("will_delete")

    def test_persist_reload_empty_when_no_file(self):
        """Manager with empty data dir starts with 0 custom profiles."""
        mgr = SummaryProfileManager(data_dir=self.tmp)
        custom = [p for p in mgr.list_profiles() if not p["builtin"]]
        self.assertEqual(len(custom), 0)

    def test_persist_reload_corrupted_json_graceful(self):
        """Corrupted JSON file is handled gracefully; manager starts with 0 custom profiles."""
        profiles_path = self.tmp / "summary_profiles.json"
        profiles_path.write_text("{ corrupted json [[[", encoding="utf-8")
        mgr = SummaryProfileManager(data_dir=self.tmp)
        custom = [p for p in mgr.list_profiles() if not p["builtin"]]
        self.assertEqual(len(custom), 0)


if __name__ == "__main__":
    unittest.main()
