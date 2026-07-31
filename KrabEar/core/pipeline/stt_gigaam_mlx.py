"""GigaAM v3 через MLX (aystream/gigaam-mlx) — транспорт "mlx".

Отличия от PyTorch-ветки (stt_gigaam.GigaAMAdapter):

* Инференс идёт в ГЛАВНОМ процессе на MLX/Metal, поэтому каждый вызов ОБЯЗАН
  держать mlx_lock (инвариант mlx_lock.py: любой MLX-инференс — под локом).
  PyTorch-ветка лока не берёт — там Metal используется через torch.mps.
* Лок берётся ПО-ЧАНКОВО (аудио режется до 20 c): живая диктовка whisper
  ждёт максимум один чанк (доли секунды при ~77x RT), а не весь файл.
  Межпроцессный flock имеет 5-секундный timeout с raise — длительное
  удержание роняло бы чужие вызовы.
* Загрузка модели и скачивание весов — ВНЕ лока.
* Каждый инференс-вызов — под MLX watchdog: зависший Metal-вызов без
  watchdog повесил бы RLock навечно (freeze-class).
* gigaam_mlx импортируется лениво: библиотека есть только на Apple Silicon,
  py3.12 ubuntu-parity CI работает без неё.

Модель выдаёт нативную пунктуацию/капитализацию → в результат добавляется
``native_punctuation=True`` (engine по нему пропускает punctuation-LLM-pass).
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import wave
from typing import Optional

import numpy as np

from core.audio_chunker import AudioChunker
from core.mlx_inter_lock import mlx_inter_process_lock
from core.mlx_lock import mlx_lock
from core.mlx_subprocess import get_watchdog

logger = logging.getLogger("KrabEar.GigaAMMLX")

_REQUIRED_SAMPLE_RATE = 16000

# Жёсткий предел GigaAM — 25 c на массив; режем по 20 c с осознанным запасом
# (см. engine._GIGAAM_MAX_CHUNK_SEC и инцидент «старый порог 30 s терял записи»).
_MAX_CHUNK_SEC = 20.0

# Маппинг режимов конфига (stt_gigaam_mode) на типы моделей gigaam-mlx.
_MODE_TO_MLX = {
    "rnnt": "rnnt",
    "ctc": "ctc",
    "v3_e2e_rnnt": "rnnt",
    "v3_e2e_ctc": "ctc",
}


class GigaAMMLXAdapter:
    """Адаптер gigaam-mlx с тем же публичным контрактом, что GigaAMAdapter.

    Пример::

        adapter = GigaAMMLXAdapter(mode="v3_e2e_rnnt")
        result = adapter.transcribe(audio_array, sample_rate=16000)
        # result == {"text": "...", "language": "ru", "confidence": 0.9,
        #            "engine": "gigaam-mlx-rnnt", "native_punctuation": True}
    """

    def __init__(
        self,
        mode: str = "rnnt",
        chunker: Optional[AudioChunker] = None,
        watchdog_timeout_sec: float = 120.0,
    ) -> None:
        if mode not in _MODE_TO_MLX:
            raise ValueError(
                f"GigaAMMLXAdapter: неподдерживаемый mode={mode!r}. "
                f"Допустимые значения: {sorted(_MODE_TO_MLX)}"
            )
        self._mlx_model_type = _MODE_TO_MLX[mode]
        self._chunker = chunker or AudioChunker()
        self._watchdog_timeout_sec = watchdog_timeout_sec
        self._model: Optional[object] = None
        self._tokenizer: Optional[object] = None
        # Сериализация тяжёлой lazy-загрузки (зеркало GigaAMAdapter._model_lock).
        self._model_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Публичный API (контракт GigaAMAdapter)
    # ------------------------------------------------------------------

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        longform: bool = False,
        hf_token: str = "",
    ) -> dict:
        """Транскрибирует аудиомассив; longform/hf_token приняты для
        совместимости сигнатуры и не используются (чанкинг здесь всегда свой,
        веса — публичные)."""
        audio_16k = self._ensure_16k(audio, sample_rate)
        chunks = self._chunker.chunk(
            audio_16k, _REQUIRED_SAMPLE_RATE, max_chunk_sec=_MAX_CHUNK_SEC
        )

        # Загрузка модели — строго ВНЕ mlx_lock (скачивание весов при первом
        # запуске занимает десятки секунд и не трогает Metal).
        model, tokenizer = self._get_model()

        import gigaam_mlx  # lazy: см. докстроку модуля

        texts: list[str] = []
        watchdog = get_watchdog()
        for chunk in chunks:
            tmp_path = self._write_temp_wav(chunk.audio)
            try:
                # Critical section — минимальный: один инференс одного чанка.
                with mlx_inter_process_lock(), mlx_lock():
                    piece = watchdog.run_with_timeout(
                        fn=lambda p=tmp_path: gigaam_mlx.transcribe(
                            model, tokenizer, p
                        ),
                        timeout_sec=self._watchdog_timeout_sec,
                        model_name=f"gigaam-mlx-{self._mlx_model_type}",
                    )
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            piece = (piece or "").strip()
            if piece:
                texts.append(piece)

        text = " ".join(texts)
        logger.debug(
            "GigaAMMLXAdapter: %d чанков → %d символов (engine=gigaam-mlx-%s)",
            len(chunks), len(text), self._mlx_model_type,
        )
        return {
            "text": text,
            "language": "ru",
            # gigaam-mlx не отдаёт покадровые вероятности; константа как у
            # PyTorch-ветки (типичное качество модели на RU речи).
            "confidence": 0.9,
            "engine": f"gigaam-mlx-{self._mlx_model_type}",
            "native_punctuation": True,
        }

    def is_loaded(self) -> bool:
        return self._model is not None

    def close(self) -> None:
        """Выгружает модель (память вернёт GC/Metal при потере ссылок)."""
        with self._model_lock:
            self._model = None
            self._tokenizer = None
        logger.debug("GigaAMMLXAdapter: модель выгружена")

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    def _get_model(self):
        if self._model is None:
            with self._model_lock:
                if self._model is None:
                    import gigaam_mlx  # lazy
                    logger.info(
                        "GigaAMMLXAdapter: загрузка модели (type=%s)",
                        self._mlx_model_type,
                    )
                    self._model, self._tokenizer = gigaam_mlx.load_model(
                        self._mlx_model_type
                    )
        return self._model, self._tokenizer

    @staticmethod
    def _ensure_16k(audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Resample до 16 кГц (линейная интерполяция — как fallback-ветка
        GigaAMAdapter; scipy не обязателен)."""
        if sample_rate == _REQUIRED_SAMPLE_RATE:
            return audio.astype(np.float32)
        old_indices = np.linspace(0, len(audio) - 1, len(audio))
        new_length = int(len(audio) * _REQUIRED_SAMPLE_RATE / sample_rate)
        new_indices = np.linspace(0, len(audio) - 1, new_length)
        return np.interp(new_indices, old_indices, audio).astype(np.float32)

    @staticmethod
    def _write_temp_wav(audio: np.ndarray) -> str:
        """float32 → 16-bit mono WAV во временном файле; путь возвращается."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        audio_clipped = np.clip(audio, -1.0, 1.0)
        audio_int16 = (audio_clipped * 32767).astype(np.int16)
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(_REQUIRED_SAMPLE_RATE)
            wf.writeframes(audio_int16.tobytes())
        return tmp_path
