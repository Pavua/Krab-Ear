"""AudioNormalizationStage — нормализация аудиоданных в pipeline."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

import numpy as np

from ..context import PipelineContext

logger = logging.getLogger("KrabEar.Pipeline.AudioNormalization")

# Целевой RMS для нормализации (≈ -20 dBFS при peak 1.0)
_TARGET_RMS = 0.1


class AudioNormalizationStage:
    """Нормализует аудио перед передачей в STT.

    Для np.ndarray (live mic buffer):
    - Преобразует стерео → моно (усреднение по axis=1)
    - Нормализует амплитуду до _TARGET_RMS

    Для file path (str | Path):
    - Читает файл через soundfile
    - Применяет те же преобразования
    - Записывает результат во временный WAV
    - Сохраняет путь в ctx.normalized_audio
    - iCloud copy workaround (errno 11 / EAGAIN)
    """

    @property
    def name(self) -> str:
        return "audio_normalization"

    def should_run(self, ctx: PipelineContext) -> bool:
        """Нормализация всегда нужна — для файлов и для live-буферов."""
        return True

    def process(self, ctx: PipelineContext) -> PipelineContext:
        audio = ctx.audio_input

        if isinstance(audio, np.ndarray):
            ctx.normalized_audio = self._normalize_array(audio)
        elif isinstance(audio, (str, Path)):
            ctx.normalized_audio = self._normalize_file(str(audio), ctx)
        else:
            ctx.errors.append(
                f"audio_normalization: неизвестный тип audio_input: {type(audio)}"
            )
            ctx.normalized_audio = audio  # passthrough

        return ctx

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _normalize_array(self, data: np.ndarray) -> np.ndarray:
        """Нормализует numpy array: стерео→моно + амплитуда.

        Перед нормализацией заменяет NaN/±Inf нулями (защита от некорректных
        буферов, которые иначе отравили бы RMS-вычисление и вывод STT).
        """
        if data.ndim > 1:
            data = data.mean(axis=1)

        data = data.astype(np.float32)
        # Санитизация: inf/nan → 0.0 (некорректные сэмплы не должны уходить в STT)
        data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)

        # Тишина или полностью пустой буфер — нормализация невозможна/не нужна
        if np.max(np.abs(data)) == 0.0:
            return data

        rms = float(np.sqrt(np.mean(data ** 2)))
        if rms < 1e-6:
            # Тишина — возвращаем как есть
            return data

        gain = _TARGET_RMS / rms
        return np.clip(data * gain, -1.0, 1.0).astype(np.float32)

    def _normalize_file(self, audio_path: str, ctx: PipelineContext) -> str:
        """Читает аудиофайл, нормализует, пишет во временный WAV.

        Returns: путь к нормализованному файлу (может совпадать с исходным
                 если запись не удалась).
        """
        import soundfile as sf  # импорт здесь — не тянуть в тесты без sf

        # iCloud workaround: файлы из Mobile Documents могут вернуть EAGAIN
        source_path = Path(audio_path)
        temp_copy: str | None = None
        if "com~apple~CloudDocs" in str(source_path) or "Mobile Documents" in str(source_path):
            try:
                tmp = tempfile.NamedTemporaryFile(
                    suffix=source_path.suffix, delete=False
                )
                tmp.close()
                shutil.copy2(str(source_path), tmp.name)
                temp_copy = tmp.name
                audio_path = tmp.name
                logger.debug("iCloud copy: %s → %s", source_path, tmp.name)
            except Exception as exc:
                logger.warning("Не удалось скопировать iCloud файл: %s", exc)

        try:
            if not os.path.exists(audio_path):
                logger.error("Файл не найден: %s", audio_path)
                return audio_path

            data, samplerate = sf.read(audio_path)
            data = self._normalize_array(data)

            # Пишем нормализованный WAV во temp-файл рядом с данными
            out_tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            out_tmp.close()
            sf.write(out_tmp.name, data, samplerate, subtype="PCM_16")
            logger.debug(
                "Нормализован файл %s → %s (sr=%d)",
                source_path.name, out_tmp.name, samplerate,
            )
            # Регистрируем temp-файл для очистки executor'ом
            if ctx._temp_path is None:
                ctx._temp_path = out_tmp.name
            return out_tmp.name

        except Exception as exc:
            logger.error("Ошибка нормализации файла %s: %s", audio_path, exc)
            return audio_path
        finally:
            if temp_copy and os.path.exists(temp_copy):
                try:
                    os.unlink(temp_copy)
                except OSError:
                    pass
