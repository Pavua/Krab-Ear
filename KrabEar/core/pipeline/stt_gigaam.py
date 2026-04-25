"""GigaAM-RNNT v2 адаптер для Krab Ear — специализированная RU-модель от Sber.

GigaAM (Giga Audio Model) — Conformer-based SSL модель, дообученная на 50 000 часах
русскоязычной речи. Версия v2-RNNT достигает WER ~3.8% на Common Voice RU
против ~9.8% у whisper-large-v3 (≈2.5× улучшение).

Лицензия: MIT — коммерческое использование разрешено.
Источник: https://github.com/salute-developers/GigaAM
PyPI: pip install gigaam

Паттерн адаптера следует тому же интерфейсу, что и другие STT-адаптеры проекта:
- Lazy-load модели (не в __init__)
- PyTorch + MPS, не MLX → mlx_lock НЕ нужен
- Возврат стандартного dict {"text", "language", "confidence", "engine"}
"""

from __future__ import annotations

import logging
import os
import tempfile
import wave
from typing import Optional

import numpy as np

logger = logging.getLogger("KrabEar.GigaAM")

# Gigaam требует 16 кГц моно PCM
_REQUIRED_SAMPLE_RATE = 16000

# Модели, поддерживаемые адаптером
_VALID_MODES = frozenset({"rnnt", "ctc", "v2_rnnt", "v2_ctc", "v1_rnnt", "v1_ctc"})


