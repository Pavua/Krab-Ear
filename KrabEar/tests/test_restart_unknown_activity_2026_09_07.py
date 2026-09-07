"""Настоящий IPC/parser и shell gates с частным сокетом и fake launchctl.

HOME не меняется: тестовая копия заменяет только два литерала socket path.
Установщик исполняется лишь до границы мутации, без чтения credentials.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("safe_backend_restart.command", "install_backend_launchagent.command")


def reply(**fields):
    return json.dumps({"id": "1", "ok": True, "result": fields})


def run_gate(tmp_path, script, recording=None, meeting=None, *, missing=False,
             fragmented=False, wait=False, function_only=False, with_rest=False, force=False, silent=False, oversized=False):
    """Читаем real shell и IPC-код; все mutation-команды замыкаются на marker."""
    marker = tmp_path / "launchctl.log"
    source = (ROOT / "scripts" / script).read_text()
    stopped = threading.Event()
    with tempfile.TemporaryDirectory(prefix="ke-gate-", dir="/tmp") as short:
        sock_path = str(Path(short) / "ipc.sock")
        source = source.replace(
            'os.path.expanduser("~/Library/Application Support/KrabEar/krabear.sock")',
            repr(sock_path),
        )
        source = source.replace('"$HOME/Library/Application Support/KrabEar/krabear.sock"',
                                '"' + sock_path + '"')
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if not missing:
            server.bind(sock_path)
            server.listen(8)
            server.settimeout(0.1)

        def serve():
            while not stopped.is_set() and not missing:
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with conn:
                    conn.settimeout(1)
                    data = b""
                    try:
                        while b"\n" not in data:
                            part = conn.recv(4096)
                            if not part:
                                break
                            data += part
                        method = json.loads(data).get("method")
                        if method == "ping":
                            with marker.open("a") as log:
                                log.write("IPC:ping\n")
                        value = {"get_recording_state": recording,
                                 "get_meeting_live_state": meeting,
                                 "ping": reply(status="ok")}[method]
                        if silent and method == "get_recording_state":
                            stopped.wait(6)
                            continue
                        payload = value if isinstance(value, bytes) else ((value or "") + "\n").encode()
                        if oversized and method == "get_recording_state":
                            payload = b"x" * 1048577
                        if fragmented:
                            for pos in range(0, len(payload), 7):
                                conn.sendall(payload[pos:pos + 7])
                                stopped.wait(0.003)
                        else:
                            conn.sendall(payload)
                    except (OSError, ValueError, KeyError):
                        pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        (bindir / "python3").symlink_to(sys.executable)
        for name, body in {
            "launchctl": 'printf "%s\\n" "$*" >> "$GATE_TEST_MARKER"\nexit 0\n',
            "sleep": 'exit 0\n',
        }.items():
            p = bindir / name
            p.write_text("#!/bin/sh\n" + body)
            p.chmod(0o700)
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"],
                   GATE_TEST_MARKER=str(marker))
        if function_only or script.startswith("install_"):
            functions = []
            for name in ("ipc_call", "busy_reason"):
                match = re.search(rf"{name}\(\) \{{.*?\n\}}\n", source, re.S)
                assert match
                functions.append(match.group())
            if function_only:
                gate = ('if REASON=$(busy_reason); then printf "BLOCK:%s" "$REASON"; exit 1; '
                        'else launchctl bootout TEST_ONLY; fi\n')
            else:
                start = source.index('if [ "$FORCE" -eq 1 ]; then')
                end = min(source.index(mark, start) for mark in
                          ('# 1. Bootout', '# HF_TOKEN resolution order:') if mark in source[start:])
                gate = source[start:end] + '\nlaunchctl bootout TEST_ONLY\n'
            source = ("set -e\n" + "\n".join(functions)
                      + '\nlog() { printf "%s\\n" "$*"; }\n'
                      + 'fail() { printf "%s\\n" "$*" >&2; exit 1; }\n'
                      + f'FORCE={int(force)}; WAIT_SEC={int(wait)}; SOCKET="{sock_path}"\n'
                      + gate)
        driver = tmp_path / "driver.sh"
        driver.write_text(source)
        try:
            result = subprocess.run(["/bin/bash", str(driver), *(["--wait", "1"] if wait else []), *(["--with-rest"] if with_rest else [])],
                                    env=env, capture_output=True, text=True, timeout=8)
        finally:
            stopped.set()
            server.close()
            thread.join(timeout=2)
            assert not thread.is_alive()
        return result, marker.read_text() if marker.exists() else ""


BAD = [
    "", "not-json", "{}", '{"ok":false,"error":"failed"}',
    reply(), reply(is_recording="false", active="false"),
    reply(is_recording=0, active=0),
    reply(is_recording=False, active=False, privacy_mode_active=True),
    '{"id":"wrong","ok":true,"result":{"is_recording":false,"active":false}}',
    '{"id":"1","ok":true,"result":{"is_recording":false,"is_recording":true,"active":false}}',
    '{"id":"1","ok":true,"result":{"is_recording":false,"active":false},"ok":false}',
    reply(ok=False, is_recording=False, active=False),
    '{"ok":true,"result":{"is_recording":false,"active":false},"error":"failed"}',
    'garbage "is_recording": false, "active": false',
    '{"id":"1","ok":true,"result":{"is_recording":false,"active":false},"error":{"message":"failed"}}',
    b'{"id":"1","ok":true,"result":{"is_recording":false,"active":false}}',
]


@pytest.mark.parametrize("script", SCRIPTS)
@pytest.mark.parametrize("bad", BAD)
@pytest.mark.parametrize("which", ["recording", "meeting"])
def test_unknown_activity_never_reaches_mutation(tmp_path, script, bad, which):
    rec = bad if which == "recording" else reply(is_recording=False)
    meet = bad if which == "meeting" else reply(ok=True, active=False)
    result, calls = run_gate(tmp_path, script, rec, meet)
    assert result.returncode == 1, result.stdout + result.stderr
    assert not calls, calls


@pytest.mark.parametrize("script", SCRIPTS)
def test_verified_idle_allows_mutation(tmp_path, script):
    result, calls = run_gate(tmp_path, script, reply(is_recording=False), reply(ok=True, active=False))
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls


@pytest.mark.parametrize("script", SCRIPTS)
def test_fragmented_activity_reply_is_read_completely(tmp_path, script):
    result, calls = run_gate(tmp_path, script, reply(is_recording=False), reply(ok=True, active=True),
                             fragmented=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert not calls


@pytest.mark.parametrize("script", SCRIPTS)
def test_missing_socket_does_not_prove_idle(tmp_path, script):
    result, calls = run_gate(tmp_path, script, missing=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert not calls


def test_installer_gate_is_after_interactive_token_resolution():
    source = (ROOT / "scripts/install_backend_launchagent.command").read_text()
    prompt = source.index("read -r HF_TOKEN")
    gate = source.index("while REASON=$(busy_reason)")
    stop = source.index('launchctl bootout "gui/$UID_NUM/$LABEL"')
    assert prompt < gate < stop


@pytest.mark.parametrize("script", SCRIPTS)
def test_wait_unknown_expires_without_mutation(tmp_path, script):
    result, calls = run_gate(tmp_path, script, missing=True, wait=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert not calls


def test_installer_force_is_explicit_existing_bypass(tmp_path):
    result, calls = run_gate(tmp_path, SCRIPTS[1], missing=True, force=True)
    assert result.returncode == 0
    assert "--force" in result.stdout
    assert calls


def test_both_scripts_use_identical_ipc_and_busy_functions():
    sources = [(ROOT / "scripts" / script).read_text() for script in SCRIPTS]
    for name in ("ipc_call", "busy_reason"):
        pattern = rf"{name}\(\) \{{.*?\n\}}\n"
        assert re.search(pattern, sources[0], re.S).group() == re.search(pattern, sources[1], re.S).group()


@pytest.mark.parametrize("script", SCRIPTS)
@pytest.mark.parametrize("mode", ["silent", "oversized"])
def test_transport_limits_refuse_restart(tmp_path, script, mode):
    result, calls = run_gate(tmp_path, script, reply(is_recording=False), reply(ok=True, active=False),
                             **{mode: True})
    assert result.returncode == 1, result.stdout + result.stderr
    assert not calls
