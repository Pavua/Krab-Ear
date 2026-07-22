"""Контракт безопасного рестарта production-бэкенда Krab Ear.

Скрипт обязан распознавать фактические IPC-схемы обычной записи и live meeting
до любого обращения к launchctl. Тест запускает настоящий shell-скрипт, но
подменяет Python/launchctl/sleep внутри временного HOME, поэтому живые сервисы
и пользовательские данные принципиально недоступны.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SAFE_RESTART = ROOT / "scripts" / "safe_backend_restart.command"


def _write_executable(path: Path, source: str) -> None:
    """Создать изолированную command-заглушку для subprocess-теста."""
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)


def test_active_meeting_boolean_refuses_restart(tmp_path: Path) -> None:
    """IPC ``active: true`` останавливает скрипт до вызова launchctl."""
    fake_home = tmp_path / "home"
    real_socket_path = fake_home / "Library/Application Support/KrabEar/krabear.sock"
    real_socket_path.parent.mkdir(parents=True)
    # AF_UNIX на macOS ограничивает путь примерно 104 байтами; pytest tmp_path
    # длиннее. Короткая временная ссылка сохраняет изоляцию и реальную схему HOME.
    short_home = Path("/tmp") / f"krab-safe-{uuid.uuid4().hex}"
    short_home.symlink_to(fake_home, target_is_directory=True)
    socket_path = short_home / "Library/Application Support/KrabEar/krabear.sock"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launchctl_marker = tmp_path / "launchctl-called"

    _write_executable(
        fake_bin / "python3",
        """#!/bin/sh
case "$2" in
  get_recording_state) printf '%s\n' "$FAKE_RECORDING_RESPONSE" ;;
  get_meeting_live_state) printf '%s\n' "$FAKE_MEETING_RESPONSE" ;;
  ping) printf '%s\n' '{"ok": true}' ;;
esac
""",
    )
    _write_executable(
        fake_bin / "launchctl",
        """#!/bin/sh
printf 'called\n' >> "$FAKE_LAUNCHCTL_MARKER"
exit 0
""",
    )
    _write_executable(fake_bin / "sleep", "#!/bin/sh\nexit 0\n")

    ipc_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    ipc_socket.bind(str(socket_path))
    try:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(short_home),
                "PATH": f"{fake_bin}:{env['PATH']}",
                "FAKE_LAUNCHCTL_MARKER": str(launchctl_marker),
                "FAKE_RECORDING_RESPONSE": json.dumps(
                    {"id": "1", "ok": True, "result": {"is_recording": False}}
                ),
                "FAKE_MEETING_RESPONSE": json.dumps(
                    {
                        "id": "1",
                        "ok": True,
                        "result": {"ok": True, "active": True},
                    }
                ),
            }
        )
        completed = subprocess.run(
            [str(SAFE_RESTART)],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    finally:
        ipc_socket.close()
        short_home.unlink(missing_ok=True)

    assert completed.returncode == 1
    assert "активная сессия (meeting)" in completed.stderr
    assert not launchctl_marker.exists()
