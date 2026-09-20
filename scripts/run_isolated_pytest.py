#!/usr/bin/env python3
"""Запускает один тестовый процесс в собственной POSIX process group.

После завершения команды добирает только её потомков. Это заменяет глобальный
`pkill -f gigaam_worker`, который на self-hosted Mac мог совпасть с живым
воркером Krab Ear владельца.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Sequence


_termination_signal: int | None = None


def _mark_termination(signum: int, _frame: object) -> None:
    """Не делает I/O: основной цикл увидит флаг и выполнит cleanup."""
    global _termination_signal
    if _termination_signal is None:
        _termination_signal = signum


def run_isolated(command: Sequence[str]) -> int:
    """Вернуть код команды, завершив только её оставшуюся process group."""
    process: subprocess.Popen[object] | None = None
    try:
        process = subprocess.Popen(list(command), start_new_session=True)
        while True:
            if _termination_signal is not None:
                return 128 + _termination_signal
            try:
                result = process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                continue
            return result if result >= 0 else 128 - result
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                # Главный процесс и все потомки уже завершились — это штатно.
                pass


def main(argv: Sequence[str]) -> int:
    global _termination_signal
    if not argv:
        print("usage: run_isolated_pytest.py COMMAND [ARG ...]", file=sys.stderr)
        return 2
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
