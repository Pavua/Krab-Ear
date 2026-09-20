"""Одноразовые e2e-backend'ы обязаны быть изолированы от боевых ресурсов.

Инцидент 2026-09-16: `run_e2e_smokes.command` поднимал backend без
выключателя EventBridge, мост целил в боевой REST :5005 с токеном из
временного каталога, и прод-лог REST ловил пачки
`event_bridge: неверный bridge-токен` на каждом e2e-прогоне. Тот же класс,
что уже закрытая утечка privacy-журнала (KRAB_EAR_PRIVACY_AUDIT_DIR):
запускатель обязан выставить КАЖДУЮ изолирующую переменную ДО старта процесса.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SHELL_LAUNCHER = REPO / "scripts" / "run_e2e_smokes.command"
PY_LAUNCHERS = (
    REPO / "scripts" / "e2e_recommended_setup_smoke.py",
    REPO / "scripts" / "e2e_owner_gate_smoke.py",
    REPO / "scripts" / "e2e_rescue_smoke.py",
)

# переменная -> допустимые значения (None = любое непустое)
ISOLATION_ENV: dict[str, set[str] | None] = {
    "KRAB_EAR_PRIVACY_AUDIT_DIR": None,
    "KRAB_EAR_EVENT_BRIDGE_ENABLED": {"0", "false", "False"},
}


def shell_exports_before_spawn(text: str) -> dict[str, str]:
    """Значения `export VAR=...`, объявленных строкой РАНЬШЕ запуска main.py."""
    spawn = re.search(r"^.*KrabEar/main\.py --data-dir.*$", text, re.MULTILINE)
    if spawn is None:
        raise AssertionError("в shell-запускателе не найден запуск KrabEar/main.py")
    head = text[: spawn.start()]
    found: dict[str, str] = {}
    for m in re.finditer(r'^\s*export\s+([A-Z_]+)="?([^"\n]*)"?\s*$', head, re.MULTILINE):
        found[m.group(1)] = m.group(2)
    return found


def python_envs_before_backend_popen(source: str) -> list[tuple[str, dict[str, str]]]:
    """Env каждой функции у каждого собственного backend Popen до его старта."""
    tree = ast.parse(source)
    launches: list[tuple[str, dict[str, str]]] = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        scope_nodes = list(_nodes_in_current_scope(function))
        assignments = sorted(
            (node for node in scope_nodes if isinstance(node, ast.Assign)),
            key=lambda node: node.lineno,
        )
        calls = sorted(
            (node for node in scope_nodes if isinstance(node, ast.Call)),
            key=lambda node: node.lineno,
        )
        for call in calls:
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "Popen"
                and call.args
                and "main.py" in ast.unparse(call.args[0])
            ):
                continue
            env_name = None
            for keyword in call.keywords:
                if keyword.arg == "env" and isinstance(keyword.value, ast.Name):
                    env_name = keyword.value.id
                    break
            found: dict[str, str] = {}
            if env_name is not None:
                for node in assignments:
                    if node.lineno >= call.lineno:
                        continue
                    for target in node.targets:
                        if not (
                            isinstance(target, ast.Subscript)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == env_name
                            and isinstance(target.slice, ast.Constant)
                            and isinstance(target.slice.value, str)
                        ):
                            continue
                        value = node.value.value if isinstance(node.value, ast.Constant) else "<expr>"
                        found[target.slice.value] = str(value)
            launches.append((function.name, found))
    if not launches:
        raise AssertionError("в Python-запускателе не найден backend subprocess.Popen")
    return launches


def _nodes_in_current_scope(node: ast.AST):
    """Обходит scope, не приписывая родителю код вложенной функции/класса."""
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield from _nodes_in_current_scope(child)


def missing_isolation(found: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for name, allowed in ISOLATION_ENV.items():
        if name not in found:
            problems.append(f"{name}: не выставлена до старта backend")
        elif allowed is not None and found[name] not in allowed:
            problems.append(f"{name}={found[name]!r}: ожидалось одно из {sorted(allowed)}")
    return problems


class ThrowawayLauncherIsolationTests(unittest.TestCase):
    def test_shell_launcher_isolates_backend(self) -> None:
        found = shell_exports_before_spawn(SHELL_LAUNCHER.read_text(encoding="utf-8"))
        self.assertEqual(missing_isolation(found), [])

    def test_python_launchers_isolate_backend(self) -> None:
        for launcher in PY_LAUNCHERS:
            with self.subTest(launcher=launcher.name):
                spawns = python_envs_before_backend_popen(
                    launcher.read_text(encoding="utf-8"))
                self.assertEqual(len(spawns), 1)
                self.assertEqual(missing_isolation(spawns[0][1]), [])


class DetectorSelfTest(unittest.TestCase):
    """Гард, тихо переставший находить нарушение, отчитывался бы зелёным вечно."""

    def test_shell_detector_flags_export_after_spawn(self) -> None:
        text = (
            'export KRAB_EAR_PRIVACY_AUDIT_DIR="$D"\n'
            'PYTHONPATH=x "$PY" KrabEar/main.py --data-dir "$D" &\n'
            "export KRAB_EAR_EVENT_BRIDGE_ENABLED=0\n"
        )
        self.assertEqual(
            missing_isolation(shell_exports_before_spawn(text)),
            ["KRAB_EAR_EVENT_BRIDGE_ENABLED: не выставлена до старта backend"],
        )

    def test_python_detector_flags_wrong_value(self) -> None:
        source = (
            "import subprocess\n"
            "def main():\n"
            "    env = {}\n"
            '    env["KRAB_EAR_PRIVACY_AUDIT_DIR"] = "/tmp/x"\n'
            '    env["KRAB_EAR_EVENT_BRIDGE_ENABLED"] = "1"\n'
            '    subprocess.Popen(["KrabEar/main.py"], env=env)\n'
        )
        self.assertEqual(
            missing_isolation(python_envs_before_backend_popen(source)[0][1]),
            ["KRAB_EAR_EVENT_BRIDGE_ENABLED='1': ожидалось одно из ['0', 'False', 'false']"],
        )

    def test_python_detector_scopes_each_backend_spawn_and_its_env(self) -> None:
        source = (
            "import subprocess\n"
            "def unrelated():\n"
            "    env = {}\n"
            '    env["KRAB_EAR_EVENT_BRIDGE_ENABLED"] = "0"\n'
            "def first():\n"
            "    env = {}\n"
            '    env["KRAB_EAR_PRIVACY_AUDIT_DIR"] = "/tmp/x"\n'
            '    env["KRAB_EAR_EVENT_BRIDGE_ENABLED"] = "0"\n'
            '    subprocess.Popen(["KrabEar/main.py"], env=env)\n'
            "def second():\n"
            "    env2 = {}\n"
            '    env2["KRAB_EAR_PRIVACY_AUDIT_DIR"] = "/tmp/y"\n'
            '    subprocess.Popen(["KrabEar/main.py"], env=env2)\n'
            "def no_env():\n"
            '    subprocess.Popen(["KrabEar/main.py"])\n'
            "def nested_false_green():\n"
            "    env = {}\n"
            '    env["KRAB_EAR_PRIVACY_AUDIT_DIR"] = "/tmp/z"\n'
            "    def unused():\n"
            '        env["KRAB_EAR_EVENT_BRIDGE_ENABLED"] = "0"\n'
            '    subprocess.Popen(["KrabEar/main.py"], env=env)\n'
            "def outer_with_nested_spawn():\n"
            "    def inner():\n"
            "        env3 = {}\n"
            '        env3["KRAB_EAR_PRIVACY_AUDIT_DIR"] = "/tmp/q"\n'
            '        subprocess.Popen(["KrabEar/main.py"], env=env3)\n'
        )
        spawns = python_envs_before_backend_popen(source)
        self.assertEqual(
            [name for name, _ in spawns],
            ["first", "second", "no_env", "nested_false_green", "inner"],
        )
        self.assertEqual(missing_isolation(spawns[0][1]), [])
        self.assertEqual(
            missing_isolation(spawns[1][1]),
            ["KRAB_EAR_EVENT_BRIDGE_ENABLED: не выставлена до старта backend"],
        )
        self.assertEqual(
            missing_isolation(spawns[2][1]),
            [
                "KRAB_EAR_PRIVACY_AUDIT_DIR: не выставлена до старта backend",
                "KRAB_EAR_EVENT_BRIDGE_ENABLED: не выставлена до старта backend",
            ],
        )
        self.assertEqual(
            missing_isolation(spawns[3][1]),
            ["KRAB_EAR_EVENT_BRIDGE_ENABLED: не выставлена до старта backend"],
        )
        self.assertEqual(
            missing_isolation(spawns[4][1]),
            ["KRAB_EAR_EVENT_BRIDGE_ENABLED: не выставлена до старта backend"],
        )


if __name__ == "__main__":
    unittest.main()
