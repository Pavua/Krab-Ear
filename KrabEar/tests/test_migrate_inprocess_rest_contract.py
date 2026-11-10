"""Изолированные контракты отказа REST migration без admission/drain."""
import json
import os
import socket
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "migrate_to_inprocess_rest.command"


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



@pytest.mark.parametrize("args", [["--wait"], ["--wait", "x"], ["--wait", "1" * 7], ["--bad"]])
def test_invalid_arguments_refuse_without_lifecycle(tmp_path, args):
    env = _FakeEnv(tmp_path)
    try:
        result = env.run(args, unit_loaded=True)
        assert result.returncode == 2
        assert env.launchctl_calls() == []
    finally:
        env.cleanup()


def test_valid_wait_refuses_without_lifecycle(tmp_path):
    env = _FakeEnv(tmp_path)
    try:
        result = env.run(["--wait", "08"], unit_loaded=True)
        assert result.returncode == 1
        assert env.launchctl_calls() == []
    finally:
        env.cleanup()
