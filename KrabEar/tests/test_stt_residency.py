"""Actual owner chains: metadata/выгрузка без моделей, inference и spawn."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.stt_management_service import STTManagementService
from core.engine import AudioEngine
from core.pipeline.stt_gigaam import GigaAMAdapter
from core.stt_router import STTRouter


class FakeSession:
    def __init__(self):
        self.loaded = True
        self.close_result = True
        self.closes = 0

    def is_loaded(self):
        return self.loaded

    def close(self):
        self.closes += 1
        if self.close_result:
            self.loaded = False
        return self.close_result


def owner():
    adapter = GigaAMAdapter(transport="subprocess", mode="v3_e2e_rnnt")
    session = FakeSession()
    adapter._subprocess = session
    adapter._active_transport = "subprocess"
    router = STTRouter(SimpleNamespace(STT_GIGAAM_ENABLED=True))
    router._gigaam_adapter = adapter
    router._gigaam_adapter_fingerprint = ("v3_e2e_rnnt", "mps", "subprocess", None)
    engine = AudioEngine.__new__(AudioEngine)
    engine._router = router
    engine.current_model = "org/configured"
    engine.quality_profile = "balanced"
    service = STTManagementService(SimpleNamespace(), SimpleNamespace(engine=engine))
    return service, engine, router, adapter, session


@pytest.mark.parametrize("block", ["call_pin", "inflight", "cleanup"])
def test_unload_refusal_keeps_actual_owner_and_reports_false_then_drain_succeeds(block):
    service, engine, router, adapter, session = owner()
    lease = None
    if block == "call_pin":
        lease, status = adapter.reserve_call()
        assert status == "ok"
    elif block == "inflight":
        adapter.inflight = 1
    else:
        session.close_result = False
    try:
        result = service.handle_unload_stt_model({})
        assert result["unloaded"] is False
        assert result["error"]
        assert router._gigaam_adapter is adapter
        assert adapter._subprocess is session
        assert session.loaded is True
    finally:
        if lease:
            lease.release()
        adapter.inflight = 0
        session.close_result = True
    assert service.handle_unload_stt_model({}) == {"unloaded": True, "error": None}
    assert router._gigaam_adapter is None
    assert session.loaded is False


@pytest.mark.parametrize("result", [False, None, 1, "yes"])
def test_unload_requires_explicit_confirmation(result):
    service = STTManagementService(SimpleNamespace(), SimpleNamespace(engine=SimpleNamespace(unload_stt_models=lambda: result)))
    assert service.handle_unload_stt_model({})["unloaded"] is False


def snapshot(engine, owner_name="backend"):
    method = getattr(engine, "stt_residency", None)
    assert callable(method), "У actual engine нет nonwarming residency API"
    return method(owner=owner_name)


def test_cold_snapshot_never_creates_adapter_or_whisper_session(monkeypatch):
    import core.mlx_whisper_session as whisper
    service, engine, router, adapter, session = owner()
    router._gigaam_adapter = None
    monkeypatch.setattr(whisper, "_session", None)
    monkeypatch.setenv("KRAB_EAR_MLX_WHISPER_WORKER", "1")
    router.get_gigaam_adapter = Mock(side_effect=AssertionError("adapter creation"))
    monkeypatch.setattr(whisper, "get_mlx_whisper_session", Mock(side_effect=AssertionError("session creation")))
    result = snapshot(engine)
    assert result["owner"] == "backend"
    assert result["gigaam"]["state"] == "unloaded"
    assert result["whisper"]["state"] == "unloaded"
    assert router._gigaam_adapter is None
    assert whisper.peek_session() is None


class NoModelEvaluation:
    def __bool__(self):
        raise AssertionError("GPU object bool")
    def __repr__(self):
        raise AssertionError("GPU object repr")


def test_observed_model_is_not_configured_model_and_call_pin_is_busy(monkeypatch):
    import sys
    service, engine, router, adapter, session = owner()
    holder = SimpleNamespace(model=NoModelEvaluation(), model_path="org/observed")
    monkeypatch.setitem(sys.modules, "mlx_whisper.transcribe", SimpleNamespace(ModelHolder=holder))
    monkeypatch.setenv("KRAB_EAR_MLX_WHISPER_WORKER", "0")
    lease, status = adapter.reserve_call()
    try:
        result = snapshot(engine)
        assert result["configured"]["model"] == "org/configured"
        assert result["whisper"]["model"] == "org/observed"
        assert result["whisper"]["state"] == "loaded"
        assert result["gigaam"]["state"] == "busy"
        assert result["gigaam"]["call_pins"] == 1
        assert result["gigaam"]["inflight"] == 1
    finally:
        lease.release()


@pytest.mark.parametrize("path", ["/Users/private/model", "../model", "~/model", "C:\\private\\model"])
def test_snapshot_hides_local_model_paths(monkeypatch, path):
    import sys
    _, engine, _, _, _ = owner()
    engine.current_model = path
    monkeypatch.setenv("KRAB_EAR_MLX_WHISPER_WORKER", "0")
    monkeypatch.setitem(sys.modules, "mlx_whisper.transcribe", SimpleNamespace(ModelHolder=SimpleNamespace(model=NoModelEvaluation(), model_path=path)))
    result = snapshot(engine)
    assert result["configured"]["model"] is None
    assert result["whisper"]["model"] is None
    assert result["whisper"]["state"] == "loaded"


def test_live_worker_model_is_unknown_without_child_metadata(monkeypatch):
    import core.mlx_whisper_session as whisper
    _, engine, _, _, _ = owner()
    session = whisper.MLXWhisperSession()
    session._proc = SimpleNamespace(pid=123, poll=lambda: None)
    session.start = Mock(side_effect=AssertionError("spawn"))
    session._send = Mock(side_effect=AssertionError("child IPC"))
    monkeypatch.setattr(whisper, "_session", session)
    monkeypatch.setenv("KRAB_EAR_MLX_WHISPER_WORKER", "1")
    result = snapshot(engine, "rest")
    assert result["owner"] == "rest"
    assert result["whisper"]["state"] == "unknown"
    assert result["whisper"]["model"] is None
    assert result["whisper"]["pid"] == 123
    session._inflight = 1
    assert snapshot(engine)["whisper"]["state"] == "busy"
    session._proc = None


def test_actual_ipc_handler_returns_owner_snapshot_without_warmup(monkeypatch):
    service, engine, _, _, _ = owner()
    monkeypatch.setenv("KRAB_EAR_MLX_WHISPER_WORKER", "1")
    method = getattr(service, "handle_get_stt_residency", None)
    assert callable(method), "Residency handler отсутствует"
    assert method({})["owner"] == "backend"


def test_actual_dispatch_routes_new_ipc_without_constructing_backend(monkeypatch):
    from backend.service import BackendService
    service, _, _, _, _ = owner()
    monkeypatch.setenv("KRAB_EAR_MLX_WHISPER_WORKER", "1")
    # Остальные dispatch collaborators не участвуют. Исполняем настоящий
    # builder/request/handler/engine, без BackendService.__init__/warmup.
    shell = Mock()
    shell._stt_mgmt_svc = service
    shell._request_signer = None
    shell._ipc_throttle = None
    shell._get_runtime_setting = lambda *_: False
    shell._dispatch_table = BackendService._build_dispatch_table(shell)
    result = BackendService.handle_request(shell, {"id": "residency", "method": "get_stt_residency", "params": {}})
    assert result["ok"] is True
    assert result["result"]["owner"] == "backend"
    assert result["result"]["configured"]["model"] == "org/configured"


def test_missing_engine_is_explicitly_unavailable():
    service = STTManagementService(SimpleNamespace(), None)
    assert service.handle_get_stt_residency({}) == {"owner": "backend", "state": "unavailable"}


def test_missing_mlx_module_snapshot_does_not_import_it(monkeypatch):
    import builtins
    import sys
    _, engine, _, _, _ = owner()
    monkeypatch.delitem(sys.modules, "mlx_whisper.transcribe", raising=False)
    monkeypatch.setenv("KRAB_EAR_MLX_WHISPER_WORKER", "0")
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name == "mlx" or name.startswith("mlx.") or name == "mlx_whisper" or name.startswith("mlx_whisper."):
            raise AssertionError("Metadata импортирует MLX")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert snapshot(engine)["whisper"]["state"] == "unloaded"
    assert "mlx_whisper.transcribe" not in sys.modules


@pytest.mark.parametrize("resident", ["router", "adapter", "worker", "mlx", "singleton"])
def test_contended_owner_snapshot_returns_busy_without_waiting_for_work(monkeypatch, resident):
    import threading
    import core.mlx_whisper_session as whisper
    from core.mlx_lock import mlx_lock
    _, engine, router, adapter, _ = owner()
    session = whisper.MLXWhisperSession()
    monkeypatch.setattr(whisper, "_session", session)
    monkeypatch.setenv("KRAB_EAR_MLX_WHISPER_WORKER", "0" if resident == "mlx" else "1")
    lock = {"router": router._gigaam_adapter_lock, "adapter": adapter._spawn_lock,
            "worker": session._lock, "mlx": mlx_lock(), "singleton": whisper._session_lock}[resident]
    entered, release, observed = threading.Event(), threading.Event(), threading.Event()
    values = []
    def hold():
        with lock:
            entered.set(); assert release.wait(3)
    def observe():
        try:
            values.append(snapshot(engine))
        finally:
            observed.set()
    owner_thread = threading.Thread(target=hold)
    observer_thread = threading.Thread(target=observe)
    owner_thread.start()
    try:
        assert entered.wait(1)
        observer_thread.start()
        assert observed.wait(1), "Диагностика ждёт завершения inference/owner lock"
    finally:
        release.set(); owner_thread.join(2)
        if observer_thread.ident is not None:
            observer_thread.join(2)
    assert not owner_thread.is_alive() and not observer_thread.is_alive()
    component = "gigaam" if resident in ("router", "adapter") else "whisper"
    assert values[0][component]["state"] == "busy"
