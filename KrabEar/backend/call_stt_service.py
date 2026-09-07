"""Эфемерное STT звонка через уже загруженного владельца GigaAM.

Таймаут IPC завершает только ожидание вызывающего. Единственный daemon-поток
держит lease до реального завершения инференса; новые запросы не копятся в
очереди. Здесь нет загрузки модели, истории, временных файлов или STT fallback.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import math
import re
import threading
import time
from typing import Any, Callable

from backend.call_stt_wire import CALL_MAX_WAV_BYTES, decode_call_wav
from backend.ipc_constants import IPC_MAX_MESSAGE_BYTES


_MAX_DEADLINE_SECONDS = 25.0
_MAX_BASE64_LENGTH = min(4 * ((CALL_MAX_WAV_BYTES + 2) // 3), IPC_MAX_MESSAGE_BYTES)
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_STATUSES = frozenset({
    "ok", "busy", "not_ready", "timeout", "privacy_mode", "closing", "error",
})
_METADATA_FIELDS = (
    "engine", "adapter", "mode", "model", "transport", "reason", "confidence_source",
)


def _response(status: str, reason: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "text": ""}
    if reason:
        result["reason"] = reason
    return result


def _sanitize_result(value: Any) -> dict[str, Any]:
    """Передаём только поля протокола; текст допустим лишь при успехе."""
    if not isinstance(value, dict) or value.get("status") not in _STATUSES:
        return _response("error", "invalid_owner_result")
    status = value["status"]
    result = _response(status)
    if status == "ok":
        text = value.get("text")
        if not isinstance(text, str):
            return _response("error", "invalid_owner_result")
        if not text.strip():
            return _response("not_ready", "empty_transcription")
        # Ответ короткого телефонного хода не может заполнить IPC-конверт.
        if len(text) > 65536:
            return _response("error", "owner_result_too_large")
        result["text"] = text
        if value.get("language") == "ru":
            result["language"] = "ru"
        confidence = value.get("confidence")
        if (
            isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and 0 <= confidence <= 1
            and math.isfinite(confidence)
        ):
            result["confidence"] = confidence
    for key in _METADATA_FIELDS:
        field_value = value.get(key)
        if isinstance(field_value, str) and len(field_value) <= 256:
            result[key] = field_value
    return result


@dataclass
class _CallWork:
    done: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    result: dict[str, Any] | None = None
    discard: bool = False


class CallSTTService:
    """Одна owner-задача без очереди, с закрываемым admission и bounded drain.

    ``privacy_mode_fn`` возвращает строго bool; ошибка чтения и любой иной
    результат закрывают доступ. Router атомарно резервирует существующий
    worker: ``reserve_gigaam_call() -> (lease | None, status)``. Lease владеет
    ресурсами вплоть до ``release()``, в том числе после таймаута клиента.
    """

    def __init__(
        self,
        router: Any,
        privacy_mode_fn: Callable[[], bool],
        *,
        shutdown_timeout_sec: float = 0.25,
    ) -> None:
        self._router = router
        self._privacy_mode_fn = privacy_mode_fn
        self._shutdown_timeout_sec = self._bounded_timeout(shutdown_timeout_sec)
        self._lock = threading.Lock()
        self._closing = False
        self._release_failed = False
        self._active: _CallWork | None = None

    @staticmethod
    def _bounded_timeout(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("shutdown timeout must be finite and nonnegative")
        if not math.isfinite(value) or not 0 <= value <= _MAX_DEADLINE_SECONDS:
            raise ValueError("shutdown timeout must be between 0 and 25 seconds")
        return float(value)

    def _privacy_enabled(self) -> bool:
        try:
            return self._privacy_mode_fn() is not False
        except Exception:
            return True

    def _reap_finished_locked(self) -> None:
        work = self._active
        if work and work.thread and work.done.is_set() and not work.thread.is_alive():
            self._active = None

    def handle(self, params: dict[str, Any]) -> dict[str, Any]:
        """Валидируем короткий RU-запрос и ждём только остаток его дедлайна."""
        if self._privacy_enabled():
            return _response("privacy_mode")
        with self._lock:
            if self._closing:
                return _response("closing")
            self._reap_finished_locked()
            if self._active is not None:
                return _response("busy")
        if not isinstance(params, dict):
            return _response("error", "invalid_params")
        request_id = params.get("request_id")
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            return _response("error", "invalid_request_id")
        if params.get("language") != "ru":
            return _response("error", "unsupported_language")
        deadline = params.get("deadline_monotonic")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            return _response("error", "invalid_deadline")
        try:
            deadline = float(deadline)
        except (OverflowError, ValueError):
            return _response("error", "invalid_deadline")
        if not math.isfinite(deadline):
            return _response("error", "invalid_deadline")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _response("timeout")
        if remaining > _MAX_DEADLINE_SECONDS:
            return _response("error", "invalid_deadline")
        encoded = params.get("audio_wav_b64")
        if not isinstance(encoded, str) or not 0 < len(encoded) <= _MAX_BASE64_LENGTH:
            return _response("error", "invalid_audio_payload")
        try:
            wav_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return _response("error", "invalid_audio_payload")
        if not wav_bytes or len(wav_bytes) > CALL_MAX_WAV_BYTES:
            return _response("error", "invalid_audio_payload")
        try:
            audio, sample_rate = decode_call_wav(wav_bytes)
        except Exception:
            return _response("error", "invalid_audio")

        with self._lock:
            # Повторяем gates после decode: shutdown/privacy могут измениться.
            if self._privacy_enabled():
                return _response("privacy_mode")
            if self._closing:
                return _response("closing")
            self._reap_finished_locked()
            if self._active is not None:
                return _response("busy")
            if time.monotonic() >= deadline:
                return _response("timeout")
            try:
                lease, status = self._router.reserve_gigaam_call()
            except Exception:
                return _response("error", "owner_admission_failed")
            if lease is None:
                if status in {"busy", "not_ready", "closing"}:
                    return _response(status)
                return _response("error", "invalid_owner_admission")
            work = _CallWork()
            self._active = work
            try:
                work.thread = threading.Thread(
                    target=self._run,
                    args=(work, lease, audio, sample_rate, deadline),
                    name="KrabEar-CallSTT",
                    daemon=True,
                )
                # Под общим lock: close не увидит ещё не запущенный поток.
                work.thread.start()
            except Exception:
                self._active = None
                try:
                    lease.release()
                except Exception:
                    self._release_failed = True
                    self._closing = True
                return _response("error", "owner_thread_start_failed")

        finished = work.done.wait(max(0.0, deadline - time.monotonic()))
        with self._lock:
            # Privacy запрещает и выдачу текста, и retryable timeout: иначе
            # REST/VG может принять его за разрешение облачного fallback.
            if self._privacy_enabled():
                work.discard = True
                work.result = None
                return _response("privacy_mode")
            if not finished or time.monotonic() >= deadline:
                work.discard = True
                work.result = None
                return _response("timeout")
            result = work.result
            work.result = None  # Эфемерная передача; нет кэша текста по request_id.
            return result if result is not None else _response("error", "missing_owner_result")

    def _run(self, work: _CallWork, lease: Any, audio: Any, rate: int, deadline: float) -> None:
        result = _response("error", "owner_inference_failed")
        release_failed = False
        try:
            result = _sanitize_result(lease.run(audio, rate, deadline))
        except Exception:
            # Исключение может содержать транскрипт; наружу только reason-код.
            pass
        finally:
            try:
                lease.release()
            except Exception:
                release_failed = True
                result = _response("error", "owner_release_failed")
            with self._lock:
                if release_failed:
                    self._release_failed = True
                    self._closing = True
                if not work.discard:
                    work.result = result
                work.done.set()

    def begin_shutdown(self) -> None:
        """Атомарно закрываем admission до начала ожидания owner drain."""
        with self._lock:
            self._closing = True

    def close(self, timeout_sec: float | None = None) -> bool:
        """False запрещает владельцу закрывать transcriber под активным STT."""
        self.begin_shutdown()
        timeout = self._shutdown_timeout_sec if timeout_sec is None else self._bounded_timeout(timeout_sec)
        with self._lock:
            self._reap_finished_locked()
            work = self._active
        if work is not None and work.thread is not None:
            if work.thread is threading.current_thread():
                return False
            work.thread.join(timeout=timeout)
        with self._lock:
            self._reap_finished_locked()
            return self._active is None and not self._release_failed
