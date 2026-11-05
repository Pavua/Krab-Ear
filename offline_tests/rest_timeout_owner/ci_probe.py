"""Fresh-process ownership probe for disposable GitHub-hosted CI only.

Python audit and mocks are not an OS sandbox. No live models or runtime proof.
"""

import os
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]

# This functional probe is restricted to an explicitly requested disposable
# GitHub-hosted Linux runner. Never point it at the owner's local runtime.
if os.environ.get("GITHUB_ACTIONS") != "true" or sys.platform != "linux":
    raise SystemExit("This probe runs only on a disposable hosted CI runner")


class SafetyOracleViolation(Exception):
    """Мутация не меняет критерий; она обязана нарушить этот oracle."""


def _prepare(scratch, site_packages):
    """Установить Python-level запреты до импорта модулей проекта."""
    scratch = Path(scratch).resolve(strict=True)
    sys.dont_write_bytecode = True
    sys.pycache_prefix = str(scratch / "cache")
    # Без site.addsitedir(): этот путь не должен исполнять .pth.
    sys.path.insert(0, site_packages)
    test_root = SOURCE_ROOT / "KrabEar" / "tests"
    package_root = test_root.parent
    sys.path.insert(0, str(test_root))
    sys.path.insert(0, str(package_root))
    sys.path.insert(0, str(package_root.parent))
    Path.home = classmethod(lambda cls: scratch)
    from rest_stageb_boundary import Boundary

    class TimeoutBoundary(Boundary):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "backend.rest_server":
                return None
            return super().find_spec(fullname, path, target)

    boundary = TimeoutBoundary(scratch)
    boundary.install()
    return boundary


def _canaries(boundary, protected_path):
    """Проверяем Python audit до импорта проекта; это не OS sandbox."""
    import socket
    import subprocess

    def socket_create():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM):
            raise AssertionError("socket unexpectedly allowed")

    def spawn():
        child = subprocess.Popen(["/usr/bin/true"])
        child.wait(timeout=5)
        raise AssertionError("spawn unexpectedly allowed")

    def forbidden_read():
        with open(protected_path, "rb"):
            raise AssertionError("protected file unexpectedly readable")

    def forbidden_exit():
        os._exit(70)
        raise AssertionError("os._exit unexpectedly returned")

    tests = (("network", socket_create), ("spawn", spawn),
             ("protected_read", forbidden_read), ("exit", forbidden_exit))
    checked = []
    for name, invoke in tests:
        count = len(boundary.violations)
        try:
            invoke()
        except PermissionError:
            if len(boundary.violations) != count + 1:
                raise AssertionError("unexpected audit denial count")
            boundary.violations.pop()
            boundary.denials.pop()
            checked.append(name)
        else:
            raise AssertionError("canary did not deny " + name)
    return checked


def _instrument(rest):
    from unittest.mock import patch
    from core import mlx_whisper_session

    events = []
    patches = [
        patch.object(rest.os, "_exit", side_effect=lambda code: events.append(("exit", code))),
        patch.object(mlx_whisper_session, "kill_mlx_whisper_session",
                     side_effect=lambda: events.append(("kill", None))),
        patch.object(rest, "_flush_logs_before_exit", side_effect=lambda: events.append(("flush", None))),
    ]
    return events, patches


def _response(rest, scratch):
    from flask import Response

    upload = scratch / "synthetic-upload"
    upload.write_bytes(b"synthetic")
    response = Response(status=504)
    rest._arm_timeout_exit(response, upload)
    return response


def _standalone(rest, scratch, static_enabled):
    from contextlib import ExitStack
    from unittest.mock import patch

    assert rest._PROCESS_HOSTS_BACKEND is False
    events, patches = _instrument(rest)
    with ExitStack() as stack:
        stack.enter_context(patch.object(rest.settings, "REST_IN_PROCESS_ENABLED", static_enabled))
        for item in patches:
            stack.enter_context(item)
        _response(rest, scratch).close()
    expected = [] if static_enabled else [("kill", None), ("flush", None), ("exit", 70)]
    assert events == expected, events
    assert rest._PROCESS_HOSTS_BACKEND is False
    return {"scenario": "standalone_true" if static_enabled else "standalone_false", "events": events}


