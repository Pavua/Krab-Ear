"""Контракт детекции живого Swift-агента в ``scripts/ensure_agent_running.command``.

Живой баг (2026-07-23): ``pgrep`` на macOS использует ERE, а скрипт передавал
BRE-альтернацию ``\\|`` — то есть искал ЛИТЕРАЛ ``KrabEarAgent|native`` и не
находил ни один процесс. Итог: 12 подряд ``FAIL agent still absent`` в
``.remember/agent-recovery.log`` с 2026-07-18 и НИ ОДНОГО успеха, при живом
агенте. Три смок-отчёта подряд предлагали увеличить таймаут — это лечило бы
симптом, а не причину.

Тест поднимает настоящий процесс по пути, содержащему
``Krab Ear.app/Contents/MacOS/KrabEarAgent``, и проверяет РЕАЛЬНЫЙ ``pgrep``
с паттерном из скрипта. Прод не затрагивается: используется временный каталог,
процесс — обычный ``sleep``, и он гарантированно убивается в finally.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ensure_agent_running.command"


def _extract_pgrep_pattern(source: str) -> str:
    """Достаёт строковый аргумент реального вызова ``pgrep -fl`` из скрипта."""
    match = re.search(r'pgrep\s+-fl\s+"([^"]+)"', source)
    assert match, "не найден вызов pgrep -fl с двойными кавычками"
    return match.group(1)


def test_pgrep_pattern_detects_running_agent(tmp_path: Path) -> None:
    """Паттерн из скрипта обязан находить живой процесс агента.

    RED до фикса: BRE-альтернация ``\\|`` не матчит ничего (pgrep = ERE).
    """
    pattern = _extract_pgrep_pattern(SCRIPT.read_text(encoding="utf-8"))

    fake_bundle = tmp_path / "Krab Ear.app" / "Contents" / "MacOS"
    fake_bundle.mkdir(parents=True)
    fake_agent = fake_bundle / "KrabEarAgent"
    # Настоящий исполняемый файл: pgrep -f матчит полный путь в argv.
    shutil.copy("/bin/sleep", fake_agent)

    proc = subprocess.Popen([str(fake_agent), "30"])
    try:
        # Даём ядру зарегистрировать процесс в таблице.
        deadline = time.monotonic() + 3.0
        found = ""
        while time.monotonic() < deadline:
            found = subprocess.run(
                ["pgrep", "-fl", pattern],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            if str(proc.pid) in found:
                break
            time.sleep(0.05)

        assert str(proc.pid) in found, (
            "паттерн pgrep из ensure_agent_running.command не находит живой "
            f"процесс агента (pgrep на macOS — ERE, не BRE). Паттерн: {pattern!r}"
        )
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_pgrep_pattern_has_no_bre_alternation() -> None:
    """Регрессия-гард именно на BRE-ловушку: ``\\|`` в ERE — литерал."""
    pattern = _extract_pgrep_pattern(SCRIPT.read_text(encoding="utf-8"))
    assert "\\|" not in pattern, (
        "BRE-альтернация '\\|' в pgrep-паттерне: на macOS pgrep использует ERE, "
        "поэтому '\\|' ищет литеральный символ '|' и не матчит ничего"
    )
