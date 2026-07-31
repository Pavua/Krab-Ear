"""Контракт scripts/migrate_to_inprocess_rest.command (S3, Задача 11).

Спека: docs/superpowers/specs/2026-07-31-s3-rest-flip-design.md §4 + находка
I-D. Скрипт выгружает легаси REST-юнит (`ai.krab.ear.rest`) без удаления его
plist — файл на диске остаётся единственным механизмом отката на всё время
двухнедельной канарейки.

Два уровня проверки, по образцу двух существующих тестов миграционных
скриптов:
  - `TestMigrateInprocessRestSource*` (образец: test_migration_scripts.py) —
    статические грепы по тексту скрипта. Дёшево, но доказывает только
    присутствие литералов, не поведение.
  - `test_*_contract` (образец: test_safe_backend_restart_contract.py) —
    запускает НАСТОЯЩИЙ shell-скрипт с подменёнными python3/launchctl/sleep
    внутри временного HOME. Живые сервисы и данные владельца принципиально
    недоступны (сокет и launchd — фейковые), но реальная логика busy-гейта и
    rollback-гейта исполняется взаправду, а не угадывается по тексту.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import unittest
import uuid
from pathlib import Path

import pytest


_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, "..", ".."))
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
MIGRATE_CMD = os.path.join(_SCRIPTS_DIR, "migrate_to_inprocess_rest.command")

ROOT = Path(_REPO_ROOT)
SCRIPT_PATH = ROOT / "scripts" / "migrate_to_inprocess_rest.command"


def _strip_comments(path) -> list[str]:
    """Возвращает строки файла с отброшенной `#`-частью комментария.

    Комментарий распознаётся только по `#`, которому предшествует пробел или
    начало строки — так `$#` (число позиционных параметров в `while [ $# ...`)
    не режется по ошибке. Нужно, чтобы шапка-докстринг («без mapfile/
    readarray») не путала грепы, которые проверяют РЕАЛЬНОЕ использование
    конструкции в коде, а не документацию требования в комментарии.
    """
    import re

    with open(path, "r", encoding="utf-8") as fh:
        raw_lines = fh.readlines()
    stripped = []
    for line in raw_lines:
        m = re.search(r"(?:^|\s)#", line)
        stripped.append(line[: m.start()] if m else line)
    return stripped


# ---------------------------------------------------------------------------
# Статические source-контракт тесты (образец: test_migration_scripts.py)
# ---------------------------------------------------------------------------
class TestMigrateInprocessRestSource(unittest.TestCase):

    def test_script_exists(self):
        self.assertTrue(
            os.path.isfile(MIGRATE_CMD),
            f"migrate_to_inprocess_rest.command not found at {MIGRATE_CMD}",
        )

    def test_script_is_executable(self):
        mode = os.stat(MIGRATE_CMD).st_mode
        is_exec = bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
        self.assertTrue(
            is_exec,
            f"migrate_to_inprocess_rest.command must be executable (mode={oct(mode)})",
        )

    def test_script_contains_busy_gate(self):
        """Busy-гейт обязан опрашивать оба IPC-метода до launchctl (находка I-D)."""
        with open(MIGRATE_CMD, "r", encoding="utf-8") as fh:
            content = fh.read()
        for needle in ("get_recording_state", "get_meeting_live_state", "busy_reason"):
            self.assertIn(
                needle, content,
                f"migrate_to_inprocess_rest.command must reference {needle} (busy-gate)",
            )

    def test_busy_gate_precedes_first_launchctl_call(self):
        """Busy-гейт — первым делом: должен идти текстуально раньше первого launchctl."""
        code_lines = _strip_comments(MIGRATE_CMD)
        busy_gate_pos = None
        first_launchctl_pos = None
        for idx, line in enumerate(code_lines):
            if busy_gate_pos is None and "busy_reason)" in line:
                busy_gate_pos = idx
            is_launchctl_call = "launchctl bootout" in line or "launchctl bootstrap" in line
            if first_launchctl_pos is None and is_launchctl_call:
                first_launchctl_pos = idx
        self.assertIsNotNone(busy_gate_pos, "busy_reason() must actually be invoked, not just defined")
        self.assertIsNotNone(first_launchctl_pos, "script must actually call launchctl bootout/bootstrap")
        self.assertLess(
            busy_gate_pos, first_launchctl_pos,
            "busy_reason() invocation must appear (in code, comments excluded) before any launchctl bootout/bootstrap call",
        )

    def test_script_contains_rollback_branch(self):
        with open(MIGRATE_CMD, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("--rollback", content)
        self.assertIn("launchctl bootstrap", content)

    def test_script_checks_rest_in_process_running_before_rollback(self):
        """Р5/I1: перед bootstrap обязана быть проверка get_diagnostics.rest_in_process.running."""
        with open(MIGRATE_CMD, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("rest_in_process", content)
        self.assertIn("get_diagnostics", content)

    def test_script_never_deletes_rest_plist(self):
        """🔴 Главный инвариант задачи: plist легаси-юнита никогда не удаляется.

        Сканирует каждую строку, упоминающую путь к rest-plist, и требует
        отсутствия `rm`/`unlink` на этой же строке — а не просто отсутствия
        подстроки "rm" в файле (которая легко встречается как часть другого
        слова/комментария).
        """
        with open(MIGRATE_CMD, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        plist_markers = ("REST_PLIST", "rest.plist", "ai.krab.ear.rest.plist")
        delete_markers = ("rm ", "rm -f", "rm -rf", "unlink", "os.remove", "os.unlink")
        offending = []
        for lineno, line in enumerate(lines, start=1):
            if any(marker in line for marker in plist_markers):
                if any(marker in line for marker in delete_markers):
                    offending.append((lineno, line.strip()))
        self.assertEqual(
            offending, [],
            f"Found rm/unlink applied to rest-plist reference: {offending}",
        )

    def test_script_bash32_compatible(self):
        """Bash 3.2 / BSD gotchas: без mapfile/readarray/declare -A в реальном коде.

        Проверяет только код (строки-комментарии исключены) — сам этот файл
        ОБЯЗАН упоминать имена запрещённых конструкций в докстринге/шапке
        скрипта как документацию требования, это не значит, что они
        используются.
        """
        code_lines = _strip_comments(MIGRATE_CMD)
        code_only = "\n".join(code_lines)
        for forbidden in ("map" + "file", "readarray", "declare -A"):
            self.assertNotIn(
                forbidden, code_only, f"Bash 3.2 incompatible construct in actual code: {forbidden}"
            )


# ---------------------------------------------------------------------------
# Живые контракт-тесты (образец: test_safe_backend_restart_contract.py)
# ---------------------------------------------------------------------------
def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)


class _FakeEnv:
    """Изолированный HOME + подмена python3/launchctl/sleep для контракт-тестов.

    AF_UNIX ограничивает путь сокета ~104 байтами; pytest tmp_path длиннее —
    короткая символьная ссылка в /tmp сохраняет изоляцию (тот же трюк, что
    test_safe_backend_restart_contract.py).
    """

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.real_home = tmp_path / "home"
        (self.real_home / "Library" / "Application Support" / "KrabEar").mkdir(parents=True)
        (self.real_home / "Library" / "LaunchAgents").mkdir(parents=True)
        # AF_UNIX ограничивает путь ~104 байтами на macOS — короткий префикс
        # обязателен (тот же трюк, что test_safe_backend_restart_contract.py,
        # но с более коротким именем: полный "krab-migrate-rest-<hex32>" уже
        # сам по себе выбивает бюджет вместе с суффиксом сокет-пути).
        self.short_home = Path("/tmp") / f"km-{uuid.uuid4().hex[:12]}"
        self.short_home.symlink_to(self.real_home, target_is_directory=True)
        self.socket_path = self.short_home / "Library/Application Support/KrabEar/krabear.sock"
        self.plist_path = self.short_home / "Library/LaunchAgents/ai.krab.ear.rest.plist"

        self.fake_bin = tmp_path / "bin"
        self.fake_bin.mkdir()
        self.launchctl_log = tmp_path / "launchctl.log"
        self.unit_loaded_marker = tmp_path / "unit-loaded"

        _write_executable(
            self.fake_bin / "python3",
            """#!/bin/sh
