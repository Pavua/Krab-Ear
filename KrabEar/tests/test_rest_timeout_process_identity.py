"""Изолированный source-контракт роли REST; без import живых singleton-ов."""
import ast
import builtins
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from werkzeug.wrappers import Response


@pytest.fixture
def isolated_rest():
    source = Path(__file__).resolve().parents[1] / "backend" / "rest_server.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    names = {"adopt_external_singletons", "release_external_singletons",
             "_exit_poisoned_rest_process", "_arm_timeout_exit"}
    definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name in names]
    assert {node.name for node in definitions} == names
    initializers = [node for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id in {
                        "_ADOPTABLE_NAMES", "_singletons_adopted", "_external_process_owner_seen",
                    } for target in node.targets)]
    assert len(initializers) == 3
    kill_session, exit_process = Mock(), Mock()

    def isolated_import(name, *args, **kwargs):
        if name == "core.mlx_whisper_session":
            return SimpleNamespace(kill_mlx_whisper_session=kill_session)
        return builtins.__import__(name, *args, **kwargs)

    module = ModuleType("isolated_rest_process_identity")
    module.__dict__.update({
        "__builtins__": dict(vars(builtins), __import__=isolated_import),
        "logger": Mock(), "settings": SimpleNamespace(REST_IN_PROCESS_ENABLED=False),
        "os": SimpleNamespace(_exit=exit_process), "_flush_logs_before_exit": Mock(),
        "_TRANSCRIBE_TIMEOUT_EXIT_CODE": 70,
    })
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
             *initializers, *definitions]
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 str(source), "exec"), module.__dict__)
    module._OWN_SINGLETONS = {name: object() for name in module._ADOPTABLE_NAMES}
    return module, exit_process, kill_session


@pytest.mark.parametrize("released", [False, True])
def test_adopted_backend_process_never_exits_on_timeout(isolated_rest, tmp_path, released):
    rest, exit_process, kill_session = isolated_rest
    rest.adopt_external_singletons(**{name: object() for name in rest._ADOPTABLE_NAMES})
    if released:
        rest.release_external_singletons()
    upload = tmp_path / "synthetic.wav"
    upload.write_bytes(b"synthetic")
    response = Response("timeout", status=504)
    rest._arm_timeout_exit(response, upload)
    response.close()
    exit_process.assert_not_called()
    kill_session.assert_not_called()


def test_standalone_process_retains_timeout_exit(isolated_rest):
    rest, exit_process, kill_session = isolated_rest
    rest._exit_poisoned_rest_process(70)
    kill_session.assert_called_once_with()
    exit_process.assert_called_once_with(70)


def test_unknown_process_identity_does_not_exit(isolated_rest):
    rest, exit_process, kill_session = isolated_rest
    del rest._external_process_owner_seen
    rest._exit_poisoned_rest_process(70)
    exit_process.assert_not_called()
    kill_session.assert_not_called()


def test_late_timeout_callback_keeps_upload_after_backend_adoption(isolated_rest, tmp_path):
    rest, exit_process, kill_session = isolated_rest
    upload = tmp_path / "synthetic.wav"
    upload.write_bytes(b"synthetic")
    response = Response("timeout", status=504)
    rest._arm_timeout_exit(response, upload)
    rest.adopt_external_singletons(**{name: object() for name in rest._ADOPTABLE_NAMES})
    response.close()
    assert upload.exists()
    exit_process.assert_not_called()
    kill_session.assert_not_called()
