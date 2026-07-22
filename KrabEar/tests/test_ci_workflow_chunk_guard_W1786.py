"""Проверяет, что chunked-CI не пропускает Python-тесты на системном Bash macOS.

Этот тест защищает оба GitHub Actions workflow: macOS использует Bash 3.2 без
``mapfile``, а пустой список тестов обязан завершать gate ошибкой, а не ложным успехом.
"""

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = (
    REPO_ROOT / ".github" / "workflows" / "ci.yml",
    REPO_ROOT / ".github" / "workflows" / "krabear-ci.yml",
)


@pytest.mark.parametrize("workflow_path", WORKFLOWS, ids=lambda path: path.name)
def test_chunked_python_gate_is_bash_3_2_compatible(workflow_path: Path) -> None:
    """Workflow не должен использовать команды, отсутствующие в Bash 3.2."""

    source = workflow_path.read_text(encoding="utf-8")

    assert "\n          mapfile " not in source
    assert "\n          readarray " not in source
    assert "for file in KrabEar/tests/test_*.py; do" in source
    assert '[ -f "$file" ] || continue' in source


@pytest.mark.parametrize("workflow_path", WORKFLOWS, ids=lambda path: path.name)
def test_chunked_python_gate_fails_when_no_tests_are_found(workflow_path: Path) -> None:
    """Пустое обнаружение тестов является ошибкой инфраструктуры CI."""

    source = workflow_path.read_text(encoding="utf-8")

    guard_start = source.index('if [ "$n" -eq 0 ]; then')
    guard_end = source.index("\n          fi", guard_start)
    guard = source[guard_start:guard_end]

    assert "::error::Не найдено ни одного Python test-файла" in guard
    assert "exit 1" in guard
    assert guard_start < source.index("Total test files: $n")


def test_macos_gate_has_a_portable_timeout_command() -> None:
    """macOS-job явно устанавливает и fail-closed выбирает GNU timeout."""

    source = WORKFLOWS[0].read_text(encoding="utf-8")

    assert "brew install ffmpeg coreutils" in source
    assert "command -v timeout" in source
    assert "command -v gtimeout" in source
    assert '"$timeout_cmd" 600 python -m pytest' in source
    assert '"$timeout_cmd" 150 python -m pytest' in source
