"""Отмена live-subs: только свои fake-коллабораторы и bounded Event/worker."""
import threading
import ast
import logging
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import backend.live_subs_service as module


PCM = b"\x01\x00" * 48000


@pytest.mark.parametrize("action", ["reset", "close"])
@pytest.mark.parametrize("stage", ["stt", "translate"])
def test_cancel_retains_worker_and_suppresses_late_delivery(action, stage):
    entered, release = threading.Event(), threading.Event()

    def blocked(value):
        entered.set()
        if not release.wait(3):
            raise AssertionError("test collaborator was not released")
        return value

    transcriber = Mock()
    translator = Mock()
    transcriber.transcribe.side_effect = lambda *a, **k: (
        blocked({"text": "old phrase", "language": "en"}) if stage == "stt"
        else {"text": "old phrase", "language": "en"}
    )
    translator.translate.side_effect = lambda **k: blocked(SimpleNamespace(text="old translation"))
    service = module.LiveSubsService(transcriber, translator)
    worker = None
    with patch.object(module, "_WORKER_JOIN_TIMEOUT_SEC", 0), patch.object(module.event_bus, "emit_typed") as emit:
        try:
            service.ingest(PCM, 16000, "ru", False)
            assert entered.wait(2)
            worker = service._worker_thread
            outcome = getattr(service, action)()
            retained = service._worker_thread
            if action == "close":
                assert outcome is False
                with pytest.raises(RuntimeError, match="closed"):
                    service.ingest(PCM, 16000, "off", False)
                assert service._buffer_samples == 0
            if action == "reset":
                # Новая сессия принимается, пока старый STT ещё занят.
                transcriber.transcribe.side_effect = None
                transcriber.transcribe.return_value = {"text": "new phrase", "language": "en"}
                service.ingest(PCM, 16000, "off", False)
                assert service._worker_thread is worker
            release.set()
            if action == "close":
                worker.join(2)
                assert not worker.is_alive()
                assert service.close() is True
            else:
                assert service.wait_until_idle(2)
            assert retained is worker
            if stage == "stt":
                translator.translate.assert_not_called()
            if action == "close":
                emit.assert_not_called()
                assert service._completed_result is None
                transcriber.transcribe.side_effect = None
                transcriber.transcribe.return_value = {"text": "new phrase", "language": "en"}
                with pytest.raises(RuntimeError, match="closed"):
                    service.ingest(PCM, 16000, "off", False)
                service = module.LiveSubsService(transcriber, translator)
                service.ingest(PCM, 16000, "off", False)
                assert service.wait_until_idle(2)
            assert service._completed_result["text"] == "new phrase"
            assert emit.call_count == 1
            assert emit.call_args.args[1].text == "new phrase"
        finally:
            release.set()
            service.close()
            if worker:
                worker.join(2)
            if service._worker_thread:
                service._worker_thread.join(2)


@pytest.mark.parametrize("raises", [False, True])
def test_backend_close_does_not_release_transcriber_with_live_subs_worker(raises):
    """Настоящий AST-body close(), без импорта/конструктора BackendService."""
    source = Path(module.__file__).with_name("service.py").read_text()
    tree = ast.parse(source)
    service_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BackendService")
    close = next(node for node in service_class.body if isinstance(node, ast.FunctionDef) and node.name == "close")
    namespace = {"logger": logging.getLogger(__name__)}
    exec(compile(ast.Module(body=[close], type_ignores=[]), "<actual BackendService.close>", "exec"), namespace)
    transcriber = Mock()
    live_subs = Mock()
    if raises:
        live_subs.close.side_effect = RuntimeError("still running")
    else:
        live_subs.close.return_value = False
    meeting = Mock()
    meeting.close.return_value = True
    service = SimpleNamespace(
        _memory_conductor=Mock(), _meeting_svc=meeting,
        _live_subs=live_subs, transcriber=transcriber,
    )
    assert namespace["close"](service) is False
    transcriber.close.assert_not_called()


@pytest.mark.parametrize("stage", ["stt", "translate"])
def test_privacy_enabled_during_processing_blocks_delivery(stage):
    entered, release = threading.Event(), threading.Event()
    privacy = {"enabled": False}

    def pause(value):
        entered.set()
        assert release.wait(3)
        return value

    transcriber, translator = Mock(), Mock()
    transcriber.transcribe.side_effect = lambda *a, **k: (
        pause({"text": "private phrase", "language": "en"}) if stage == "stt"
        else {"text": "private phrase", "language": "en"}
    )
    translator.translate.side_effect = lambda **k: pause(SimpleNamespace(text="private translation"))
    service = module.LiveSubsService(
        transcriber, translator,
        settings_get=lambda key, default: privacy["enabled"] if key == "privacy_mode_enabled" else default,
    )
    with patch.object(module.event_bus, "emit_typed") as emit:
        try:
            service.ingest(PCM, 16000, "ru", False)
            assert entered.wait(2)
            privacy["enabled"] = True
            release.set()
            assert service.wait_until_idle(2)
            emit.assert_not_called()
            if stage == "stt":
                translator.translate.assert_not_called()
            assert not (service._completed_result or {}).get("text")
        finally:
            release.set()
            service.close()


