#!/usr/bin/env python3
"""CI-команда в собственной POSIX process group с удерживаемым leader.

Leader живёт до cleanup: PID/PGID не может быть переиспользован между
получением результата команды и сигналом группе. Глобальных name-match нет.
"""
from __future__ import annotations

import os
from pathlib import Path
import select
import signal
import subprocess
import sys
from collections.abc import Sequence


_termination_signal: int | None = None
_LEADER_FLAG = "--internal-group-leader"


def _mark_termination(signum: int, _frame: object) -> None:
    """Не делает I/O: основной цикл увидит флаг и выполнит cleanup."""
    global _termination_signal
    if _termination_signal is None:
        _termination_signal = signum


def _shell_status(result: int) -> int:
    return result if result >= 0 else 128 - result


def _run_group_leader(command: Sequence[str], status_fd: int, control_fd: int) -> None:
    """Живой session leader удерживает PGID до parent cleanup/parent EOF."""
    leader_pid = os.getpid()
    if leader_pid != os.getpgrp() or leader_pid != os.getsid(0):
        raise RuntimeError("internal keeper must be a dedicated session leader")
    if min(status_fd, control_fd) <= 2 or status_fd == control_fd:
        raise RuntimeError("internal keeper pipe must not alias stdio")
    try:
        try:
            command_process = subprocess.Popen(list(command), close_fds=True)
        except OSError:
            command_process = None
        reported = False
        while True:
            if select.select([control_fd], [], [], 0.1)[0]:
                os.read(control_fd, 1)
                return
            if not reported:
                result = command_process.poll() if command_process is not None else 127
                if result is not None:
                    os.write(status_fd, f"{_shell_status(result)}\n".encode("ascii"))
                    os.close(status_fd)
                    reported = True
            # После результата не выходим: parent сигналит группу ДО reap leader.
    finally:
        # EOF и любая ошибка (включая BrokenPipe после гибели parent):
        # мы сами ещё живой leader именно этой проверенной группы.
        os.killpg(leader_pid, signal.SIGKILL)


def run_isolated(command: Sequence[str]) -> int:
    """Вернуть результат команды, сначала завершив группу, затем собрав leader."""
    if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
        raise RuntimeError("SIGCHLD must use the default non-auto-reaping disposition")
    status_read, status_write = os.pipe()
    descriptors = {status_read, status_write}
    process: subprocess.Popen[object] | None = None
    result = 125
    try:
        control_read, control_write = os.pipe()
        descriptors.update((control_read, control_write))
        if min(descriptors) <= 2:
            raise RuntimeError("isolated runner pipe must not alias stdio")
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), _LEADER_FLAG,
             str(status_write), str(control_read), *command],
            start_new_session=True, pass_fds=(status_write, control_read),
        )
        for descriptor in (status_write, control_read):
            os.close(descriptor)
            descriptors.remove(descriptor)
        payload = b""
        while _termination_signal is None:
            if not select.select([status_read], [], [], 0.1)[0]:
                continue
            chunk = os.read(status_read, 16)
            if not chunk:
                break
            payload += chunk
            if len(payload) > 4:
                break
            if payload.endswith(b"\n"):
                digits = payload[:-1]
                if digits.isdigit() and 0 <= int(digits) <= 255:
                    result = int(digits)
                break
    finally:
        try:
            if process is not None:
                # Ни poll(), ни wait() до сигнала: unreaped leader резервирует PID.
                if process.returncode is not None:
                    raise RuntimeError("group leader ownership lost before cleanup")
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
    return 128 + _termination_signal if _termination_signal is not None else result


def main(argv: Sequence[str]) -> int:
    global _termination_signal
    if not argv:
        print("usage: run_isolated_pytest.py COMMAND [ARG ...]", file=sys.stderr)
        return 2
    if argv[0] == _LEADER_FLAG:
        _run_group_leader(argv[3:], int(argv[1]), int(argv[2]))
        return 125
    _termination_signal = None
    previous_handlers = {
        signum: signal.signal(signum, _mark_termination)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        return run_isolated(argv)
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
