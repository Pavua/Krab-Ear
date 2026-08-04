"""Busy-гейт ``scripts/install_backend_launchagent.command`` (S3/Р3, находка I-D).

Установщик делает **безусловный** ``bootout`` живого backend перед записью
нового plist. Инцидент 2026-07-22: рестарт под активной диктовкой безвозвратно
теряет аудио (оно живёт только в памяти backend-процесса). Задача 1 добавляет
busy-гейт по образцу ``scripts/safe_backend_restart.command::busy_reason()`` —
опрос ``get_recording_state``/``get_meeting_live_state`` через IPC ДО bootout.

Тест НЕ запускает установщик целиком (живой прогон запрещён — см. план волны
S3, задача 1, п. «Как это тестировать»). Вместо этого он извлекает РЕАЛЬНЫЕ
функции ``ipc_call``/``busy_reason`` из текста скрипта (тот же приём, что
``test_ensure_agent_running_contract.py`` использует для pgrep-паттерна) и
исполняет ТОЛЬКО их в изолированном ``sh``-подпроцессе с фейковым ``python3``
в PATH — ни реальный сокет, ни реальный launchctl, ни реальный backend не
задействуются.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts" / "install_backend_launchagent.command"


def _extract_function(source: str, name: str) -> str:
    match = re.search(rf"{re.escape(name)}\(\) \{{.*?\n\}}\n", source, re.S)
    assert match, f"{name}() не найдена в install_backend_launchagent.command"
    return match.group(0)


def _run_busy_reason(
    tmp_path: Path, recording_response: str, meeting_response: str
) -> subprocess.CompletedProcess:
    source = INSTALLER.read_text(encoding="utf-8")
    ipc_call_src = _extract_function(source, "ipc_call")
    busy_reason_src = _extract_function(source, "busy_reason")

    driver = tmp_path / "driver.sh"
    driver.write_text(
        "#!/bin/sh\n"
        + ipc_call_src
        + "\n"
        + busy_reason_src
        + '\nif REASON=$(busy_reason); then echo "BUSY:$REASON"; else echo FREE; fi\n',
        encoding="utf-8",
    )
    driver.chmod(0o755)

    fake_home = tmp_path / "home"
    (fake_home / "Library/Application Support/KrabEar").mkdir(parents=True)
    # AF_UNIX-совместимая короткая ссылка не нужна — сокет тут вообще не
    # создаётся: fake python3 отвечает по имени метода, не открывая сокет.
    short_home = Path("/tmp") / f"krab-install-busy-{uuid.uuid4().hex}"
    short_home.symlink_to(fake_home, target_is_directory=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    python3_stub = fake_bin / "python3"
    python3_stub.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        '  get_recording_state) printf "%s\\n" "$FAKE_RECORDING_RESPONSE" ;;\n'
        '  get_meeting_live_state) printf "%s\\n" "$FAKE_MEETING_RESPONSE" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    python3_stub.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(short_home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "FAKE_RECORDING_RESPONSE": recording_response,
            "FAKE_MEETING_RESPONSE": meeting_response,
        }
    )
    try:
        return subprocess.run(
            ["/bin/sh", str(driver)],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    finally:
        short_home.unlink(missing_ok=True)


def test_busy_reason_detects_active_recording(tmp_path: Path) -> None:
    """RED до фикса: busy_reason() не существует в install_backend_launchagent.command."""
    result = _run_busy_reason(
        tmp_path,
        recording_response=json.dumps(
            {"id": "1", "ok": True, "result": {"is_recording": True}}
        ),
        meeting_response=json.dumps({"id": "1", "ok": True, "result": {"active": False}}),
    )
    assert result.stdout.strip() == "BUSY:recording", result.stdout + result.stderr


def test_busy_reason_detects_active_meeting(tmp_path: Path) -> None:
    result = _run_busy_reason(
        tmp_path,
        recording_response=json.dumps(
            {"id": "1", "ok": True, "result": {"is_recording": False}}
        ),
        meeting_response=json.dumps({"id": "1", "ok": True, "result": {"active": True}}),
    )
    assert result.stdout.strip() == "BUSY:meeting", result.stdout + result.stderr


def test_busy_reason_free_when_idle(tmp_path: Path) -> None:
    """Свободная ветка — обе IPC-схемы говорят «не занято»."""
    result = _run_busy_reason(
        tmp_path,
        recording_response=json.dumps(
            {"id": "1", "ok": True, "result": {"is_recording": False}}
        ),
        meeting_response=json.dumps({"id": "1", "ok": True, "result": {"active": False}}),
    )
    assert result.stdout.strip() == "FREE", result.stdout + result.stderr


def test_busy_gate_precedes_bootout_in_script_order() -> None:
    """Гейт обязан идти РАНЬШЕ безусловного bootout — иначе он не гейт."""
    text = INSTALLER.read_text(encoding="utf-8")
    gate_call = text.index("while REASON=$(busy_reason)")
    bootout_call = text.index('launchctl bootout "gui/$UID_NUM/$LABEL"')
    assert gate_call < bootout_call, (
        "busy-гейт находится ПОСЛЕ launchctl bootout — под записью установщик "
        "успеет снести живой backend до проверки"
    )


def test_busy_reason_never_called_as_bare_assignment_under_set_e() -> None:
    """🔴 Ловушка set -e: голый REASON=$(busy_reason) молча завершит скрипт,

    когда backend свободен (busy_reason по контракту возвращает 1 = «не
    занято»). Вызывать только внутри if/while, как в safe_backend_restart.command.
    """
    text = INSTALLER.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("REASON=$(busy_reason)"):
            raise AssertionError(
                f"строка {lineno}: голый 'REASON=$(busy_reason)' вне if/while "
                f"под set -e молча завершит установку на каждом свободном "
                f"прогоне: {line!r}"
            )


def test_supports_wait_and_force_flags() -> None:
    """Источник правды по флагам — safe_backend_restart.command (--wait/--force)."""
    text = INSTALLER.read_text(encoding="utf-8")
    assert "--wait" in text, "install_backend_launchagent.command не поддерживает --wait N"
    assert "--force" in text, "install_backend_launchagent.command не поддерживает --force"


# ---------------------------------------------------------------------------
# S3-хвост (2026-08-04/05) — фиксированный `sleep 1` после bootout не
# гарантирует, что launchd реально освободил label.
#
# На загруженной машине (сегодняшний живой пример: load average 294 при
# индексации после перезагрузки) `launchctl bootout` может занять больше
# секунды. Следующий `bootstrap` того же label рискует race'ом со старым,
# ещё не до конца выгруженным сервисом на том же socket path (EADDRINUSE
# на bind() или launchd путается, у какого экземпляра владение сокетом).
# Опрос вместо слепого ожидания — тот же принцип, что уже применён ниже по
# скрипту (smoke-test ping ждёт готовности до 15с циклом, а не одним sleep).
# ---------------------------------------------------------------------------

def _run_wait_for_bootout(
    tmp_path: Path, *, still_loaded_calls: int, timeout_sec: int
) -> subprocess.CompletedProcess:
    """Извлекает wait_for_bootout() из скрипта и гоняет с фейковым launchctl.

    Фейковый launchctl возвращает 0 (label ещё загружен) первые
    ``still_loaded_calls`` обращений к `launchctl print`, затем 1 (не найден —
    выгружен). Реальный интервал опроса (1с) не подменяется — тест держит
    ``timeout_sec`` небольшим (1-2с), чтобы не раздувать время прогона.
    """
    source = INSTALLER.read_text(encoding="utf-8")
    fn_src = _extract_function(source, "wait_for_bootout")

    driver = tmp_path / "driver.sh"
    driver.write_text(
        "#!/bin/sh\n"
        "UID_NUM=501\n"
        + fn_src
        + f'\nwait_for_bootout "test.label" {timeout_sec}\n'
        "echo \"EXIT:$?\"\n",
        encoding="utf-8",
    )
    driver.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    counter_file = tmp_path / "counter"
    counter_file.write_text("0", encoding="utf-8")
    launchctl_stub = fake_bin / "launchctl"
    launchctl_stub.write_text(
        "#!/bin/sh\n"
        f'COUNTER_FILE="{counter_file}"\n'
        'n=$(cat "$COUNTER_FILE")\n'
        'n=$((n + 1))\n'
        'echo "$n" > "$COUNTER_FILE"\n'
        f'if [ "$n" -le {still_loaded_calls} ]; then exit 0; else exit 1; fi\n',
        encoding="utf-8",
    )
    launchctl_stub.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    return subprocess.run(
        ["/bin/sh", str(driver)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_wait_for_bootout_returns_promptly_once_unloaded(tmp_path: Path) -> None:
    """RED до фикса: wait_for_bootout() не существует — блокирует sleep-1 регрессию."""
    result = _run_wait_for_bootout(tmp_path, still_loaded_calls=1, timeout_sec=5)
    assert "EXIT:0" in result.stdout, result.stdout + result.stderr


def test_wait_for_bootout_gives_up_after_timeout_without_hanging(tmp_path: Path) -> None:
    """launchctl никогда не отпускает label — функция обязана вернуться (не зависнуть)
    с ненулевым кодом за разумное время, а не ждать вечно."""
    result = _run_wait_for_bootout(tmp_path, still_loaded_calls=999, timeout_sec=1)
    assert "EXIT:1" in result.stdout, result.stdout + result.stderr


def test_bootout_call_site_uses_wait_for_bootout_not_bare_sleep() -> None:
    """Голый `sleep 1` сразу после bootout — источник живого бага (S3-хвост).
    Вызывающий код обязан пройти через wait_for_bootout(), а не слепо спать."""
    text = INSTALLER.read_text(encoding="utf-8")
    bootout_idx = text.find('launchctl bootout "gui/$UID_NUM/$LABEL"')
    assert bootout_idx != -1, "bootout call site не найден — тест устарел"
    tail = text[bootout_idx:bootout_idx + 300]
    assert "wait_for_bootout" in tail, (
        "После bootout ожидается вызов wait_for_bootout(), а не голый sleep 1 — "
        f"нашёл: {tail!r}"
    )
