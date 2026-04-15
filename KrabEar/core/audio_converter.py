"""Утилита конвертации аудиоформатов для Krab Ear.

AudioConverter конвертирует аудиофайлы в WAV (16kHz mono) через ffmpeg subprocess.
Поддерживает: wav, mp3, ogg, flac, m4a.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# soundfile требует libsndfile system library.
# На Ubuntu CI pip wheel обычно содержит его, но для безопасности обёртываем.
try:
    import soundfile as sf  # type: ignore
except Exception:
    sf = None  # type: ignore[assignment]

SUPPORTED_FORMATS = {".wav", ".mp3", ".ogg", ".flac", ".m4a"}
_FFMPEG_CANDIDATES = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "ffmpeg"]


def _find_ffmpeg() -> Optional[str]:
    """Возвращает путь к ffmpeg или None, если не найден."""
    for candidate in _FFMPEG_CANDIDATES:
        if os.path.isabs(candidate):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


@dataclass
class AudioInfo:
    """Метаданные аудиофайла."""
    duration: float       # секунды
    sample_rate: int      # Гц
    channels: int
    format: str           # расширение без точки, нижний регистр
    size_mb: float        # размер файла в МБ


class AudioConverter:
    """Конвертер аудиоформатов через ffmpeg.

    Все публичные методы thread-safe (каждый вызов создаёт отдельный temp-файл).
    """

    def __init__(self, ffmpeg_path: Optional[str] = None) -> None:
        """
        Args:
            ffmpeg_path: явный путь к бинарнику ffmpeg. Если None — автодетект.
        """
        if ffmpeg_path is not None:
            # Принимаем явный путь только если файл существует и исполняем.
            if os.path.isfile(ffmpeg_path) and os.access(ffmpeg_path, os.X_OK):
                self._ffmpeg: Optional[str] = ffmpeg_path
            else:
                self._ffmpeg = None
        else:
            self._ffmpeg = _find_ffmpeg()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_ffmpeg_available(self) -> bool:
        """Возвращает True, если ffmpeg доступен."""
        return self._ffmpeg is not None

    def is_supported_format(self, path: str) -> bool:
        """Возвращает True, если расширение файла поддерживается конвертером."""
        return Path(path).suffix.lower() in SUPPORTED_FORMATS

    def get_audio_info(self, path: str) -> AudioInfo:
        """Возвращает метаданные аудиофайла через soundfile.

        Args:
            path: путь к аудиофайлу.

        Returns:
            AudioInfo с duration, sample_rate, channels, format, size_mb.

        Raises:
            FileNotFoundError: если файл не существует.
            RuntimeError: если soundfile не может прочитать файл.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Файл не найден: {path}")
        try:
            info = sf.info(str(p))
            size_mb = p.stat().st_size / (1024 * 1024)
            return AudioInfo(
                duration=info.duration,
                sample_rate=info.samplerate,
                channels=info.channels,
                format=p.suffix.lower().lstrip("."),
                size_mb=round(size_mb, 4),
            )
        except Exception as exc:
            raise RuntimeError(f"Не удалось получить информацию о файле {path}: {exc}") from exc

    def convert(
        self,
        input_path: str,
        output_format: str = "wav",
        sample_rate: int = 16000,
        output_path: Optional[str] = None,
    ) -> str:
        """Конвертирует аудиофайл в указанный формат через ffmpeg.

        Args:
            input_path: путь к исходному файлу.
            output_format: целевой формат (по умолчанию "wav").
            sample_rate: частота дискретизации в Hz (по умолчанию 16000).
            output_path: явный путь для выходного файла. Если None — создаётся
                         временный файл (вызывающий код отвечает за удаление).

        Returns:
            Путь к сконвертированному файлу.

        Raises:
            FileNotFoundError: если входной файл не существует.
            RuntimeError: если ffmpeg недоступен или завершился с ошибкой.
            ValueError: если входной формат не поддерживается.
        """
        src = Path(input_path)
        if not src.exists():
            raise FileNotFoundError(f"Файл не найден: {input_path}")
        if not self.is_supported_format(input_path):
            raise ValueError(
                f"Формат {src.suffix!r} не поддерживается. "
                f"Поддерживаемые форматы: {sorted(SUPPORTED_FORMATS)}"
            )
        if not self.is_ffmpeg_available():
            raise RuntimeError(
                "ffmpeg не найден. Установите через Homebrew: brew install ffmpeg"
            )

        fmt = output_format.lower().lstrip(".")
        if output_path is None:
            handle = tempfile.NamedTemporaryFile(
                prefix="krab_ear_conv_", suffix=f".{fmt}", delete=False
            )
            handle.close()
            dst = handle.name
        else:
            dst = output_path

        cmd = [
            self._ffmpeg,
            "-y",
            "-i", str(src),
            "-ac", "1",
            "-ar", str(sample_rate),
            dst,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except (FileNotFoundError, OSError) as exc:
            Path(dst).unlink(missing_ok=True)
            raise RuntimeError(
                f"ffmpeg не удалось запустить ({self._ffmpeg}): {exc}"
            ) from exc
        if result.returncode != 0:
            Path(dst).unlink(missing_ok=True)
            raise RuntimeError(
                f"ffmpeg завершился с кодом {result.returncode}: {result.stderr.strip()}"
            )
        return dst
