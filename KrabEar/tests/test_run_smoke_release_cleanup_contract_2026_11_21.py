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
# Заглушка агента: публикует свой PID, ждёт флаг quit, backend поднимает как свой дочерний процесс.
echo $$ > "$ROOT/agent.pid"
"$PYTHON" "$ROOT/KrabEar/main.py" &
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

# После exec командная строка лаунчера перестаёт содержать путь start_agent.command (как у open -W).
START_AGENT_EXEC = """#!/bin/sh
echo $$ > "$ROOT/launcher.pid"
exec "$PYTHON" -c "import time; time.sleep(300)"
"""

VENV_PYTHON = """#!/bin/sh
# python - <pid>: запрос завершения конкретному агенту (NSRunningApplication.terminate).
# Принимает ТОЛЬКО PID заглушки агента, любой другой отклоняется (код 1).
if [ -r "$ROOT/agent.pid" ] && [ "$2" = "$(cat "$ROOT/agent.pid")" ]; then
  echo "$2" >> "$ROOT/terminate.requests"
  touch "$ROOT/quit.flag"
  [ -n "$PGREP_FAIL_AFTER_TERMINATE" ] && touch "$ROOT/pgrep.fail"
  exit 0
fi
echo "rejected:$2" >> "$ROOT/terminate.rejected"
exit 1
"""

# pgrep, который после запроса завершения «ломается» (код 3), как при сбое перечисления.
PGREP_STUB = """#!/bin/sh
if [ -e "$ROOT/pgrep.fail" ]; then
  exit 3
fi
exec /usr/bin/pgrep "$@"
"""


PY_LOOP = "import time\nwhile True:\n    time.sleep(1)\n"

# Лаунчер, чей запрос «уже принят LaunchServices»: агент появится позже, из отдельной сессии.
START_AGENT_LATE = """#!/bin/sh
touch "$ROOT/start_agent.ran"
ROOT="$ROOT" IGNORE_QUIT="$IGNORE_QUIT" "$PYTHON" -c "
import os, subprocess
subprocess.Popen(
    ['/bin/sh', os.environ['ROOT'] + '/late_launch.sh'],
    start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
"
while true; do sleep 1; done
"""

# Отложенный запуск агента из отдельного файла: путь с пробелом корректно квотируется в самом sh.
LATE_LAUNCH = """#!/bin/sh
sleep 1.5
exec "$ROOT/native/runtime/KrabEarAgent" --project-root "$ROOT"
"""


def re_escape(root: Path) -> str:
    return re.escape(str(root))


def _make_tree(root: Path) -> Path:
    _write(root / "scripts" / "run_smoke_release.command", SCRIPT.read_text(encoding="utf-8"))
    _write(root / "scripts" / "start_agent.command", START_AGENT)
    _write(root / "native" / "runtime" / "KrabEarAgent", FAKE_AGENT)
    _write(root / "KrabEar" / "main.py", PY_LOOP)
    return root


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root = _make_tree(tmp_path / "krab ear")
    monkeypatch.setenv("ROOT", str(root))
    monkeypatch.setenv("PYTHON", sys.executable)
    # Изоляция от настоящей машины: свой HOME (сокет), агент только внутри временного каталога.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KRAB_EAR_SOCKET", raising=False)
    monkeypatch.setenv(
        "KRAB_SMOKE_AGENT_PATTERN_OVERRIDE", "^/bin/sh " + re_escape(root) + "/native/runtime/KrabEarAgent( |$)"
    )
    monkeypatch.setenv("KRAB_SMOKE_WAIT_START", "4")
    monkeypatch.setenv("KRAB_SMOKE_WAIT_BACKEND", "4")
    monkeypatch.setenv("KRAB_SMOKE_WAIT_STOP", "4")
    monkeypatch.setenv("KRAB_SMOKE_WAIT_STOP_RETRY", "2")
    monkeypatch.setenv("KRAB_SMOKE_SETTLE", "0")
    spawned = []
    yield root, spawned
    for proc in spawned:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(timeout=10)
    # Заглушки smoke, поднятые в отдельных сессиях, в spawned не попадают: убираем по уникальному
    # временному пути (обычный pgrep/pkill -f по каталогу теста, чужие процессы сюда не входят).
    subprocess.run(["pkill", "-9", "-f", re_escape(root)], capture_output=True)


def _with_venv(root: Path) -> None:
    _write(root / ".venv_krab_ear" / "bin" / "python", VENV_PYTHON)


