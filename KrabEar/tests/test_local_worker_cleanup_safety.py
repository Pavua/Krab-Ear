"""Source-contract локальной проверки: pytest не завершает чужие worker-процессы.

Тест фиксирует границу ответственности между локальными и disposable CI-запусками:
локальный pytest полагается на ``addCleanup`` конкретного теста, а Python 3.12
harness только обнаруживает появившиеся процессы и сообщает об утечке.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFTEST_PATH = _REPO_ROOT / "KrabEar" / "tests" / "conftest.py"
_HARNESS_PATH = _REPO_ROOT / "scripts" / "pre_merge_py312_check.sh"
_TERMINATION_METHODS = {"kill", "pkill", "send_signal", "terminate"}
_TERMINATION_LITERAL_RE = re.compile(r"(?:^|[\s/])(?:p?kill)(?:\s|$)")
_SHELL_TERMINATION_RE = re.compile(
    r"(?:^|[;&|()])[\t ]*"
    r"(?:(?:if|then|do|else|elif|while|until)[\t ]+)?"
    r"(?:(?:command|env|sudo)[\t ]+)*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=[^\s;&|()]+[\t ]+)*"
    r"(?:[^\s;&|()]*/)?(?:pkill|kill)(?=[\t ]|$|[;&|()])",
    re.MULTILINE,
)


def _call_leaf_name(call: ast.Call) -> str:
    """Возвращает имя вызываемой функции без имени модуля или объекта."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _literal_strings(call: ast.Call) -> list[str]:
    """Собирает строковые литералы только из аргументов исполняемого вызова."""
    values: list[str] = []
    for argument in (*call.args, *(item.value for item in call.keywords)):
        for node in ast.walk(argument):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                values.append(node.value)
    return values


def _strip_shell_comment(line: str) -> str:
    """Обрезает первый неэкранированный ``#`` вне shell-кавычек."""
    quote: str | None = None
    escaped = False

    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote != "'":
            escaped = True
            continue
        if quote is not None:
            if character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
            continue
        if character == "#":
            return line[:index].rstrip()
    return line.rstrip()


def _shell_code(source: str) -> str:
    """Удаляет пустые строки и настоящие shell-комментарии."""
    code_lines = (_strip_shell_comment(line) for line in source.splitlines())
    return "\n".join(line for line in code_lines if line.strip())


def _has_shell_termination(source: str) -> bool:
    """Проверяет наличие исполняемой команды завершения в shell-фрагменте."""
    return _SHELL_TERMINATION_RE.search(_shell_code(source)) is not None


def test_shell_code_strips_only_real_comments() -> None:
    """Inline-комментарий обрезается, а кавычки и escaped hash сохраняются."""
    cases = (
        ("echo ok # kill worker", "echo ok"),
        ("echo '# kill'", "echo '# kill'"),
        ('echo "# pkill"', 'echo "# pkill"'),
        (r"echo \# kill", r"echo \# kill"),
    )

    for source, expected in cases:
        assert _shell_code(source) == expected


def test_shell_termination_detection_uses_command_position() -> None:
    """Текстовый аргумент не равен команде, а реальные kill/pkill находятся."""
    harmless_sources = (
        "echo ok # kill worker",
        "echo '# kill'",
        'echo "# pkill"',
        r"echo \# kill",
    )

    for source in harmless_sources:
        assert not _has_shell_termination(source)
    assert _has_shell_termination("kill 123")
    assert _has_shell_termination("pkill -f x")


def test_conftest_has_no_global_process_termination() -> None:
    """Conftest не должен посылать сигналы процессам на уровне всей сессии."""
    tree = ast.parse(_CONFTEST_PATH.read_text(encoding="utf-8"))
    offenders: list[str] = []

    session_hooks = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "pytest_sessionfinish"
    ]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        literals = _literal_strings(node)
        if any(_TERMINATION_LITERAL_RE.search(value) for value in literals):
            offenders.append(
                f"строка {node.lineno}: {_call_leaf_name(node) or '<вызов>'}"
            )

    for hook in session_hooks:
        for node in ast.walk(hook):
            if isinstance(node, ast.Call) and _call_leaf_name(node) in _TERMINATION_METHODS:
                offenders.append(f"строка {node.lineno}: {_call_leaf_name(node)}")

    assert not offenders, (
        "Глобальное завершение процессов из conftest запрещено; "
        f"найдено: {', '.join(offenders)}"
    )


def test_local_harness_uses_passive_worker_leak_detection() -> None:
    """Harness сравнивает PID+command до/после файла и не завершает процессы."""
    code = _shell_code(_HARNESS_PATH.read_text(encoding="utf-8"))

    assert not _SHELL_TERMINATION_RE.search(code), (
        "Локальный harness не должен вызывать kill/pkill"
    )
    assert "ps -axo pid=,command=" in code

    ordered_contract = (
        'snapshot_matching_workers "$before_workers"',
        '"$HARNESS_VENV/bin/python" -m pytest "$t"',
        'snapshot_matching_workers "$after_workers"',
        'comm -13 "$before_workers" "$after_workers"',
        'if [ -s "$new_workers" ]',
    )
    positions = [code.find(fragment) for fragment in ordered_contract]
    assert all(position >= 0 for position in positions), (
        "Harness обязан снимать PID+command до и после pytest, затем искать "
        "только новые worker-процессы"
    )
    assert positions == sorted(positions)

    leak_branch = code[positions[-1]:]
    assert "file_failed=1" in leak_branch
    assert 'fails+=("$t")' in leak_branch