def _backend(rest, scratch, *, setup_failure=False, mutation=None):
    from contextlib import ExitStack
    from types import ModuleType
    from unittest.mock import MagicMock, patch
    from backend.service import BackendService
    from backend.state_store import StateStore
    from core.config import settings
    from test_backend_service import FakeRecorder, FakeTranscriber, FakeTranslator

    assert rest._PROCESS_HOSTS_BACKEND is False
    assert settings.REST_IN_PROCESS_ENABLED is False
    # REST import уже создал собственный data-каталог; backend получает новый
    # профиль, владельцем первичной записи которого является именно этот store.
    store = StateStore(scratch / "backend-data")
    store.save_settings({"rest_in_process_enabled": True, "stt_warmup_on_startup": False,
                         "rewriter_warmup_on_startup": False,
                         "binary_drift_check_on_startup": False})
    events, patches = _instrument(rest)
    server = MagicMock(name="fake_inprocess_server")
    standalone_engine = rest.engine
    server_state = {"running": False}
    server.start.side_effect = lambda: server_state.update(running=True)
    server.stop.side_effect = lambda: server_state.update(running=False)
    server.status.side_effect = lambda: {"enabled": True, "running": server_state["running"]}
    adoption = rest.adopt_external_singletons
    seen = []

    def adopt(**kwargs):
        seen.append(rest._PROCESS_HOSTS_BACKEND)
        if setup_failure:
            raise RuntimeError("synthetic adoption failure")
        return adoption(**kwargs)

    assert sys.modules.get("backend.rest_server") is rest
    mark = None
    if mutation == "skip_mark":
        mark = patch.object(rest, "_mark_backend_process", lambda: None)
    elif mutation == "wrong_module":
        fake_other = ModuleType("synthetic_wrong_rest_module")
        fake_other.marker = False
        assert fake_other is not rest
        mark = patch.object(rest, "_mark_backend_process", lambda: setattr(fake_other, "marker", True))
    elif mutation == "bypass_guard":
        from core import mlx_whisper_session

        def bypass(code):
            mlx_whisper_session.kill_mlx_whisper_session()
            rest._flush_logs_before_exit()
            rest.os._exit(code)

        mark = patch.object(rest, "_exit_poisoned_rest_process", bypass)
    with ExitStack() as stack:
        stack.enter_context(patch.object(rest, "adopt_external_singletons", side_effect=adopt))
        stack.enter_context(patch("backend.rest_inprocess.InProcessRestServer", return_value=server))
        stack.enter_context(patch("backend.rest_watchdog.RestWatchdog"))
        for item in patches:
            stack.enter_context(item)
        if mark is not None:
            stack.enter_context(mark)
        # Сохраняем handle и при исключении в __init__: полная очистка такой
        # частичной конструкции ещё не доказана и не является успехом.
        service = BackendService.__new__(BackendService)
        close_attempted = False
        try:
            BackendService.__init__(service, store=store, recorder=FakeRecorder(),
                                    transcriber=FakeTranscriber(), translator=FakeTranslator())
            response = _response(rest, scratch)
            if not setup_failure:
                server.start.assert_called_once_with()
                assert server.status()["running"] is True
                assert rest._singletons_adopted is True
            else:
                server.start.assert_not_called()
                assert service._rest_inprocess.status()["tombstone"] is True
            close_attempted = True
            close_result = service.close()
            assert close_result is not False
            if not setup_failure:
                server.stop.assert_called()
                assert server.status()["running"] is False
                assert rest._singletons_adopted is False
                assert rest.engine is standalone_engine
            assert sys.modules.get("backend.rest_server") is rest
            settings.REST_IN_PROCESS_ENABLED = False
            response.close()
            # Один и тот же safety oracle во всех сценариях: мутация обязана
            # нарушить его, никогда не получает отдельного «успешного» правила.
            failures = []
            if seen != [True]:
                failures.append("marker_before_adopt")
            if rest._PROCESS_HOSTS_BACKEND is not True:
                failures.append("marker_after_close")
            if events:
                failures.append("forbidden_operations")
            if mutation == "wrong_module":
                assert fake_other.marker is True
            if failures:
                raise SafetyOracleViolation(tuple(failures))
        finally:
            if not close_attempted:
                close_attempted = True
                if service.close() is False:
                    raise RuntimeError("partial service cleanup unproven")
    return {"scenario": "backend", "setup_failure": setup_failure, "mutation": mutation,
            "adoption_marker": seen, "events": events}


