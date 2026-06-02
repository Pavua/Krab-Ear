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
from typing import Any, Callable, TYPE_CHECKING

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

# Абсолютный потолок буфера (в сэмплах) — 1 минута при 16 kHz.
# Защита от OOM: sample_rate приходит от клиента (handle_ingest) и контролируется
# атакующим. Огромный sample_rate делает buffer_sec≈0 навсегда → flush по
# порогу _FLUSH_THRESHOLD_SEC никогда не срабатывает → буфер растёт без границ.
# Этот потолок форсирует flush независимо от sample_rate. См. W1770 HIGH.
_MAX_BUFFER_SAMPLES = 16000 * 60

# Допустимый диапазон частоты дискретизации (Гц). Значения вне диапазона
# (включая отрицательные/огромные) клампятся в handle_ingest со структурным
# warning — нельзя доверять полю sample_rate из IPC-запроса.
_MIN_SAMPLE_RATE = 8000
_MAX_SAMPLE_RATE = 192000


class LiveSubsService:
    """Буферизация и обработка потоковых аудио-чанков для живых субтитров."""

    def __init__(
        self,
        transcriber: "Transcriber",
        translator: "Translator",
        settings_get: Callable[[str, Any], Any] | None = None,
    ) -> None:
        self._transcriber = transcriber
        self._translator = translator
        self._settings_get: Callable[[str, Any], Any] = settings_get or (lambda k, d: d)
        self._lock = threading.RLock()
        self._buffer: list[np.ndarray] = []
        self._buffer_samples: int = 0
        self._session_start: float = time.monotonic()

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
        audio_array = self._decode_audio(audio_bytes, sample_rate)
        # Под локом — ТОЛЬКО мутация буфера и решение о flush. STT (несколько
        # секунд) намеренно НЕ выполняется здесь: head-of-line blocking
        # задерживал бы конкурентные ingest-чанки и приводил к их потере (W1770 MED).
        with self._lock:
            self._buffer.append(audio_array)
            self._buffer_samples += len(audio_array)
            buffer_sec = self._buffer_samples / max(sample_rate, 1)
            # Потолок _MAX_BUFFER_SAMPLES форсирует flush даже если sample_rate
            # настолько велик, что buffer_sec никогда не достигнет порога (OOM-защита).
            over_cap = self._buffer_samples >= _MAX_BUFFER_SAMPLES
            should_flush = is_final or buffer_sec >= _FLUSH_THRESHOLD_SEC or over_cap
            if over_cap and not (is_final or buffer_sec >= _FLUSH_THRESHOLD_SEC):
                logger.warning(
                    "LiveSubsService: буфер достиг потолка — форсирую flush",
                    extra={
                        "buffer_samples": self._buffer_samples,
                        "max_buffer_samples": _MAX_BUFFER_SAMPLES,
                        "sample_rate": sample_rate,
                    },
                )
        # Лок отпущен — _flush снимет снапшот буфера под локом и выполнит STT уже без него.
        if should_flush:
            return self._flush(sample_rate=sample_rate, target_lang=target_lang)
        return None

    def stop(self) -> dict[str, Any]:
        """Flush оставшегося буфера и сброс состояния.

        Если privacy_mode_enabled=True — буфер сбрасывается БЕЗ транскрипции
        и эмиссии событий (аудио, накопленное до переключения режима, не утекает).
        """
        with self._lock:
            if self._settings_get("privacy_mode_enabled", False):
                self._reset()
                return {"status": "stopped", "flushed": False, "skipped": True,
                        "reason": "privacy_mode_active"}
            has_data = bool(self._buffer)
        # STT выполняется вне service-лока (_flush сам берёт лок только под снапшот).
        result = self._flush(sample_rate=16000, target_lang="off") if has_data else None
        with self._lock:
            self._reset()
        return {"status": "stopped", "flushed": result is not None}

    def buffer_duration_sec(self, sample_rate: int = 16000) -> float:
        """Текущая длительность буфера в секундах."""
        with self._lock:
            return self._buffer_samples / max(sample_rate, 1)

    def reset(self) -> None:
        """Очищает буфер БЕЗ транскрипции и эмиссии событий (под локом).

        Публичная точка для privacy-purge: handle_purge_all_data вызывает её,
        чтобы накопленное system-audio было стёрто немедленно, не пройдя через
        STT/EventBus. Отличается от stop(): никакого flush, никаких событий.
        """
        with self._lock:
            self._reset()

    # ── IPC handlers ──────────────────────────────────────────────────────────

    def handle_ingest(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC handler: live_subs_ingest."""
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": True, "skipped": True, "reason": "privacy_mode_active"}

        audio_b64 = params.get("audio_chunk", "")
        target_lang = str(params.get("target_lang", "off"))
        sample_rate = self._sanitize_sample_rate(params.get("sample_rate", 16000))
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
        """Выполняет STT + translate по накопленному буферу и сбрасывает его.

        W1770 MED: под service-локом выполняется ТОЛЬКО снапшот+detach буфера
        (concatenate + _reset). Многосекундный STT/translate выполняется уже
        ПОСЛЕ освобождения лока — иначе конкурентные ingest-чанки блокируются
        (head-of-line blocking) и теряются. MLX-сериализация при этом сохраняется:
        она живёт внутри transcriber/engine (mlx_lock), а не в этом service-локе.
        """
        # Снапшот буфера под локом: атомарно забираем накопленное и сбрасываем,
        # чтобы конкурентный ingest не дописал в уже обрабатываемый массив.
        with self._lock:
            if not self._buffer:
                return {"text": "", "translation": None}
            start_ts = self._session_start
            end_ts = time.monotonic()
            audio = np.concatenate(self._buffer).astype(np.float32)
            self._reset()

        # ── Дальше — вне service-лока (тяжёлый STT/translate) ──────────────────

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
                    network_mode="offline_default",
                )
                translation = tr.text or None
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
        # W1770 MED: НИКОГДА не логируем сам текст транскрипта/перевода (PII).
        # Только метаданные — гарантия metadata-only логирования.
        if text:
            logger.info(
                "LiveSubsService: flush OK",
                extra={
                    "text_len": len(text),
                    "lang": language_detected,
                    "translation_len": len(translation) if translation else 0,
                },
            )
        else:
            logger.info(
                "LiveSubsService: flush EMPTY (Whisper вернул пустой текст)",
                extra={"lang": language_detected},
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
        self._buffer = []
        self._buffer_samples = 0
        self._session_start = time.monotonic()

    @staticmethod
    def _sanitize_sample_rate(raw: Any) -> int:
        """Валидирует и клампит sample_rate из IPC-запроса в безопасный диапазон.

        sample_rate приходит от клиента и нельзя ему доверять (W1770 HIGH):
        огромное значение ломает flush-gate по buffer_sec → OOM; нулевое/
        отрицательное приводит к делению на ноль / некорректному ресемплингу.
        Значения вне [_MIN_SAMPLE_RATE, _MAX_SAMPLE_RATE] клампятся со структурным
        warning; нечисловой ввод → дефолт 16000.
        """
        try:
            sr = int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "LiveSubsService: нечисловой sample_rate — дефолт 16000",
                extra={"raw_sample_rate": repr(raw)},
            )
            return 16000
        if sr < _MIN_SAMPLE_RATE or sr > _MAX_SAMPLE_RATE:
            clamped = min(max(sr, _MIN_SAMPLE_RATE), _MAX_SAMPLE_RATE)
            logger.warning(
                "LiveSubsService: sample_rate вне диапазона — клампинг",
                extra={
                    "requested_sample_rate": sr,
                    "clamped_sample_rate": clamped,
                    "min_sample_rate": _MIN_SAMPLE_RATE,
                    "max_sample_rate": _MAX_SAMPLE_RATE,
                },
            )
            return clamped
        return sr

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
