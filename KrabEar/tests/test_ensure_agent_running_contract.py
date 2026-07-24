"""Контракт детекции живого Swift-агента в ``scripts/ensure_agent_running.command``.

Живой баг (2026-07-23): ``pgrep`` на macOS использует ERE, а скрипт передавал
BRE-альтернацию ``\\|`` — то есть искал ЛИТЕРАЛ ``KrabEarAgent|native`` и не
находил ни один процесс. Итог: 12 подряд ``FAIL agent still absent`` в
``.remember/agent-recovery.log`` с 2026-07-18 и НИ ОДНОГО успеха, при живом
агенте. Три смок-отчёта подряд предлагали увеличить таймаут — это лечило бы
симптом, а не причину.

Тест поднимает настоящий процесс по пути, содержащему
``Krab Ear.app/Contents/MacOS/KrabEarAgent``, и проверяет РЕАЛЬНЫЙ ``pgrep``
с паттерном из скрипта. Прод не затрагивается: используется временный каталог.

2026-07-24, второй раунд на self-hosted раннере: копия скомпилированного
``/bin/sleep`` под чужим именем (``KrabEarAgent``) на non-interactive
launchd-раннере становилась ``<defunct>`` почти мгновенно (подтверждено
диагностикой ``ps -p`` — 0.00s CPU, процесс не проработал ни секунды), тогда
как интерактивно (Terminal-сессия) тот же трюк работал стабильно. Похоже на
код-signing/Gatekeeper-разницу между interactive и launchd-service контекстом
для СКОПИРОВАННОГО Mach-O бинарника под непривычным путём/именем — тот же
класс TCC-квирков, что уже не раз ловился в этом проекте (см. CLAUDE.md).
Обход: shell-скрипт вместо бинарной копии — текстовые скрипты с shebang не
код-signed/не quarantine-чувствительны на macOS, `pgrep -f` видит путь к
скрипту как аргумент интерпретатора точно так же.
"""

from __future__ import annotations

import os
import re
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
    # Shell-скрипт, не бинарная копия — см. пояснение в докстринге модуля.
    fake_agent.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    fake_agent.chmod(0o755)

    proc = subprocess.Popen([str(fake_agent)])
    try:
        deadline = time.monotonic() + 10.0
        found = ""
        while time.monotonic() < deadline:
            found = subprocess.run(
                ["pgrep", "-fl", pattern],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            # Ищем ПУТЬ, а не PID: shebang-скрипт запускается как
            # `/bin/sh <путь>` — PID процесса-обёртки sh не связан с
            # proc.pid предсказуемо, а вот путь к самому скрипту всегда
            # присутствует в argv как есть. Другие процессы на этой машине
            # (в т.ч. реальный прод-агент того же приложения) могут ТОЖЕ
            # матчить паттерн — это не ошибка, а корректное поведение
            # реального pgrep -f на этой машине; проверяем СВОЙ конкретный
            # tmp_path, не факт единственности совпадения.
            if str(fake_agent) in found:
                break
            time.sleep(0.1)

        if str(fake_agent) not in found:
            ps_sample = subprocess.run(
                ["ps", "-p", str(proc.pid)], capture_output=True, text=True, check=False
            ).stdout
        assert str(fake_agent) in found, (
            "паттерн pgrep из ensure_agent_running.command не находит живой "
            f"процесс агента (pgrep на macOS — ERE, не BRE). Паттерн: {pattern!r}\n"
            f"pgrep stdout: {found!r}\nps -p {proc.pid}: {ps_sample!r}\n"
            f"fake_agent existed: {fake_agent.exists()}, "
            f"executable: {os.access(fake_agent, os.X_OK)}"
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