def test_normal_stop_preserves_final_flush():
    transcriber, translator = Mock(), Mock()
    transcriber.transcribe.return_value = {"text": "final phrase", "language": "en"}
    service = module.LiveSubsService(transcriber, translator)
    with patch.object(module.event_bus, "emit_typed") as emit:
        try:
            service.ingest(PCM[:1000], 16000, "off", False)
            assert service.stop() == {"status": "stopped", "flushed": True}
            assert emit.call_count == 1
        finally:
            service.close()


def test_emit_callback_can_reset_without_self_join_or_stale_completion():
    transcriber = Mock()
    transcriber.transcribe.return_value = {"text": "published phrase", "language": "en"}
    service = module.LiveSubsService(transcriber, Mock())
    errors = []

    def on_emit(*args):
        try:
            service.reset()
        except Exception as exc:
            errors.append(exc)

    with patch.object(module.event_bus, "emit_typed", side_effect=on_emit) as emit:
        try:
            service.ingest(PCM, 16000, "off", False)
            assert service.wait_until_idle(2)
            assert not errors
            assert emit.call_count == 1
            assert service._completed_result is None
        finally:
            service.close()


def test_snapshot_accepted_before_reset_cannot_be_relabelled_new():
    transcriber = Mock()
    transcriber.transcribe.return_value = {"text": "stale snapshot", "language": "en"}
    service = module.LiveSubsService(transcriber, Mock())
    submit = service._submit_window

    def reset_before_submit(**kwargs):
        service.reset()
        return submit(**kwargs)

    with patch.object(service, "_submit_window", side_effect=reset_before_submit), patch.object(module.event_bus, "emit_typed") as emit:
        try:
            assert service.ingest(PCM, 16000, "off", True) is None
            assert service.wait_until_idle(2)
            transcriber.transcribe.assert_not_called()
            emit.assert_not_called()
        finally:
            service.close()


def test_old_stop_timeout_cannot_cancel_reset_new_session():
    entered, release = threading.Event(), threading.Event()

    def old_stt(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return {"text": "old stop phrase", "language": "en"}

    transcriber = Mock()
    transcriber.transcribe.side_effect = old_stt
    service = module.LiveSubsService(transcriber, Mock())

    def reset_while_old_stop_waits(*args, **kwargs):
        assert entered.wait(2)
        service.reset()
        transcriber.transcribe.side_effect = None
        transcriber.transcribe.return_value = {"text": "new session phrase", "language": "en"}
        service.ingest(PCM, 16000, "off", False)
        return None

    with patch.object(module.event_bus, "emit_typed") as emit:
        try:
            service.ingest(PCM[:1000], 16000, "off", False)
            with patch.object(service, "_await_completion", side_effect=reset_while_old_stop_waits):
                assert service.stop()["reason"] == "flush_timeout"
            release.set()
            assert service.wait_until_idle(2)
            assert service._completed_result["text"] == "new session phrase"
            assert emit.call_count == 1
            assert emit.call_args.args[1].text == "new session phrase"
        finally:
            release.set()
            service.close()


def test_rest_connection_finally_terminally_closes_owned_live_subs():
    """Проверяем настоящий owning finally, не импортируя REST/его модели."""
    source = Path(module.__file__).with_name("rest_server.py").read_text()
    tree = ast.parse(source)
    owners = [node for node in ast.walk(tree) if isinstance(node, ast.Try) and any(
        isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
        and isinstance(child.func.value, ast.Name) and child.func.value.id == "live_subs"
        for statement in node.finalbody for child in ast.walk(statement)
    )]
    assert len(owners) == 1
    calls = [node.func.attr for statement in owners[0].finalbody for node in ast.walk(statement)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and isinstance(node.func.value, ast.Name) and node.func.value.id == "live_subs"]
    assert calls == ["close"]


def test_reset_reusable_worker_releases_cancelled_audio_before_idle():
    entered, release, idle = threading.Event(), threading.Event(), threading.Event()
    audio_refs = []

    def transcribe(audio, **kwargs):
        audio_refs.append(weakref.ref(audio))
        entered.set()
        assert release.wait(3)
        return {"text": "cancelled phrase", "language": "en"}

    service = module.LiveSubsService(SimpleNamespace(transcribe=transcribe), Mock())
    original_wait = service._worker_cond.wait

    def waiting(*args, **kwargs):
        idle.set()
        return original_wait(*args, **kwargs)

    try:
        service.ingest(PCM, 16000, "off", False)
        assert entered.wait(2)
        service.reset()
        with patch.object(service._worker_cond, "wait", side_effect=waiting):
            release.set()
            assert idle.wait(2)
            assert audio_refs[0]() is None
    finally:
        release.set()
        service.close()


def test_close_between_submit_check_and_ensure_cannot_spawn_worker():
    service = module.LiveSubsService(Mock(), Mock())
    ensure = service._ensure_worker_started

    def close_before_ensure():
        assert service.close() is True
        return ensure()

    try:
        with patch.object(service, "_ensure_worker_started", side_effect=close_before_ensure):
            assert service.ingest(PCM, 16000, "off", True) is None
        assert service._worker_thread is None
    finally:
        service.close()
