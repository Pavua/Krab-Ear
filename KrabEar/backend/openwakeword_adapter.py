"""OpenWakeWordAdapter — адаптер openWakeWord для Krab Ear.

openWakeWord — open-source wake word detection (Apache 2.0, без email/signup).
GitHub: https://github.com/dscripka/openWakeWord

Встроенные модели: "alexa", "hey_mycroft", "hey_jarvis".
Кастомные модели ("Краб") требуют ~15 мин обучения через Jupyter notebook —
в данном PR не включены, только инфраструктура для их загрузки.

Установка (optional):
    pip install openwakeword

Адаптер использует lazy import: если библиотека не установлена,
логирует предупреждение и работает в stub-режиме.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("KrabEar.Backend.OpenWakeWordAdapter")

# Встроенные модели openWakeWord
_BUILTIN_MODELS: list[str] = [
    "alexa",
    "hey_mycroft",
    "hey_jarvis",
]

# Имя директории для пользовательских .onnx / .tflite моделей
_CUSTOM_MODELS_DIR = "wake_word_models"


class OpenWakeWordAdapter:
    """Адаптер openWakeWord для Krab Ear.

    Поддерживает:
    - Встроенные модели openWakeWord: alexa, hey_mycroft, hey_jarvis.
    - Пользовательские .onnx/.tflite модели из {data_dir}/wake_word_models/.
    - Graceful stub-режим если openwakeword не установлен.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        self._custom_dir = self._data_dir / _CUSTOM_MODELS_DIR
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._oww: Any = None  # openwakeword.Model instance
        self._on_detected: Callable[[str, float], None] | None = None
        self._active_model: str | None = None
        self._oww_available = self._check_lib_available()

    # ------------------------------------------------------------------
    # Проверка наличия библиотеки
    # ------------------------------------------------------------------

    def _check_lib_available(self) -> bool:
        try:
            import importlib
            spec = importlib.util.find_spec("openwakeword")
            return spec is not None
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def list_models(self) -> list[dict[str, Any]]:
        """Возвращает список доступных моделей: built-in + пользовательские.

        Returns:
            Список dict с полями: name (str), source ("builtin" | "custom"),
            path (str | None).
        """
        models: list[dict[str, Any]] = [
            {"name": m, "source": "builtin", "path": None}
            for m in _BUILTIN_MODELS
        ]

        # Сканируем директорию пользовательских моделей
        if self._custom_dir.exists():
            for f in sorted(self._custom_dir.iterdir()):
                if f.suffix.lower() in (".onnx", ".tflite") and f.is_file():
                    models.append({
                        "name": f.stem,
                        "source": "custom",
                        "path": str(f),
                    })

        return models

    def is_available(self) -> bool:
        """True если openwakeword установлен и может быть использован."""
        return self._oww_available

    def start(
        self,
        model_name: str,
        on_detected: Callable[[str, float], None],
        threshold: float = 0.5,
        chunk_size: int = 1280,
        sample_rate: int = 16000,
    ) -> None:
        """Запускает фоновый поток прослушивания.

        Args:
            model_name: Имя встроенной модели или stem пользовательского .onnx.
            on_detected: Callback (model_name, score) при обнаружении wake word.
            threshold: Порог уверенности [0.0, 1.0], по умолчанию 0.5.
            chunk_size: Размер аудио-чанка в сэмплах (int16).
            sample_rate: Частота дискретизации в Гц.

        Raises:
            RuntimeError: Если openwakeword не установлен.
            ValueError: Если модель не найдена.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                logger.warning(
                    "OpenWakeWordAdapter: уже запущен (модель %r), сначала stop()",
                    self._active_model,
                )
                return

            if not self._oww_available:
                logger.warning(
                    "OpenWakeWordAdapter: openwakeword не установлен. "
                    "Установите: pip install openwakeword. "
                    "Работаем в stub-режиме."
                )
                raise RuntimeError(
                    "openwakeword не установлен. "
                    "Выполните: pip install openwakeword"
                )

            model_path = self._resolve_model_path(model_name)
            self._on_detected = on_detected
            self._active_model = model_name
            self._stop_event.clear()

            self._oww = self._load_model(model_name, model_path)
            self._thread = threading.Thread(
                target=self._listen_loop,
                kwargs={
                    "threshold": threshold,
                    "chunk_size": chunk_size,
                    "sample_rate": sample_rate,
                },
                daemon=True,
                name="OpenWakeWordListener",
            )
            self._thread.start()
            logger.info(
                "OpenWakeWordAdapter: запущен (model=%r, threshold=%.2f)",
                model_name,
                threshold,
            )

    def stop(self) -> None:
        """Останавливает фоновый поток прослушивания."""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return
            self._stop_event.set()
            thread = self._thread
            self._thread = None
            self._oww = None
            self._active_model = None

        thread.join(timeout=3.0)
        logger.info("OpenWakeWordAdapter: остановлен")

    def is_running(self) -> bool:
        """True если поток прослушивания активен."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def active_model(self) -> str | None:
        """Имя активной модели или None."""
        with self._lock:
            return self._active_model

    # ------------------------------------------------------------------
    # IPC-обработчики
    # ------------------------------------------------------------------

    def handle_wake_word_list_models(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """IPC: список доступных wake word моделей."""
        models = self.list_models()
        return {
            "ok": True,
            "models": models,
            "engine_available": self._oww_available,
            "custom_models_dir": str(self._custom_dir),
        }

    def handle_wake_word_start(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: запустить wake word detection.

        Параметры: model (str), threshold (float, optional).
        """
        model_name = str(params.get("model", "hey_jarvis"))
        threshold = float(params.get("threshold", 0.5))

        def _on_detected(name: str, score: float) -> None:
            logger.info(
                "Wake word обнаружен: model=%r score=%.3f", name, score
            )

        try:
            self.start(model_name, _on_detected, threshold=threshold)
            return {"ok": True, "model": model_name, "threshold": threshold}
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def handle_wake_word_stop(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: остановить wake word detection."""
        self.stop()
        return {"ok": True}

    def handle_wake_word_status(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """IPC: статус адаптера."""
        return {
            "ok": True,
            "running": self.is_running(),
            "active_model": self.active_model(),
            "engine_available": self._oww_available,
        }

    # ------------------------------------------------------------------
    # Внутренние
    # ------------------------------------------------------------------

    def _resolve_model_path(self, model_name: str) -> str | None:
        """Возвращает путь к файлу модели или None для встроенных."""
        if model_name in _BUILTIN_MODELS:
            return None  # openWakeWord загрузит по имени автоматически

        # Поиск в директории пользовательских моделей
        if self._custom_dir.exists():
            for ext in (".onnx", ".tflite"):
                candidate = self._custom_dir / (model_name + ext)
                if candidate.exists():
                    return str(candidate)

        raise ValueError(
            f"Модель {model_name!r} не найдена. "
            f"Встроенные: {_BUILTIN_MODELS}. "
            f"Пользовательские: {self._custom_dir}"
        )

    def _load_model(self, model_name: str, model_path: str | None) -> Any:
        """Загружает openwakeword.Model."""
        try:
            from openwakeword.model import Model as OWWModel  # type: ignore[import]
        except ImportError:
            raise RuntimeError("openwakeword не установлен")

        if model_path is not None:
            return OWWModel(wakeword_models=[model_path])
        # Встроенная модель — openWakeWord скачает при первом запуске
        return OWWModel(wakeword_models=[model_name])

    def _listen_loop(
        self,
        threshold: float,
        chunk_size: int,
        sample_rate: int,
    ) -> None:
        """Фоновый поток: читает аудио с микрофона и передаёт в openWakeWord."""
        try:
            import sounddevice as sd  # type: ignore[import]
        except ImportError:
            logger.error(
                "OpenWakeWordAdapter: sounddevice не установлен"
            )
            return

        logger.debug(
            "OpenWakeWordAdapter._listen_loop: старт "
            "(chunk=%d, rate=%d, threshold=%.2f)",
            chunk_size,
            sample_rate,
            threshold,
        )

        try:
            with sd.InputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="int16",
                blocksize=chunk_size,
            ) as stream:
                while not self._stop_event.is_set():
                    audio_chunk, _ = stream.read(chunk_size)
                    flat = audio_chunk.flatten().tolist()

                    with self._lock:
                        oww = self._oww

                    if oww is None:
                        break

                    prediction = oww.predict(flat)
                    for mdl_name, score in prediction.items():
                        if score >= threshold and self._on_detected is not None:
                            self._on_detected(mdl_name, float(score))

        except Exception:
            logger.exception("OpenWakeWordAdapter._listen_loop: ошибка")