def _run_smoke(root: Path, extra_env: dict | None = None):
    env = {**os.environ, **(extra_env or {})}
    return subprocess.run(
        ["/bin/zsh", str(root / "scripts" / "run_smoke_release.command")],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def _decoy_argv(mentioned_path: Path) -> list[str]:
    """Процесс, в командной строке которого есть путь (как у редактора/отладчика), но это не агент и не backend.

    ``sh -c "sleep N" path`` не годится: sh делает exec sleep и путь из командной строки пропадает.
    """
    return ["/usr/bin/awk", 'BEGIN { while (1) system("sleep 5") }', str(mentioned_path)]


def _bystander(root: Path, spawned: list, kind: str) -> subprocess.Popen:
    """Чужой процесс владельца, совпадающий с шаблоном агента или backend."""
    if kind == "agent":
        argv = [str(root / "native" / "runtime" / "KrabEarAgent"), "--project-root", str(root)]
        env = {**os.environ, "IGNORE_QUIT": "1"}
    elif kind == "backend":
        argv = [sys.executable, str(root / "KrabEar" / "main.py")]
        env = os.environ.copy()
    elif kind == "editor":  # командная строка содержит путь backend, но это не backend
        argv = _decoy_argv(root / "KrabEar" / "main.py")
        env = os.environ.copy()
    elif kind == "debugger":  # командная строка содержит путь агента, но это не агент
        argv = _decoy_argv(root / "native" / "runtime" / "KrabEarAgent")
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


def _settle(root: Path, seconds: float = 10) -> list[str]:
    deadline = time.monotonic() + seconds
    leftovers = _matching(root)
    while leftovers and time.monotonic() < deadline:
        time.sleep(0.2)
        leftovers = _matching(root)
    return leftovers


def _code_lines() -> str:
    return "\n".join(
        line for line in SCRIPT.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
    )


def test_source_has_no_pattern_kill():
    code = _code_lines().splitlines()
    assert not [line for line in code if "pkill" in line or "killall" in line]


def test_script_never_broadcasts_control_notifications():
    code = _code_lines()
    for forbidden in ("NSDistributedNotificationCenter", "com.krabear.agent.control", "show_history", '"quit"'):
        assert forbidden not in code, forbidden


def test_default_agent_discovery_is_by_process_name_and_includes_ancestors(tree, tmp_path):
    """Без override агент ищется pgrep -x по имени процесса, а preflight берёт и предков (-a)."""
    root, _ = tree
    _with_venv(root)
    log = tmp_path / "pgrep.args"
    bin_dir = tmp_path / "bin"
    _write(
        bin_dir / "pgrep",
        f'#!/bin/sh\necho "$@" >> "{log}"\ncase "$*" in *"-x KrabEarAgent"*) echo 4242; exit 0;; esac\nexit 1\n',
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "KRAB_SMOKE_AGENT_PATTERN_OVERRIDE": ""}
    result = _run_smoke(root, env)
    assert result.returncode != 0
    assert not (root / "start_agent.ran").exists()
    first = log.read_text().splitlines()[0]
    assert "-a" in first.split() and "-x" in first.split() and "KrabEarAgent" in first.split()


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


def test_existing_socket_is_refused_and_left_alone(tree):
    root, spawned = tree
    _with_venv(root)
    sock = Path(os.environ["HOME"]) / "Library/Application Support/KrabEar/krabear.sock"
    _write(sock, "", mode=0o600)
    result = _run_smoke(root)
    assert result.returncode != 0
    assert sock.exists()
    assert not (root / "start_agent.ran").exists()


def test_checkout_path_with_regex_metacharacters_is_matched_literally(tmp_path, monkeypatch):
    root = _make_tree(tmp_path / "krab.ear (rel) [x]")
    _with_venv(root)
    monkeypatch.setenv("ROOT", str(root))
    monkeypatch.setenv("PYTHON", sys.executable)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(
        "KRAB_SMOKE_AGENT_PATTERN_OVERRIDE", "^/bin/sh " + re_escape(root) + "/native/runtime/KrabEarAgent( |$)"
    )
    proc = subprocess.Popen(
        [sys.executable, str(root / "KrabEar" / "main.py")],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.5)
        result = _run_smoke(root)
        assert result.returncode != 0
        assert proc.poll() is None, "backend каталога со скобками не распознан, smoke его тронул"
        assert not (root / "start_agent.ran").exists()
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_backend_script_with_a_longer_name_is_not_a_backend(tree, tmp_path, spawned_name="main.py.backup"):
    root, spawned = tree
    _with_venv(root)
    other = _write(root / "KrabEar" / spawned_name, PY_LOOP)
    proc = subprocess.Popen(
        [sys.executable, str(other)], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    spawned.append(proc)
    time.sleep(0.5)
    result = _run_smoke(root)
    assert (root / "start_agent.ran").exists(), "main.py.backup принят за backend и заблокировал smoke"
    assert _alive(proc)
    assert result.returncode == 0, result.stdout + result.stderr


def test_success_stops_only_its_own_processes(tree):
    root, spawned = tree
    _with_venv(root)
    unrelated = _bystander(root, spawned, "unrelated")
    result = _run_smoke(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _alive(unrelated)
    leftovers = _settle(root)
    assert leftovers == [], f"свои процессы smoke остались после успешного завершения: {leftovers}"


def test_terminate_request_goes_exactly_to_our_agent_pid(tree):
    root, spawned = tree
    _with_venv(root)
    decoy = _bystander(root, spawned, "debugger")  # его PID не должен получить запрос
    result = _run_smoke(root)
    assert result.returncode == 0, result.stdout + result.stderr
    agent_pid = (root / "agent.pid").read_text().strip()
    assert (root / "terminate.requests").read_text().split() == [agent_pid]
    assert not (root / "terminate.rejected").exists(), "запрос ушёл на чужой PID"
    assert _alive(decoy), "процесс, лишь упоминающий путь агента, тронут"


def test_unrelated_process_mentioning_the_agent_path_survives_cleanup(tree):
    root, spawned = tree
    _with_venv(root)
    decoy = _bystander(root, spawned, "debugger")
    result = _run_smoke(root, {"IGNORE_QUIT": "1"})
    assert result.returncode != 0  # зависший агент
    assert _alive(decoy), "очистка убила процесс, не являющийся агентом"


def test_hung_agent_is_cleaned_up_and_reported_as_failed(tree):
    root, spawned = tree
    _with_venv(root)
    unrelated = _bystander(root, spawned, "unrelated")
    result = _run_smoke(root, {"IGNORE_QUIT": "1"})
    assert result.returncode != 0
    assert _alive(unrelated)
    reports = list((root / "docs" / "reports").glob("smoke_release_*.md"))
    assert reports and "status: **FAILED**" in reports[0].read_text()
    leftovers = _settle(root)
    assert leftovers == [], f"зависший агент smoke не убран своей очисткой: {leftovers}"


def test_process_enumeration_failure_is_not_reported_as_a_clean_shutdown(tree, tmp_path):
    """pgrep упал (код 3) после запроса завершения при живом агенте: это «неизвестно», не «остановлен»."""
    root, spawned = tree
    _with_venv(root)
    bin_dir = tmp_path / "bin"
    _write(bin_dir / "pgrep", PGREP_STUB)
    result = _run_smoke(
        root, {"PATH": f"{bin_dir}:{os.environ['PATH']}", "IGNORE_QUIT": "1", "PGREP_FAIL_AFTER_TERMINATE": "1"}
    )
    assert result.returncode != 0
    reports = list((root / "docs" / "reports").glob("smoke_release_*.md"))
    assert reports
    text = reports[0].read_text()
    assert "status: **FAILED**" in text and "shutdown_ok: no" in text
    assert "current_agent_count: unknown" in text
    # Очистка идентифицирует свои PID через ps, а не через сломанный pgrep: ничего не остаётся.
    leftovers = _settle(root)
    assert leftovers == [], f"при сбое pgrep очистка оставила свои процессы: {leftovers}"


def test_exec_transition_of_the_launcher_does_not_hide_it_from_cleanup(tree):
    """start_agent.command делает exec open -W: командная строка меняется, но он остаётся нашим потомком."""
    root, spawned = tree
    _with_venv(root)
    _write(root / "scripts" / "start_agent.command", START_AGENT_EXEC)
    result = _run_smoke(root)
    assert result.returncode != 0  # агент так и не появился
    launcher_pid = int((root / "launcher.pid").read_text().strip())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(launcher_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.2)
    else:
        pytest.fail("лаунчер после exec остался жить: очистка его не узнала")


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
    leftovers = _settle(root)
    assert leftovers == [], f"после SIGTERM остались процессы smoke: {leftovers}"


def test_unrelated_process_mentioning_the_backend_path_survives_cleanup(tree):
    root, spawned = tree
    _with_venv(root)
    editor = _bystander(root, spawned, "editor")
    result = _run_smoke(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _alive(editor), "очистка убила редактор/отладчик, лишь упоминающий путь backend"


def test_agent_started_late_by_launch_services_is_still_cleaned_within_settle(tree):
    root, spawned = tree
    _with_venv(root)
    _write(root / "scripts" / "start_agent.command", START_AGENT_LATE)
    _write(root / "late_launch.sh", LATE_LAUNCH)
    env = {**os.environ, "KRAB_SMOKE_SETTLE": "4", "KRAB_SMOKE_WAIT_START": "30", "KRAB_SMOKE_WAIT_BACKEND": "30"}
    proc = subprocess.Popen(
        ["/bin/zsh", str(root / "scripts" / "run_smoke_release.command")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    spawned.append(proc)
    deadline = time.monotonic() + 15
    while not (root / "start_agent.ran").exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    time.sleep(0.3)  # лаунчер уже отправил запрос, агента ещё нет
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=40) == 143
    assert (root / "agent.pid").exists(), "поздний агент вообще не стартовал: тест пустой"
    leftovers = _settle(root)
    assert leftovers == [], f"поздно поднятый агент остался после очистки: {leftovers}"


def test_native_executable_is_found_by_process_name(tmp_path):
    """pgrep -x KrabEarAgent находит НАСТОЯЩИЙ исполняемый файл с таким именем (не скрипт-заглушку)."""
    native = tmp_path / "KrabEarAgent"
    shutil.copy("/bin/sleep", native)
    proc = subprocess.Popen([str(native), "300"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.7)
        if proc.poll() is not None:
            pytest.skip("копия системного бинарника не запускается (code signing)")
        out = subprocess.run(["pgrep", "-x", "KrabEarAgent"], capture_output=True, text=True)
        assert str(proc.pid) in out.stdout.split()
    finally:
        proc.kill()
        proc.wait(timeout=10)


def _full_python_invocation_decoy(root: Path) -> list[str]:
    """Чужой процесс (awk), в аргументах которого лежит ПОЛНАЯ строка запуска backend."""
    return ["/usr/bin/awk", 'BEGIN { while (1) system("sleep 5") }', sys.executable, str(root / "KrabEar" / "main.py")]


def test_unrelated_process_carrying_a_full_python_invocation_is_not_a_backend(tree, spawned_decoy=None):
    root, spawned = tree
    _with_venv(root)
    decoy = subprocess.Popen(
        _full_python_invocation_decoy(root),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    spawned.append(decoy)
    time.sleep(0.5)
    result = _run_smoke(root)
    assert (root / "start_agent.ran").exists(), "awk с полной строкой запуска принят за backend и заблокировал smoke"
    assert result.returncode == 0, result.stdout + result.stderr
    assert _alive(decoy), "очистка убила процесс, только несущий строку запуска backend в аргументах"


def test_python_with_separate_value_options_is_still_a_backend(tree):
    root, spawned = tree
    _with_venv(root)
    owner = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(root / "KrabEar" / "main.py")],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    spawned.append(owner)
    time.sleep(0.5)
    result = _run_smoke(root)
    assert result.returncode != 0
    assert not (root / "start_agent.ran").exists(), "backend с `-X utf8` не распознан, smoke стартовал поверх него"
    assert _alive(owner)


def test_pid_found_by_the_last_good_enumeration_is_not_lost(tree, tmp_path):
    """pgrep ломается СРАЗУ после первого успешного перечисления агента: PID всё равно записан и убирается."""
    root, spawned = tree
    _with_venv(root)
    bin_dir = tmp_path / "bin"
    _write(
        bin_dir / "pgrep",
        '#!/bin/sh\n'
        'if [ -e "$ROOT/pgrep.fail" ]; then exit 3; fi\n'
        'out=$(/usr/bin/pgrep "$@"); rc=$?\n'
        'if [ "$rc" -eq 0 ] && [ -e "$ROOT/agent.pid" ]; then touch "$ROOT/pgrep.fail"; fi\n'
        'printf "%s" "$out"; exit $rc\n',
    )
    result = _run_smoke(root, {"PATH": f"{bin_dir}:{os.environ['PATH']}", "IGNORE_QUIT": "1"})
    assert result.returncode != 0
    agent_pid = int((root / "agent.pid").read_text().strip())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(agent_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.2)
    else:
        pytest.fail("PID агента потерян из-за сбоя второго pgrep: очистка его не убрала")


def test_second_signal_during_cleanup_does_not_interrupt_it(tree):
    root, spawned = tree
    _with_venv(root)
    _write(root / "scripts" / "start_agent.command", START_AGENT_LATE)
    _write(root / "late_launch.sh", LATE_LAUNCH)
    env = {**os.environ, "KRAB_SMOKE_SETTLE": "4", "KRAB_SMOKE_WAIT_START": "30", "KRAB_SMOKE_WAIT_BACKEND": "30"}
    proc = subprocess.Popen(
        ["/bin/zsh", str(root / "scripts" / "run_smoke_release.command")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    spawned.append(proc)
    deadline = time.monotonic() + 15
    while not (root / "start_agent.ran").exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    time.sleep(0.3)
    proc.send_signal(signal.SIGTERM)
    time.sleep(0.8)  # cleanup уже в окне settle
    proc.send_signal(signal.SIGINT)
    proc.wait(timeout=40)
    assert (root / "agent.pid").exists(), "поздний агент не стартовал: тест пустой"
    leftovers = _settle(root)
    assert leftovers == [], f"повторный сигнал прервал очистку: {leftovers}"
