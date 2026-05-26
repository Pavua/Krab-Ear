"""LiveSubsService — потоковый STT + перевод для живых субтитров (Sprint 2B).

Принимает audio-чанки (base64 PCM 16 kHz mono), аккумулирует в буфере
и выполняет flush при накоплении ≥3 секунд или при is_final=True.
После flush: Whisper STT → translate → emit live_subs.result через EventBus.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from typing import Any, TYPE_CHECKING

import numpy as np

from backend.event_bus import bus as event_bus
from contracts.live_subs_events import LiveSubsResult
from contracts.registry import EventType

if TYPE_CHECKING:
    from backend.transcriber import Transcriber
    from backend.translator import Translator

logger = logging.getLogger("KrabEar.Backend.LiveSubsService")

# Размер буфера (в секундах), при достижении которого происходит авто-flush.
_FLUSH_THRESHOLD_SEC = 3.0


class LiveSubsService:
    """Буферизация и обработка потоковых аудио-чанков для живых субтитров."""

    def __init__(
        self,
        transcriber: "Transcriber",
        translator: "Translator",
        settings: dict[str, Any] | None = None,
    ) -> None:
        self._transcriber = transcriber
        self._translator = translator
        self._settings: dict[str, Any] = settings if settings is not None else {}
        self._buffer: list[np.ndarray] = []
        self._buffer_samples: int = 0
        self._session_start: float = time.monotonic()
        # W1147 F2: lock protects all _buffer/_buffer_samples/_session_start mutations
        self._buffer_lock = threading.Lock()

    # ── public API ────────────────────────────────────────────────────────────

    def ingest(
        self,
        audio_bytes: bytes,
        sample_rate: int,
        target_lang: str,
        is_final: bool,
    ) -> dict[str, Any] | None:
        """Добавляет чанк в буфер; при необходимости выполняет flush.

        Returns:
            None если flush не произошёл, иначе dict с результатами.
        """
        # W1147 F5: skip all processing when privacy mode is enabled
        if self._settings.get("privacy_mode_enabled"):
            return None

        audio_array = self._decode_audio(audio_bytes, sample_rate)
        with self._buffer_lock:
            self._buffer.append(audio_array)
            self._buffer_samples += len(audio_array)
            buffer_sec = self._buffer_samples / max(sample_rate, 1)
            should_flush = is_final or buffer_sec >= _FLUSH_THRESHOLD_SEC

        if should_flush:
            return self._flush(sample_rate=sample_rate, target_lang=target_lang)
        return None

    def stop(self) -> dict[str, Any]:
        """Flush оставшегося буфера и сброс состояния."""
        with self._buffer_lock:
            has_data = bool(self._buffer)
        result = self._flush(sample_rate=16000, target_lang="off") if has_data else None
        self._reset()
        return {"status": "stopped", "flushed": result is not None}

    def buffer_duration_sec(self, sample_rate: int = 16000) -> float:
        """Текущая длительность буфера в секундах."""
        with self._buffer_lock:
            return self._buffer_samples / max(sample_rate, 1)

    # ── IPC handlers ──────────────────────────────────────────────────────────

    def handle_ingest(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC handler: live_subs_ingest."""
        # W1147 F5: top-level privacy guard for IPC entry point
        if self._settings.get("privacy_mode_enabled"):
            return {"ok": True, "skipped": "privacy_mode"}

        audio_b64 = params.get("audio_chunk", "")
        target_lang = str(params.get("target_lang", "off"))
        sample_rate = int(params.get("sample_rate", 16000))
        is_final = bool(params.get("is_final", False))

        try:
            audio_bytes = base64.b64decode(audio_b64)
        except Exception as exc:
            raise ValueError(f"audio_chunk: invalid base64: {exc}") from exc

        result = self.ingest(
            audio_bytes=audio_bytes,
            sample_rate=sample_rate,
            target_lang=target_lang,
            is_final=is_final,
        )

        buf_sec = self.buffer_duration_sec(sample_rate)
        if result is not None:
            return {
                "status": "flushed",
                "buffer_duration_sec": buf_sec,
                "text": result.get("text"),
                "translation": result.get("translation"),
            }
        return {"status": "accepted", "buffer_duration_sec": buf_sec}

    def handle_stop(self, params: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        """IPC handler: live_subs_stop."""
        return self.stop()

    # ── internals ─────────────────────────────────────────────────────────────

    def _flush(self, sample_rate: int, target_lang: str) -> dict[str, Any]:
        """Выполняет STT + translate по накопленному буферу и сбрасывает его."""
        # W1147 F5: guard emission even when called from stop() after privacy mode change
        if self._settings.get("privacy_mode_enabled"):
            self._reset()
            return {"text": "", "translation": None}

        with self._buffer_lock:
            if not self._buffer:
                return {"text": "", "translation": None}
            start_ts = self._session_start
            end_ts = time.monotonic()
            audio = np.concatenate(self._buffer).astype(np.float32)

        self._reset()

        # Ресемплинг: Swift/SCStream отдаёт нативную частоту (обычно 48 kHz),
        # Whisper ожидает строго 16 kHz. Без ресемплинга Whisper воспринимает
        # audio pitch-shifted (×3 медленнее) → text="" → confidence=0.00.
        _WHISPER_SR = 16000
        if sample_rate != _WHISPER_SR and sample_rate > 0:
            try:
                from scipy.signal import resample_poly  # type: ignore[import]
                from math import gcd
                _g = gcd(sample_rate, _WHISPER_SR)
                audio = resample_poly(audio, _WHISPER_SR // _g, sample_rate // _g).astype(np.float32)
                logger.debug(
                    "LiveSubsService: resampled %d Hz → %d Hz (%d → %d samples)",
                    sample_rate, _WHISPER_SR, len(audio) * sample_rate // _WHISPER_SR, len(audio),
                )
            except Exception:
                logger.exception("LiveSubsService: ресемплинг не удался, STT получит raw %d Hz", sample_rate)

        # STT (skip_vad_prefilter=True для live_subs: VAD-модель тренирована на
        # mic input и speech_ratio=0.0 на компрессированном system-audio из YouTube
        # → STT никогда не вызывается. Для live субтитров VAD контрпродуктивен —
        # короткие чанки уже отфильтрованы на уровне Swift SystemAudioCapture.)
        stt_result = self._transcriber.transcribe(
            audio, quality_profile="balanced", skip_vad_prefilter=True
        )
        text = stt_result.get("text", "").strip()
        language_detected = stt_result.get("language")

        # Translate
        translation: str | None = None
        if text and target_lang and target_lang not in ("off", "none", ""):
            try:
                tr = self._translator.translate(
                    text=text,
                    mode=target_lang,
                    network_mode="offline",
                )
                translation = tr.translated_text or None
            except Exception:
                logger.exception("LiveSubsService: ошибка перевода")

        # Emit event
        event_payload = LiveSubsResult(
            text=text,
            translation=translation,
            start_ts=start_ts,
            end_ts=end_ts,
            language_detected=language_detected,
        )
        event_bus.emit_typed(EventType.LIVE_SUBS_RESULT, event_payload)
        if text:
            logger.info(
                "LiveSubsService: flush OK text_len=%d preview=%r lang=%s translation_len=%d",
                len(text),
                text[:30],
                language_detected,
                len(translation) if translation else 0,
            )
        else:
            logger.info(
                "LiveSubsService: flush EMPTY (Whisper вернул пустой текст, lang=%s)",
                language_detected,
            )

        return {
            "text": text,
            "translation": translation,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "language_detected": language_detected,
        }

    def _reset(self) -> None:
        """Сбрасывает буфер и метки времени."""
        with self._buffer_lock:
            self._buffer = []
            self._buffer_samples = 0
            self._session_start = time.monotonic()

    @staticmethod
    def _decode_audio(audio_bytes: bytes, sample_rate: int) -> np.ndarray:
        """Декодирует сырые PCM int16 байты в float32 [-1, 1]."""
        if len(audio_bytes) == 0:
            return np.zeros(0, dtype=np.float32)
        # Ожидаем 16-bit PCM (2 байта на сэмпл)
        if len(audio_bytes) % 2 != 0:
            audio_bytes = audio_bytes[: len(audio_bytes) - 1]
        pcm = np.frombuffer(audio_bytes, dtype=np.int16)
        return pcm.astype(np.float32) / 32768.0
