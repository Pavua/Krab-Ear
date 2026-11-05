"""Проверки timeout без запуска REST, чтения настроек и настоящего os._exit."""

import ast
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


_PACKAGE = Path(__file__).resolve().parents[1]
_SOURCE = _PACKAGE / "backend" / "rest_server.py"
_SERVICE = _PACKAGE / "backend" / "service.py"


def _load_timeout_functions(*, backend_owner=False, static_setting=False):
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"), filename=str(_SOURCE))
    names = {"_mark_backend_process", "_exit_poisoned_rest_process", "_arm_timeout_exit"}
    definitions = [node for node in tree.body
                   if (isinstance(node, ast.FunctionDef) and node.name in names)
                   or (isinstance(node, ast.Assign) and any(
                       isinstance(target, ast.Name) and target.id == "_PROCESS_HOSTS_BACKEND"
                       for target in node.targets))]
    fake_os = types.SimpleNamespace(_exit=Mock())
    namespace = {
        "settings": types.SimpleNamespace(REST_IN_PROCESS_ENABLED=static_setting),
        "logger": Mock(),
        "os": fake_os,
        "_flush_logs_before_exit": Mock(),
        "_TRANSCRIBE_TIMEOUT_EXIT_CODE": 70,
        "Response": object,
        "_Any": object,
    }
    module = ast.fix_missing_locations(ast.Module(body=definitions, type_ignores=[]))
    exec(compile(module, str(_SOURCE), "exec"), namespace)
    if backend_owner:
        namespace["_mark_backend_process"]()
    return namespace, fake_os


def _close_timeout_response(namespace):
    response = Mock()
    file = Mock()
    namespace["_arm_timeout_exit"](response, file)
    callback = response.call_on_close.call_args.args[0]
    callback()
    return file


class RestTimeoutProcessOwnerTests(unittest.TestCase):
    def _exercise(self, namespace):
        kill = Mock()
        fake_worker = types.ModuleType("core.mlx_whisper_session")
        fake_worker.kill_mlx_whisper_session = kill
        with patch.dict(sys.modules, {"core.mlx_whisper_session": fake_worker}):
            _close_timeout_response(namespace)
        return kill

    def test_backend_process_survives_even_when_static_setting_is_false(self):
        namespace, fake_os = _load_timeout_functions(backend_owner=True)
        kill = self._exercise(namespace)
        fake_os._exit.assert_not_called()
        kill.assert_not_called()

    def test_standalone_path_retains_fail_fast(self):
        namespace, fake_os = _load_timeout_functions()
        kill = self._exercise(namespace)
        kill.assert_called_once_with()
        namespace["_flush_logs_before_exit"].assert_called_once_with()
        fake_os._exit.assert_called_once_with(70)

    def test_static_true_still_blocks_exit(self):
        namespace, fake_os = _load_timeout_functions(static_setting=True)
        kill = self._exercise(namespace)
        fake_os._exit.assert_not_called()
        kill.assert_not_called()

    def test_mark_is_sticky_when_settings_change_before_response_close(self):
        namespace, fake_os = _load_timeout_functions(static_setting=True)
        namespace["_mark_backend_process"]()
        response = Mock()
        namespace["_arm_timeout_exit"](response, Mock())
        namespace["settings"].REST_IN_PROCESS_ENABLED = False
        kill = Mock()
        fake_worker = types.ModuleType("core.mlx_whisper_session")
        fake_worker.kill_mlx_whisper_session = kill
        with patch.dict(sys.modules, {"core.mlx_whisper_session": fake_worker}):
            response.call_on_close.call_args.args[0]()
        self.assertIs(namespace["_PROCESS_HOSTS_BACKEND"], True)
        namespace["_mark_backend_process"]()
        self.assertIs(namespace["_PROCESS_HOSTS_BACKEND"], True)
        fake_os._exit.assert_not_called()
        kill.assert_not_called()

    def test_owner_mark_precedes_adoption_and_app_creation(self):
        tree = ast.parse(_SERVICE.read_text(encoding="utf-8"), filename=str(_SERVICE))
        calls = [(node.func.attr, node.lineno) for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and isinstance(node.func.value, ast.Name)
                 and node.func.value.id == "_rest_module"]
        by_name = {name: line for name, line in calls}
        self.assertLess(by_name["_mark_backend_process"], by_name["adopt_external_singletons"])
        self.assertLess(by_name["_mark_backend_process"], by_name["create_app"])


if __name__ == "__main__":
    unittest.main()
