"""Ephemeral STT: таймаут клиента не освобождает занятого владельца модели."""

from __future__ import annotations

import base64
import io
import threading
import time
from unittest.mock import Mock
import wave

import numpy as np
import pytest

from backend import call_stt_service as service_module
from backend.call_stt_service import CallSTTService


class Lease:
    def __init__(self, *, block=False, result=None, error=None):
        self.started = threading.Event()
        self.finish = threading.Event()
        if not block:
            self.finish.set()
        self.result = result if result is not None else {
            "status": "ok", "text": "Здравствуйте", "engine": "gigaam",
            "adapter": "gigaam", "mode": "rnnt", "model": "v3_e2e_rnnt",
            "transport": "subprocess",
            "language": "ru", "confidence": 0.9, "confidence_source": "constant",
        }
        self.error = error
        self.released = threading.Event()
        self.release_count = 0
        self.arguments = None

    def run(self, audio, sample_rate, deadline_monotonic):
        self.arguments = (audio, sample_rate, deadline_monotonic)
        self.started.set()
        if not self.finish.wait(5):
            raise AssertionError("test lease was not drained")
        if self.error:
            raise self.error
        return self.result

    def release(self):
        self.release_count += 1
        self.released.set()


@pytest.fixture
def resources(monkeypatch):
    decoder = Mock(return_value=(np.zeros(80, dtype=np.float32), 8000))
    monkeypatch.setattr(service_module, "decode_call_wav", decoder)
    tracked = []

    def make(*, block=False, privacy=None, lease=None, status="ok"):
        lease = lease or Lease(block=block)
        router = Mock()
        router.reserve_gigaam_call.return_value = (
            lease if status == "ok" else None, status,
        )
        service = CallSTTService(router, privacy or (lambda: False))
        tracked.append((service, lease))
        return service, router, lease, decoder

    yield make
    for service, lease in tracked:
        lease.finish.set()
        assert service.close(timeout_sec=5), "test leaked an owner task"


def params(**changes):
    result = {
        "request_id": "call-turn-1", "audio_wav_b64": base64.b64encode(b"wav").decode(),
        "language": "ru", "deadline_monotonic": time.monotonic() + 2,
    }
    result.update(changes)
    return result


def start_handle(service, request):
    result = []
    thread = threading.Thread(target=lambda: result.append(service.handle(request)))
    thread.start()
    return thread, result


def test_success_uses_reserved_owner_and_copies_factual_metadata(resources):
    service, router, lease, decoder = resources()
    request = params()
    result = service.handle(request)
    assert result == lease.result
    router.reserve_gigaam_call.assert_called_once_with()
    decoder.assert_called_once_with(b"wav")
    audio, rate, deadline = lease.arguments
    assert audio is decoder.return_value[0]
    assert rate == 8000 and deadline == request["deadline_monotonic"]
    assert lease.release_count == 1


@pytest.mark.parametrize("privacy", [lambda: True, lambda: None, lambda: "false"])
def test_privacy_refuses_before_decode_or_reservation(resources, privacy):
    service, router, _, decoder = resources(privacy=privacy)
    assert service.handle(params())["status"] == "privacy_mode"
    decoder.assert_not_called()
    router.reserve_gigaam_call.assert_not_called()


def test_unreadable_privacy_fails_closed(resources):
    def broken_settings():
        raise OSError("settings unavailable")
    service, router, _, decoder = resources(privacy=broken_settings)
    assert service.handle(params()) == {"status": "privacy_mode", "text": ""}
    decoder.assert_not_called()
    router.reserve_gigaam_call.assert_not_called()


def test_privacy_enabled_during_inference_suppresses_text(resources):
    private = threading.Event()
    service, _, lease, _ = resources(block=True, privacy=private.is_set)
    thread, results = start_handle(service, params())
    try:
        assert lease.started.wait(2)
        private.set()
    finally:
        lease.finish.set()
        thread.join(3)
    assert not thread.is_alive()
    assert results == [{"status": "privacy_mode", "text": ""}]
    assert lease.release_count == 1


def test_privacy_beats_busy_so_caller_cannot_treat_it_as_fallback_permission(resources):
    private = threading.Event()
    service, router, lease, _ = resources(block=True, privacy=private.is_set)
    thread, results = start_handle(service, params())
    try:
        assert lease.started.wait(2)
        private.set()
        assert service.handle(params(request_id="second"))["status"] == "privacy_mode"
        assert router.reserve_gigaam_call.call_count == 1
    finally:
        lease.finish.set()
        thread.join(3)
    assert not thread.is_alive()
    assert results[0]["status"] == "privacy_mode"