if [ "$1" = "-" ]; then
  case "$2" in
    get_recording_state) printf '%s\n' "$FAKE_RECORDING_RESPONSE" ; exit 0 ;;
    get_meeting_live_state) printf '%s\n' "$FAKE_MEETING_RESPONSE" ; exit 0 ;;
    get_diagnostics) printf '%s\n' "$FAKE_DIAGNOSTICS_RESPONSE" ; exit 0 ;;
  esac
fi
exec /usr/bin/python3 "$@"
""",
        )
        _write_executable(
            self.fake_bin / "launchctl",
            """#!/bin/sh
case "$1" in
  print)
    if [ -f "$FAKE_UNIT_LOADED_MARKER" ]; then exit 0; else exit 1; fi
    ;;
  bootout)
    printf 'bootout\n' >> "$FAKE_LAUNCHCTL_LOG"
    rm -f "$FAKE_UNIT_LOADED_MARKER"
    exit 0
    ;;
  bootstrap)
    printf 'bootstrap\n' >> "$FAKE_LAUNCHCTL_LOG"
    touch "$FAKE_UNIT_LOADED_MARKER"
    exit 0
    ;;
  *) exit 0 ;;
esac
""",
        )
        _write_executable(self.fake_bin / "sleep", "#!/bin/sh\nexit 0\n")

    def bind_socket(self) -> socket.socket:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(self.socket_path))
        return s

    def run(self, args, recording=False, meeting=False, diagnostics=None, unit_loaded=None):
        if unit_loaded is True:
            self.unit_loaded_marker.touch()
        elif unit_loaded is False:
            self.unit_loaded_marker.unlink(missing_ok=True)

        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.short_home),
                "PATH": f"{self.fake_bin}:{env['PATH']}",
                "FAKE_LAUNCHCTL_LOG": str(self.launchctl_log),
                "FAKE_UNIT_LOADED_MARKER": str(self.unit_loaded_marker),
                "FAKE_RECORDING_RESPONSE": json.dumps(
                    {"id": "1", "ok": True, "result": {"is_recording": recording}}
                ),
                "FAKE_MEETING_RESPONSE": json.dumps(
                    {"id": "1", "ok": True, "result": {"ok": True, "active": meeting}}
                ),
                "FAKE_DIAGNOSTICS_RESPONSE": (
                    "" if diagnostics is None else json.dumps(diagnostics)
                ),
            }
        )
        return subprocess.run(
            [str(SCRIPT_PATH)] + list(args),
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def cleanup(self):
        self.short_home.unlink(missing_ok=True)

    def launchctl_calls(self) -> list[str]:
        if not self.launchctl_log.exists():
            return []
        return self.launchctl_log.read_text(encoding="utf-8").splitlines()


@pytest.fixture
def fake_env(tmp_path: Path):
    env = _FakeEnv(tmp_path)
    sock = env.bind_socket()
    try:
        yield env
    finally:
        sock.close()
        env.cleanup()


def _diag(running: bool | None, data_dir: str = "/tmp/krab-ear-data") -> dict:
    return {
        "id": "1",
        "ok": True,
        "result": {
            "history": {"data_dir": data_dir},
            "rest_in_process": {
                "enabled": running,
                "running": running,
                "port": 5005 if running else None,
                "error": None,
            },
        },
    }


def test_busy_recording_refuses_before_any_launchctl_call(fake_env: _FakeEnv):
    """Инцидент 22 июля: активная запись обязана отказывать ДО bootout."""
    completed = fake_env.run([], recording=True, unit_loaded=True)
    assert completed.returncode == 1
    assert "активная сессия (recording)" in completed.stderr
    assert fake_env.launchctl_calls() == []


def test_busy_meeting_refuses_rollback_before_any_launchctl_call(fake_env: _FakeEnv):
    """Busy-гейт — первым делом в ЛЮБОМ направлении, включая --rollback."""
    completed = fake_env.run(
        ["--rollback"], meeting=True, diagnostics=_diag(running=False), unit_loaded=False
    )
    assert completed.returncode == 1
    assert "активная сессия (meeting)" in completed.stderr
    assert fake_env.launchctl_calls() == []


def test_forward_bootout_when_idle_leaves_plist_on_disk(fake_env: _FakeEnv):
    """Не занято → bootout выполняется, но plist-файл остаётся на диске."""
    fake_env.plist_path.write_text("<plist/>", encoding="utf-8")
    completed = fake_env.run([], recording=False, meeting=False, diagnostics=_diag(running=False), unit_loaded=True)
    assert completed.returncode == 0, completed.stderr
    assert fake_env.launchctl_calls() == ["bootout"]
    assert fake_env.plist_path.exists(), "plist must survive bootout — rollback mechanism for the canary"


def test_forward_idempotent_when_already_unloaded(fake_env: _FakeEnv):
    """Юнит уже выгружен → скрипт не падает, bootout не вызывается повторно."""
    completed = fake_env.run([], recording=False, meeting=False, diagnostics=_diag(running=False), unit_loaded=False)
    assert completed.returncode == 0, completed.stderr
    assert fake_env.launchctl_calls() == []


def test_rollback_refuses_when_rest_in_process_running(fake_env: _FakeEnv):
    """Р5/I1: in-process REST ещё держит 5005 → bootstrap легаси-юнита ушёл бы в crash-loop."""
    completed = fake_env.run(
        ["--rollback"], recording=False, meeting=False, diagnostics=_diag(running=True), unit_loaded=False
    )
    assert completed.returncode == 1
    assert "сначала выключи режим настройкой и перезапусти backend" in completed.stdout + completed.stderr
    assert fake_env.launchctl_calls() == []


def test_rollback_refuses_on_unparseable_diagnostics_fail_closed(fake_env: _FakeEnv):
    """Мёртвый сокет/пустой ответ → неизвестное состояние трактуется как отказ, не как 'свободно'."""
    completed = fake_env.run(
        ["--rollback"], recording=False, meeting=False, diagnostics=None, unit_loaded=False
    )
    assert completed.returncode == 1
    assert fake_env.launchctl_calls() == []


def test_rollback_proceeds_when_rest_in_process_not_running(fake_env: _FakeEnv):
    """Режим выключен и backend перезапущен (running=false) → откат разрешён."""
    fake_env.plist_path.write_text("<plist/>", encoding="utf-8")
    completed = fake_env.run(
        ["--rollback"], recording=False, meeting=False, diagnostics=_diag(running=False), unit_loaded=False
    )
    assert completed.returncode == 0, completed.stderr
    assert fake_env.launchctl_calls() == ["bootstrap"]


def test_rollback_fails_when_plist_missing(fake_env: _FakeEnv):
    """plist удалён вне процедуры волны (не этим скриптом) → явный отказ, не тихий no-op."""
    assert not fake_env.plist_path.exists()
    completed = fake_env.run(
        ["--rollback"], recording=False, meeting=False, diagnostics=_diag(running=False), unit_loaded=False
    )
    assert completed.returncode == 1
    assert fake_env.launchctl_calls() == []


if __name__ == "__main__":
    unittest.main()
