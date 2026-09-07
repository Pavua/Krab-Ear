"""Ограниченный WAV и конверт эфемерного телефонного IPC-запроса.

Модуль не читает настройки, не создаёт моделей и не сохраняет аудио. Частота
8 кГц сохраняется: подготовка 8→16 кГц принадлежит существующему STT owner.
"""

from __future__ import annotations

import base64
import json
import math
import struct
import time
import uuid

import numpy as np

from backend.ipc_constants import IPC_MAX_MESSAGE_BYTES
from backend.request_signing import RequestSigner

CALL_MAX_AUDIO_SECONDS = 25
CALL_MAX_SAMPLE_RATE = 16000
CALL_MAX_OUTPUT_SAMPLES = CALL_MAX_AUDIO_SECONDS * CALL_MAX_SAMPLE_RATE
CALL_MAX_WAV_BYTES = IPC_MAX_MESSAGE_BYTES
CALL_STT_METHOD = "transcribe_ephemeral_call"


class CallSTTValidationError(ValueError):
    """Входные данные не соответствуют ограниченному контракту звонка."""


def decode_call_wav(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    """Проверяет полный RIFF-контейнер и декодирует только mono PCM16 8/16 кГц."""
    if not isinstance(wav_bytes, bytes) or not 44 <= len(wav_bytes) <= CALL_MAX_WAV_BYTES:
        raise CallSTTValidationError("Недопустимый размер WAV")
    if wav_bytes[:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
        raise CallSTTValidationError("Ожидался RIFF/WAVE")
    if struct.unpack_from("<I", wav_bytes, 4)[0] + 8 != len(wav_bytes):
        raise CallSTTValidationError("Размер RIFF не совпадает с данными")

    fmt = None
    pcm = None
    offset = 12
    while offset < len(wav_bytes):
        if offset + 8 > len(wav_bytes):
            raise CallSTTValidationError("Обрезан WAV chunk")
        chunk_id = wav_bytes[offset:offset + 4]
        size = struct.unpack_from("<I", wav_bytes, offset + 4)[0]
        start = offset + 8
        end = start + size
        offset = end + (size & 1)
        if offset > len(wav_bytes):
            raise CallSTTValidationError("Обрезаны данные WAV chunk")
        if chunk_id == b"fmt ":
            if fmt is not None or pcm is not None or size not in (16, 18):
                raise CallSTTValidationError("Недопустимый fmt WAV")
            if size == 18 and wav_bytes[end - 2:end] != b"\0\0":
                raise CallSTTValidationError("PCM расширение не поддерживается")
            fmt = struct.unpack_from("<HHIIHH", wav_bytes, start)
        elif chunk_id == b"data":
            if pcm is not None or fmt is None:
                raise CallSTTValidationError("Недопустимый порядок или повтор data WAV")
            pcm = memoryview(wav_bytes)[start:end]

    if fmt is None or pcm is None:
        raise CallSTTValidationError("Отсутствует fmt или data WAV")
    encoding, channels, rate, byte_rate, alignment, bits = fmt
    if (encoding, channels, bits) != (1, 1, 16) or rate not in (8000, 16000):
        raise CallSTTValidationError("Нужен mono PCM16 WAV с частотой 8 или 16 кГц")
    if byte_rate != rate * 2 or alignment != 2:
        raise CallSTTValidationError("Некорректные byte rate или block align WAV")
    frames = len(pcm) // 2
    if len(pcm) % 2 or not 0 < frames <= min(rate * CALL_MAX_AUDIO_SECONDS, CALL_MAX_OUTPUT_SAMPLES):
        raise CallSTTValidationError("Пустое, слишком длинное или обрезанное PCM аудио")
    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / np.float32(32768)
    if not np.isfinite(audio).all():
        raise CallSTTValidationError("PCM содержит нечисловые samples")
    return audio, rate


def build_call_request(
    wav_bytes: bytes,
    *,
    request_id: str,
    deadline_monotonic: float,
    signing_enabled: bool,
    signing_secret: str,
) -> bytes:
    """Строит один JSON+LF кадр и проверяет полный IPC cap до соединения."""
    if type(signing_enabled) is not bool:
        raise CallSTTValidationError("Некорректный режим IPC signing")
    if (
        type(deadline_monotonic) not in (float, int)
        or not math.isfinite(deadline_monotonic)
        or deadline_monotonic <= time.monotonic()
    ):
        raise CallSTTValidationError("Недопустимый или истёкший deadline")
    try:
        valid_id = isinstance(request_id, str) and str(uuid.UUID(request_id)) == request_id
    except (ValueError, AttributeError):
        valid_id = False
    if not valid_id:
        raise CallSTTValidationError("request_id должен быть каноническим UUID")
    decode_call_wav(wav_bytes)
    params = {
        "request_id": request_id,
        "audio_wav_b64": base64.b64encode(wav_bytes).decode("ascii"),
        "language": "ru",
        "deadline_monotonic": deadline_monotonic,
    }
    envelope = {"id": request_id, "method": CALL_STT_METHOD, "params": params}
    if signing_enabled:
        try:
            signed = RequestSigner().sign_request(CALL_STT_METHOD, params, signing_secret)
        except (ValueError, TypeError) as exc:
            raise CallSTTValidationError("Некорректный секрет IPC signing") from exc
        envelope.update(signature=signed.signature, timestamp=signed.timestamp, nonce=signed.nonce)
    frame = (json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    if len(frame) > IPC_MAX_MESSAGE_BYTES:
        raise CallSTTValidationError("Полный конверт превышает лимит IPC")
    return frame