@pytest.mark.parametrize("privacy_failure", [False, True])
def test_privacy_beats_timeout_and_keeps_real_owner_busy_until_drain(resources, privacy_failure):
    private = threading.Event()

    def privacy():
        if private.is_set() and privacy_failure:
            raise OSError("settings changed while waiting")
        return private.is_set()

    service, router, lease, _ = resources(block=True, privacy=privacy)
    thread, results = start_handle(service, params(deadline_monotonic=time.monotonic() + 0.15))
    try:
        assert lease.started.wait(2)
        private.set()
        thread.join(2)
        assert not thread.is_alive()
        assert results == [{"status": "privacy_mode", "text": ""}]
        assert lease.release_count == 0
        private.clear()
        assert service.handle(params(request_id="next"))["status"] == "busy"
        assert router.reserve_gigaam_call.call_count == 1
    finally:
        lease.finish.set()
        thread.join(3)
    assert service.close(timeout_sec=2)
    assert lease.release_count == 1


@pytest.mark.parametrize("changes", [
    {"request_id": ""}, {"request_id": 7}, {"request_id": "x" * 129},
    {"language": "auto"}, {"language": "es"},
    {"deadline_monotonic": float("nan")}, {"deadline_monotonic": float("inf")},
    {"deadline_monotonic": "invalid"}, {"deadline_monotonic": True},
    {"deadline_monotonic": 10 ** 1000},
    {"deadline_monotonic": time.monotonic() + 1000},
    {"audio_wav_b64": "%%%"}, {"audio_wav_b64": ""}, {"audio_wav_b64": b"d2F2"},
    {"audio_wav_b64": "A" * (2 * 1024 * 1024)},
])
def test_invalid_envelope_refused_before_decoder(resources, changes):
    service, router, _, decoder = resources()
    response = service.handle(params(**changes))
    assert response["status"] == "error" and response["text"] == ""
    decoder.assert_not_called()
    router.reserve_gigaam_call.assert_not_called()


def test_expired_deadline_refused_before_decoder(resources):
    service, router, _, decoder = resources()
    assert service.handle(params(deadline_monotonic=time.monotonic() - 1))["status"] == "timeout"
    decoder.assert_not_called()
    router.reserve_gigaam_call.assert_not_called()


def test_decode_failure_refused_before_reservation(resources):
    service, router, _, decoder = resources()
    decoder.side_effect = ValueError("malformed audio")
    result = service.handle(params())
    assert result["status"] == "error" and result["text"] == ""
    router.reserve_gigaam_call.assert_not_called()


@pytest.mark.parametrize("status", ["busy", "not_ready"])
def test_router_rejection_preserved(resources, status):
    service, _, lease, _ = resources(status=status)
    assert service.handle(params()) == {"status": status, "text": ""}
    assert not lease.started.is_set() and lease.release_count == 0


def test_concurrent_request_never_queues_at_router(resources):
    service, router, lease, _ = resources(block=True)
    thread, results = start_handle(service, params())
    try:
        assert lease.started.wait(2)
        assert service.handle(params(request_id="second"))["status"] == "busy"
        assert router.reserve_gigaam_call.call_count == 1
    finally:
        lease.finish.set()
        thread.join(3)
    assert not thread.is_alive() and results[0]["status"] == "ok"


def test_timeout_keeps_busy_until_actual_worker_release_and_drains(resources):
    service, router, lease, _ = resources(block=True)
    thread, results = start_handle(service, params(deadline_monotonic=time.monotonic() + 0.15))
    try:
        assert lease.started.wait(2)
        thread.join(2)
        assert not thread.is_alive()
        assert results == [{"status": "timeout", "text": ""}]
        assert lease.release_count == 0
        assert service.handle(params(request_id="second"))["status"] == "busy"
        assert router.reserve_gigaam_call.call_count == 1
        lease.finish.set()
        assert lease.released.wait(2)
        assert service.close(timeout_sec=2)
        assert lease.release_count == 1
    finally:
        lease.finish.set()
        thread.join(3)


def test_timeout_then_shutdown_is_bounded_and_repeated_close_can_finish(resources):
    service, _, lease, _ = resources(block=True)
    result = service.handle(params(deadline_monotonic=time.monotonic() + 0.05))
    assert result["status"] == "timeout"
    before = time.monotonic()
    assert service.close(timeout_sec=0.01) is False
    assert time.monotonic() - before < 0.5
    assert lease.release_count == 0
    assert service.handle(params())["status"] == "closing"
    lease.finish.set()
    assert service.close(timeout_sec=2) is True
    assert service.close(timeout_sec=0) is True
    assert lease.release_count == 1


def test_begin_shutdown_closes_admission_before_decode(resources):
    service, router, lease, decoder = resources()
    service.begin_shutdown()
    service.begin_shutdown()
    assert service.handle(params()) == {"status": "closing", "text": ""}
    decoder.assert_not_called()
    router.reserve_gigaam_call.assert_not_called()
    assert lease.release_count == 0