def main():
    import atexit
    import json
    import threading
    from unittest.mock import patch

    if len(sys.argv) != 4:
        raise SystemExit("usage: probe SCRATCH SITE_PACKAGES SCENARIO")
    scratch = Path(sys.argv[1]).resolve(strict=True)
    info = scratch.stat()
    if not scratch.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("scratch must be an owned private directory")
    os.chdir(scratch)
    initial_threads = set(threading.enumerate())
    boundary = _prepare(scratch, sys.argv[2])
    protected = os.environ.get("EAR_CI_PROTECTED_CANARY")
    if not protected:
        raise ValueError("missing protected canary path")
    checked_canaries = _canaries(boundary, protected)
    from core import engine as engine_module

    class SyntheticStandaloneEngine:
        def __init__(self, **_kwargs):
            self._router = None

    # rest_server создаёт собственный движок при импорте. Меняем именно
    # конструктор до импорта, не ожидая очистки настоящего ML-ресурса.
    with patch.object(engine_module, "AudioEngine", SyntheticStandaloneEngine):
        from backend import rest_server as rest

    assert type(rest.engine) is SyntheticStandaloneEngine
    from core import mlx_whisper_session
    # Cleanup должен быть проверен ДО receipt; процесс всё равно одноразовый.
    atexit.unregister(rest._rest_engine_cleanup)

    scenario = sys.argv[3]
    try:
        if scenario == "standalone_false":
            receipt = _standalone(rest, scratch, False)
        elif scenario == "standalone_true":
            receipt = _standalone(rest, scratch, True)
        elif scenario in {"backend", "backend_setup_failure", "backend_skip_mark", "backend_wrong_module",
                          "backend_bypass_guard"}:
            mutations = {"backend_skip_mark": "skip_mark", "backend_wrong_module": "wrong_module",
                         "backend_bypass_guard": "bypass_guard"}
            try:
                receipt = _backend(rest, scratch, setup_failure=scenario == "backend_setup_failure",
                                   mutation=mutations.get(scenario))
            except SafetyOracleViolation as exc:
                receipt = {"scenario": scenario, "oracle": "failed", "failures": list(exc.args[0])}
        else:
            raise ValueError("unrecognised scenario")
    finally:
        assert type(rest.engine) is SyntheticStandaloneEngine
        assert rest._singletons_adopted is False
        with patch.object(mlx_whisper_session, "close_mlx_whisper_session") as close_worker:
            rest._rest_engine_cleanup()
            close_worker.assert_called_once_with()
    # Только имя функции/файла и строка: пути и содержимое не публикуются.
    assert not boundary.violations, [
        {"label": item["label"], "frames": item["frames"]} for item in boundary.denials
    ]
    assert not (set(threading.enumerate()) - initial_threads), "unaccounted Python threads"
    receipt["violations"] = boundary.violations
    receipt["canaries"] = checked_canaries
    receipt["suppressed_thread_starts"] = boundary.scheduled
    receipt["suppressed_thread_joins"] = boundary.joined
    receipt["scope"] = "synthetic response-close; not real 504, model, runtime, or drain"
    payload = json.dumps(receipt, sort_keys=True).encode("utf-8")
    if len(payload) > 8192:
        raise ValueError("receipt too large")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(scratch / "receipt.json", flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


if __name__ == "__main__":
    main()
