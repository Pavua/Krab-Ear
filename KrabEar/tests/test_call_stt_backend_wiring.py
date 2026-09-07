"""Живая таблица IPC и порядок закрытия владельца телефонного STT."""
import base64
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import uuid
import wave

import numpy as np
import pytest

from backend.service import BackendService, _shutdown_backend


def _closing_backend(call_stt, transcriber):
    service = BackendService.__new__(BackendService)
    service._memory_conductor = Mock()
    service._meeting_svc = Mock()
    service._meeting_svc.close.return_value = True
    service._call_stt = call_stt
    service.transcriber = transcriber
    return service


def test_backend_preserves_transcriber_while_phone_drain_incomplete():
    call_stt = Mock()
    call_stt.close.side_effect = [False, True]
    transcriber = Mock()
    transcriber.close.return_value = True
    service = _closing_backend(call_stt, transcriber)
    assert service.close() is False
    transcriber.close.assert_not_called()
    assert service.close() is True
    transcriber.close.assert_called_once()


def test_backend_propagates_transcriber_close_refusal():
    call_stt = Mock()
    call_stt.close.return_value = True
    transcriber = Mock()
    transcriber.close.return_value = False
    assert _closing_backend(call_stt, transcriber).close() is False


def test_shutdown_closes_phone_admission_before_ipc_drain():
    events = []
    service = SimpleNamespace(
        _call_stt=SimpleNamespace(begin_shutdown=lambda: events.append("admission_closed")),
        close=lambda: events.append("owner_closed") or True,
    )
    server = SimpleNamespace(stop=lambda **_: events.append("ipc_drain") or True)
    metadata = SimpleNamespace(shutdown=lambda **_: True)
    assert _shutdown_backend(service, server, metadata, flush_fn=lambda: None)
    assert events == ["admission_closed", "ipc_drain", "owner_closed"]


def test_real_constructor_registers_phone_ipc_without_new_engine():
    from test_ipc_dispatch_build import _build_minimal_backend_service
    service = _build_minimal_backend_service()
    try:
        response = service.handle_request({
            "id": "test", "method": "transcribe_ephemeral_call", "params": {},
        })
        assert "transcribe_ephemeral_call" in service._dispatch_table
        assert response["ok"] is True
        assert response["result"]["text"] == ""
        assert response["result"]["status"] in {"error", "privacy_mode"}
    finally:
        service.close()


class _ExistingWorkerInput(io.StringIO):
    """Проверяет WAV на границе stdin существующего worker, до удаления temp."""

    def __init__(self, commands, audio_paths):
        super().__init__()
        self.commands = commands
        self.audio_paths = audio_paths

    def write(self, value):
        command = json.loads(value)
        self.commands.append(command)
        if command["op"] == "transcribe":
            audio_path = Path(command["audio_path"])
            self.audio_paths.append(audio_path)
            with wave.open(str(audio_path), "rb") as wav:
                assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
                assert wav.getnframes() == 16000
        return super().write(value)


