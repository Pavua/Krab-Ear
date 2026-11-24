"""Ревью #2229, F10: регресс-тесты на фиксы, мутации которых раньше выживали при зелёных тестах.

Каждый тест краснеет при откате соответствующего фикса (проверено мутацией):
  R01      policy_blocks fail-closed;
  M04-09   get_storage_breakdown не раскрывает объёмы в privacy;
  M04-11   LLM-summary в Obsidian-экспорте — opt-in (структурный AST-тест);
  M05-01   lock-файл реестра памяти: симлинк и hardlink отвергаются;
  M05-03   resurrection: purge между чтением NPZ и присваиванием не оживляет старый индекс;
  M05-14   PasteAppMemory.record: откат при сбое записи (включая вытесненную LRU-запись);
  M05-17   ContextMemory(window_size<1) отвергается.
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class PolicyBlocksFailClosedTests(unittest.TestCase):
    def test_missing_reader_blocks(self) -> None:
        from backend.history_encryption_policy import policy_blocks
        self.assertIs(policy_blocks(None), True)

    def test_reader_exception_blocks(self) -> None:
        from backend.history_encryption_policy import policy_blocks

        def boom() -> bool:
            raise OSError("keychain недоступен")

        self.assertIs(policy_blocks(boom), True)

    def test_non_bool_reader_value_blocks(self) -> None:
        from backend.history_encryption_policy import policy_blocks
        for garbage in (1, 0, "false", None, [], object()):
            with self.subTest(garbage=garbage):
                self.assertIs(policy_blocks(lambda g=garbage: g), True)

    def test_real_bool_passes_through(self) -> None:
        from backend.history_encryption_policy import policy_blocks
        self.assertIs(policy_blocks(lambda: False), False)
        self.assertIs(policy_blocks(lambda: True), True)


class StorageBreakdownPrivacyGateTests(unittest.TestCase):
    def _call(self, privacy: bool):
        from backend.service import BackendService

        store = MagicMock()
        store.get_storage_breakdown.return_value = {"total_mb": 123.4}
        fake_self = SimpleNamespace(_history=SimpleNamespace(_is_privacy_mode=lambda: privacy), store=store)
        return BackendService._handle_get_storage_breakdown(fake_self, {}), store

    def test_privacy_on_hides_sizes_and_never_touches_store(self) -> None:
        result, store = self._call(True)
        self.assertEqual(result["reason"], "privacy_mode_active")
        self.assertEqual(result["total_mb"], 0.0)
        store.get_storage_breakdown.assert_not_called()

    def test_privacy_off_returns_real_breakdown(self) -> None:
        result, store = self._call(False)
        self.assertEqual(result, {"total_mb": 123.4})
        store.get_storage_breakdown.assert_called_once()


class ObsidianLlmSummaryOptInTests(unittest.TestCase):
    def test_include_llm_summary_defaults_to_false(self) -> None:
        """Структурно (AST): транскрипты не уходят внешнему LLM без явного include_llm_summary."""
        src = (PROJECT_ROOT / "backend" / "history_service.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        func = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "handle_export_obsidian"
        )
        assigns = [
            n for n in ast.walk(func)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "include_llm_summary" for t in n.targets)
        ]
        self.assertEqual(len(assigns), 1)
        call = assigns[0].value
        self.assertIsInstance(call, ast.Call)
        defaults = {kw.arg: kw.value for kw in call.keywords}
        self.assertIn("default", defaults)
        self.assertIsInstance(defaults["default"], ast.Constant)
        self.assertIs(defaults["default"].value, False)


class LedgerLockSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

    def _client(self):
        from backend.memory_ledger import LedgerClient
        return LedgerClient("test", path=self.root / "ledger.json")

    def test_symlinked_lock_file_is_refused_not_followed(self) -> None:
        client = self._client()
        victim = self.root / "victim.txt"
        victim.write_text("чужой файл", encoding="utf-8")
        client._lock_path.symlink_to(victim)
        with self.assertRaises(OSError):
            client._acquire(nowait=True)
        self.assertEqual(victim.read_text(encoding="utf-8"), "чужой файл")

    def test_hardlinked_lock_file_is_refused(self) -> None:
        from backend.memory_ledger import UnsafeLedgerPath

        client = self._client()
        client._lock_path.write_text("", encoding="utf-8")
        os.link(client._lock_path, self.root / "second_name")
        with self.assertRaises(UnsafeLedgerPath):
            client._acquire(nowait=True)

    def test_normal_lock_file_is_acquired(self) -> None:
        client = self._client()
        fd = client._acquire(nowait=True)
        try:
            self.assertGreater(fd, 2)
        finally:
            os.close(fd)


class LoadFromDiskResurrectionTests(unittest.TestCase):
    """M05-03: purge, случившийся МЕЖДУ чтением NPZ и присваиванием, не оживляет индекс."""

    def test_purge_during_snapshot_read_aborts_load(self) -> None:
        import numpy as np
        from backend.semantic_search import SemanticSearcher

        with tempfile.TemporaryDirectory() as td:
            def make() -> SemanticSearcher:
                s = SemanticSearcher(data_dir=Path(td), model_name="t", enabled=True, max_items=0)
                s._model = object()
                s._model_loaded = True
                s._encode = lambda model, text, prefix="query: ": np.ones(4, dtype=np.float32)
                return s

            writer = make()
            self.assertTrue(writer.index_item("secret", "секретный текст"))
            self.assertTrue(writer._snapshot_path.exists())

            reader = make()
            real_load = np.load

            def load_then_purge(*args, **kwargs):
                data = real_load(*args, **kwargs)
                reader.purge_all()  # purge строго между чтением и присваиванием
                return data

            with patch("numpy.load", load_then_purge):
                reader._load_from_disk()

            self.assertEqual(reader._index, [])
            self.assertIsNone(reader._embeddings)


class PasteAppMemoryRollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

    def test_failed_save_rolls_back_new_entry(self) -> None:
        from backend.paste_app_memory import PasteAppMemory, VALID_PROFILES

        mem = PasteAppMemory(self.root)
        profile = sorted(VALID_PROFILES)[0]
        with patch.object(mem, "_save", return_value=False):
            self.assertFalse(mem.record("com.example.app", profile))
        self.assertNotIn("com.example.app", mem._data)

    def test_failed_save_restores_previous_profile(self) -> None:
        from backend.paste_app_memory import PasteAppMemory, VALID_PROFILES

        mem = PasteAppMemory(self.root)
        first, second = sorted(VALID_PROFILES)[:2]
        self.assertTrue(mem.record("com.example.app", first))
        with patch.object(mem, "_save", return_value=False):
            self.assertFalse(mem.record("com.example.app", second))
        self.assertEqual(mem._data["com.example.app"]["profile"], first)

    def test_failed_save_restores_lru_evicted_entry(self) -> None:
        """Ревью #2229 F9: вытесненная при записи старейшая запись не должна пропасть."""
        from backend import paste_app_memory as pam

        mem = pam.PasteAppMemory(self.root)
        profile = sorted(pam.VALID_PROFILES)[0]
        mem._data = {f"com.app{i}": {"profile": profile, "last_used": f"2026-01-{(i % 28) + 1:02d}T00:00:00+00:00"}
                     for i in range(pam.MAX_BUNDLE_IDS)}
        oldest = min(mem._data, key=lambda k: mem._data[k]["last_used"])
        before = {k: dict(v) for k, v in mem._data.items()}
        with patch.object(mem, "_save", return_value=False):
            self.assertFalse(mem.record("com.new.app", profile))
        self.assertIn(oldest, mem._data)
        self.assertNotIn("com.new.app", mem._data)
        self.assertEqual(mem._data, before)


class ContextMemoryWindowGuardTests(unittest.TestCase):
    def test_window_size_below_one_is_rejected(self) -> None:
        from core.context_memory import ContextMemory
        for bad in (0, -1, -50):
            with self.subTest(window_size=bad):
                with self.assertRaises(ValueError):
                    ContextMemory(window_size=bad)

    def test_window_size_one_is_accepted(self) -> None:
        from core.context_memory import ContextMemory
        ContextMemory(window_size=1)


if __name__ == "__main__":
    unittest.main()
