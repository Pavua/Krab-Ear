"""Паттерны поиска живого backend-процесса обязаны матчить ОБА имени точки входа (S3/Р9).

До этой волны прод-плист запускал ``KrabEar/backend/service.py`` напрямую;
S3/Задача 1 переключила его на ``KrabEar/main.py`` (см.
``ai.krab.ear.backend.plist.template`` и ``test_backend_plist_data_dir_parity_S3.py``).
Но во время перехода (старый юнит ещё не переустановлен, standalone
active-режим агента) живой процесс может называться ЛЮБЫМ из двух имён — и
два скрипта до этой правки искали ТОЛЬКО старое:

- ``scripts/remove_agent.command`` (``pkill -f``) — деинсталлятор оставил бы
  backend, запущенный через ``main.py``, живым.
- ``scripts/run_smoke_release.command`` (``pgrep -f`` через ``BACKEND_PATTERN``)
  — релизный смок счёл бы такой backend отсутствующим.

Тест извлекает РЕАЛЬНЫЕ паттерны из текста скриптов (а не переписывает их
вручную) и матчит их настоящим ``pgrep``/``pkill`` (macOS = ERE, см.
``test_ensure_agent_running_contract.py`` — BRE-альтернация ``\\|`` там же
не матчит НИЧЕГО) против настоящих короткоживущих процессов с обоими именами
в argv. Продовые процессы не затрагиваются — всё в ``tmp_path``.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REMOVE_AGENT = ROOT / "scripts" / "remove_agent.command"
RUN_SMOKE_RELEASE = ROOT / "scripts" / "run_smoke_release.command"


def _extract_remove_agent_pkill_pattern(source: str) -> str:
    """Достаёт паттерн из ``pkill -f "...KrabEar/..."`` (не трогает AGENT_BIN pkill)."""
    match = re.search(r'pkill -f "(\$ROOT_DIR/KrabEar/[^"]+)"', source)
    assert match, "не найден pkill -f по $ROOT_DIR/KrabEar/... в remove_agent.command"
    return match.group(1)


def _extract_backend_pattern(source: str) -> str:
    match = re.search(r'BACKEND_PATTERN="(\$ROOT_DIR/KrabEar/[^"]+)"', source)
    assert match, "не найден BACKEND_PATTERN=\"$ROOT_DIR/KrabEar/...\" в run_smoke_release.command"
    return match.group(1)


def _spawn_fake_process(script_path: Path) -> subprocess.Popen:
    """Поднимает настоящий процесс, argv которого содержит нужный путь.

    Shell-скрипт, а не бинарная копия — code-signing/Gatekeeper на macOS
    иначе может убить скопированный Mach-O почти мгновенно (см.
    test_ensure_agent_running_contract.py, раунд 2026-07-24).
    """
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    script_path.chmod(0o755)
    return subprocess.Popen([str(script_path)])


def _pgrep_matches(pattern: str, pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, text=True, check=False
        ).stdout
        matched_pids = {line.strip() for line in found.splitlines() if line.strip()}
        if str(pid) in matched_pids:
            return True
        time.sleep(0.1)
    return False


def _check_pattern_matches_both_entrypoints(raw_pattern: str, tmp_path: Path) -> None:
    fake_root = tmp_path / "fake_root"
    pattern = raw_pattern.replace("$ROOT_DIR", str(fake_root))

    old_script = fake_root / "KrabEar" / "backend" / "service.py"
    new_script = fake_root / "KrabEar" / "main.py"

    for script_path, label in ((old_script, "backend/service.py"), (new_script, "main.py")):
        proc = _spawn_fake_process(script_path)
        try:
            assert _pgrep_matches(pattern, proc.pid), (
                f"паттерн {pattern!r} не находит процесс, запущенный через {label} "
                f"(pid={proc.pid})"
            )
        finally:
            proc.kill()
            proc.wait(timeout=5)


def test_remove_agent_pkill_matches_both_entrypoints(tmp_path: Path) -> None:
    """RED до фикса: pkill-паттерн деинсталлятора не находит процесс через main.py."""
    raw_pattern = _extract_remove_agent_pkill_pattern(
        REMOVE_AGENT.read_text(encoding="utf-8")
    )
    _check_pattern_matches_both_entrypoints(raw_pattern, tmp_path)


def test_run_smoke_release_backend_pattern_matches_both_entrypoints(tmp_path: Path) -> None:
    """RED до фикса: BACKEND_PATTERN релизного смока не находит процесс через main.py."""
    raw_pattern = _extract_backend_pattern(
        RUN_SMOKE_RELEASE.read_text(encoding="utf-8")
    )
    _check_pattern_matches_both_entrypoints(raw_pattern, tmp_path)


def test_remove_agent_pattern_has_no_bre_alternation() -> None:
    """Регрессия-гард: ``\\|`` в ERE (macOS pgrep/pkill) — литерал, не альтернация."""
    raw_pattern = _extract_remove_agent_pkill_pattern(
        REMOVE_AGENT.read_text(encoding="utf-8")
    )
    assert "\\|" not in raw_pattern, (
        "BRE-альтернация '\\|' в pkill-паттерне: pkill/pgrep на macOS — ERE, "
        "'\\|' ищет литеральный символ '|' и не матчит ничего"
    )


def test_run_smoke_release_pattern_has_no_bre_alternation() -> None:
    raw_pattern = _extract_backend_pattern(RUN_SMOKE_RELEASE.read_text(encoding="utf-8"))
    assert "\\|" not in raw_pattern, (
        "BRE-альтернация '\\|' в BACKEND_PATTERN: pgrep на macOS — ERE, "
        "'\\|' ищет литеральный символ '|' и не матчит ничего"
    )