def test_shutdown_while_decoding_prevents_owner_admission(resources):
    service, router, _, decoder = resources()
    decoded = decoder.return_value
    def shutdown_decode(_):
        service.begin_shutdown()
        return decoded
    decoder.side_effect = shutdown_decode
    assert service.handle(params())["status"] == "closing"
    router.reserve_gigaam_call.assert_not_called()


def test_privacy_enabled_while_decoding_prevents_owner_admission(resources):
    private = threading.Event()
    service, router, _, decoder = resources(privacy=private.is_set)
    decoded = decoder.return_value
    def private_decode(_):
        private.set()
        return decoded
    decoder.side_effect = private_decode
    assert service.handle(params())["status"] == "privacy_mode"
    router.reserve_gigaam_call.assert_not_called()


def test_thread_start_failure_releases_lease_and_keeps_service_usable(resources, monkeypatch):
    service, router, lease, _ = resources()
    original_start = threading.Thread.start
    monkeypatch.setattr(threading.Thread, "start", Mock(side_effect=RuntimeError("start failed")))
    result = service.handle(params())
    assert result["status"] == "error" and result["text"] == ""
    assert lease.release_count == 1
    assert not lease.started.is_set()
    monkeypatch.setattr(threading.Thread, "start", original_start)
    next_lease = Lease()
    router.reserve_gigaam_call.return_value = (next_lease, "ok")
    assert service.handle(params(request_id="retry"))["status"] == "ok"
    assert next_lease.release_count == 1


def test_inference_exception_releases_without_exposing_exception_text(resources):
    service, _, lease, _ = resources(lease=Lease(error=RuntimeError("sensitive text")))
    result = service.handle(params())
    assert result["status"] == "error" and result["text"] == ""
    assert "sensitive" not in str(result)
    assert lease.release_count == 1


@pytest.mark.parametrize("worker_result,expected", [
    ({"status": "ok", "text": "  "}, "not_ready"),
    ({"status": "busy", "text": "hidden"}, "busy"),
    ({"status": "unexpected", "text": "hidden"}, "error"),
    (None, "error"),
])
def test_non_ok_and_invalid_worker_results_do_not_release_text(resources, worker_result, expected):
    lease = Lease()
    lease.result = worker_result
    service, _, _, _ = resources(lease=lease)
    result = service.handle(params())
    assert result["status"] == expected and result["text"] == ""


@pytest.mark.parametrize("confidence", [True, "0.9", -1, 2, float("nan"), float("inf"), 10 ** 1000])
def test_invalid_confidence_metadata_is_not_represented_as_model_quality(resources, confidence):
    lease = Lease()
    lease.result["confidence"] = confidence
    service, _, _, _ = resources(lease=lease)
    result = service.handle(params())
    assert result["status"] == "ok"
    assert "confidence" not in result
    assert result["confidence_source"] == "constant"


def test_success_is_never_replayed_from_a_transcript_cache(resources):
    service, router, _, _ = resources()
    assert service.handle(params())["status"] == "ok"
    # Drain the first worker without closing admission.
    service._active.thread.join(2)
    router.reserve_gigaam_call.return_value = (None, "not_ready")
    assert service.handle(params()) == {"status": "not_ready", "text": ""}
    assert router.reserve_gigaam_call.call_count == 2


def test_real_phone_wav_decodes_to_owner_float_samples_without_second_engine():
    raw = io.BytesIO()
    samples = np.array([0, 8192, -8192, 16384], dtype="<i2")
    with wave.open(raw, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(samples.tobytes())
    lease = Lease()
    router = Mock()
    router.reserve_gigaam_call.return_value = (lease, "ok")
    service = CallSTTService(router, lambda: False)
    try:
        result = service.handle(params(audio_wav_b64=base64.b64encode(raw.getvalue()).decode()))
        assert result["status"] == "ok"
        audio, rate, _ = lease.arguments
        assert rate == 8000
        assert audio.dtype == np.float32
        np.testing.assert_allclose(audio, samples.astype(np.float32) / 32768)
    finally:
        assert service.close(timeout_sec=2)


def test_owner_release_failure_blocks_future_admission_and_transcriber_close(resources):
    lease = Lease()
    def failing_release():
        lease.release_count += 1
        raise RuntimeError("release failed")
    lease.release = failing_release
    router = Mock()
    router.reserve_gigaam_call.return_value = (lease, "ok")
    service = CallSTTService(router, lambda: False)
    assert service.handle(params())["status"] == "error"
    assert service.close(timeout_sec=2) is False
    assert service.handle(params())["status"] == "closing"
    assert router.reserve_gigaam_call.call_count == 1
    assert service.close(timeout_sec=0) is False
    assert lease.release_count == 1
