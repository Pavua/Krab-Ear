"""A5.3 D: настоящий изолированный IPC + Swift writer, без рабочего профиля."""
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid

import pytest

from _plaintext_export_integration_backend import MARKER, SENTINELS, run_child

pytestmark = pytest.mark.timeout(150)


class IsolatedBackend:
    def __init__(self, root):
        self.root = Path(root)
        self.process = self.connection = None
        self.ready = False

    def start(self):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        self.connection = parent
        source = Path(__file__).resolve().parents[1]
        self.process = context.Process(target=run_child, args=(child, str(self.root), str(source)))
        self.process.start()
        child.close()
        ready = self.receive()
        assert ready == {"ready": True, "mode": 0o600}, "isolated listener did not become ready"
        self.ready = True
        return self

    def receive(self):
        assert self.connection.poll(40), "isolated child/control deadline"
        result = self.connection.recv()
        if "error_type" in result:
            print(json.dumps({"fixture_failure_metadata": result}))
        assert "error_type" not in result, "isolated child failed at " + result.get("stage", "unknown")
        return result

    def control(self, command, **params):
        self.connection.send({"command": command, **params})
        return self.receive()["result"]

    def call(self, method, **params):
        # Даже pytest showlocals=False выводит **kwargs через funcargs.
        failed = False
        result = None
        payload = b""
        try:
            payload = json.dumps({"id": "integration", "method": method, "params": params}).encode()
            result = self.raw(payload)
        except Exception:
            failed = True
        finally:
            params.clear()
            method = ""
            payload = b""
        if failed:
            raise AssertionError("isolated IPC request failed") from None
        return result

    def raw(self, payload):
        failed = False
        result = None
        parts = bytearray()
        data = b""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(20)
                peer.connect(str(self.root / "ipc.sock"))
                peer.sendall(payload + b"\n")
                while b"\n" not in parts:
                    data = peer.recv(65536)
                    assert data, "isolated IPC closed before response"
                    parts.extend(data)
                result = json.loads(parts.split(b"\n", 1)[0])
            for value in SENTINELS:
                assert value not in json.dumps(result), "sentinel escaped real socket response"
        except Exception:
            failed = True
            result = None
        finally:
            payload = b""
            data = b""
            parts.clear()
        # Вне except: JSONDecodeError с raw response не остаётся в цепочке.
        if failed:
            raise AssertionError("isolated IPC transport failed") from None
        return result

    def grant(self):
        policy = self.call("get_plaintext_export_policy")["result"]
        session = str(uuid.uuid4())
        result = self.call("grant_plaintext_export_session", app_session_id=session,
                           expected_epoch=policy["epoch"], expected_policy_generation=policy["policy_generation"])["result"]
        assert result["ok"] is True
        return {"app_session_id": session, "epoch": result["epoch"], "capability": result["capability"],
                "expected_policy_generation": result["policy_generation"]}

    def scan_streams(self, blob):
        # Не оставляем blob в generic control kwargs / multiprocessing frames
        # при отказе. Новый generic failure не цепляет исходный traceback.
        failure = False
        verdict = None
        payload = {"command": "scan_streams", "blob": blob}
        try:
            self.connection.send(payload)
            payload.clear()
            blob = ""
            verdict = self.receive()["result"]
        except BaseException:
            failure = True
        finally:
            payload.clear()
            blob = ""
        if failure:
            raise AssertionError("dynamic stream scan unavailable") from None
        return verdict

    def close(self):
        if self.process is None:
            return
        inspection_failed = self.ready and not self.process.is_alive()
        try:
            if not self.ready:
                self.process.join(timeout=5)
            elif self.process.is_alive():
                if self.ready:
                    try:
                        inspection = self.control("inspect")
                        inspection_failed = not (
                            inspection["guard_violations"] == 0
                            and inspection["leaked_files"] == 0
                            and inspection["logs_clean"] is True)
                    except BaseException:
                        inspection_failed = True
                # Даже отказ inspection не пропускает нормальный service.close().
                self.connection.send({"command": "stop"})
                stopped = self.receive()
                assert stopped == {"stopped": True, "guard_violations": 0}, "isolated teardown failed"
                self.process.join(timeout=10)
        finally:
            if self.process.is_alive():
                self.process.terminate()  # Только созданный этим fixture child.
                self.process.join(timeout=5)
            else:
                self.process.join(timeout=5)
            inspection_failed = inspection_failed or self.process.exitcode != 0
            self.connection.close()
            self.process = None
            self.ready = False
        assert not inspection_failed, "pre-stop secret inspection failed"


@pytest.fixture(scope="module")
def backend():
    # AF_UNIX macOS pathname limit: pytest tmp_path может оказаться >104 bytes.
    with tempfile.TemporaryDirectory(prefix="a53d-", dir="/tmp") as directory:
        child = IsolatedBackend(directory)
        try:
            yield child.start()
        finally:
            child.close()


