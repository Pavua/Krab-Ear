"""Распознаватель для STT-споттера слова-пробуждения: отдельная копия GigaAM-MLX, аудио только в памяти.

Решение владельца (25.11.2026): в проде диктовка идёт через whisper-large-v3 (``stt_gigaam_enabled=False``), загруженного
GigaAM в backend нет, поэтому споттеру нужна СВОЯ копия модели (замер: ~+1,1 ГБ RSS после загрузки и первого прогона,
~0,03 с на реплику; режим off — модель не загружается вовсе, ``release()`` её выгружает).

Инварианты (тесты ``test_wake_word_stt_bridge_2026_11_25.py``):
  * АУДИО ТОЛЬКО В ПАМЯТИ: ``gigaam_mlx.transcribe`` принимает путь к файлу (временный WAV + ffmpeg) — здесь он НЕ
    вызывается; те же шаги (``compute_mel`` → ``encode`` → ``decode``) выполняются прямо над массивом
    (``load_audio`` даёт ровно ``int16 / 32768`` — результат идентичен). Ни временных файлов, ни subprocess.
  * ЛЮБОЙ MLX-инференс — под ``mlx_lock`` (инвариант проекта: MLX не потокобезопасен); блокировка берётся КОРОТКО
    (``lock_wait_sec``): занято диктовкой — реплика пропускается (``TranscriberUnavailable``), диктовку споттер
    не задерживает. Загрузка модели — вне ``mlx_lock``.
  * Текст возвращается только вызывающему, не сохраняется и не логируется; исключения наружу — только коды причин.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from backend.stt_keyword_spotter import TranscriberUnavailable

logger = logging.getLogger("KrabEar.Backend.WakeWordSttBridge")

DEFAULT_MODE = "v3_e2e_rnnt"
DEFAULT_LOCK_WAIT_SEC = 0.25
SAMPLE_RATE = 16000


def _default_loader(mode: str) -> tuple[Any, Any]:
    from core.pipeline.stt_gigaam_mlx import _MODE_TO_MLX
    import gigaam_mlx

    return gigaam_mlx.load_model(_MODE_TO_MLX[mode])


def _default_infer(model: Any, tokenizer: Any, audio: Any) -> str:
    """Те же шаги, что ``gigaam_mlx.transcribe``, но над массивом float32 [-1, 1] вместо файла."""
    import gigaam_mlx
    import mlx.core as mx
    import numpy as np

    mel = gigaam_mlx.compute_mel(audio)
    encoded, seq_len = model.encode(mx.array(mel[np.newaxis]))
    mx.eval(encoded)
    return tokenizer.decode(model.decode(encoded, seq_len))


def _clear_mlx_cache() -> None:
    try:
        import mlx.core as mx

        mx.clear_cache()
    except Exception:  # noqa: BLE001 — освобождение кэша best-effort
        pass


class GigaAmMemoryTranscriber:
    """Вызываемый объект ``transcribe(samples_int16) -> str`` для ``SttKeywordModel`` + ``warmup()`` / ``release()``."""

    def __init__(
        self,
        *,
        mode: str = DEFAULT_MODE,
        lock_wait_sec: float = DEFAULT_LOCK_WAIT_SEC,
        loader: Optional[Callable[[str], tuple[Any, Any]]] = None,
        infer: Optional[Callable[[Any, Any, Any], str]] = None,
        mlx_lock_fn: Optional[Callable[[], Any]] = None,
        inter_lock_fn: Optional[Callable[..., Any]] = None,
        release_cache: Optional[Callable[[], None]] = None,
    ) -> None:
        # release_cache вызывается ТОЛЬКО под mlx_lock (инвариант проекта: любое обращение к MLX-кэшу — под локом).
        self._mode = mode
        self._lock_wait = float(lock_wait_sec)
        self._loader = loader or _default_loader
        self._infer = infer or _default_infer
        self._mlx_lock_fn = mlx_lock_fn
        self._inter_lock_fn = inter_lock_fn
        self._release_cache = release_cache or _clear_mlx_cache
        self._load_lock = threading.Lock()
        self._model: Any = None
        self._tokenizer: Any = None
        self._warmed = False
        self._gen = 0  # release() увеличивает: прогрев, начатый до выгрузки, не должен вернуть модель в память
        self._gen_lock = threading.Lock()  # отдельный лёгкий лок: release() не ждёт конца идущей загрузки, чтобы объявить отмену

    # ------------------------------------------------------------------ модель
    def is_loaded(self) -> bool:
        return self._model is not None

    def _ensure_model(self) -> tuple[Any, Any]:
        if self._model is None:
            with self._load_lock:  # загрузка ВНЕ mlx_lock: на первом запуске это секунды и скачивание весов
                if self._model is None:
                    try:
                        self._model, self._tokenizer = self._loader(self._mode)
                    except Exception as exc:  # noqa: BLE001 — нет пакета/весов: споттер глохнет, backend живёт
                        logger.warning("WakeWordSttBridge: модель не загрузилась (%s)", type(exc).__name__)
                        raise TranscriberUnavailable("model_load_failed") from None
        return self._model, self._tokenizer

    def release(self) -> None:
        """Выгружает модель (владелец выключил режим): ссылки сбрасываются сразу, кэш MLX — под mlx_lock с коротким
        ожиданием; занято диктовкой — кэш не трогаем (Metal вернёт память сам), одновременного MLX нет."""
        with self._gen_lock:
            self._gen += 1  # отмена объявлена ДО ожидания _load_lock: идущий прогрев увидит её и не станет гонять инференс
        with self._load_lock:
            self._model = self._tokenizer = None
            self._warmed = False
        try:
            mlx_lock_fn, inter_fn = self._locks()
            with inter_fn(timeout_sec=self._lock_wait):
                lock = mlx_lock_fn()
                if not lock.acquire(timeout=self._lock_wait):
                    return
                try:
                    self._release_cache()
                finally:
                    lock.release()
        except Exception:  # noqa: BLE001 — занято/сбой: память вернётся без явного сброса кэша
            return

    # ------------------------------------------------------------------ инференс
    def _locks(self) -> tuple[Callable[[], Any], Callable[..., Any]]:
        mlx_lock_fn, inter_fn = self._mlx_lock_fn, self._inter_lock_fn
        if mlx_lock_fn is None:
            from core.mlx_lock import mlx_lock as mlx_lock_fn  # type: ignore[no-redef]
        if inter_fn is None:
            from core.mlx_inter_lock import mlx_inter_process_lock as inter_fn  # type: ignore[no-redef]
        return mlx_lock_fn, inter_fn  # type: ignore[return-value]

    def _run_locked(self, model: Any, tokenizer: Any, audio: Any) -> str:
        mlx_lock_fn, inter_fn = self._locks()
        try:
            inter = inter_fn(timeout_sec=self._lock_wait)
            with inter:
                lock = mlx_lock_fn()
                if not lock.acquire(timeout=self._lock_wait):
                    raise TranscriberUnavailable("mlx_busy")
                try:
                    return self._infer(model, tokenizer, audio)
                finally:
                    lock.release()
        except TranscriberUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — исключение может нести фрагмент текста: наружу только код
            if type(exc).__name__ == "MLXInterLockTimeout":
                raise TranscriberUnavailable("mlx_busy") from None
            logger.warning("WakeWordSttBridge: распознавание не удалось (%s)", type(exc).__name__)
            raise TranscriberUnavailable("run_failed") from None

    def warmup(self) -> None:
        """Прогрев (загрузка + первый прогон компилирует ядра, ~0,9 с): вызывается при включении режима, а не на
        первой реплике. Сбой/занятость молча игнорируются — реплики тогда просто пропустятся."""
        try:
            import numpy as np

            if self._model is not None and self._warmed:
                return  # адаптер перезапускается после каждой диктовки: прогретую модель не гоняем зря
            gen = self._gen
            model, tokenizer = self._ensure_model()
            if self._gen != gen:  # release() случился, пока грузились: модель не должна остаться в памяти
                with self._load_lock:
                    if self._gen != gen:
                        self._model = self._tokenizer = None
                return
            self._run_locked(model, tokenizer, np.zeros(SAMPLE_RATE, dtype=np.float32))
            if self._gen == gen:
                self._warmed = True
        except TranscriberUnavailable:
            pass

    def __call__(self, samples: Any) -> str:
        import numpy as np

        gen = self._gen
        model, tokenizer = self._ensure_model()
        if self._gen != gen:  # release() случился, пока грузились: реплика перед выключением не возвращает 1,1 ГБ
            with self._load_lock:
                if self._gen != gen:
                    self._model = self._tokenizer = None
            raise TranscriberUnavailable("released")
        audio = np.asarray(samples, dtype=np.int16).astype(np.float32) / 32768.0
        text = self._run_locked(model, tokenizer, audio)
        if not isinstance(text, str):
            raise TranscriberUnavailable("not_text")
        self._warmed = True
        return text
