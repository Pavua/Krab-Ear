"""Узкий actual-AST контракт; backend, модели, диск кэша и потоки не запускаются."""

from __future__ import annotations

import ast
import copy
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = "_init_translation_client"
BODY = """
self.translator = translator or Translator()
self._translation_cache = TranslationCache(data_dir=str(store.data_dir))
self.translator._translation_cache = self._translation_cache
self.translator._settings_getter = self._get_runtime_setting
"""


def definition(tree, name):
    matches = [n for n in tree.body if getattr(n, "name", None) == name]
    assert len(matches) == 1, name
    return matches[0]


def dump(node):
    return ast.dump(node, include_attributes=False)


def contract(tree):
    cls = definition(tree, "BackendService")
    init = definition(cls, "__init__")
    # Прежние соседние statements остаются прямыми, без recursive walker.
    start = next(i for i, n in enumerate(init.body)
                 if dump(n) == dump(ast.parse(
                     'if hasattr(self.transcriber, "engine"):\n'
                     '    self.transcriber.engine._phonetic_provider = self._phonetic_vocab_svc.get_entries'
                 ).body[0]))
    end = next(i for i, n in enumerate(init.body)
               if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Attribute)
               and n.target.attr == "_start_time")
    nodes = init.body[start + 1:end]
    helpers = [n for n in cls.body if getattr(n, "name", None) == HELPER]
    if helpers:
        assert len(helpers) == 1
        helper = helpers[0]
        signature = ast.parse(
            f'def {HELPER}(self, store: StateStore, translator: Translator | None) -> None: pass'
        ).body[0]
        assert dump(helper.args) == dump(signature.args)
        assert dump(helper.returns) == dump(signature.returns)
        assert not helper.decorator_list
        assert [dump(n) for n in nodes] == [dump(ast.parse(f'self.{HELPER}(store, translator)').body[0])]
        calls = [n for n in ast.walk(cls) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == HELPER]
        assert len(calls) == 1
        body = helper.body
    else:
        body = nodes
    assert [dump(n) for n in body] == [dump(n) for n in ast.parse(BODY).body]
    return nodes, helpers


def probe(tree, supplied=None, fail=None):
    events = []

    class Service:
        def _get_runtime_setting(self, key):
            return key

    service = Service()
    cache = object()

    class Client:
        def __init__(self, truth=True):
            object.__setattr__(self, "truth", truth)

        def __bool__(self):
            events.append("bool")
            return self.truth

        def __setattr__(self, name, value):
            events.append(name)
            if fail == name:
                raise ValueError(name)
            object.__setattr__(self, name, value)

    default = Client()
    client = None if supplied is None else Client(supplied)

    class Directory:
        def __str__(self):
            events.append("str")
            if fail == "str":
                raise ValueError("str")
            return "fixture-only"

    class Store:
        @property
        def data_dir(self):
            events.append("data_dir")
            if fail == "data_dir":
                raise ValueError("data_dir")
            return Directory()

    def translator_factory():
        events.append("translator")
        if fail == "translator":
            raise ValueError("translator")
        return default

    def cache_factory(*, data_dir):
        events.append(("cache", data_dir))
        if fail == "cache":
            raise ValueError("cache")
        return cache

    nodes, helpers = contract(tree)
    ns = dict(self=service, store=Store(), translator=client,
              Translator=translator_factory, TranslationCache=cache_factory)
    future = ast.parse("from __future__ import annotations").body[0]
    module = ast.fix_missing_locations(ast.Module(
        body=[future, *copy.deepcopy(helpers)], type_ignores=[]))
    exec(compile(module, "<translation-helper>", "exec"), ns)
    if helpers:
        setattr(service, HELPER, ns[HELPER].__get__(service))
    error = None
    try:
        exec(compile(ast.fix_missing_locations(ast.Module(
            body=copy.deepcopy(nodes), type_ignores=[])), "<translation-init>", "exec"), ns)
    except ValueError as exc:
        error = exc
    return service, client, default, cache, events, error