@pytest.fixture(scope="module")
def swift_harness(tmp_path_factory):
    if sys.platform != "darwin":
        pytest.skip("Darwin IPCClient integration executes on macOS")
    compiler = shutil.which("swiftc")
    if compiler is None:
        pytest.skip("Swift toolchain unavailable")
    root = Path(__file__).resolve().parents[2] / "native/KrabEarAgent"
    compile_root = tmp_path_factory.mktemp("a53-swift")
    binary = compile_root / "integration"
    (compile_root / "module-cache").mkdir()
    env = synthetic_subprocess_env(compile_root)
    result = subprocess.run([
        compiler, "-swift-version", "5", "-parse-as-library",
        "-module-cache-path", str(compile_root / "module-cache"),
        str(root / "Sources/KrabEarAgent/IPCClient.swift"),
        str(root / "Sources/KrabEarAgent/PlaintextExportCoordinator.swift"),
        str(root / "Tests/Integration/PlaintextExportIntegrationHarness.swift"), "-o", str(binary),
    ], capture_output=True, text=True, timeout=120, env=env)
    assert result.returncode == 0, "Swift fixture compilation failed (diagnostic withheld)"
    return binary


def synthetic_subprocess_env(root):
    """Native Swift subprocess получает собственные home/cache/temp пути."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("KRAB_EAR_", "SENTRY_"))
           and not key.endswith(("_TOKEN", "_API_KEY", "_SECRET"))
           and key not in {"AWS_ACCESS_KEY_ID", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACEHUB_API_TOKEN"}}
    for name in ("home", "tmp", "cache"):
        (root / name).mkdir(exist_ok=True)
    env.update(HOME=str(root / "home"), TMPDIR=str(root / "tmp"),
               TMP=str(root / "tmp"), TEMP=str(root / "tmp"), XDG_CACHE_HOME=str(root / "cache"))
    return env


def assert_secret_free_streams(backend, stdout, stderr, *, receipt=True):
    # Проверяем ДО JSON parse/assertions: pytest не должен показать secret stream.
    try:
        scan = backend.scan_streams(stdout + stderr)
    finally:
        stdout = stderr = ""
    assert scan == {"clean": True, "positive_control": True,
                    "auth_kinds": {"capability": True, "receipt": receipt, "app_session_id": True}}, \
        "Swift stream failed dynamic secret check"


def test_synthetic_seed_exists_exactly_once(backend):
    assert backend.control("seed_count") == {"count": 1, "marker_count": 1, "has_more": False}


@pytest.mark.parametrize("phase,writes", [("before", 0), ("after", 1)])
def test_swift_real_socket_revocation_linearization(backend, swift_harness, phase, writes):
    output = backend.root / ("swift-" + phase + ".txt")
    backend.control("arm", phase=phase)
    process = subprocess.Popen([str(swift_harness), str(backend.root / "ipc.sock"), str(output), str(writes)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               env=synthetic_subprocess_env(backend.root))
    try:
        assert backend.control("barrier") is True
        assert backend.control("revoke_release") is True
        stdout, stderr = process.communicate(timeout=20)
        assert_secret_free_streams(backend, stdout, stderr, receipt=phase == "after")
        assert process.returncode == 0, "Swift real-socket harness failed"
        result = json.loads(stdout)
        assert result == {"writes": writes, "repeated_callback_denied": True}
        assert not any(secret in stdout + stderr for secret in SENTINELS)
        assert output.exists() is bool(writes)
        if writes:
            assert output.read_text() == MARKER
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)


def test_real_epoch_session_and_sequence_replay(backend):
    context = backend.grant()
    params = {**context, "operation_seq": 1, "sink_kind": "history-md"}
    first = backend.call("validate_plaintext_export", **params)["result"]
    assert first["ok"] is True and first["receipt"]
    assert backend.call("validate_plaintext_export", **params)["result"]["ok"] is False
    assert backend.call("validate_plaintext_export", **{**params, "operation_seq": 2,
                        "app_session_id": str(uuid.uuid4())})["result"]["ok"] is False
    backend.close()
    backend.start()
    assert backend.control("seed_count") == {"count": 1, "marker_count": 1, "has_more": False}
    policy = backend.call("get_plaintext_export_policy")["result"]
    assert policy["epoch"] != context["epoch"]
    stale = {**params, "operation_seq": 3, "expected_policy_generation": policy["policy_generation"]}
    assert backend.call("validate_plaintext_export", **stale)["result"]["ok"] is False


@pytest.mark.parametrize("kind", ["batch", "obsidian"])
def test_real_dispatcher_per_file_gate_and_partial(backend, kind):
    items = [{"id": "one", "ts": "2026-01-01T01:00:00+00:00", "text": MARKER},
             {"id": "two", "ts": "2026-01-01T02:00:00+00:00", "text": MARKER}]
    count = 3 if kind == "batch" else 2
    for mode in ("deny", "full", "partial"):
        target = Path(backend.control("write_probe", directory=kind + "-" + mode, partial=mode == "partial"))
        context = {} if mode == "deny" else {"plaintext_export": backend.grant()}
        method = "batch_export" if kind == "batch" else "run_obsidian_sync"
        params = {"formats": ["markdown", "srt", "csv"], "output_dir": str(target)} if kind == "batch" else {"items": items}
        result = backend.call(method, **params, **context, force=True)["result"]
        metrics = backend.control("metrics")
        files = list(target.rglob("*")) if target.exists() else []
        actual_files = [path for path in files if path.is_file()]
        expected = 0 if mode == "deny" else 1 if mode == "partial" else count
        assert metrics == {"writes": expected, "gates": expected}
        assert len(actual_files) == expected
        if mode == "deny":
            assert not target.exists() and result.get("reason") == "plaintext_confirmation_required"
        elif mode == "partial":
            assert result["partial"] is True and result["reason"] == "plaintext_session_expired"
            if kind == "obsidian":
                assert result["synced_count"] == 1
            else:
                assert list(result["files"]) == ["markdown"]
        else:
            assert not result.get("errors")
        assert all(MARKER in path.read_text() for path in actual_files)


def test_real_error_paths_and_persistence_are_secret_free(backend):
    context = backend.grant()
    granted = backend.call("validate_plaintext_export", **context, operation_seq=1, sink_kind="history-md")["result"]
    assert granted["ok"]
    auth = dict(zip(("capability", "receipt", "app_session_id"), SENTINELS))
    assert backend.call("fixture_error", plaintext_export=auth)["error"]["code"] == "internal_error"
    assert backend.call("fixture_runtime_error", plaintext_export=auth)["error"]["code"] == "invalid_request"
    assert backend.call("fixture_socket_failure", plaintext_export=auth)["error"]["code"] == "internal_error"
    backend.call(SENTINELS[0], plaintext_export=auth)
    backend.raw(json.dumps({"method": "ping", "params": [auth]}).encode())
    backend.raw(b'{"method":"ping","params":{"capability":"' + SENTINELS[0].encode() + b'"},')
    backend.control("signing", deny=True)
    try:
        response = backend.call("ping", plaintext_export=auth)
        assert response["error"]["code"] == "unauthorized"
    finally:
        backend.control("signing", deny=False)
    for method in ("list_recent_errors", "get_audit_log"):
        response = backend.call(method)
        assert_secret_free_streams(backend, json.dumps(response), "")
    exported = backend.call("export_settings")["result"]
    backup = backend.call("create_manual_settings_backup", reason="integration")["result"]
    assert exported["settings_count"] > 0 and backup["backup_id"]
    data = json.loads((backend.root / "profile/settings.json").read_text())
    assert isinstance(data["_plaintext_export_policy_revision"], str)
    for key in ("capability", "receipt", "app_session_id"):
        assert key not in data
    inspection = backend.control("inspect")
    assert inspection["guard_violations"] == 0
    assert inspection["leaked_files"] == 0 and inspection["logs_clean"] is True
    assert inspection["fault_events"] >= 1, "actual TypeError→logging→fake Sentry path was not exercised"
    allowed = ("profile/history", "swift-after.txt", "batch-full/", "batch-partial/",
               "obsidian-full/", "obsidian-partial/")
    assert all(path.startswith(allowed) for path in inspection["marker_paths"])


def test_swift_unsanitized_error_after_actual_validate_is_secret_free(backend, swift_harness):
    output = backend.root / "swift-raw-error.txt"
    backend.control("raw_error")
    result = subprocess.run([str(swift_harness), str(backend.root / "ipc.sock"), str(output), "0"],
                            capture_output=True, text=True, timeout=20,
                            env=synthetic_subprocess_env(backend.root))
    assert_secret_free_streams(backend, result.stdout, result.stderr)
    assert result.returncode == 0, "Swift raw error harness failed"
    assert json.loads(result.stdout) == {"writes": 0, "repeated_callback_denied": True}
    assert backend.control("raw_error_metrics") == {"raw_error_injections": 1, "validations": 1}
    assert not output.exists()
    inspection = backend.control("inspect")
    assert inspection["guard_violations"] == 0
    assert inspection["leaked_files"] == 0 and inspection["logs_clean"] is True


def test_stream_checker_failure_traceback_does_not_expose_arguments():
    """Настоящий pytest formatter обязан видеть очищенные helper args."""
    secret = "probe-" + uuid.uuid4().hex

    def unsafe_control(raw_stream):
        raise AssertionError("formatter positive control")

    with pytest.raises(AssertionError) as positive:
        unsafe_control(secret)
    exposed = secret in str(positive.getrepr(style="long", funcargs=True, showlocals=False))

    class Scanner:
        def scan_streams(self, _blob):
            return {"clean": False, "positive_control": True,
                    "auth_kinds": {"capability": True, "receipt": True, "app_session_id": True}}

    with pytest.raises(AssertionError) as negative:
        assert_secret_free_streams(Scanner(), secret, secret)
    clean = secret not in str(negative.getrepr(style="long", funcargs=True, showlocals=False))
    secret = ""
    assert exposed, "formatter positive control was not exercised"
    assert clean, "scanner failure exposed a raw stream argument"


@pytest.mark.parametrize("fault", ["eof", "timeout", "decode"])
@pytest.mark.parametrize("entrypoint", ["raw", "call"])
def test_ipc_transport_failure_traceback_does_not_expose_auth(monkeypatch, tmp_path, fault, entrypoint):
    """Обычные IPC failures не раскрывают request args через pytest formatter."""
    secret = "transport-" + uuid.uuid4().hex
    params = {"plaintext_export": {"app_session_id": secret, "capability": secret, "receipt": secret}}
    payload = json.dumps({"method": "validate_plaintext_export", "params": params}).encode()

    def unsafe_transport(raw_payload):
        raise AssertionError("formatter positive control")

    with pytest.raises(AssertionError) as positive:
        unsafe_transport(payload)
    exposed = secret in str(positive.getrepr(style="long", funcargs=True, showlocals=False))

    class FailedPeer:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def settimeout(self, _timeout):
            pass

        def connect(self, _path):
            pass

        def sendall(self, _request):
            pass

        def recv(self, _size):
            if fault == "timeout":
                raise TimeoutError("synthetic transport timeout")
            return b"" if fault == "eof" else ("invalid-json-" + secret + "\n").encode()

    client = IsolatedBackend(tmp_path)
    with monkeypatch.context() as patcher:
        patcher.setattr(socket, "socket", lambda *_args, **_kwargs: FailedPeer())
        with pytest.raises(Exception) as negative:
            if entrypoint == "raw":
                client.raw(payload)
            else:
                client.call("validate_plaintext_export", **params)
    clean = secret not in str(negative.getrepr(style="long", funcargs=True, showlocals=False))
    payload = b""
    params.clear()
    secret = ""
    assert exposed, "transport formatter positive control was not exercised"
    assert clean, "IPC transport failure exposed an auth-bearing argument"


def _exercise_ml_import_guard(connection, prefix, submodule):
    """Fresh child: installed-like loader только считает marker, без native SDK."""
    import importlib
    from importlib.abc import MetaPathFinder, Loader
    from importlib.machinery import ModuleSpec
    from _plaintext_export_integration_backend import cpu_only_optional_imports

    loaded = []

    class InstalledLoader(Loader):
        def create_module(self, spec):
            return None

        def exec_module(self, module):
            loaded.append(module.__name__)

    class InstalledFinder(MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == prefix or fullname.startswith(prefix + "."):
                return ModuleSpec(fullname, InstalledLoader(), is_package=True)
            return None

    finder = InstalledFinder()
    sys.meta_path.insert(0, finder)
    name = prefix + ".a53_probe" if submodule else prefix
    try:
        before = 0
        blocked = False
        with cpu_only_optional_imports():
            try:
                importlib.import_module(name)
            except ModuleNotFoundError:
                blocked = True
            before = len(loaded)
        # Positive control: тот же настоящий import проходит installed-like loader.
        importlib.import_module(name)
        positive = len(loaded) > 0
        cached_blocked = False
        try:
            with cpu_only_optional_imports():
                importlib.import_module(name)
        except (AssertionError, ModuleNotFoundError):
            cached_blocked = True
        connection.send({"blocked": blocked, "loader_runs_under_guard": before,
                         "positive_control": positive, "cached_blocked": cached_blocked})
    except BaseException as error:
        connection.send({"error_type": type(error).__name__})
    finally:
        sys.meta_path.remove(finder)
        connection.close()


@pytest.mark.parametrize("prefix", ["torch", "mlx", "mlx_whisper", "pyannote", "numba", "cuda"])
@pytest.mark.parametrize("submodule", [False, True])
def test_cpu_fixture_blocks_installed_optional_ml_imports(prefix, submodule):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_exercise_ml_import_guard, args=(child, prefix, submodule))
    process.start()
    child.close()
    try:
        assert parent.poll(30), "native import probe deadline"
        result = parent.recv()
        process.join(timeout=5)
        assert process.exitcode == 0
        assert result == {"blocked": True, "loader_runs_under_guard": 0,
                          "positive_control": True, "cached_blocked": True}
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        parent.close()
