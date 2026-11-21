"""Smoke-скрипт релиза не трогает чужие процессы Ear и убирает только свои.

Дефект (G2 Ear 02a F01 / W6-B): ``trap cleanup EXIT`` стоял ДО проверки окружения и делал
``pkill -f`` по шаблонам агента и backend. Ошибка preflight (или любой выход) убивала живые
процессы владельца в том же каталоге. Тесты работают на заглушках во временном каталоге:
реальные агент, backend, launchd и сокеты не используются.
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_smoke_release.command"

pytestmark = pytest.mark.skipif(
    shutil.which("zsh") is None or shutil.which("pgrep") is None,
    reason="нужны zsh и pgrep",
)


def _write(path: Path, text: str, mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


FAKE_AGENT = """#!/bin/sh
# Заглушка агента: ждёт флаг quit, backend поднимает как свой дочерний процесс.
sh "$ROOT/KrabEar/main.py" &
BACKEND=$!
if [ -n "$IGNORE_QUIT" ]; then
  while true; do sleep 1; done
fi
while [ ! -e "$ROOT/quit.flag" ]; do sleep 0.2; done
kill "$BACKEND" 2>/dev/null
exit 0
"""

START_AGENT = """#!/bin/sh
# Как настоящий start_agent.command (open -W): агент НЕ потомок этого скрипта.
touch "$ROOT/start_agent.ran"
ROOT="$ROOT" IGNORE_QUIT="$IGNORE_QUIT" "$PYTHON" -c "
import os, subprocess
subprocess.Popen(
    [os.environ['ROOT'] + '/native/runtime/KrabEarAgent', '--project-root', os.environ['ROOT']],
    start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
"
while true; do sleep 1; done
"""

VENV_PYTHON = """#!/bin/sh
# python - <action>: «control» через флаг-файл вместо NSDistributedNotificationCenter.
[ "$2" = "quit" ] && touch "$ROOT/quit.flag"
exit 0
"""


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root = tmp_path / "krab ear"
    _write(root / "scripts" / "run_smoke_release.command", SCRIPT.read_text(encoding="utf-8"))
    _write(root / "scripts" / "start_agent.command", START_AGENT)
    _write(root / "native" / "runtime" / "KrabEarAgent", FAKE_AGENT)
    _write(root / "KrabEar" / "main.py", "while true; do sleep 1; done\n")
    monkeypatch.setenv("ROOT", str(root))
    monkeypatch.setenv("PYTHON", sys.executable)
    monkeypatch.setenv("KRAB_SMOKE_WAIT_START", "4")
    monkeypatch.setenv("KRAB_SMOKE_WAIT_BACKEND", "4")
    monkeypatch.setenv("KRAB_SMOKE_WAIT_STOP", "4")
    monkeypatch.setenv("KRAB_SMOKE_WAIT_STOP_RETRY", "2")
    spawned = []
    yield root, spawned
    for proc in spawned:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(timeout=10)


def _with_venv(root: Path) -> None:
    _write(root / ".venv_krab_ear" / "bin" / "python", VENV_PYTHON)


def _run_smoke(root: Path):
    return subprocess.run(
        ["/bin/zsh", str(root / "scripts" / "run_smoke_release.command")],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _bystander(root: Path, spawned: list, kind: str) -> subprocess.Popen:
    """Чужой процесс владельца, совпадающий с шаблоном агента или backend."""
    if kind == "agent":
        argv = [str(root / "native" / "runtime" / "KrabEarAgent"), "--project-root", str(root)]
        env = {**os.environ, "IGNORE_QUIT": "1"}
    elif kind == "backend":
        argv = ["/bin/sh", str(root / "KrabEar" / "main.py")]
        env = os.environ.copy()
    else:
        argv = ["/bin/sleep", "300"]
        env = os.environ.copy()
    proc = subprocess.Popen(argv, env=env, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    spawned.append(proc)
    time.sleep(0.5)
    assert proc.poll() is None
    return proc


def _alive(proc: subprocess.Popen) -> bool:
    return proc.poll() is None


def _matching(root: Path) -> list[str]:
    out = subprocess.run(["pgrep", "-f", re_escape(root)], capture_output=True, text=True)
    described = []
    for pid in out.stdout.split():
        cmd = subprocess.run(["ps", "-p", pid, "-o", "stat=,command="], capture_output=True, text=True).stdout.strip()
        if cmd and not cmd.startswith("Z"):  # зомби уже мёртв и ждёт родителя
            described.append(f"{pid} {cmd}")
    return described


def re_escape(root: Path) -> str:
    import re

    return re.escape(str(root))


def test_source_has_no_pattern_kill():
    code = [line for line in SCRIPT.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")]
    assert not [line for line in code if "pkill" in line or "killall" in line]


def test_missing_venv_leaves_foreign_agent_untouched(tree):
    root, spawned = tree
    owner_agent = _bystander(root, spawned, "agent")
    result = _run_smoke(root)
    assert result.returncode != 0
    assert _alive(owner_agent), "ошибка preflight убила процесс владельца"
    assert not (root / "start_agent.ran").exists()


@pytest.mark.parametrize("kind", ["agent", "backend"])
def test_live_instance_is_refused_and_left_running(tree, kind):
    root, spawned = tree
    _with_venv(root)
    owner = _bystander(root, spawned, kind)
    result = _run_smoke(root)
    assert result.returncode != 0
    assert _alive(owner), "smoke остановил чужой экземпляр"
    assert not (root / "start_agent.ran").exists(), "smoke не должен стартовать поверх живого экземпляра"


def test_success_stops_only_its_own_processes(tree):
    root, spawned = tree
    _with_venv(root)
    unrelated = _bystander(root, spawned, "unrelated")
    result = _run_smoke(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _alive(unrelated)
    deadline = time.monotonic() + 10
    while _matching(root) and time.monotonic() < deadline:
        time.sleep(0.2)
    leftovers = _matching(root)
    assert leftovers == [], f"свои процессы smoke остались после успешного завершения: {leftovers}"


def test_hung_agent_is_cleaned_up_but_unrelated_process_survives(tree, monkeypatch):
    root, spawned = tree
    _with_venv(root)
    monkeypatch.setenv("IGNORE_QUIT", "1")
    unrelated = _bystander(root, spawned, "unrelated")
    result = _run_smoke(root)
    assert result.returncode != 0
    assert _alive(unrelated)
    deadline = time.monotonic() + 10
    while _matching(root) and time.monotonic() < deadline:
        time.sleep(0.2)
    leftovers = _matching(root)
    assert leftovers == [], f"зависший агент smoke не убран своей очисткой: {leftovers}"
