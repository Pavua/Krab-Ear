"""Проверяет, что chunked-CI не пропускает Python-тесты на системном Bash macOS.

Этот тест защищает оба GitHub Actions workflow: macOS использует Bash 3.2 без
``mapfile``, а пустой список тестов обязан завершать gate ошибкой, а не ложным успехом.
"""

import re
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
    # 🔴 Проверяем НАЛИЧИЕ таймаута у обоих прогонов (чанк и per-file), а не
    # конкретные секунды: бюджет — настраиваемая величина, и его правка не
    # должна ронять гейт. 30.08.2026 per-file подняли 150→600с (три прогона
    # подряд гибли по таймауту на файлах, честно идущих 16-37с), и тест упал не
    # на пропаже защиты, а на изменении числа. Исчезновение самого timeout —
    # то, ради чего гейт существует, — по-прежнему ловится.
    timed_runs = re.findall(r'"\$timeout_cmd" (\d+) python -m pytest', source)
    assert len(timed_runs) >= 2, (
        f"ожидались таймаут-обёртки для чанка и per-file прогона, найдено: {timed_runs}"
    )
    assert all(int(sec) > 0 for sec in timed_runs), f"нулевой бюджет: {timed_runs}"
