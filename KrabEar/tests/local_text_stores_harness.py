"""Actual AST узкого constructor slice; без backend imports, I/O и потоков."""

from __future__ import annotations

import ast
import copy
from types import SimpleNamespace


ATTRS = ("vocabulary", "_text_snippet_svc", "_phonetic_vocab_svc")
FACTORIES = ("VocabularyStore", "TextSnippetService", "PhoneticVocabService")
EXPECTED_BODY = """
self.vocabulary = VocabularyStore(data_dir=store.data_dir)
self._text_snippet_svc = TextSnippetService(data_dir=store.data_dir)
self._phonetic_vocab_svc = PhoneticVocabService(data_dir=store.data_dir)
"""
HELPER = "_init_local_text_stores"


def definition(tree, name):
    matches = [n for n in tree.body if getattr(n, "name", None) == name]
    assert len(matches) == 1, f"expected one definition: {name}"
    return matches[0]


def dump(node):
    return ast.dump(node, include_attributes=False)


def slice_contract(tree):
    """Не обходим произвольные helpers: разрешён ровно один заранее заданный."""
    cls = definition(tree, "BackendService")
    init = definition(cls, "__init__")
    ipc = [i for i, n in enumerate(init.body) if isinstance(n, ast.Assign)
           and any(isinstance(t, ast.Attribute) and t.attr == "_plaintext_export_ipc"
                   for t in n.targets)]
    assert len(ipc) == 1, "expected one plaintext IPC boundary"
    end = [i for i, n in enumerate(init.body) if isinstance(n, ast.FunctionDef)
           and n.name == "_emit_audio_level"]
    assert len(end) == 1 and end[0] > ipc[0], "audio boundary changed"
    nodes = init.body[ipc[0] + 1:end[0]]
    helpers = [n for n in cls.body if getattr(n, "name", None) == HELPER]
    calls = [n for n in ast.walk(cls) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == HELPER]
    if not helpers:
        assert not calls, "unknown helper must fail closed"
        return nodes, []
    assert len(helpers) == 1 and isinstance(helpers[0], ast.FunctionDef), "one sync helper required"
    helper = helpers[0]
    signature = ast.parse(f"def {HELPER}(self, store: StateStore) -> None: pass").body[0]
    assert dump(helper.args) == dump(signature.args), "helper signature changed"
    assert dump(helper.returns) == dump(signature.returns), "helper return annotation changed"
    assert not helper.decorator_list, "helper decorators forbidden"
    assert [dump(n) for n in helper.body] == [dump(n) for n in ast.parse(EXPECTED_BODY).body], \
        "helper body must be exact three assignments"
    expected_call = ast.parse(f"self.{HELPER}(store)").body[0]
    assert len(calls) == 1 and len(nodes) == 1 and dump(nodes[0]) == dump(expected_call), \
        "one direct helper call required at IPC boundary"
    return nodes, [helper]


def assert_inline_contract(tree):
    nodes, helpers = slice_contract(tree)
    assert not helpers, "production extraction outside this tests-only card"
    assert [dump(n) for n in nodes] == [dump(n) for n in ast.parse(EXPECTED_BODY).body], \
        "inline slice changed"


class StoreInstance:
    """Инертный storage boundary; bound methods позволяют проверить владельца."""

    def get_snippets(self):
        return self

    def get_entries(self):
        return self

    def handle_add_text_snippet(self):
        return self

    def handle_list_text_snippets(self):
        return self

    def handle_remove_text_snippet(self):
        return self

    def handle_add_phonetic_entry(self):
        return self

    def handle_list_phonetic_entries(self):
        return self

    def handle_remove_phonetic_entry(self):
        return self


def build_probe(tree, *, fail_at=None):
    """Исполняет реальные Assign/Call, factories заменяют mkdir/locks/storage."""
    nodes, helpers = slice_contract(tree)
    events, instances, reads = [], {}, []
    paths = (object(), object(), object())
    error = RuntimeError("injected constructor failure")
    state = SimpleNamespace()

    class Store:
        @property
        def data_dir(self):
            value = paths[len(reads)]  # Четвёртое чтение громко ломает fixture.
            reads.append(value)
            events.append(("read", value))
            return value

    namespace = {}

    def factory(name):
        def create(*, data_dir):
            events.append((name, data_dir))
            # Снимок в момент вызова, до присваивания результата constructor.
            state.snapshots.append(tuple(hasattr(state.service, attr) for attr in ATTRS))
            if name == fail_at:
                raise error
            result = StoreInstance()
            instances[name] = result
            return result
        return create

    namespace.update({name: factory(name) for name in FACTORIES})
    init = ast.parse("def __init__(self, store): pass").body[0]
    init.body = copy.deepcopy(nodes)
    cls = ast.ClassDef(name="SliceProbe", bases=[], keywords=[],
                       body=[init, *copy.deepcopy(helpers)], decorator_list=[])
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", level=0,
                             names=[ast.alias(name="annotations")]), cls],
        type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<ear-text-stores-source>", "exec"), namespace)
    probe_cls = namespace["SliceProbe"]
    state.service = probe_cls.__new__(probe_cls)
    state.snapshots = []
    caught = None
    try:
        probe_cls.__init__(state.service, Store())
    except RuntimeError as exc:
        caught = exc
    return SimpleNamespace(service=state.service, events=events, instances=instances,
                           reads=reads, paths=paths, error=error, caught=caught,
                           snapshots=state.snapshots)


def wire_consumers(tree, service):
    """Actual engine Assign и шесть actual IPC dict values, не весь BackendService."""
    cls = definition(tree, "BackendService")
    init = definition(cls, "__init__")
    providers = []
    for statement in init.body:
        if isinstance(statement, ast.If) and len(statement.body) == 1:
            assign = statement.body[0]
            if isinstance(assign, ast.Assign) and any(
                isinstance(t, ast.Attribute) and t.attr in {"_snippets_provider", "_phonetic_provider"}
                for t in assign.targets
            ):
                providers.append(copy.deepcopy(statement))
    assert len(providers) == 2, "provider wiring changed"
    keys = {"add_text_snippet", "list_text_snippets", "remove_text_snippet",
            "add_phonetic_entry", "list_phonetic_entries", "remove_phonetic_entry"}
    entries = [(copy.deepcopy(k), copy.deepcopy(v)) for node in ast.walk(cls)
               if isinstance(node, ast.Dict) for k, v in zip(node.keys, node.values)
               if isinstance(k, ast.Constant) and k.value in keys]
    assert len(entries) == 6 and {k.value for k, _ in entries} == keys, "IPC wiring changed"
    service.transcriber = SimpleNamespace(engine=SimpleNamespace())
    namespace = {"self": service}
    providers.append(ast.Assign(targets=[ast.Name(id="handlers", ctx=ast.Store())],
                                value=ast.Dict(keys=[k for k, _ in entries],
                                               values=[v for _, v in entries])))
    exec(compile(ast.fix_missing_locations(ast.Module(body=providers, type_ignores=[])),
                 "<ear-text-store-consumers>", "exec"), namespace)
    return service.transcriber.engine, namespace["handlers"]
