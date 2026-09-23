"""Проверяет, что chunked-CI не пропускает Python-тесты на системном Bash macOS.

Этот тест защищает public Ubuntu chunked workflow: пустой список тестов обязан
завершать gate ошибкой, а не ложным успехом.
"""

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
# MLX/Metal gate теперь private и тестируется в Pavua/Krab-CI-Control. В public
# Ear остаётся один chunked Python workflow; перечисление удалённого файла
# превращало осмысленный security migration в ложный CI failure.
WORKFLOWS = (REPO_ROOT / ".github" / "workflows" / "krabear-ci.yml",)


def _uses_owned_process_group_cleanup(source: str) -> bool:
    """True, если cleanup ограничен process group конкретного pytest-запуска."""
    return (
        "scripts/run_isolated_pytest.py" in source
        and "pkill -9 -f" not in source
    )


def test_guarded_workflows_all_exist() -> None:
    """🔴 Файлы гарда обязаны существовать.

    Гард парametrized по путям: исчезни файл (переименование, перенос джоба) —
    тесты бы просто не нашли, что проверять, и остались зелёными. Явная
    проверка существования превращает молчаливую слепоту в честный красный.
    """
    missing = [p.name for p in WORKFLOWS if not p.exists()]
    assert not missing, f"гард указывает на несуществующие workflow: {missing}"


@pytest.mark.parametrize("workflow_path", WORKFLOWS, ids=lambda path: path.name)
def test_chunk_cleanup_is_limited_to_its_own_process_group(
    workflow_path: Path,
) -> None:
    """A1: CI не должен выбирать чужой GigaAM/MLX worker по общему имени."""
    assert _uses_owned_process_group_cleanup(workflow_path.read_text(encoding="utf-8"))


def test_process_group_cleanup_detector_rejects_global_name_match() -> None:
    """Сам guard обязан краснеть для прежнего глобального cleanup-паттерна."""
    legacy = 'reap() { pkill -9 -f "gigaam_worker"; }\npython -m pytest'
    assert not _uses_owned_process_group_cleanup(legacy)


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

    # Гард проверяет ИНВАРИАНТ (пустое обнаружение = падение), а не дословную
    # форму. Допустимы обе: `-eq 0` (нашли ноль файлов) и более строгая
    # `-lt N` — последняя ловит ещё и тихую ПОЧТИ-пустоту, когда сломавшийся
    # отбор оставляет три файла и job рапортует «всё покрыто». Закрепление
    # дословного `-eq 0` роняло CI на законном усилении (01.09.2026).
    m = re.search(r'if \[ "\$n" -(?:eq 0|lt \d+) \]; then', source)
    assert m, "не найден fail-closed гард на размер списка тестов"
    guard_start = m.start()
    guard_end = source.index("\n          fi", guard_start)
    guard = source[guard_start:guard_end]

    assert "::error::" in guard, "гард обязан явно сообщать об ошибке"
    assert "exit 1" in guard
    assert guard_start < source.index("Total test files: $n")
