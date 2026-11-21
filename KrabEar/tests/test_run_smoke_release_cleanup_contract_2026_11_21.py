"""Smoke-скрипт релиза не трогает чужие процессы Ear и убирает только свои.

Дефект (G2 Ear 02a F01 / W6-B): ``trap cleanup EXIT`` стоял ДО проверки окружения и делал
``pkill -f`` по шаблонам агента и backend. Ошибка preflight (или любой выход) убивала живые
процессы владельца в том же каталоге. Тесты работают на заглушках во временном каталоге:
реальные агент, backend, launchd и сокеты не используются.
"""

import os
import re
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
# python - <pid>: запрос завершения конкретному агенту (NSRunningApplication.terminate) -> флаг-файл.
echo "$2" >> "$ROOT/terminate.requests"
touch "$ROOT/quit.flag"
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
    # Изоляция от настоящей машины: свой HOME (сокет), агент только внутри временного каталога.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KRAB_EAR_SOCKET", raising=False)
    monkeypatch.setenv("KRAB_SMOKE_AGENT_PATTERN_OVERRIDE", re_escape(root) + "/native/runtime/KrabEarAgent")
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


def _default_agent_pattern() -> "re.Pattern[str]":
    match = re.search(
        r'^AGENT_PATTERN="\$\{KRAB_SMOKE_AGENT_PATTERN_OVERRIDE:-(.*)\}"$', SCRIPT.read_text(encoding="utf-8"), re.M
    )
    assert match, "не найден AGENT_PATTERN с дефолтом"
    return re.compile(match.group(1))


@pytest.mark.parametrize(
    "cmdline",
    [
        "/Users/x/Krab Ear/Krab Ear.app/Contents/MacOS/KrabEarAgent",  # живой агент: без --project-root
        "/Users/x/Krab Ear/Krab Ear.app/Contents/MacOS/KrabEarAgent --project-root /Users/x/Krab Ear",
        "/Users/x/Krab Ear/native/runtime/KrabEarAgent --project-root /Users/x/Krab Ear --launched-by-launchd",
        "/tmp/other checkout/Krab Ear.app/Contents/MacOS/KrabEarAgent",  # агент другого checkout
        "KrabEarAgent",
    ],
)
def test_default_agent_pattern_sees_any_agent_on_the_machine(cmdline):
    assert _default_agent_pattern().search(cmdline)


@pytest.mark.parametrize(
    "cmdline",
    [
        "tail -f /Users/x/Library/Logs/KrabEar/KrabEarAgent.log",
        "/usr/bin/grep KrabEarAgent",
        "/opt/tools/KrabEarAgentHelper --x",
    ],
)
def test_default_agent_pattern_ignores_unrelated_commands(cmdline):
    assert not _default_agent_pattern().search(cmdline)


def test_script_never_broadcasts_control_notifications():
    code = "\n".join(
        line for line in SCRIPT.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
    )
    for forbidden in ("NSDistributedNotificationCenter", "com.krabear.agent.control", "show_history", '"quit"'):
        assert forbidden not in code, forbidden


def test_existing_socket_is_refused_and_left_alone(tree):
    root, spawned = tree
    _with_venv(root)
    sock = Path(os.environ["HOME"]) / "Library/Application Support/KrabEar/krabear.sock"
    _write(sock, "", mode=0o600)
    result = _run_smoke(root)
    assert result.returncode != 0
    assert sock.exists()
    assert not (root / "start_agent.ran").exists()


def test_terminate_request_goes_to_our_agent_pid_only(tree):
    root, spawned = tree
    _with_venv(root)
    result = _run_smoke(root)
    assert result.returncode == 0, result.stdout + result.stderr
    requests = (root / "terminate.requests").read_text().split()
    assert requests and all(pid.isdigit() for pid in requests)


def test_hung_agent_is_reported_as_failed_not_ok(tree, monkeypatch):
    root, spawned = tree
    _with_venv(root)
    monkeypatch.setenv("IGNORE_QUIT", "1")
    result = _run_smoke(root)
    assert result.returncode != 0
    reports = list((root / "docs" / "reports").glob("smoke_release_*.md"))
    assert reports and "status: **FAILED**" in reports[0].read_text()


def test_sigterm_on_the_script_still_cleans_up_its_own_processes(tree):
    root, spawned = tree
    _with_venv(root)
    env = {**os.environ, "IGNORE_QUIT": "1", "KRAB_SMOKE_WAIT_STOP": "30", "KRAB_SMOKE_WAIT_STOP_RETRY": "30"}
    proc = subprocess.Popen(
        ["/bin/zsh", str(root / "scripts" / "run_smoke_release.command")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    spawned.append(proc)
    deadline = time.monotonic() + 20
    while not _matching(root) and time.monotonic() < deadline:
        time.sleep(0.2)
    time.sleep(1.5)  # агент и backend подняты, скрипт ждёт завершения
    assert any("KrabEarAgent" in line for line in _matching(root))
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 143
    deadline = time.monotonic() + 10
    leftovers = _matching(root)
    while leftovers and time.monotonic() < deadline:
        time.sleep(0.2)
        leftovers = _matching(root)
    assert leftovers == [], f"после SIGTERM остались процессы smoke: {leftovers}"
