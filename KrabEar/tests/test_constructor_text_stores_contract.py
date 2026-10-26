"""Tests-only срез: порядок, identity и fail-closed подготовка helper extraction."""

import ast
import copy
import importlib.util
import sys
from pathlib import Path

import pytest


def load_helper(filename, name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


harness = load_helper("local_text_stores_harness.py", "ear_local_text_stores_harness")
startup = load_helper("startup_prefix_harness.py", "ear_startup_slice1_parent_harness")


def source():
    return ast.parse((Path(__file__).parents[1] / "backend/service.py").read_text(encoding="utf-8"))


def helper_candidate():
    """Только AST fixture будущего review-патча, production source не записывается."""
    tree = source()
    cls = harness.definition(tree, "BackendService")
    init = harness.definition(cls, "__init__")
    nodes, _ = harness.slice_contract(tree)
    first = init.body.index(nodes[0])
    helper = ast.parse(f"def {harness.HELPER}(self, store: StateStore) -> None:\n    pass").body[0]
    helper.body = copy.deepcopy(nodes)
    init.body[first:first + len(nodes)] = ast.parse(f"self.{harness.HELPER}(store)").body
    cls.body.insert(0, helper)
    return tree


def assert_success_contract(tree):
    probe = harness.build_probe(tree)
    assert probe.caught is None
    a, b, c = probe.paths
    assert probe.events == [
        ("read", a), ("VocabularyStore", a),
        ("read", b), ("TextSnippetService", b),
        ("read", c), ("PhoneticVocabService", c),
    ], "three separate data_dir reads must alternate with ordered constructors"
    assert probe.snapshots == [(False, False, False), (True, False, False), (True, True, False)], \
        "each completed constructor must be assigned before the next call"
    assert probe.service.vocabulary is probe.instances["VocabularyStore"]
    assert probe.service._text_snippet_svc is probe.instances["TextSnippetService"]
    assert probe.service._phonetic_vocab_svc is probe.instances["PhoneticVocabService"]
    return probe


def test_three_reads_order_and_immediate_instance_assignments():
    assert_success_contract(source())


def assert_consumers_contract(tree):
    probe = assert_success_contract(tree)
    engine, handlers = harness.wire_consumers(tree, probe.service)
    snippet = probe.instances["TextSnippetService"]
    phonetic = probe.instances["PhoneticVocabService"]
    assert engine._snippets_provider.__self__ is snippet
    assert engine._snippets_provider.__func__ is type(snippet).get_snippets
    assert engine._phonetic_provider.__self__ is phonetic
    assert engine._phonetic_provider.__func__ is type(phonetic).get_entries
    for key, method, owner in (
        ("add_text_snippet", "handle_add_text_snippet", snippet),
        ("list_text_snippets", "handle_list_text_snippets", snippet),
        ("remove_text_snippet", "handle_remove_text_snippet", snippet),
        ("add_phonetic_entry", "handle_add_phonetic_entry", phonetic),
        ("list_phonetic_entries", "handle_list_phonetic_entries", phonetic),
        ("remove_phonetic_entry", "handle_remove_phonetic_entry", phonetic),
    ):
        assert handlers[key].__self__ is owner
        assert handlers[key].__func__ is getattr(type(owner), method)


def test_actual_engine_and_ipc_bindings_keep_created_instances():
    assert_consumers_contract(source())


@pytest.mark.parametrize("factory,assigned,called", [
    ("VocabularyStore", (), ("VocabularyStore",)),
    ("TextSnippetService", ("vocabulary",), ("VocabularyStore", "TextSnippetService")),
    ("PhoneticVocabService", ("vocabulary", "_text_snippet_svc"),
     ("VocabularyStore", "TextSnippetService", "PhoneticVocabService")),
])
@pytest.mark.parametrize("shape", ["inline", "future_helper"])
def test_failure_preserves_prior_assignments_and_same_exception(factory, assigned, called, shape):
    tree = source() if shape == "inline" else helper_candidate()
    probe = harness.build_probe(tree, fail_at=factory)
    assert probe.caught is probe.error, "same exception must propagate, no fallback/wrapping"
    assert tuple(attr for attr in harness.ATTRS if hasattr(probe.service, attr)) == assigned
    assert tuple(name for name, _ in probe.events if name != "read") == called
    assert len(probe.reads) == len(called), "no reads after failing constructor"
    for attr, constructor in (("vocabulary", "VocabularyStore"),
                              ("_text_snippet_svc", "TextSnippetService")):
        if attr in assigned:
            assert getattr(probe.service, attr) is probe.instances[constructor]


def test_actual_inline_scope_is_exact_and_not_yet_extracted():
    tree = source()
    cls = harness.definition(tree, "BackendService")
    assert not cls.bases, "new inheritance requires reviewing slice assignment semantics"
    assert not any(getattr(node, "name", None) == "__setattr__" for node in cls.body)
    harness.assert_inline_contract(tree)


def test_future_exact_helper_is_executable_and_has_same_consumers():
    tree = helper_candidate()
    harness.slice_contract(tree)
    assert_success_contract(tree)
    assert_consumers_contract(tree)


@pytest.mark.parametrize("change", [
    "signature", "decorator", "extra_body", "reordered_body", "duplicate_call",
    "wrong_argument", "moved_call", "duplicate_definition", "unknown_helper", "async_helper",
])
def test_future_helper_gate_rejects_unsafe_ast_before_execution(change):
    tree = helper_candidate()
    cls = harness.definition(tree, "BackendService")
    init = harness.definition(cls, "__init__")
    nodes, helpers = harness.slice_contract(tree)
    helper, call = helpers[0], nodes[0]
    if change == "signature":
        helper.args.args[1].arg = "other_store"
    elif change == "decorator":
        helper.decorator_list = [ast.Name(id="staticmethod", ctx=ast.Load())]
    elif change == "extra_body":
        helper.body.append(ast.parse("unexpected_side_effect()").body[0])
    elif change == "reordered_body":
        helper.body[0], helper.body[1] = helper.body[1], helper.body[0]
    elif change == "duplicate_call":
        init.body.insert(init.body.index(call), copy.deepcopy(call))
    elif change == "wrong_argument":
        call.value.args = [ast.parse("self.store", mode="eval").body]
    elif change == "moved_call":
        init.body.remove(call)
        init.body.insert(0, call)
    elif change == "duplicate_definition":
        cls.body.append(copy.deepcopy(helper))
    elif change == "unknown_helper":
        cls.body.remove(helper)
    elif change == "async_helper":
        cls.body[cls.body.index(helper)] = ast.AsyncFunctionDef(
            name=helper.name, args=helper.args, body=helper.body, decorator_list=[], returns=helper.returns)
    with pytest.raises(AssertionError):
        harness.slice_contract(tree)


def test_existing_startup_harness_does_not_silently_skip_unknown_helper(tmp_path):
    # Старый harness не подключает helper сам: до будущей явной адаптации fail closed.
    with pytest.raises(AttributeError, match="_init_local_text_stores"):
        startup.build_harness(tmp_path / "settings", {}, source=helper_candidate())


def test_sensitivity_to_tuple_assignment_delaying_partial_state():
    tree = source()
    cls = harness.definition(tree, "BackendService")
    init = harness.definition(cls, "__init__")
    nodes, _ = harness.slice_contract(tree)
    offset = init.body.index(nodes[0])
    init.body[offset:offset + 3] = ast.parse("""
self.vocabulary, self._text_snippet_svc, self._phonetic_vocab_svc = (
    VocabularyStore(data_dir=store.data_dir), TextSnippetService(data_dir=store.data_dir),
    PhoneticVocabService(data_dir=store.data_dir))
""").body
    with pytest.raises(AssertionError, match="assigned before"):
        assert_success_contract(tree)
