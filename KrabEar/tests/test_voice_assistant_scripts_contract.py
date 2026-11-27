"""Контракт launcher-скриптов Voice Assistant (ревью ChatGPT G2-Krab-Ear-02b1).

Скрипты запускаются по-настоящему под ``zsh``/``bash``, но против ЗАГЛУШЕК в
``PATH`` (curl, nc, pgrep, open, pkill, lsof) во временном дереве каталогов:
ни реальные процессы, ни реальный ``open``, ни сеть не затрагиваются.

Что закрыто (проверено на коде базы 94f68198, находки подтверждены вручную):
- KE02B1-01/05/12: скрипты ничего не убивают по порту/имени; занятый чужим
  процессом порт — отказ, живой агент — «не трогаю»; остановка делегирована
  каноническому скрипту, статус «остановлено» только при успехе;
- KE02B1-03: ожидание health — целочисленные попытки (раньше дробный ``elapsed``
  ломал ``[ -lt ]`` и ожидание обрывалось после первого неуспеха);
- KE02B1-04/10: корневой ``start_krab_agent.command`` делегирует каноническому
  ``scripts/start_agent.command``, не запускает legacy-бинарник;
- KE02B1-06/07: успех агента — по факту появления процесса; частичный запуск
  помечается DEGRADED; в zsh нет BASH_SOURCE (раньше SCRIPT_DIR = cwd);
- KE02B1-08/13: нет GNU ``timeout`` и зашитого пути Gateway;
- KE02B1-09: ``start_rest_production`` fail-closed без venv.

Заглушки ``kill``/``pkill`` через PATH работают только для внешних команд
(``kill`` — builtin zsh), поэтому отсутствие ``kill -9`` дополнительно проверяется
по тексту скриптов.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
START_VA = SCRIPTS / "start_voice_assistant.command"
STOP_VA = SCRIPTS / "stop_voice_assistant.command"
START_AGENT_ROOT = ROOT / "start_krab_agent.command"
START_REST_PROD = SCRIPTS / "start_rest_production.command"

needs_zsh = pytest.mark.skipif(shutil.which("zsh") is None, reason="нужен zsh")


def _code_lines(path: Path) -> list[str]:
    """Строки скрипта без комментариев и пустых."""
    return [
        ln
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


# ---------------------------------------------------------------------------
# Source-контракты (работают без zsh)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", [START_VA, STOP_VA])
def test_scripts_never_kill_by_port_or_name(script: Path) -> None:
    code = "\n".join(_code_lines(script))
    assert not re.search(r"\bkill\b", code), "kill в исполняемом коде запрещён (KE02B1-01/05/12)"
    assert not re.search(r"\bpkill\b", code)
    assert "xargs" not in code


@pytest.mark.parametrize("script", [START_VA, STOP_VA])
def test_scripts_have_no_bashism_or_gnu_only_tools(script: Path) -> None:
    code = "\n".join(_code_lines(script))
    assert "BASH_SOURCE" not in code, "в zsh BASH_SOURCE нет → SCRIPT_DIR молча = cwd"
    assert not re.search(r"(^|[\s|;(])timeout\s", code), "GNU timeout нет в базовой macOS (KE02B1-08)"
    assert "/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway" not in code, "зашитый путь Gateway (KE02B1-13)"


def test_start_script_does_not_dump_gateway_log() -> None:
    code = "\n".join(_code_lines(START_VA))
    assert not re.search(r"\b(cat|tail)\b[^\n]*\blog\b", code, re.IGNORECASE), (
        "содержимое лога Gateway не печатаем в терминал (KE02B1-11)"
    )
    assert "OPENCLAW_LOG" not in code, "лог, который скрипт не пишет (KE02B1-14)"


def test_scripts_are_executable() -> None:
    for script in (START_VA, STOP_VA, START_AGENT_ROOT, START_REST_PROD):
        assert os.access(script, os.X_OK), f"{script.name} потерял бит исполнения"


# ---------------------------------------------------------------------------
# Поведенческие тесты против заглушек
# ---------------------------------------------------------------------------

_STUB_CURL = r"""#!/bin/sh
for a; do url=$a; done
echo "curl $url" >> "$STATE/calls.log"
case "$url" in
  *v1/models*) echo "{\"data\":[{\"id\":\"${FAKE_MODEL:-qwen3-30b}\"}]}"; exit 0 ;;
  */health*)
    n=$(cat "$STATE/health_n" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$STATE/health_n"
    [ "${FAKE_HEALTH_ALWAYS_FAIL:-0}" = 1 ] && exit 22
    [ "$n" -le "${FAKE_HEALTH_FAILS:-0}" ] && exit 22
    exit 0 ;;
esac
exit 0
"""

_STUB_NC = r"""#!/bin/sh
for a; do port=$a; done
grep -qx "$port" "$STATE/open_ports" 2>/dev/null
"""

_STUB_LAUNCHCTL = r"""#!/bin/sh
echo "launchctl $*" >> "$STATE/calls.log"
# print gui/<uid>/<label>: «юнит загружен» только если тест это задал.
[ "${FAKE_LAUNCHD_LOADED:-0}" = 1 ]
"""

_STUB_PGREP = r"""#!/bin/sh
echo "pgrep $*" >> "$STATE/calls.log"
if [ -f "$STATE/agent_running" ]; then echo 4242; exit 0; fi
exit 1
"""

_STUB_OPEN = r"""#!/bin/sh
echo "open $*" >> "$STATE/calls.log"
[ "${FAKE_OPEN_NOOP:-0}" = 1 ] || touch "$STATE/agent_running"
exit 0
"""

_STUB_RECORD = r"""#!/bin/sh
echo "$(basename "$0") $*" >> "$STATE/calls.log"
exit 0
"""

_STUB_GATEWAY_START = r"""#!/bin/zsh
echo "gateway_start" >> "$STATE/calls.log"
exit ${FAKE_GATEWAY_START_RC:-0}
"""

_STUB_GATEWAY_STOP = r"""#!/bin/zsh
echo "gateway_stop" >> "$STATE/calls.log"
exit ${FAKE_GATEWAY_STOP_RC:-0}
"""


def _write_exec(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class _Tree:
    """Временное дерево репозитория + каталог заглушек."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "repo"
        self.scripts = self.root / "scripts"
        self.scripts.mkdir(parents=True)
        (self.root / "Krab Ear.app").mkdir()
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        (self.state / "open_ports").write_text("5005\n")
        for name, body in {
            "curl": _STUB_CURL,
            "nc": _STUB_NC,
            "pgrep": _STUB_PGREP,
            "open": _STUB_OPEN,
            "pkill": _STUB_RECORD,
            "lsof": _STUB_RECORD,
            "launchctl": _STUB_LAUNCHCTL,
        }.items():
            _write_exec(self.bin / name, body)
        for src in (START_VA, STOP_VA):
            shutil.copy2(src, self.scripts / src.name)
        _write_exec(self.scripts / "start_voice_gateway.command", _STUB_GATEWAY_START)
        _write_exec(self.scripts / "stop_voice_gateway.command", _STUB_GATEWAY_STOP)

    def open_ports(self, *ports: int, rest: bool = True) -> None:
        # REST Krab Ear (:5005) по умолчанию жив: иначе start отказывается звать лаунчер Gateway (он рестартил бы backend).
        all_ports = list(ports) + ([5005] if rest else [])
        (self.state / "open_ports").write_text("\n".join(str(p) for p in all_ports) + "\n")

    def agent_running(self) -> None:
        (self.state / "agent_running").touch()

    def calls(self) -> str:
        p = self.state / "calls.log"
        return p.read_text() if p.exists() else ""

    def run(self, script: str, **env: str) -> subprocess.CompletedProcess[str]:
        full_env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.root),
            "STATE": str(self.state),
            "HEALTH_POLL_INTERVAL": "0",
            "STARTUP_TIMEOUT": "3",
            "AGENT_START_ATTEMPTS": "3",
            "STOP_VERIFY_INTERVAL": "0",
            **env,
        }
        # cwd — НЕ каталог скрипта: путь к проекту обязан определяться по $0.
        return subprocess.run(
            ["zsh", str(self.scripts / script)],
            env=full_env,
            cwd=str(self.state),
            capture_output=True,
            text=True,
            timeout=60,
        )