@pytest.fixture
def real_call_stack(monkeypatch, tmp_path):
    """Реальные REST/IPC/service/router/adapter/session; fake только вместо ML."""
    from backend import call_stt_client as client_module
    from backend import ipc_server as ipc_module
    from backend import service as backend_module
    from backend.request_signing import RequestSigner
    from backend.state_store import StateStore
    from core.pipeline import stt_gigaam
    from core.stt_router import STTRouter
    # Импортный helper подавляет standalone AudioEngine/StateStore construction.
    from test_rest_vg_contract_M1 import _deps_with, rs

    active = []

    def build(*, signing=True, owner_private=False):
        secret = RequestSigner.generate_secret() if signing else ""
        monkeypatch.setattr(backend_module.settings, "IPC_SIGNING_ENABLED", signing)
        monkeypatch.setattr(backend_module.settings, "IPC_SIGNING_SECRET", secret)
        monkeypatch.setattr(backend_module.settings, "IPC_THROTTLE_ENABLED", False)
        # Диагностика подписанных Swift-бинарей не относится к STT/IPC contract.
        monkeypatch.setattr(BackendService, "_dwarfdump_uuid", staticmethod(lambda _: None))
        monkeypatch.setattr(rs.settings, "REST_API_AUTH_ENABLED", False)
        monkeypatch.setattr(rs.settings, "REST_API_KEY", "test-only-rest-call-key")

        adapter = stt_gigaam.GigaAMAdapter(mode="v3_e2e_rnnt", transport="subprocess")
        session = stt_gigaam._GigaAMSubprocessSession("unused", "unused", "v3_e2e_rnnt", "cpu")
        commands, audio_paths = [], []
        process = SimpleNamespace(
            stdin=_ExistingWorkerInput(commands, audio_paths),
            stdout=io.StringIO('{"ok":true,"text":"Запишите меня на приём."}\n' * 8),
            stderr=io.StringIO(),
            poll=Mock(return_value=None), wait=Mock(return_value=0),
            terminate=Mock(side_effect=AssertionError("must not terminate owner")),
            kill=Mock(side_effect=AssertionError("must not kill owner")),
        )
        session._proc = process
        session._loaded = True
        session.start = Mock(side_effect=AssertionError("must not start a second worker"))
        forbidden_spawn = Mock(side_effect=AssertionError("test must never spawn a process"))
        monkeypatch.setattr(stt_gigaam.subprocess, "Popen", forbidden_spawn)
        monkeypatch.setattr(stt_gigaam.GigaAMAdapter, "_get_model", Mock(
            side_effect=AssertionError("test must never load an in-process model"),
        ))
        adapter._active_transport = "subprocess"
        adapter._subprocess = session
        adapter._get_subprocess_session = Mock(side_effect=AssertionError("must use pinned session"))
        router = STTRouter(SimpleNamespace(
            STT_GIGAAM_ENABLED=True, STT_GIGAAM_MODE="v3_e2e_rnnt",
            STT_GIGAAM_DEVICE="mps", STT_GIGAAM_TRANSPORT="subprocess",
        ))
        router._gigaam_adapter = adapter
        router._gigaam_adapter_fingerprint = ("v3_e2e_rnnt", "mps", "subprocess", None)

        class Engine:
            _router = router
            _last_llm_diff = None
            _llm_rewriter = None
            quality_profile = "balanced"
            current_model = "fake-whisper-never-called"

            def _resolve_diarization_device(self):
                return "cpu"

        transcriber = SimpleNamespace(
            engine=Engine(),
            transcribe=Mock(side_effect=AssertionError("must not use dictation transcriber")),
            close=Mock(wraps=router.close),
        )
        store = StateStore(tmp_path / "owner")
        service = BackendService(
            store=store,
            recorder=SimpleNamespace(is_recording=False, sample_rate=16000),
            transcriber=transcriber,
            translator=SimpleNamespace(last_mode="off", translate=Mock()),
        )
        assert service._call_stt._router is router  # Реальный constructor seam.
        assert service._dispatch_table["transcribe_ephemeral_call"].__self__ is service._call_stt
        assert (service._request_signer is not None) is signing
        store.save_settings({"privacy_mode_enabled": owner_private})
        monkeypatch.setattr(store, "add_history_item", Mock(side_effect=AssertionError("ephemeral history write")))

        socket_dir = tempfile.TemporaryDirectory(prefix="call-stt-e2e-", dir="/tmp")
        socket_path = Path(socket_dir.name) / "ipc.sock"
        server = ipc_module.IPCServer(socket_path, service)
        ready = threading.Event()
        original_mark = ipc_module.SocketOwnershipClaim.mark_listening

        def mark_listening(claim):
            original_mark(claim)
            ready.set()

        monkeypatch.setattr(ipc_module.SocketOwnershipClaim, "mark_listening", mark_listening)
        server_errors = []

        def serve():
            try:
                server.serve_forever()
            except BaseException as exc:
                server_errors.append(exc)
                ready.set()

        server_thread = threading.Thread(target=serve, daemon=True, name="test-call-ipc")
        active.append((service, server, server_thread, socket_dir, process, adapter, forbidden_spawn))
        server_thread.start()
        assert ready.wait(3), "temporary IPC did not start"
        assert not server_errors
        monkeypatch.setenv("KRAB_EAR_SOCKET", str(socket_path))

        rest_deps = _deps_with(privacy=False)
        rest_deps.store.call_privacy_mode.return_value = False
        app = rs.create_app(rest_deps)
        app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
        http = app.test_client()
        frames = []
        original_build = client_module.build_call_request

        def capture_build(*args, **kwargs):
            frame = original_build(*args, **kwargs)
            frames.append(frame)
            return frame

        monkeypatch.setattr(client_module, "build_call_request", capture_build)

        def post(*, auth=True):
            wav_data = io.BytesIO()
            with wave.open(wav_data, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(8000)
                wav.writeframes(np.full(8000, 4096, dtype="<i2").tobytes())
            return http.post("/v1/stt/transcribe", data={
                "file": (io.BytesIO(wav_data.getvalue()), "phone.wav"),
                "request_profile": "voice_gateway_call", "language": "ru",
                "deadline_sec": "5", "persist_history": "true",
            }, headers={"Authorization": "Bearer test-only-rest-call-key"} if auth else {})

        return SimpleNamespace(
            post=post, frames=frames, commands=commands, audio_paths=audio_paths,
            owner=service, store=store, rest_deps=rest_deps, transcriber=transcriber,
            session=session, adapter=adapter, client_module=client_module,
            capture_build=capture_build, server_errors=server_errors,
        )

    yield build
    for service, server, thread, socket_dir, process, adapter, forbidden_spawn in reversed(active):
        try:
            service._call_stt.begin_shutdown()
            assert server.stop(timeout_sec=3)
            thread.join(3)
            assert not thread.is_alive(), "temporary IPC thread leaked"
            assert service.close(), "temporary BackendService did not drain"
            assert adapter.inflight == 0
            process.kill.assert_not_called()
            process.terminate.assert_not_called()
            forbidden_spawn.assert_not_called()
        finally:
            socket_dir.cleanup()


@pytest.mark.parametrize("signing", [False, True])
def test_rest_to_owner_gigaam_real_stack_signing_modes(real_call_stack, signing):
    stack = real_call_stack(signing=signing)
    response = stack.post()
    assert response.status_code == 200
    body = response.json
    assert body["text"] == "Запишите меня на приём."
    assert body["status"] == "ok" and body["language"] == "ru"
    assert body["adapter"] == "gigaam" and body["mode"] == "v3_e2e_rnnt"
    assert body["model"] == "gigaam-v3_e2e_rnnt" and body["transport"] == "subprocess"
    assert body["confidence"] == 0.9 and body["confidence_source"] == "constant"
    assert body["history_id"] == "" and body["request_profile"] == "voice_gateway_call"
    assert len(stack.frames) == 1
    envelope = json.loads(stack.frames[0])
    assert ("signature" in envelope) is signing
    assert envelope["id"] == envelope["params"]["request_id"]
    assert [cmd["op"] for cmd in stack.commands] == ["transcribe"]
    assert stack.audio_paths and all(not path.exists() for path in stack.audio_paths)
    stack.session.start.assert_not_called()
    stack.transcriber.transcribe.assert_not_called()
    stack.rest_deps.transcriber.transcribe.assert_not_called()
    stack.store.add_history_item.assert_not_called()
    stack.rest_deps.store.add_history_item.assert_not_called()


def test_rest_to_owner_privacy_refusal_crosses_real_ipc_as_403(real_call_stack):
    stack = real_call_stack(owner_private=True)
    response = stack.post()
    assert response.status_code == 403
    assert response.json["skipped"] == "privacy_mode" and response.json["text"] == ""
    assert len(stack.frames) == 1  # REST privacy off, refusal пришёл от owner.
    assert stack.commands == []
    stack.rest_deps.transcriber.transcribe.assert_not_called()
    stack.store.add_history_item.assert_not_called()


def test_real_owner_corrupt_settings_refuses_privacy_without_model_work(real_call_stack):
    stack = real_call_stack(owner_private=False)
    stack.store.settings_path.write_text("{invalid", encoding="utf-8")
    response = stack.post()
    assert response.status_code == 403
    assert response.json["skipped"] == "privacy_mode"
    assert len(stack.frames) == 1 and stack.commands == []


def test_rest_auth_refusal_prevents_any_ipc_envelope(real_call_stack):
    stack = real_call_stack()
    response = stack.post(auth=False)
    assert response.status_code == 401
    assert stack.frames == [] and stack.commands == []


@pytest.mark.parametrize("attack", [
    "signed_id_tamper", "signed_audio_tamper", "signed_deadline_tamper", "unsigned",
])
def test_rest_to_real_signer_rejects_tamper_without_invocation_or_retry(real_call_stack, monkeypatch, attack):
    stack = real_call_stack(signing=True)

    def corrupt(*args, **kwargs):
        envelope = json.loads(stack.capture_build(*args, **kwargs))
        if attack == "signed_id_tamper":
            envelope["params"]["request_id"] = str(uuid.uuid4())
        elif attack == "signed_audio_tamper":
            wav = bytearray(base64.b64decode(envelope["params"]["audio_wav_b64"]))
            wav[-1] ^= 1
            envelope["params"]["audio_wav_b64"] = base64.b64encode(wav).decode()
        elif attack == "signed_deadline_tamper":
            envelope["params"]["deadline_monotonic"] += 1
        else:
            envelope.pop("signature")
        return (json.dumps(envelope) + "\n").encode()

    monkeypatch.setattr(stack.client_module, "build_call_request", corrupt)
    response = stack.post()
    assert response.status_code == 403
    assert response.json["text"] == ""
    assert len(stack.frames) == 1 and stack.commands == []
    stack.rest_deps.transcriber.transcribe.assert_not_called()


def test_rest_to_real_signer_replay_is_403_and_does_not_repeat_audio(real_call_stack, monkeypatch):
    stack = real_call_stack(signing=True)
    fixed_id = uuid.uuid4()
    monkeypatch.setattr(stack.client_module.uuid, "uuid4", lambda: fixed_id)
    assert stack.post().status_code == 200
    sent = stack.frames[0]
    attempts = []

    def replay(*args, **kwargs):
        attempts.append(1)
        return sent

    monkeypatch.setattr(stack.client_module, "build_call_request", replay)
    response = stack.post()
    assert response.status_code == 403 and response.json["text"] == ""
    assert attempts == [1]
    assert [cmd["op"] for cmd in stack.commands] == ["transcribe"]
    stack.store.add_history_item.assert_not_called()
