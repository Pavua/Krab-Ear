"""Actual constructor prefix + actual TranslationService, без full service import."""
import ast
import copy
import importlib.util
import json
import logging
from pathlib import Path
import shutil
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def main():
    scratch = Path(sys.argv[1]).resolve()
    assert Path.home() == scratch / "home" and Path.cwd() == scratch
    spec = importlib.util.spec_from_file_location("component_guards", ROOT / "tests/translation_components_probe.py")
    guards = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guards)
    guards.LOCAL_MODULES.update({
        "backend.translation_service", "core.language_detector", "backend.vocabulary_store",
        "backend.text_snippet_service", "backend.phonetic_vocab_service", "backend.plaintext_export_ipc",
        "backend.live_subs_service", "backend.event_bus", "core.utils",
    })
    violations = guards.install_guards(scratch)
    sys.path.insert(0, str(ROOT))
    from backend.state_store import StateStore
    from backend.settings_service import SettingsService
    from backend.translator import Translator
    from backend.translation_cache import TranslationCache
    from backend.translation_service import TranslationService
    from backend.vocabulary_store import VocabularyStore
    from backend.text_snippet_service import TextSnippetService
    from backend.phonetic_vocab_service import PhoneticVocabService
    from backend.plaintext_export_authorization import PlaintextExportAuthorizer
    from backend.plaintext_export_ipc import PlaintextExportIPC
    from backend.live_subs_service import LiveSubsService
    import core.config as config

    # Единственная импортируемая заглушка — STT owner, не запускающий engine.
    class CallSTTBoundary:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    stt_module = ModuleType("backend.call_stt_service")
    stt_module.CallSTTService = CallSTTBoundary
    sys.modules[stt_module.__name__] = stt_module

    def forbidden(*args, **kwargs):
        violations.append("unexpected constructor external dependency")
        raise PermissionError(violations[-1])

    tree = ast.parse((ROOT / "backend/service.py").read_text(encoding="utf-8"))
    source_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BackendService")
    constructor = next(n for n in source_class.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")

    def assigns(statement, name):
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target] if isinstance(statement, ast.AnnAssign) else []
        return any(isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                   and t.value.id == "self" and t.attr == name for t in targets)

    cut = next(i for i, n in enumerate(constructor.body) if assigns(n, "_start_time"))
    prefix = copy.deepcopy(constructor)
    prefix.body = prefix.body[:cut]
    # Actual assignment from later constructor, not a handwritten rewiring.
    for field in ("_live_subs", "_translation"):
        prefix.body.append(copy.deepcopy(next(n for n in constructor.body if assigns(n, field))))
    methods = {"_init_local_text_stores", "_init_translation_client", "_get_runtime_setting",
               "_cached_settings", "_invalidate_settings_cache"}
    extracted = [copy.deepcopy(n) for n in source_class.body if isinstance(n, ast.FunctionDef) and n.name in methods]
    assert len(extracted) == len(methods)
    warmup = copy.deepcopy(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "llm_warmup_needed"))
    namespace = dict(SettingsService=SettingsService, Translator=Translator, TranslationCache=TranslationCache,
                     TranslationService=TranslationService, LiveSubsService=LiveSubsService, VocabularyStore=VocabularyStore,
                     TextSnippetService=TextSnippetService, PhoneticVocabService=PhoneticVocabService,
                     PlaintextExportAuthorizer=PlaintextExportAuthorizer, PlaintextExportIPC=PlaintextExportIPC,
                     logger=logging.getLogger("aggregate-owner"), threading=threading,
                     AudioRecorder=forbidden, Transcriber=forbidden, event_bus=SimpleNamespace(emit=forbidden))
    module = ast.Module(body=[ast.parse("from __future__ import annotations").body[0], warmup, prefix, *extracted], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(ROOT / "backend/service.py"), "exec"), namespace)
    Owner = type("ActualPrefixOwner", (), {name: namespace[name] for name in methods | {"__init__"}})
    Owner._init_llm_rewriter = lambda self: None
    Owner._init_action_items_extractor = lambda self: None

    class Wiring(unittest.TestCase):
        def setUp(self):
            self.directory = scratch / "home/Library/Application Support/KrabEar"
            shutil.rmtree(self.directory, ignore_errors=True)
            self.store = StateStore(self.directory)
            self.assertEqual(config._SETTINGS_JSON_FILE, self.store.settings_path)
            self.store.save_settings(dict(privacy_mode_enabled=False, history_encryption_enabled=False,
                                          network_mode="offline_strict", translation_mode="ru_to_es",
                                          rewriter_warmup_on_startup=False))
            self.calls = []

            def transport(text):
                self.calls.append(text)
                return [{"translation_text": "hola fixture"}]

            mock = patch.object(Translator, "_build_pipeline", return_value=transport)
            mock.start()
            self.addCleanup(mock.stop)
            self.owner = self.reopen()

        def reopen(self):
            owner = Owner(StateStore(self.directory), recorder=SimpleNamespace(),
                          transcriber=SimpleNamespace(engine=SimpleNamespace(_router="fixture-router")))
            self.addCleanup(owner._translation_cache.close)
            self.addCleanup(owner._live_subs.close)
            return owner

        def text(self, text="fixture text"):
            return self.owner._translation.handle_translate_text(dict(text=text, translation_mode="ru_to_es"))

        def test_actual_prefix_providers_and_shared_service_identity(self):
            owner = self.owner
            owner.vocabulary.add_words(["fixtureword"])
            owner._text_snippet_svc.handle_add_text_snippet(dict(trigger="fixture", expansion="expanded fixture"))
            owner._phonetic_vocab_svc.handle_add_phonetic_entry(dict(canonical="fixture", variants=["fixtur"]))
            self.assertIn("fixtureword", VocabularyStore(self.directory).load())
            self.assertEqual(owner.transcriber.engine._snippets_provider(), owner._text_snippet_svc.get_snippets())
            self.assertEqual(owner.transcriber.engine._phonetic_provider(), owner._phonetic_vocab_svc.get_entries())
            self.assertIs(owner._translation.translator, owner.translator)
            self.assertIs(owner._translation._settings_svc, owner._settings_svc)
            self.assertIs(owner._translation.store, owner.store)
            self.assertIs(owner._translation._vocabulary_store, owner.vocabulary)
            self.assertIs(owner.translator._settings_getter.__self__, owner)
            self.assertIs(owner.translator._translation_cache, owner._translation_cache)
            self.assertEqual(owner._call_stt.kwargs["router"], "fixture-router")
            self.assertFalse(owner._call_stt.kwargs["privacy_mode_fn"]())

        def test_real_handler_cache_reopen_avoids_transport(self):
            first = self.text()
            self.assertEqual(first["text"], "hola fixture")
            self.assertEqual(first["status"], "ok")
            self.owner._translation_cache.close()
            self.owner = self.reopen()
            second = self.text()
            self.assertEqual(second["text"], first["text"])
            self.assertTrue(second["engine"].endswith("_cached"))
            self.assertEqual(self.calls, ["fixture text"])

        def test_selection_and_real_glossary_settings_save(self):
            result = self.owner._translation.handle_translate_selection(dict(text="Привет мир"))
            self.assertEqual(result["source_lang_detected"], "ru")
            self.assertEqual(result["target_lang"], "es")
            self.assertEqual(result["translated_text"], "hola fixture")
            saved = self.owner._translation.handle_set_translation_glossary_item(dict(source="fixture", target="ejemplo"))
            self.assertTrue(saved["updated"])
            self.assertEqual(StateStore(self.directory).load_settings()["translation_glossary"], {"fixture": "ejemplo"})
            self.owner = self.reopen()
            self.assertEqual(self.owner._cached_settings()["translation_glossary"], {"fixture": "ejemplo"})

        def test_privacy_commit_and_corruption_seen_by_real_handler(self):
            self.assertEqual(self.text()["status"], "ok")
            self.owner._settings_svc.handle_set_settings(dict(privacy_mode_enabled=True))
            self.assertTrue(StateStore(self.directory).load_settings()["privacy_mode_enabled"])
            self.assertEqual(self.text("")["status"], "empty_text")
            self.assertFalse(self.owner.translator._cache)
            self.assertEqual(json.loads((self.directory / "translation_cache.json").read_text()), {})
            self.store.settings_path.write_text("{broken", encoding="utf-8")
            self.owner._invalidate_settings_cache()
            self.assertTrue(self.owner._get_runtime_setting("privacy_mode_enabled", False))
            self.assertTrue(self.owner._translation._is_privacy_mode())

        def test_live_subs_real_idle_wiring_and_privacy_gate(self):
            subs = self.owner._live_subs
            self.assertIs(subs._translator, self.owner.translator)
            self.assertIs(subs._transcriber, self.owner.transcriber)
            self.assertIs(subs._settings_get.__self__, self.owner)
            self.owner._settings_svc.handle_set_settings(dict(privacy_mode_enabled=True))
            self.assertEqual(subs.handle_ingest({}), dict(ok=True, skipped=True, reason="privacy_mode_active"))
            self.assertIsNone(subs._worker_thread)
            self.assertTrue(subs.close())
            self.assertTrue(subs.close())

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Wiring))
    receipt = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                   skipped=len(result.skipped), violations=violations, threads=threading.active_count(),
                   backend_service_imported="backend.service" in sys.modules,
                   prefix_statements=cut, full_startup=False, ipc_transport=False)
    print("COMPONENT_RECEIPT=" + json.dumps(receipt), flush=True)
    return 0 if result.wasSuccessful() and not result.skipped and not violations and threading.active_count() == 1 else 1


if __name__ == "__main__":
    sys.exit(main())
