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
PY_LAUNCHER = REPO / "scripts" / "e2e_recommended_setup_smoke.py"

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


def python_env_before_popen(source: str) -> dict[str, str]:
    """`env["VAR"] = <значение>` в функции, строками РАНЬШЕ subprocess.Popen."""
    tree = ast.parse(source)
    popen_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Popen"
    ]
    if not popen_lines:
        raise AssertionError("в Python-запускателе не найден subprocess.Popen")
    first_popen = min(popen_lines)
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or node.lineno >= first_popen:
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "env"
                and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)
            ):
                value = node.value.value if isinstance(node.value, ast.Constant) else "<expr>"
                found[target.slice.value] = str(value)
    return found


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

    def test_python_launcher_isolates_backend(self) -> None:
        found = python_env_before_popen(PY_LAUNCHER.read_text(encoding="utf-8"))
        self.assertEqual(missing_isolation(found), [])


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
            '    subprocess.Popen(["python"], env=env)\n'
        )
        self.assertEqual(
            missing_isolation(python_env_before_popen(source)),
            ["KRAB_EAR_EVENT_BRIDGE_ENABLED='1': ожидалось одно из ['0', 'False', 'false']"],
        )


if __name__ == "__main__":
    unittest.main()