class TranslationConstructorContract(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse((ROOT / "backend/service.py").read_text(encoding="utf-8"))

    def test_reviewed_helper_is_connected(self):
        _, helpers = contract(self.tree)
        self.assertEqual(len(helpers), 1, "reviewed translation helper extraction is required")

    def test_default_client_and_cache_identity(self):
        service, _, default, cache, events, error = probe(self.tree)
        self.assertIsNone(error)
        self.assertIs(service.translator, default)
        self.assertIs(service._translation_cache, cache)
        self.assertIs(default._translation_cache, cache)
        self.assertIs(default._settings_getter.__self__, service)
        self.assertIs(default._settings_getter.__func__, type(service)._get_runtime_setting)
        self.assertEqual(events, ["translator", "data_dir", "str", ("cache", "fixture-only"),
                                  "_translation_cache", "_settings_getter"])

    def test_injected_truthy_client_preserved(self):
        service, client, _, _, events, error = probe(self.tree, supplied=True)
        self.assertIsNone(error)
        self.assertIs(service.translator, client)
        self.assertEqual(events[0], "bool")
        self.assertNotIn("translator", events)

    def test_falsey_client_uses_default(self):
        service, client, default, _, events, error = probe(self.tree, supplied=False)
        self.assertIsNone(error)
        self.assertIs(service.translator, default)
        self.assertIsNot(service.translator, client)
        self.assertEqual(events[:2], ["bool", "translator"])

    def test_factory_failure_leaves_translator_unassigned(self):
        service, _, _, _, events, error = probe(self.tree, fail="translator")
        self.assertEqual(str(error), "translator")
        self.assertFalse(hasattr(service, "translator"))
        self.assertEqual(events, ["translator"])

    def test_cache_input_and_factory_failures_preserve_partial_state(self):
        for fail in ("data_dir", "str", "cache"):
            with self.subTest(fail=fail):
                service, _, default, _, _, error = probe(self.tree, fail=fail)
                self.assertEqual(str(error), fail)
                self.assertIs(service.translator, default)
                self.assertFalse(hasattr(service, "_translation_cache"))
                self.assertFalse(hasattr(default, "_settings_getter"))

    def test_setter_failures_do_not_rollback_cache(self):
        for fail in ("_translation_cache", "_settings_getter"):
            with self.subTest(fail=fail):
                service, _, default, cache, _, error = probe(self.tree, fail=fail)
                self.assertEqual(str(error), fail)
                self.assertIs(service._translation_cache, cache)
                self.assertFalse(hasattr(default, "_settings_getter"))
                if fail == "_settings_getter":
                    self.assertIs(default._translation_cache, cache)

    def test_shape_rejects_changed_order(self):
        nodes, helpers = contract(self.tree)
        body = helpers[0].body if helpers else nodes
        init = definition(definition(self.tree, "BackendService"), "__init__")
        first = init.body.index(nodes[0])
        body[0], body[1] = body[1], body[0]
        if not helpers:
            init.body[first:first + 4] = body
        with self.assertRaises(AssertionError):
            contract(self.tree)

    def test_existing_static_getter_assertions(self):
        path = ROOT / "tests/test_translator_settings_getter_W1492.py"
        cls = copy.deepcopy(definition(ast.parse(path.read_text(encoding="utf-8")),
                                       "TestSettingsGetterInjectedByBackendService"))
        namespace = dict(unittest=unittest, Path=Path, __file__=str(path))
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
                     str(path), "exec"), namespace)
        for name in ("test_injection_line_present", "test_injection_after_translator_construction",
                     "test_slot_declared_in_translator_init"):
            with self.subTest(reader=name):
                getattr(namespace[cls.name](name), name)()


if __name__ == "__main__":
    unittest.main(verbosity=2)
