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
* Каждый инференс-вызов — под таймаут-защитой на ПЕРСИСТЕНТНОМ потоке
  (single-worker executor + future.result(timeout)): зависший Metal-вызов
  без таймаута повесил бы RLock навечно (freeze-class). get_watchdog()
  здесь непригоден: он создаёт НОВЫЙ поток на каждый вызов, а MLX платит
  прогрев графа per-thread — живой бенч показал многократную деградацию
  на по-чанковом пути. При таймауте executor выбрасывается (его поток —
  daemon) и создаётся заново.
* Первый инференс после загрузки модели — прогрев на 0.5 c тишины
  (компиляция MLX-графа ~секунды), чтобы чанки шли с честной скоростью.
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

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError

from core.audio_chunker import AudioChunker
from core.mlx_inter_lock import mlx_inter_process_lock
from core.mlx_lock import mlx_lock
from core.mlx_subprocess import MLX_HANG_HARD_KILL_SEC, MLXTimeoutError

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
        # Персистентный инференс-поток (см. докстроку модуля) + флаг прогрева.
        self._executor: Optional[ThreadPoolExecutor] = None
        self._warmed = False
        # Отравление: рабочий поток пережил hard-kill окно и остался в Metal-вызове.
        # Пока флаг стоит, инференс не запускается — иначе новый вызов пойдёт
        # ПАРАЛЛЕЛЬНО живому зависшему потоку. Снимается успешным прогревом
        # (см. _warmup) — липкое состояние обязано иметь выход.
        self._poison_lock = threading.Lock()
        self._poison_reason: Optional[str] = None
        # Future зависшего инференса. Единственный честный признак того, что
        # Metal освободился: этот future завершился. Ждать его нельзя (он и есть
        # зависший вызов), опрашивать — можно.
        self._stuck_future = None

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

        self._warmup(gigaam_mlx, model, tokenizer)

        texts: list[str] = []
        for chunk in chunks:
            piece = self._infer_chunk(gigaam_mlx, model, tokenizer, chunk.audio)
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
            self._warmed = False
            if self._executor is not None:
                self._executor.shutdown(wait=False, cancel_futures=True)
                self._executor = None
        logger.debug("GigaAMMLXAdapter: модель выгружена")

    # ------------------------------------------------------------------
    # Инференс: персистентный поток + таймаут + локи
    # ------------------------------------------------------------------

    def _infer_chunk(self, gigaam_mlx, model, tokenizer, audio: np.ndarray) -> str:
        tmp_path = self._write_temp_wav(audio)
        try:
            # Critical section — минимальный: один инференс одного чанка.
            with mlx_inter_process_lock(), mlx_lock():
                return self._run_with_timeout(
                    lambda: gigaam_mlx.transcribe(model, tokenizer, tmp_path)
                )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def is_poisoned(self) -> bool:
        """True, если прошлый инференс оставил живой поток внутри Metal-вызова.

        Самовосстанавливается: как только зависший future завершается, Metal
        свободен и отравление снимается. Без этого выхода состояние было бы
        липким до перезапуска процесса — класс, который проект уже ловил на
        state-machine состояниях без timeout-выхода.
        """
        with self._poison_lock:
            if self._poison_reason is None:
                return False
            stuck = self._stuck_future
            if stuck is not None and stuck.done():
                logger.warning(
                    "зависший инференс gigaam-mlx завершился — отравление снято, "
                    "движок снова доступен без перезапуска backend"
                )
                self._poison_reason = None
                self._stuck_future = None
                return False
            return True

    def _mark_poisoned(self, reason: str, stuck_future=None) -> None:
        with self._poison_lock:
            self._poison_reason = reason
            self._stuck_future = stuck_future

    def _run_with_timeout(self, fn):
        """Выполняет один инференс под watchdog, НЕ отпуская mlx_lock под живым потоком.

        🔴 Инвариант критической секции. Вызывающий (`_infer_chunk`) держит
        `mlx_inter_process_lock()` и `mlx_lock()`, а рабочий поток исполняет fn()
        БЕЗ них. Если бросить исключение, пока поток ещё внутри Metal-вызова,
        вызывающий выйдет из `with` и отпустит замок — следующая транскрипция
        запустит ВТОРОЙ параллельный MLX-инференс, ровно тот конкурентный
        доступ, ради запрета которого mlx_lock и существует (SIGSEGV/зависание,
        класс PR #71).

        Живой инцидент 2026-08-27: после первого таймаута КАЖДАЯ следующая
        диктовка вставала намертво (три `handle_request завис дольше 180с`
        подряд), состояние лечил только перезапуск backend. В снимке процесса —
        четыре потока на `rlock_acquire` при полностью спящем libmlx.

        Паттерн взят у сиблинга `mlx_subprocess.MLXWatchdog.run_with_timeout`
        (W1358 F1 MED), который этот же баг уже чинил bounded join'ом.
        """
        if self.is_poisoned():
            poison = self._poison_reason
            # Fail-fast вместо ожидания: каскад STT должен успеть уйти на
            # резервный движок, а не упереться в IPC-backstop (180 с).
            raise MLXTimeoutError(
                self._watchdog_timeout_sec,
                f"gigaam-mlx-{self._mlx_model_type} (отравлен: {poison})",
            )

        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="gigaam-mlx"
            )
        future = self._executor.submit(fn)
        try:
            return future.result(timeout=self._watchdog_timeout_sec)
        except FuturesTimeoutError:
            pass

        # Watchdog сработал. НЕ выходим сразу: даём потоку доработать в
        # ограниченном окне, удерживая замок вызывающего.
        model_name = f"gigaam-mlx-{self._mlx_model_type}"
        logger.error(
            "инференс не уложился в %.1fс (%s) — держим mlx_lock до завершения "
            "потока или %.1fс hard-kill окна",
            self._watchdog_timeout_sec, model_name, MLX_HANG_HARD_KILL_SEC,
        )
        try:
            future.result(timeout=MLX_HANG_HARD_KILL_SEC)
        except FuturesTimeoutError:
            # Поток пережил окно и всё ещё в Metal. Замок отпустить придётся —
            # иначе бесконечный стол backend'а, — но повторять инференс поверх
            # живого потока нельзя: отравляем адаптер до успешного прогрева.
            self._mark_poisoned(
                f"поток не завершился за {MLX_HANG_HARD_KILL_SEC:.1f}с после таймаута",
                stuck_future=future,
            )
            self._warmed = False
            logger.error(
                "поток %s жив после hard-kill окна — адаптер отравлен, "
                "инференс уходит на резервный движок до успешного прогрева",
                model_name,
            )
        except Exception:
            # fn() упала уже после срабатывания watchdog: поток МЁРТВ, замок
            # держался всё это время — состояние чистое, отравлять нечего.
            self._executor.shutdown(wait=False)
            self._executor = None
            self._warmed = False
        else:
            # Поток успел доработать внутри окна: замок ни на миг не отпускался.
            self._executor.shutdown(wait=False)
            self._executor = None
            self._warmed = False

        raise MLXTimeoutError(self._watchdog_timeout_sec, model_name)

    def _warmup(self, gigaam_mlx, model, tokenizer) -> None:
        """Один прогрев на процесс: компиляция MLX-графа ~секунды."""
        if self._warmed:
            return
        silence = np.zeros(int(0.5 * _REQUIRED_SAMPLE_RATE), dtype=np.float32)
        self._infer_chunk(gigaam_mlx, model, tokenizer, silence)
        self._warmed = True
        logger.debug("GigaAMMLXAdapter: прогрев выполнен")

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
