"""Семь исходных source-reader assertions без импорта/запуска backend.

Исполняются выбранные actual unittest methods. Только точные backend imports
внутри methods заменяются заранее созданным namespace с настоящим __init__
code object (сам __init__ не вызывается). Это не полные integration suites.
"""

from __future__ import annotations

import ast
import copy
import inspect
import re
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
READERS = [
    ("test_backend_service.py", "FeatureFlagsInitOrderTestCase",
     "test_feature_flags_assigned_before_llm_rewriter_inject", ()),
    ("test_fault_signal_handler_2026_08_07.py", "StartupRecoveryUsesVerdictSourceContractTest",
     "test_verdict_is_assigned_and_passed_on", ()),
    ("test_fault_signal_handler_2026_08_07.py", "StartupRecoveryUsesVerdictSourceContractTest",
     "test_report_call_is_an_unconditional_statement", ()),
    ("test_period_comparison_svc_wiring_W1297.py", "PeriodComparisonSvcInstantiatedTest",
     "test_instantiation_in_init", ("_load_service_ast",)),
    ("test_recorder_worker_alive_safety_2026_08_09.py", "ReinitCoordinatorWiringSourceContractTest",
     "test_audio_reinit_coordinator_uses_gate_method_not_bare_lambda",
     ("_find_coordinator_call", "_assert_kwarg_is_self_attr")),
    ("test_recorder_worker_alive_safety_2026_08_09.py", "ReinitCoordinatorWiringSourceContractTest",
     "test_audio_reinit_coordinator_wires_is_worker_hung_to_recorder_signal",
     ("_find_coordinator_call", "_assert_kwarg_is_self_attr")),
    ("test_shared_recorder_constructor_no_cap_2026_08_05.py", "TestSharedRecorderConstructorHasNoCap",
     "test_audio_recorder_construction_has_no_max_recording_samples_kwarg", ()),
]


def definition(tree, name):
    matches = [node for node in tree.body if getattr(node, "name", None) == name]
    assert len(matches) == 1, name
    return matches[0]


class BindSourceOnlyBackend(ast.NodeTransformer):
    """Удаляются только две буквальные формы import, ни один assertion."""

    def visit_Import(self, node):
        if ast.dump(node) == ast.dump(ast.parse("import backend.service as service_mod").body[0]):
            return None
        return node

    def visit_ImportFrom(self, node):
        if ast.dump(node) == ast.dump(ast.parse("from backend.service import BackendService").body[0]):
            return None
        return node


@pytest.mark.parametrize("filename,class_name,method,helpers", READERS)
def test_original_constructor_reader_assertions(filename, class_name, method, helpers):
    service_path = ROOT / "backend/service.py"
    tree = ast.parse(service_path.read_text(encoding="utf-8"))
    original_init = definition(definition(tree, "BackendService"), "__init__")
    future = ast.parse("from __future__ import annotations").body[0]
    namespace = {}
    # Код компилируется с настоящими filename/lineno для inspect.getsource.
    # Тело конструктора никогда не исполняется; backend модуль не импортируется.
    exec(compile(ast.Module(body=[future, original_init], type_ignores=[]),
                 str(service_path), "exec"), namespace)
    backend = SimpleNamespace(__init__=namespace["__init__"])
    path = ROOT / "tests" / filename
    cls = copy.deepcopy(definition(ast.parse(path.read_text(encoding="utf-8")), class_name))
    selected = {method, *helpers}
    cls.body = [node for node in cls.body if getattr(node, "name", None) in selected]
    assert {node.name for node in cls.body} == selected, "reader method/helper missing"
    cls = BindSourceOnlyBackend().visit(cls)
    namespace = {
        "__name__": __name__, "__file__": str(path), "unittest": unittest,
        "ast": ast, "inspect": inspect, "re": re, "textwrap": textwrap, "Path": Path,
        "BackendService": backend, "service_mod": SimpleNamespace(BackendService=backend),
        "SERVICE_PY": service_path,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, cls], type_ignores=[])),
                 str(path), "exec"), namespace)
    instance = namespace[class_name](methodName=method)
    getattr(instance, method)()
