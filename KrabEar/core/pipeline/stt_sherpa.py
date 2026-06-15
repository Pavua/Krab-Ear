"""Sherpa-ONNX STT adapter (Paraformer) для Krab Ear (ultra-low-latency).

Реализует адаптер для sherpa-onnx (offline recognizer).
Обеспечивает double-checked locking при ленивой загрузке модели, чтобы
гарантировать отсутствие дублирования загрузки и OOM при concurrent вызовах.
Пакет sherpa-onnx опционален и не ломает систему при отсутствии (fallback-ready).
"""

import logging
import threading
from typing import Any, Optional

from core.pipeline.stt_adapter import STTAdapterBase, STTResult

logger = logging.getLogger("KrabEar.STT.Sherpa")

_warned_unavailable: bool = False


def _try_import_sherpa() -> Optional[Any]:
    """Пытается импортировать sherpa_onnx."""
    try:
        import sherpa_onnx  # noqa: PLC0415
        return sherpa_onnx
    except ImportError:
        return None


class SherpaOnnxSTTAdapter(STTAdapterBase):
    """STT адаптер на базе sherpa-onnx (Paraformer) для звонков (ultra-low-latency).

    Поддерживает ленивую инициализацию модели (thread-safe, double-checked locking).
    Если пакет sherpa-onnx отсутствует, адаптер корректно возвращает is_available() == False.
    """

    def __init__(self, model_dir: Optional[str] = None) -> None:
        # Путь к директории с моделью (из настроек или дефолтный)
        self._model_dir = model_dir or "sherpa_onnx_model"
        self._model: Any = None
        self._load_failed: bool = False
        self._load_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "sherpa"

    @property
    def model_id(self) -> str:
        return f"sherpa-onnx/{self._model_dir}"

    @property
    def display_name(self) -> str:
        return f"Sherpa-ONNX ({self._model_dir})"

    @property
    def supported_languages(self) -> set[str]:
        # Возвращаем пустое множество для обозначения поддержки нескольких языков (multilingual fallback)
        return set()

    def supports_language(self, language: str) -> bool:
        return True

    def is_available(self) -> bool:
        return _try_import_sherpa() is not None

    def transcribe(
        self,
        audio: Any,
        *,
        language: Optional[str] = None,
        max_duration_sec: Optional[float] = None,
    ) -> STTResult:
        """Транскрибирует аудио через sherpa-onnx."""
        global _warned_unavailable
        sherpa_onnx = _try_import_sherpa()
        if sherpa_onnx is None:
            if not _warned_unavailable:
                logger.warning(
                    "SherpaOnnxSTTAdapter: пакет sherpa-onnx не установлен. "
                    "Установите его через 'pip install sherpa-onnx'."
                )
                _warned_unavailable = True
            raise ImportError("sherpa-onnx не установлен")

        # Lazy-load с использованием double-checked locking (sibling-asymmetry защита)
        if self._model is None and not self._load_failed:
            with self._load_lock:
                if self._model is None and not self._load_failed:
                    self._load_model(sherpa_onnx)

        if self._load_failed or self._model is None:
            raise RuntimeError("SherpaOnnxSTTAdapter: модель недоступна (предыдущая загрузка завершилась ошибкой)")

        try:
            stream = self._model.create_stream()
            # Ожидаем 16kHz mono (float32 numpy array)
            stream.accept_waveform(16000, audio)
            self._model.decode_stream(stream)
            text = stream.result.text
        except Exception as exc:
            logger.error("SherpaOnnxSTTAdapter: ошибка инференса: %s", exc)
            raise RuntimeError(f"SherpaOnnxSTTAdapter инференс упал: {exc}") from exc

        return STTResult(
            text=text,
            engine=self.model_id,
            language=language or "auto",
            confidence=None,
            duration_sec=None,
            word_count=len(text.split()) if text else 0,
            metadata={"model_dir": self._model_dir},
        )

    def _load_model(self, sherpa_onnx: Any) -> None:
        """Загружает offline recognizer. Изолировано для обработки ошибок."""
        try:
            logger.info("SherpaOnnxSTTAdapter: загрузка модели из %s", self._model_dir)
            # Инициализация Paraformer offline recognizer
            self._model = sherpa_onnx.OfflineRecognizer.from_paraformer(
                paraformer=f"{self._model_dir}/model.int8.onnx",
                tokens=f"{self._model_dir}/tokens.txt",
                num_threads=1,
                sample_rate=16000,
                feature_dim=80,
            )
            logger.info("SherpaOnnxSTTAdapter: модель успешно загружена")
        except Exception as exc:
            self._load_failed = True
            logger.error("SherpaOnnxSTTAdapter: ошибка загрузки модели %s: %s", self._model_dir, exc)
            raise RuntimeError(f"SherpaOnnxSTTAdapter: ошибка инициализации модели: {exc}") from exc