@pytest.fixture()
def tree(tmp_path: Path) -> _Tree:
    return _Tree(tmp_path)


@needs_zsh
def test_start_retries_health_after_first_failure(tree: _Tree) -> None:
    """KE02B1-03: первый 503, второй 200 → ожидание успешно (раньше обрывалось)."""
    tree.open_ports(1234, 8081)
    proc = tree.run("start_voice_assistant.command", FAKE_HEALTH_FAILS="1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (tree.state / "health_n").read_text().strip() == "2"
    assert "gateway_start" in tree.calls()


@needs_zsh
def test_start_gives_up_when_gateway_never_healthy(tree: _Tree) -> None:
    tree.open_ports(1234, 8081)
    proc = tree.run("start_voice_assistant.command", FAKE_HEALTH_ALWAYS_FAIL="1")
    assert proc.returncode == 1
    assert "open " not in tree.calls(), "агент не запускаем, если Gateway не поднялся"


@needs_zsh
def test_busy_port_with_dead_health_is_refused_not_killed(tree: _Tree) -> None:
    """KE02B1-01: чужой процесс на 8090 → отказ, ни kill/pkill/lsof, Gateway не стартует."""
    tree.open_ports(1234, 8081, 8090)
    proc = tree.run("start_voice_assistant.command", FAKE_HEALTH_ALWAYS_FAIL="1")
    assert proc.returncode == 1
    calls = tree.calls()
    assert "gateway_start" not in calls
    assert "pkill" not in calls and "lsof" not in calls
    assert "open " not in calls


@needs_zsh
def test_busy_port_with_healthy_gateway_is_reused(tree: _Tree) -> None:
    tree.open_ports(1234, 8081, 8090)
    proc = tree.run("start_voice_assistant.command")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "gateway_start" not in tree.calls()


@needs_zsh
def test_running_agent_is_left_alone(tree: _Tree) -> None:
    """KE02B1-01: живой KrabEarAgent не убиваем и не открываем поверх (SingleInstanceGuard)."""
    tree.open_ports(1234, 8081)
    tree.agent_running()
    proc = tree.run("start_voice_assistant.command")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = tree.calls()
    assert "pkill" not in calls
    assert "open " not in calls
    assert "pgrep -x KrabEarAgent" in calls, "точное имя процесса, не -f"


@needs_zsh
def test_agent_started_by_open_and_confirmed_by_process(tree: _Tree) -> None:
    tree.open_ports(1234, 8081)
    proc = tree.run("start_voice_assistant.command")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"open {tree.root / 'Krab Ear.app'}" in tree.calls()


@needs_zsh
def test_agent_start_not_reported_when_process_never_appears(tree: _Tree) -> None:
    """KE02B1-06: `open` без появления процесса — ошибка, а не «запущена»."""
    tree.open_ports(1234, 8081)
    proc = tree.run("start_voice_assistant.command", FAKE_OPEN_NOOP="1")
    assert proc.returncode == 1
    assert "не поднялась" in proc.stdout


@needs_zsh
def test_missing_openclaw_is_reported_as_degraded(tree: _Tree) -> None:
    """KE02B1-07: частичный запуск не называется «все сервисы запущены»."""
    tree.open_ports(1234)
    proc = tree.run("start_voice_assistant.command")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DEGRADED" in proc.stdout
    assert "все сервисы запущены" not in proc.stdout


@needs_zsh
def test_all_services_up_reports_full(tree: _Tree) -> None:
    tree.open_ports(1234, 8081)
    proc = tree.run("start_voice_assistant.command")
    assert "все сервисы запущены" in proc.stdout
    assert "DEGRADED" not in proc.stdout


@needs_zsh
def test_wrong_lm_studio_model_fails_preflight(tree: _Tree) -> None:
    """KE02B1-06: проверяется точный ID модели, а не любая «qwen…»."""
    tree.open_ports(1234, 8081)
    proc = tree.run("start_voice_assistant.command", FAKE_MODEL="qwen2-7b")
    assert proc.returncode == 1
    assert "gateway_start" not in tree.calls()


@needs_zsh
def test_lm_studio_down_fails_preflight(tree: _Tree) -> None:
    tree.open_ports(8081)
    proc = tree.run("start_voice_assistant.command")
    assert proc.returncode == 1


@needs_zsh
def test_stop_delegates_to_canonical_script_and_never_kills(tree: _Tree) -> None:
    """KE02B1-05/12: остановка Gateway — только канонический скрипт."""
    tree.agent_running()
    proc = tree.run("stop_voice_assistant.command")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = tree.calls()
    assert "gateway_stop" in calls
    assert "pkill" not in calls and "lsof" not in calls
    assert "не затронута" in proc.stdout, "агент не останавливается и честно об этом говорит"


@needs_zsh
def test_stop_does_not_claim_success_when_canonical_stop_fails(tree: _Tree) -> None:
    proc = tree.run("stop_voice_assistant.command", FAKE_GATEWAY_STOP_RC="1")
    assert proc.returncode == 1
    assert "✅ Voice Gateway остановлен" not in proc.stdout
    assert "НЕ остановлен" in proc.stdout


@needs_zsh
def test_stop_does_not_claim_success_while_gateway_still_listens(tree: _Tree) -> None:
    """Ревью Opus #2303 P1: канонический stop без .gateway.pid выходит 0 при живом Gateway под launchd."""
    tree.open_ports(8090)
    proc = tree.run("stop_voice_assistant.command")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "✅ Voice Gateway остановлен" not in proc.stdout
    assert "порт 8090 всё ещё слушает" in proc.stdout and "launchd" in proc.stdout
    assert "gateway_stop" in tree.calls()
    assert "pkill" not in tree.calls() and "lsof" not in tree.calls() and "kill " not in tree.calls()


@needs_zsh
def test_stop_confirms_success_only_when_port_is_free(tree: _Tree) -> None:
    tree.open_ports(rest=False)
    proc = tree.run("stop_voice_assistant.command")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "✅ Voice Gateway остановлен" in proc.stdout


@needs_zsh
def test_start_refuses_second_gateway_when_launchd_unit_is_loaded(tree: _Tree) -> None:
    """Ревью Opus #2303 P2: порт закрыт, но юнит в launchd — не поднимаем второй Gateway с другим окружением."""
    tree.open_ports(1234, 8081)
    proc = tree.run("start_voice_assistant.command", FAKE_LAUNCHD_LOADED="1")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "gateway_start" not in tree.calls()
    assert "launchd" in proc.stdout and "launchctl print" in tree.calls()


@needs_zsh
def test_start_refuses_to_trigger_backend_restart_through_launcher(tree: _Tree) -> None:
    """Ревью Opus #2303 P2: REST :5005 мёртв — лаунчер Gateway рестартил бы прод-backend; без явного разрешения не зовём."""
    tree.open_ports(1234, 8081, rest=False)
    proc = tree.run("start_voice_assistant.command")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "gateway_start" not in tree.calls()
    assert "safe_backend_restart" in proc.stdout


@needs_zsh
def test_start_allows_launcher_when_backend_restart_is_explicitly_allowed(tree: _Tree) -> None:
    tree.open_ports(1234, 8081, rest=False)
    proc = tree.run("start_voice_assistant.command", ALLOW_BACKEND_RESTART_VIA_LAUNCHER="1", FAKE_HEALTH_FAILS="0")
    assert "gateway_start" in tree.calls(), proc.stdout + proc.stderr


@needs_zsh
def test_root_agent_launcher_delegates_to_canonical_entry(tmp_path: Path) -> None:
    """KE02B1-04/10: аргументы и код возврата проходят; legacy-бинарник не вызывается."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(START_AGENT_ROOT, root / "start_krab_agent.command")
    rec = tmp_path / "args.log"
    _write_exec(
        root / "scripts" / "start_agent.command",
        f'#!/bin/zsh\nprintf "%s|" "$@" >> "{rec}"\nexit 7\n',
    )
    legacy = root / "native" / "KrabEarAgent" / ".build" / "release"
    legacy.mkdir(parents=True)
    _write_exec(legacy / "KrabEarAgent", f'#!/bin/sh\necho legacy >> "{rec}"\n')

    proc = subprocess.run(
        ["/bin/bash", str(root / "start_krab_agent.command"), "--a", "b c"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 7, "код возврата канонического launcher'а должен пройти"
    assert rec.read_text() == "--a|b c|"


def test_root_agent_launcher_fails_without_canonical_entry(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copy2(START_AGENT_ROOT, root / "start_krab_agent.command")
    proc = subprocess.run(
        ["/bin/bash", str(root / "start_krab_agent.command")],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 1
    assert "не найден" in proc.stderr


def test_rest_production_fails_closed_without_venv(tmp_path: Path) -> None:
    """KE02B1-09: нет venv → ошибка, а не «первый gunicorn из PATH»."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(START_REST_PROD, root / "scripts" / "start_rest_production.command")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "gunicorn_called"
    _write_exec(fake_bin / "gunicorn", f'#!/bin/sh\ntouch "{marker}"\n')
    proc = subprocess.run(
        ["/bin/bash", str(root / "scripts" / "start_rest_production.command")],
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 1
    assert "venv не найден" in proc.stderr
    assert not marker.exists()


def test_rest_production_uses_absolute_paths_with_venv(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(START_REST_PROD, root / "scripts" / "start_rest_production.command")
    venv_bin = root / ".venv_krab_ear" / "bin"
    venv_bin.mkdir(parents=True)
    rec = tmp_path / "gunicorn.log"
    _write_exec(
        venv_bin / "gunicorn",
        f'#!/bin/sh\necho "$@|$PYTHONPATH|$KRAB_EAR_MLX_WHISPER_WORKER" > "{rec}"\n',
    )
    (venv_bin / "activate").write_text(
        f'export PATH="{venv_bin}:$PATH"\n', encoding="utf-8"
    )
    proc = subprocess.run(
        ["/bin/bash", str(root / "scripts" / "start_rest_production.command")],
        env={"PATH": "/usr/bin:/bin"},
        cwd=str(tmp_path),  # не корень репозитория: cwd не должен влиять
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    args, pythonpath, worker = rec.read_text().strip().split("|")
    resolved = root.resolve()
    assert args == f"-c {resolved}/KrabEar/gunicorn_config.py backend.rest_server:create_app()"
    assert pythonpath == f"{resolved}/KrabEar"
    assert worker == "1"
