"""Реальные settings/cache/translator в одноразовом процессе; не импортирует service."""

from __future__ import annotations

import ast
import asyncio
import copy
import json
import logging
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
LOCAL_MODULES = {
    "backend", "core", "backend.translator", "backend.translation_cache",
    "backend.settings_service", "backend.state_store", "backend.models",
    "backend.settings_backup", "backend.settings_validator", "backend.ipc_errors",
    "backend.observability", "backend.plaintext_export_authorization",
    "backend.plaintext_export_redaction", "backend.performance_profiler",
    "backend.history_crypto", "core.config", "core.parsing_utils", "core.search_index",
    "core.gigaam_compat", "core.stt_budget", "core.atomic_io",
}
MODEL_MODULES = {
    "mlx", "mlx_whisper", "torch", "tensorflow", "transformers", "huggingface_hub",
    "argostranslate", "pyannote", "sounddevice", "pyaudio", "sentry_sdk",
    "requests", "httpx", "keyring",
}


def install_guards(scratch):
    """Границы действуют ДО импорта компонентов; даже пойманный denial ломает gate."""
    violations = []

    def deny(label):
        violations.append(label)
        raise PermissionError(label)

    class ImportBoundary:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in MODEL_MODULES:
                deny("forbidden provider import: " + fullname)
            if fullname.startswith(("backend", "core")) and fullname not in LOCAL_MODULES:
                deny("unreviewed local import: " + fullname)
            return None

    sys.meta_path.insert(0, ImportBoundary())

    def inside(path):
        return Path(os.fsdecode(path)).resolve().is_relative_to(scratch)

    def audit(event, args):
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.fork"} or event.startswith("socket."):
            deny(event)
        if event == "sqlite3.connect" and not inside(args[0]):
            deny("non-scratch sqlite")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path, mode, flags = args
            write = (isinstance(mode, str) and any(c in mode for c in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            if write and not inside(path):
                deny("non-scratch write")
            if Path(os.fsdecode(path)).name in {".env", ".secrets", "settings.json", "translation_cache.json"} and not inside(path):
                deny("non-scratch configuration read")
        if event in {"os.mkdir", "os.remove", "os.rmdir"} and not inside(args[0]):
            deny("non-scratch mutation")
        if event == "os.rename" and not all(inside(p) for p in args[:2]):
            deny("non-scratch rename")

    sys.addaudithook(audit)
    threading.Thread.start = lambda *a, **k: deny("thread.start")
    import _thread
    _thread.start_new_thread = lambda *a, **k: deny("thread.start_new_thread")
    return violations


def component_suite(scratch):
    # Все импорты реальны. HOME/cwd/env и guards выставлены вызывающим ДО этой точки.
    from backend.state_store import StateStore, StateStoreSettingsCorruptError
    from backend.settings_service import SettingsService
    import backend.settings_service as settings_module
    from backend.settings_backup import SettingsBackup
    from backend.translation_cache import TranslationCache, _make_key
    from backend.translator import Translator
    from types import SimpleNamespace

    tree = ast.parse((ROOT / "backend/service.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BackendService")
    names = {"_init_translation_client", "_get_runtime_setting", "_cached_settings", "_invalidate_settings_cache"}
    methods = [copy.deepcopy(n) for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in methods} == names
    namespace = dict(Translator=Translator, TranslationCache=TranslationCache,
                     logger=logging.getLogger("component-owner"))
    # Только actual owner methods: компоненты/их constructors НЕ AST-копии.
    module = ast.Module(body=[ast.parse("from __future__ import annotations").body[0], *methods], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(ROOT / "backend/service.py"), "exec"), namespace)
    Owner = type("TranslationOwner", (), {name: namespace[name] for name in names})

    class Components(unittest.TestCase):
        def setUp(self):
            self.directory = scratch / self._testMethodName
            self.store = StateStore(self.directory)
            self.store.save_settings({"privacy_mode_enabled": False, "history_encryption_enabled": False,
                                      "translation_mode": "ru_to_es"})
            self.owner = self.make_owner(self.store)
            self.calls = []
            self.effect = None

            def transport(text):
                self.assertIs(self.owner.translator._settings_getter.__self__, self.owner)
                self.calls.append(text)
                if self.effect is not None:
                    raise self.effect
                return [{"translation_text": "hola fixture"}]

            self.factory = patch.object(Translator, "_build_pipeline", return_value=transport)
            self.factory.start()
            self.addCleanup(self.factory.stop)

        def make_owner(self, store):
            owner = Owner()
            owner._settings_svc = SettingsService(store, backup=SettingsBackup(scratch / "backups"))
            owner._init_translation_client(store, None)
            self.addCleanup(owner._translation_cache.close)
            return owner

        def translate(self, text="fixture text"):
            return self.owner.translator.translate(text, "ru_to_es", "offline_strict")

        def save_privacy(self, value):
            saved = self.store.load_settings()
            saved["privacy_mode_enabled"] = value
            self.store.save_settings(saved)

        def test_saved_getter_before_request(self):
            self.assertEqual(self.owner._get_runtime_setting("translation_mode", "off"), "ru_to_es")
            self.assertIs(self.owner.translator._translation_cache, self.owner._translation_cache)
            self.assertTrue(self.translate().ok)
            self.assertEqual(self.calls, ["fixture text"])

        def test_disk_reopen_reuses_real_cache_without_transport(self):
            self.assertTrue(self.translate().ok)
            self.assertTrue(self.translate().ok)
            self.assertEqual(len(self.calls), 1)
            payload = json.loads((self.directory / "translation_cache.json").read_text())
            self.assertEqual(len(payload), 1)
            self.owner = self.make_owner(StateStore(self.directory))
            result = self.translate()
            self.assertTrue(result.ok)
            self.assertTrue(result.engine.endswith("_cached"))
            self.assertEqual(len(self.calls), 1)

        def test_settings_ttl_and_explicit_invalidation(self):
            now = [1000.0]
            with patch.object(settings_module, "time", SimpleNamespace(monotonic=lambda: now[0])):
                self.assertFalse(self.owner._get_runtime_setting("privacy_mode_enabled", True))
                self.save_privacy(True)
                now[0] += 4.9
                self.assertFalse(self.owner._get_runtime_setting("privacy_mode_enabled", True))
                now[0] += 0.2
                self.assertTrue(self.owner._get_runtime_setting("privacy_mode_enabled", False))
                self.save_privacy(False)
                self.assertTrue(self.owner._get_runtime_setting("privacy_mode_enabled", False))
                self.owner._invalidate_settings_cache()
                self.assertFalse(self.owner._get_runtime_setting("privacy_mode_enabled", True))
                self.assertFalse(StateStore(self.directory).load_settings()["privacy_mode_enabled"])

        def test_privacy_transition_clears_ram_and_reopened_disk(self):
            self.assertTrue(self.translate().ok)
            self.save_privacy(True)
            self.owner._invalidate_settings_cache()
            self.assertEqual(self.translate("").status, "empty_text")
            self.assertFalse(self.owner.translator._cache)
            self.assertEqual(TranslationCache(str(self.directory)).get_stats()["entries"], 0)
            self.assertEqual(json.loads((self.directory / "translation_cache.json").read_text()), {})

        def test_transport_error_is_not_success_or_persistent_entry(self):
            self.effect = RuntimeError("fixture transport failed")
            result = self.translate()
            self.assertFalse(result.ok)
            self.assertEqual(result.status, "translate_error")
            self.assertEqual(TranslationCache(str(self.directory)).get_stats()["entries"], 0)

        def test_cancellation_propagates_without_cache_success(self):
            self.effect = asyncio.CancelledError("fixture cancelled")
            with self.assertRaises(asyncio.CancelledError):
                self.translate()
            self.assertFalse(self.owner.translator._cache)
            self.assertEqual(TranslationCache(str(self.directory)).get_stats()["entries"], 0)

        def test_failed_settings_commit_keeps_persisted_value(self):
            before = self.store.settings_path.read_bytes()
            with patch("core.atomic_io.os.replace", side_effect=OSError("fixture disk failure")):
                with self.assertRaises(OSError):
                    self.save_privacy(True)
            self.assertEqual(self.store.settings_path.read_bytes(), before)
            self.owner._invalidate_settings_cache()
            self.assertFalse(self.owner._get_runtime_setting("privacy_mode_enabled", True))

        def test_corrupt_settings_fail_closed_and_clear_translation_cache(self):
            self.assertTrue(self.translate().ok)
            self.store.settings_path.write_text("{broken", encoding="utf-8")
            self.owner._invalidate_settings_cache()
            self.assertTrue(self.owner._get_runtime_setting("privacy_mode_enabled", False))
            self.assertEqual(self.translate("").status, "empty_text")
            self.assertFalse(self.owner.translator._cache)
            self.assertEqual(TranslationCache(str(self.directory)).get_stats()["entries"], 0)
            with self.assertRaises(StateStoreSettingsCorruptError):
                self.save_privacy(False)

        def test_non_string_persistent_entry_is_a_miss(self):
            key = _make_key("fixture text", "ru_to_es", "neutral", "persistent")
            cache_path = self.directory / "translation_cache.json"
            for index, value in enumerate((17, 1.5, False, [], {}, None), start=1):
                with self.subTest(value=value):
                    cache_path.write_text(json.dumps({key: value}), encoding="utf-8")
                    self.owner = self.make_owner(StateStore(self.directory))
                    self.assertEqual(self.owner._translation_cache.get_stats()["entries"], 0)
                    self.assertTrue(self.translate().ok)
                    self.assertEqual(len(self.calls), index)
            cache_path.write_text(json.dumps({key: "valid", "bad": 17}), encoding="utf-8")
            limited = TranslationCache(str(self.directory), max_entries=1)
            self.assertEqual(limited.get("fixture text", "ru_to_es", "neutral", "persistent"), "valid")
            self.assertEqual(limited.get_stats()["entries"], 1)

    return unittest.defaultTestLoader.loadTestsFromTestCase(Components)


def main():
    scratch = Path(sys.argv[1]).resolve()
    assert Path.home() == scratch / "home" and Path.cwd() == scratch
    assert "backend.service" not in sys.modules
    violations = install_guards(scratch)
    sys.path.insert(0, str(ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(component_suite(scratch))
    loaded = sorted(name for name in sys.modules if name.startswith(("backend", "core")))
    receipt = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                   skipped=len(result.skipped), violations=violations, local_modules=loaded,
                   threads=len(threading.enumerate()), backend_service_imported="backend.service" in sys.modules)
    print("COMPONENT_RECEIPT=" + json.dumps(receipt), flush=True)
    return 0 if result.wasSuccessful() and not violations and len(threading.enumerate()) == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
