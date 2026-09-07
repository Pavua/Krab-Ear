"""Контракт ограниченного телефонного WAV и подписанного IPC-конверта."""

import importlib
import importlib.util
import io
import json
import struct
import time
import uuid
import wave

import numpy as np
import pytest

from backend.ipc_constants import IPC_MAX_MESSAGE_BYTES
from backend.request_signing import RequestSigner


def _wire():
    assert importlib.util.find_spec("backend.call_stt_wire"), "call WAV wire missing"
    return importlib.import_module("backend.call_stt_wire")


def make_wav(*, rate=8000, channels=1, width=2, frames=800):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(b"\x00" * (frames * channels * width))
    return output.getvalue()


@pytest.mark.parametrize("rate", [8000, 16000])
def test_decode_preserves_phone_sample_rate_for_owner_preparation(rate):
    wire = _wire()
    payload = bytearray(make_wav(rate=rate))
    payload[-6:] = struct.pack("<hhh", -32768, 0, 32767)
    audio, actual_rate = wire.decode_call_wav(bytes(payload))
    assert actual_rate == rate
    assert audio.shape == (800,)
    assert audio.dtype == np.float32
    assert np.isfinite(audio).all()
    np.testing.assert_array_equal(audio[-3:], [-1.0, 0.0, 32767 / 32768])


@pytest.mark.parametrize("payload", [
    b"", b"RIFF", b"garbage", make_wav(frames=0),
    make_wav(channels=2), make_wav(width=1), make_wav(width=4),
    make_wav(rate=44100), make_wav(frames=8000 * 25 + 1),
    make_wav(rate=16000, frames=16000 * 25 + 1),
    make_wav()[:-2], make_wav() + b"\x00",
    b"x" * (IPC_MAX_MESSAGE_BYTES + 1),
])
def test_rejects_empty_malformed_or_unbounded_audio(payload):
    wire = _wire()
    with pytest.raises(wire.CallSTTValidationError):
        wire.decode_call_wav(payload)


@pytest.mark.parametrize("offset,value,fmt", [
    (20, 3, "<H"),  # IEEE float даже при width=16.
    (28, 123, "<I"),  # Byte rate не соответствует фактическому PCM.
    (32, 4, "<H"),  # Block align не соответствует mono PCM16.
    (40, 0xFFFFFFFF, "<I"),  # Объявленное data длиннее файла.
    (40, 1599, "<I"),  # Нецелый PCM sample.
])
def test_rejects_inconsistent_pcm_headers(offset, value, fmt):
    wire = _wire()
    payload = bytearray(make_wav())
    struct.pack_into(fmt, payload, offset, value)
    with pytest.raises(wire.CallSTTValidationError):
        wire.decode_call_wav(bytes(payload))


def test_rejects_duplicate_data_chunk():
    wire = _wire()
    payload = bytearray(make_wav())
    payload.extend(b"data" + struct.pack("<I", 2) + b"\0\0")
    struct.pack_into("<I", payload, 4, len(payload) - 8)
    with pytest.raises(wire.CallSTTValidationError):
        wire.decode_call_wav(bytes(payload))


def test_accepts_duration_boundary_without_resampling():
    audio, rate = _wire().decode_call_wav(make_wav(frames=8000 * 25))
    assert len(audio) == 8000 * 25
    assert rate == 8000


def test_exact_signed_envelope_protects_id_audio_and_deadline():
    wire = _wire()
    request_id = str(uuid.uuid4())
    deadline = time.monotonic() + 1
    secret = RequestSigner.generate_secret()
    frame = wire.build_call_request(
        make_wav(), request_id=request_id, deadline_monotonic=deadline,
        signing_enabled=True, signing_secret=secret,
    )
    assert frame.endswith(b"\n")
    assert len(frame) <= IPC_MAX_MESSAGE_BYTES
    envelope = json.loads(frame)
    assert envelope["id"] == envelope["params"]["request_id"] == request_id
    assert envelope["method"] == "transcribe_ephemeral_call"
    assert envelope["params"]["language"] == "ru"
    assert envelope["params"]["deadline_monotonic"] == deadline
    verifier = RequestSigner()
    assert verifier.verify_request(
        envelope["method"], envelope["params"], envelope["signature"], secret,
        timestamp=envelope["timestamp"], nonce=envelope["nonce"],
    )
    for key, value in [
        ("request_id", str(uuid.uuid4())), ("audio_wav_b64", "AAAA"),
        ("deadline_monotonic", deadline + 100),
    ]:
        tampered = {**envelope["params"], key: value}
        assert not RequestSigner().verify_request(
            envelope["method"], tampered, envelope["signature"], secret,
            timestamp=envelope["timestamp"], nonce=envelope["nonce"],
        )


@pytest.mark.parametrize("secret", ["", "   ", None, 123])
def test_enabled_signing_requires_valid_secret(secret):
    wire = _wire()
    with pytest.raises(wire.CallSTTValidationError):
        wire.build_call_request(
            make_wav(), request_id=str(uuid.uuid4()),
            deadline_monotonic=time.monotonic() + 1,
            signing_enabled=True, signing_secret=secret,
        )


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), -1, True, "1"])
def test_invalid_deadline_rejected(deadline):
    wire = _wire()
    with pytest.raises(wire.CallSTTValidationError):
        wire.build_call_request(
            make_wav(), request_id=str(uuid.uuid4()), deadline_monotonic=deadline,
            signing_enabled=False, signing_secret="",
        )


def test_full_envelope_cap_accounts_for_base64_and_hmac():
    wire = _wire()
    payload = make_wav(rate=16000, frames=16000 * 25)
    assert len(payload) < IPC_MAX_MESSAGE_BYTES
    with pytest.raises(wire.CallSTTValidationError, match="IPC"):
        wire.build_call_request(
            payload, request_id=str(uuid.uuid4()),
            deadline_monotonic=time.monotonic() + 1,
            signing_enabled=True, signing_secret=RequestSigner.generate_secret(),
        )


def test_unsigned_envelope_is_only_explicitly_enabled():
    envelope = json.loads(_wire().build_call_request(
        make_wav(), request_id=str(uuid.uuid4()),
        deadline_monotonic=time.monotonic() + 1,
        signing_enabled=False, signing_secret="",
    ))
    assert set(envelope) == {"id", "method", "params"}