class GigaAMAdapter:
    """Адаптер GigaAM-RNNT v2 для распознавания русскоязычной речи.

    Аргументы:
        device: устройство для инференса — "mps" (Apple Silicon MPS) или "cpu".
                GigaAM использует PyTorch, поэтому MPS работает через torch.mps,
                а не через MLX. mlx_lock НЕ требуется.
        mode:   вариант модели — "rnnt" (по умолчанию, выше качество),
                "ctc" (быстрее, чуть ниже WER).
                Также поддерживаются полные имена: "v2_rnnt", "v2_ctc", "v1_rnnt", "v1_ctc".

    Паттерн использования::

        adapter = GigaAMAdapter(device="mps", mode="rnnt")
        result = adapter.transcribe(audio_array, sample_rate=16000)
        # result == {"text": "...", "language": "ru", "confidence": 0.9, "engine": "gigaam-rnnt"}

    Модель загружается лениво при первом вызове transcribe() — не в __init__.
    """

    def __init__(self, device: str = "mps", mode: str = "rnnt") -> None:
        if mode not in _VALID_MODES:
            raise ValueError(
                f"GigaAMAdapter: неподдерживаемый mode={mode!r}. "
                f"Допустимые значения: {sorted(_VALID_MODES)}"
            )
        self._device = device
        self._mode = mode
        self._model: Optional[object] = None  # lazy load

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
    ) -> dict:
        """Транскрибирует аудиомассив в текст.

        Параметры:
            audio:       numpy float32/int16 массив (моно, sample_rate Hz).
            sample_rate: частота дискретизации audio. Если != 16000 — будет
                         автоматически resample до 16 кГц.

        Возвращает dict::

            {
                "text":       str,    # транскрибированный текст
                "language":   "ru",   # всегда "ru" для GigaAM
                "confidence": float,  # 0.9 (GigaAM не возвращает logprob → const)
                "engine":     str,    # "gigaam-rnnt" или "gigaam-ctc"
            }

        При ошибке кидает исключение (ImportError / RuntimeError).
        """
        model = self._get_model()

        # --- Resample если нужно ---
        audio_16k = self._ensure_16k(audio, sample_rate)

        # --- GigaAM принимает путь к файлу → пишем во временный WAV ---
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            self._write_wav(tmp_path, audio_16k)
            transcription = model.transcribe(tmp_path)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        # gigaam.transcribe() возвращает объект с .text или строку в зависимости от версии
        if isinstance(transcription, str):
            text = transcription
        elif hasattr(transcription, "text"):
            text = transcription.text
        else:
            text = str(transcription)

        engine_name = self._engine_name()
        logger.debug("GigaAMAdapter: транскрибировано %d символов (engine=%s)", len(text), engine_name)

        return {
            "text": text.strip(),
            "language": "ru",
            # GigaAM не возвращает логарифмические вероятности сегментов;
            # используем константу 0.9 — типичное качество модели на RU речи.
            "confidence": 0.9,
            "engine": engine_name,
        }

    def is_loaded(self) -> bool:
        """Возвращает True если модель уже загружена в память."""
        return self._model is not None

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _get_model(self) -> object:
        """Lazy-load модели GigaAM при первом вызове."""
        if self._model is not None:
            return self._model

        try:
            import gigaam  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "GigaAM не установлен. Установите: pip install gigaam\n"
                "Подробнее: https://github.com/salute-developers/GigaAM"
            ) from exc

        logger.info(
            "GigaAMAdapter: загрузка модели mode=%s device=%s ...",
            self._mode,
            self._device,
        )
        try:
            model = gigaam.load_model(self._mode)
            # PyTorch MPS: если модель поддерживает .to(device), перемещаем
            if self._device != "cpu" and hasattr(model, "to"):
                try:
                    import torch  # type: ignore[import]
                    if self._device == "mps" and torch.backends.mps.is_available():
                        model = model.to(torch.device("mps"))
                    elif self._device == "cuda" and torch.cuda.is_available():
                        model = model.to(torch.device("cuda"))
                    else:
                        # Запрошенный device недоступен → CPU fallback
                        logger.warning(
                            "GigaAMAdapter: device=%s недоступен → CPU fallback",
                            self._device,
                        )
                except Exception as move_exc:
                    logger.warning(
                        "GigaAMAdapter: не удалось переместить модель на %s: %s → CPU",
                        self._device,
                        move_exc,
                    )
        except Exception as exc:
            raise RuntimeError(
                f"GigaAMAdapter: ошибка загрузки модели mode={self._mode!r}: {exc}"
            ) from exc

        self._model = model
        logger.info("GigaAMAdapter: модель загружена успешно (mode=%s)", self._mode)
        return self._model

    def _ensure_16k(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Resample аудио до 16 кГц если sample_rate != 16000."""
        if sample_rate == _REQUIRED_SAMPLE_RATE:
            return audio.astype(np.float32)

        logger.debug(
            "GigaAMAdapter: resample %d → %d Гц",
            sample_rate,
            _REQUIRED_SAMPLE_RATE,
        )
        try:
            import scipy.signal as ss  # type: ignore[import]
            target_length = int(len(audio) * _REQUIRED_SAMPLE_RATE / sample_rate)
            resampled = ss.resample(audio, target_length)
            return resampled.astype(np.float32)
        except ImportError:
            # Fallback: простая линейная интерполяция без scipy
            old_indices = np.linspace(0, len(audio) - 1, len(audio))
            new_length = int(len(audio) * _REQUIRED_SAMPLE_RATE / sample_rate)
            new_indices = np.linspace(0, len(audio) - 1, new_length)
            return np.interp(new_indices, old_indices, audio).astype(np.float32)

    def _write_wav(self, path: str, audio: np.ndarray) -> None:
        """Записывает float32 массив в 16-bit mono WAV файл."""
        # Конвертация float32 → int16
        audio_clipped = np.clip(audio, -1.0, 1.0)
        audio_int16 = (audio_clipped * 32767).astype(np.int16)

        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit = 2 байта
            wf.setframerate(_REQUIRED_SAMPLE_RATE)
            wf.writeframes(audio_int16.tobytes())

    def _engine_name(self) -> str:
        """Возвращает строку-идентификатор движка для result dict."""
        mode_base = self._mode.replace("v2_", "").replace("v1_", "")
        return f"gigaam-{mode_base}"
