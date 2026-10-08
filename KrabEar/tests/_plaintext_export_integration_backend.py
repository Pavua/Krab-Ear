"""Изолированный child для A5.3; до run_child импортируется только stdlib."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading
import tempfile
from importlib.machinery import ModuleSpec
from types import ModuleType, SimpleNamespace
from unittest import mock

MARKER = "A53_INTEGRATION_SYNTHETIC_TEXT"
SENTINELS = ("A53_CAPABILITY_SENTINEL", "A53_RECEIPT_SENTINEL", "A53_SESSION_SENTINEL")


def run_child(connection, root_string, source_string):
    """Настоящий IPCServer; control Pipe не пишет auth context в stdout/файлы."""
    root = Path(root_string).resolve()
    root.mkdir(parents=True, exist_ok=True)
    home = root / "home"
    home.mkdir(exist_ok=True)
    os.chdir(root)
    for key in tuple(os.environ):
        if key.startswith(("KRAB_EAR_", "SENTRY_")):
            del os.environ[key]
    os.environ.update({
        "KRAB_EAR_DATA_DIR": str(root / "profile"),
        "KRAB_EAR_SETTINGS_BACKUP_DIR": str(root / "settings_backups"),
        "KRAB_EAR_PRIVACY_AUDIT_DIR": str(root / "privacy_audit"),
        "KRAB_EAR_LLM_ENABLED": "false", "KRAB_EAR_EVENT_BRIDGE_ENABLED": "false",
        "KRAB_EAR_REST_IN_PROCESS_ENABLED": "false", "KRAB_EAR_RECAP_EMAIL_ENABLED": "false",
        "KRAB_EAR_AUTO_BACKUP_ENABLED": "false", "KRAB_EAR_DISK_MONITOR_ENABLED": "false",
        "KRAB_EAR_STT_GIGAAM_ENABLED": "false", "KRAB_EAR_STT_WARMUP_ENABLED": "false",
        "XDG_CACHE_HOME": str(home / "cache"), "MPLCONFIGDIR": str(home / "matplotlib"),
        "PYTHONDONTWRITEBYTECODE": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
    })
    sys.dont_write_bytecode = True
    sys.path.insert(0, source_string)
    os.environ.update(HOME=str(home), TMPDIR=str(root / "tmp"),
                      TMP=str(root / "tmp"), TEMP=str(root / "tmp"))
    (root / "tmp").mkdir(exist_ok=True)
    tempfile.tempdir = str(root / "tmp")
    for key in tuple(os.environ):
        if key.endswith(("_TOKEN", "_API_KEY", "_SECRET")) or key in {
            "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACEHUB_API_TOKEN", "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        }:
            os.environ.pop(key, None)
    original_expanduser = os.path.expanduser
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_bind = socket.socket.bind
    socket_path = root / "ipc.sock"
    violations, violation_sites = [], []

    def record_violation(kind):
        violations.append(kind)
        frame = sys._getframe(1)
        site = []
        for _ in range(10):
            if frame is None:
                break
            site.append({"file": Path(frame.f_code.co_filename).name,
                         "function": frame.f_code.co_name, "line": frame.f_lineno})
            frame = frame.f_back
        violation_sites.append({"kind": kind, "site": site})
    fixture_root = root.resolve()

    def audit_isolation(event, args):
        # Child-only audit hook: код/dependencies можно читать, persistent writes
        # и чтение чужого settings/history/secrets профиля запрещены до imports.
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            writable = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            private_name = path.name in {".env", ".secrets", "settings.json", "history.ndjson"}
            if not path.is_relative_to(fixture_root) and (writable or private_name):
                record_violation("filesystem")
                raise AssertionError("fixture forbids external persistent access")
        elif event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.exec", "os.fork"}:
            record_violation("process")
            raise AssertionError("fixture forbids child process spawning")
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.link", "os.symlink"}:
            targets = args[:2] if event in {"os.rename", "os.link", "os.symlink"} else args[:1]
            for target in targets:
                if isinstance(target, (str, bytes, os.PathLike)) and not Path(os.fsdecode(target)).resolve().is_relative_to(fixture_root):
                    record_violation("filesystem")
                    raise AssertionError("fixture forbids external filesystem mutation")

    sys.addaudithook(audit_isolation)

    def isolated_expanduser(value):
        text = os.fsdecode(value)
        result = str(home) + text[1:] if text == "~" or text.startswith("~/") else original_expanduser(value)
        return os.fsencode(result) if isinstance(value, bytes) else result

    def checked_socket(sock, address, operation):
        if sock.family != socket.AF_UNIX or Path(os.fsdecode(address)).resolve() != socket_path.resolve():
            record_violation("network")
            raise AssertionError("fixture forbids unowned socket endpoint")
        return operation(sock, address)

    def forbidden_datagram(*_args, **_kwargs):
        record_violation("network")
        raise AssertionError("fixture forbids datagram network")

    # До backend imports: настоящий sounddevice нельзя импортировать даже для
    # коллекции, поскольку импорт вызывает native PortAudio initialization.
    audio = ModuleType("sounddevice")
    audio.__spec__ = ModuleSpec("sounddevice", loader=None, origin="a53-no-native-audio")
    audio.default = SimpleNamespace(device=(-1, -1), samplerate=None)
    def forbidden_audio(*_args, **_kwargs):
        record_violation("audio")
        raise AssertionError("fixture forbids native audio")
    for name in ("query_devices", "query_hostapis", "InputStream", "OutputStream",
                 "RawInputStream", "play", "rec", "stop"):
        setattr(audio, name, forbidden_audio)
    audio.PortAudioError = RuntimeError
    sys.modules["sounddevice"] = audio

    def forbidden_provider(*_args, **_kwargs):
        record_violation("keychain_or_crypto")
        raise AssertionError("fixture forbids live crypto provider")

    service = server = claim = server_thread = sentry_client = None
    release = threading.Event()
    stage = "isolation"
    with (root / "child.log").open("a", encoding="utf-8") as log_file, \
            redirect_stdout(log_file), redirect_stderr(log_file), ExitStack() as stack:
        stack.enter_context(mock.patch.object(Path, "home", return_value=home))
        stack.enter_context(mock.patch("os.path.expanduser", side_effect=isolated_expanduser))
        stack.enter_context(mock.patch.object(socket.socket, "connect", lambda sock, address: checked_socket(sock, address, original_connect)))
        stack.enter_context(mock.patch.object(socket.socket, "connect_ex", lambda sock, address: checked_socket(sock, address, original_connect_ex)))
        stack.enter_context(mock.patch.object(socket.socket, "bind", lambda sock, address: checked_socket(sock, address, original_bind)))
        stack.enter_context(mock.patch.object(socket.socket, "sendto", forbidden_datagram))
        stack.enter_context(mock.patch.object(socket, "has_ipv6", False))
        try:
            stage = "imports"
            from backend import crypto_keystore, history_crypto, memory_ledger
            stack.enter_context(mock.patch.object(crypto_keystore, "_run_security", forbidden_provider))
            stack.enter_context(mock.patch.object(history_crypto, "build_history_crypto", forbidden_provider))
            memory_ledger._TEST_PATH_OVERRIDE = root / "ledger"
            from backend.service import BackendService
            from backend.state_store import StateStore
            from backend.startup_diagnostics import StartupDiagnostics
            from backend.ipc_server import IPCServer
            from backend.socket_ownership import SocketOwnershipClaim
            from backend import observability
            import sentry_sdk
            from sentry_sdk.integrations.logging import LoggingIntegration
            from sentry_sdk.transport import Transport

            captured_logs, events, secrets = [], [], set(SENTINELS)
            auth_values = {key: set() for key in ("capability", "receipt", "app_session_id")}

            class Capture(logging.Handler):
                def emit(self, record):
                    captured_logs.append(self.format(record))
                    captured_logs.append(repr(vars(record)))

            class LocalTransport(Transport):
                def capture_envelope(self, envelope):
                    event = envelope.get_event()
                    if event is not None:
                        events.append(event)

            capture = Capture()
            logging.getLogger().addHandler(capture)
            stack.callback(logging.getLogger().removeHandler, capture)
            sentry_client = sentry_sdk.Client(
                dsn="https://public@example.invalid/1", transport=LocalTransport,
                release="a53-synthetic-fixture", environment="fixture", server_name="fixture",
                default_integrations=False, auto_enabling_integrations=False,
                integrations=[LoggingIntegration(level=None, event_level=logging.ERROR)],
                before_send=observability._sentry_before_send, include_local_variables=False,
            )
            sentry_sdk.get_global_scope().set_client(sentry_client)
            stage = "service"
            fresh_profile = not (root / "profile").exists()
            store = StateStore(root / "profile")
            store.initialize_startup_plaintext_policy(new_profile=fresh_profile)
            store.save_settings({"history_encryption_enabled": False, "privacy_mode_enabled": False})
            # Constructor уже создал пустой journal: existence не равен seed.
            active_items, _cursor = store.get_history_page(None, 2)
            if not active_items:
                store.add_history_item(text=MARKER, paste_status="ok")
            engine = SimpleNamespace(quality_profile="balanced", current_model="fixture-model",
                                     _llm_rewriter=None, _settings_get=None,
                                     _resolve_diarization_device=lambda: "cpu", warmup=lambda: None)
            report = SimpleNamespace(status="ready", errors=[], warnings=[], startup_time_ms=0, checks=[])
            # Ни один несвязанный daemon из constructor не исполняет свой target.
            # Thread.start восстановлен до настоящего server/handler thread ниже.
            with mock.patch.object(threading.Thread, "start", return_value=None), \
                    mock.patch("backend.service.PurgeScheduler.start", return_value=None), \
                    mock.patch.object(BackendService, "_check_binary_drift_on_startup", return_value=None), \
                    mock.patch.object(StartupDiagnostics, "run_all_checks", return_value=report), \
                    mock.patch.object(StartupDiagnostics, "_start_lm_studio_background_check", return_value=None):
                service = BackendService(
                    store=store,
                    recorder=SimpleNamespace(is_recording=False, start=lambda: None, stop=lambda: b""),
                    transcriber=SimpleNamespace(engine=engine, _error_bus=None), translator=mock.Mock(),
                )
            store.save_settings({"history_encryption_enabled": True, "privacy_mode_enabled": False})
            service._settings_svc.invalidate_cache()
            original_dispatch = service.handle_request
            reached = threading.Event()
            state = {"phase": None, "context": None, "writes": 0, "gates": 0,
                     "output": None, "revoke_after_first": False, "raw_error": False,
                     "raw_error_injections": 0, "validations": 0, "run_auth": None}

            def retain_auth(values):
                if isinstance(values, dict):
                    for key, value in values.items():
                        if key in auth_values and isinstance(value, str) and value:
                            secrets.add(value)
                            auth_values[key].add(value)
                            if state["run_auth"] is not None:
                                state["run_auth"][key].add(value)
                        elif isinstance(value, (dict, list)):
                            retain_auth(value)
                elif isinstance(values, list):
                    for value in values:
                        retain_auth(value)

            def revoke(context):
                return original_dispatch({"id": "control-revoke", "method": "revoke_plaintext_export_session", "params": {
                    key: context.get(key) for key in ("app_session_id", "epoch", "capability")
                }})

            def dispatcher(request):
                retain_auth(request)
                method = request.get("method") if isinstance(request, dict) else None
                params = request.get("params", {}) if isinstance(request, dict) else {}
                if method == "fixture_socket_failure":
                    raise TypeError(json.dumps({key: sorted(values) for key, values in auth_values.items()}))
                if isinstance(params, dict) and isinstance(params.get("plaintext_export"), dict):
                    state["context"] = params["plaintext_export"]
                armed = method == "validate_plaintext_export" and state["phase"] is not None
                if armed:
                    state["context"] = params.copy()
                    if state["phase"] == "before":
                        reached.set()
                        if not release.wait(15):
                            raise AssertionError("fixture validation barrier deadline")
                result = original_dispatch(request)
                retain_auth(result)
                if method == "validate_plaintext_export":
                    state["validations"] += 1
                    if state["raw_error"] and result.get("result", {}).get("ok") is True:
                        state["raw_error_injections"] += 1
                        state["raw_error"] = False
                        # Намеренно несanitized envelope только в fixture, ПОСЛЕ
                        # настоящего успешного validate и регистрации receipt.
                        result = {"id": request.get("id"), "ok": False, "error": {
                            "code": "fixture_raw_error", "message": " ".join(sorted(secrets))}}

                if armed and state["phase"] == "after":
                    reached.set()
                    if not release.wait(15):
                        raise AssertionError("fixture reply barrier deadline")
                if armed:
                    state["phase"] = None
                return result

            service.handle_request = dispatcher

            def intentional_type_error(_params):
                raise TypeError(json.dumps({key: sorted(values) for key, values in auth_values.items()}))
            def intentional_runtime_error(_params):
                raise RuntimeError(json.dumps({key: sorted(values) for key, values in auth_values.items()}))
            service._dispatch_table["fixture_error"] = intentional_type_error
            service._dispatch_table["fixture_runtime_error"] = intentional_runtime_error
            original_gate = service._plaintext_export_authorizer.precheck_backend_export

            def observed_gate(context, sink):
                # Считаем только финальный gate перед writer, не ранний precheck.
                caller = sys._getframe(1)
                final_gate = caller.f_code.co_name == "precheck_export" and caller.f_back.f_code.co_name == "run_export_write"
                result = original_gate(context, sink)
                if final_gate:
                    state["gates"] += 1
                return result
            service._plaintext_export_authorizer.precheck_backend_export = observed_gate
            original_write = Path.write_text

            def observed_write(path, *args, **kwargs):
                result = original_write(path, *args, **kwargs)
                target = state["output"]
                if target is not None and path.is_relative_to(target):
                    state["writes"] += 1
                    if state["revoke_after_first"] and state["writes"] == 1:
                        revoke(state["context"])
                return result
            stack.enter_context(mock.patch.object(Path, "write_text", observed_write))
            claim = SocketOwnershipClaim(socket_path)
            claim.acquire()
            ready = threading.Event()
            original_listening = claim.mark_listening

            def mark_listening():
                original_listening()
                ready.set()
            claim.mark_listening = mark_listening
            server = IPCServer(socket_path, service, ownership=claim)
            server_thread = threading.Thread(target=server.serve_forever, name="fixture-ipc")
            server_thread.start()
            if not ready.wait(15):
                raise AssertionError("fixture listener deadline")
            assert not violations
            connection.send({"ready": True, "mode": socket_path.stat().st_mode & 0o777})
            stage = "control"
            while True:
                command = connection.recv()
                name = command["command"]
                if name == "stop":
                    break
                if name == "arm":
                    release.clear()
                    reached.clear()
                    state["phase"] = command["phase"]
                    state["run_auth"] = {key: set() for key in auth_values}
                    reply = True
                elif name == "barrier":
                    reply = reached.wait(15)
                elif name == "revoke_release":
                    assert state["context"] is not None
                    revoke(state["context"])
                    release.set()
                    reply = True
                elif name == "write_probe":
                    target = root / command["directory"]
                    assert target.resolve().is_relative_to(root.resolve())
                    state.update(output=target, writes=0, gates=0, revoke_after_first=command.get("partial", False))
                    service._obsidian_sync._vault_path = target
                    service._obsidian_sync._folder = "notes"
                    service._obsidian_sync._last_sync_ts = None
                    reply = str(target)
                elif name == "seed_count":
                    items, cursor = store.get_history_page(None, 10)
                    reply = {"count": len(items), "marker_count": sum(item.get("text") == MARKER for item in items),
                             "has_more": cursor is not None}
                elif name == "raw_error":
                    state.update(raw_error=True, raw_error_injections=0, validations=0,
                                 run_auth={key: set() for key in auth_values})
                    reply = True
                elif name == "raw_error_metrics":
                    reply = {key: state[key] for key in ("raw_error_injections", "validations")}
                elif name == "scan_streams":
                    blob = command["blob"]
                    current_auth = state["run_auth"] or auth_values
                    issued = set().union(*current_auth.values())
                    def contains_secret(text):
                        return any(secret in text for secret in secrets)
                    reply = {"clean": not contains_secret(blob),
                             "positive_control": bool(issued) and all(contains_secret(value) for value in issued),
                             "auth_kinds": {key: bool(values - set(SENTINELS)) for key, values in current_auth.items()}}
                elif name == "metrics":
                    reply = {"writes": state["writes"], "gates": state["gates"]}
                elif name == "signing":
                    service._request_signer = mock.Mock() if command["deny"] else None
                    if service._request_signer is not None:
                        service._request_signer.verify_request.return_value = False
                    reply = True
                elif name == "inspect":
                    leaked_files = 0
                    marker_paths = []
                    for path in root.rglob("*"):
                        if not path.is_file() or path.is_symlink():
                            continue
                        data = path.read_bytes()
                        leaked_files += any(secret.encode() in data for secret in secrets)
                        if MARKER.encode() in data:
                            marker_paths.append(str(path.relative_to(root)))
                    audit = service._audit_logger.get_audit_log()
                    log_blob = "\n".join(captured_logs) + json.dumps(events, default=str) + json.dumps(audit, default=str)
                    reply = {"leaked_files": leaked_files, "logs_clean": not any(s in log_blob for s in secrets),
                             "events": len(events),
                             "fault_events": sum(event.get("extra", {}).get("ipc_method") == "fixture_error" for event in events),
                             "marker_paths": sorted(marker_paths),
                             "guard_violations": len(violations)}
                else:
                    raise AssertionError("unknown control command")
                connection.send({"result": reply})
        except BaseException as exc:
            # Только тип/этап. Никаких request/raw exception/token в parent/stdout.
            frames = []
            trace = exc.__traceback__
            while trace is not None:
                frames.append({"file": Path(trace.tb_frame.f_code.co_filename).name,
                               "function": trace.tb_frame.f_code.co_name, "line": trace.tb_lineno})
                trace = trace.tb_next
            connection.send({"error_type": type(exc).__name__, "stage": stage,
                             "guard_kinds": sorted(set(violations)), "guard_sites": violation_sites, "frames": frames})
        finally:
            release.set()
            clean = True
            for cleanup in (
                (lambda: server.stop(timeout_sec=3)) if server is not None else None,
                (lambda: server_thread.join(timeout=3)) if server_thread is not None else None,
                service.close if service is not None else None,
                claim.release if claim is not None else None,
                sentry_client.close if sentry_client is not None else None,
            ):
                if cleanup is not None:
                    try:
                        if cleanup() is False:
                            clean = False
                    except BaseException:
                        clean = False
            if server_thread is not None and server_thread.is_alive():
                clean = False
            try:
                connection.send({"stopped": clean, "guard_violations": len(violations)})
            except (BrokenPipeError, EOFError, OSError):
                pass
            connection.close()
