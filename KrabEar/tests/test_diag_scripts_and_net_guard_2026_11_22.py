"""G2-KET10-T10-04/05/06, G2-KET09-F03/F05: сторож сети, soak и диагностические скрипты.

* T10-04: ``startswith("127.")`` принимал хосты-имена вроде ``127.evil.example`` за loopback.
* T10-05: ``soak_backend.ipc_call`` без таймаута — зависший backend вешал прогон навсегда.
* T10-06: soak отчитывался ``ok`` без id созданной записи и при пустой итоговой истории.
* F03/F05: ``KrabEar/scripts/test_context.py``/``test_stt.py`` печатали транскрипты/JSON и
  завершались кодом 0 при HTTP-ошибках.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = ROOT / "KrabEar" / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


# ── T10-04: сторож сети ─────────────────────────────────────────────────────


class TestLoopbackClassifier:
    @pytest.fixture(scope="class")
    def is_loopback(self):
        return importlib.import_module("conftest")._is_loopback_address

    @pytest.mark.parametrize("host", ["127.0.0.1", "127.1.2.3", "::1", "localhost", "0.0.0.0", "::ffff:127.0.0.1"])
    def test_real_loopback_is_allowed(self, is_loopback, host):
        assert is_loopback((host, 80)) is True

    @pytest.mark.parametrize("host", ["127.evil.example.com", "8.8.8.8", "10.0.0.5", "example.com", "128.0.0.1"])
    def test_lookalike_and_external_hosts_are_blocked(self, is_loopback, host):
        assert is_loopback((host, 80)) is False

    def test_bytes_host_inside_tuple_is_not_trusted(self, is_loopback):
        assert is_loopback((b"example.com", 80)) is False

    def test_unix_socket_paths_stay_allowed(self, is_loopback):
        assert is_loopback("/tmp/krabear.sock") is True
        assert is_loopback(b"\0abstract") is True


# ── T10-05/06: soak_backend ─────────────────────────────────────────────────


class TestSoakBackendHelpers:
    @pytest.fixture(scope="class")
    def soak(self):
        return importlib.import_module("soak_backend")

    def test_ipc_call_times_out_on_a_hung_backend(self, soak):
        with tempfile.TemporaryDirectory(prefix="ke-soak-", dir="/tmp") as short:
            path = Path(short) / "hang.sock"
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(path))
            srv.listen(1)
            conns = []

            def accept():
                try:
                    conns.append(srv.accept()[0])  # принимает и молчит
                except OSError:
                    pass

            thread = threading.Thread(target=accept, daemon=True)
            thread.start()
            started = time.monotonic()
            try:
                with pytest.raises((TimeoutError, socket.timeout, OSError)):
                    soak.ipc_call(path, "ping", timeout=0.5)
            finally:
                srv.close()
                for conn in conns:
                    conn.close()
            assert time.monotonic() - started < 5, "ipc_call завис без таймаута"

    def test_item_id_is_required(self, soak):
        assert soak.extract_item_id({"result": {"id": "abc"}}) == "abc"
        for bad in ({"result": {}}, {"result": {"id": None}}, {"result": {"id": 5}}, {}):
            with pytest.raises(RuntimeError):
                soak.extract_item_id(bad)

    def test_empty_final_history_with_created_items_is_a_failure(self, soak):
        ok, error = soak.final_history_check(created_ids=["a", "b"], final_page_count=0)
        assert ok is False and error
        assert soak.final_history_check(created_ids=["a"], final_page_count=1) == (True, "")
        assert soak.final_history_check(created_ids=[], final_page_count=0) == (True, "")


# ── F03/F05: диагностические скрипты ────────────────────────────────────────

SECRET_TEXT = "СЕКРЕТНЫЙ-ТРАНСКРИПТ-4242"


class _Rest(BaseHTTPRequestHandler):
    status = 200

    def _reply(self):
        body = json.dumps({"text": SECRET_TEXT, "confidence": 0.9, "words": ["a"], "segments": []}).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._reply()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._reply()

    def log_message(self, *args, **kwargs):
        return


def _run_diag(script: str, status: int, tmp_path: Path) -> subprocess.CompletedProcess:
    handler = type("H", (_Rest,), {"status": status})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        (tmp_path / "test_audio.wav").write_bytes(b"RIFF....WAVEfmt ")
        env = dict(os.environ, KRAB_EAR_REST_URL=f"http://127.0.0.1:{srv.server_address[1]}")
        return subprocess.run([sys.executable, str(ROOT / "KrabEar" / "scripts" / script)], cwd=tmp_path, env=env,
                              capture_output=True, text=True, timeout=60)
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("script", ["test_context.py", "test_stt.py"])
class TestDiagnosticScripts:
    def test_http_error_gives_nonzero_exit(self, script, tmp_path):
        res = _run_diag(script, 500, tmp_path)
        assert res.returncode != 0, res.stdout + res.stderr

    def test_success_exits_zero_without_printing_transcript_content(self, script, tmp_path):
        res = _run_diag(script, 200, tmp_path)
        assert res.returncode == 0, res.stdout + res.stderr
        assert SECRET_TEXT not in res.stdout + res.stderr, "содержимое транскрипта попало в вывод"
