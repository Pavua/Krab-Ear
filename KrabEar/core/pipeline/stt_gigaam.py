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

Транспорты (PR B-3, 2026-04-26):
- "in_process" — `import gigaam` в текущем процессе. Работает только если
  gigaam установлен в активном Python-окружении. В main Krab Ear venv
  (Python 3.14, torch 2.11) gigaam не ставится — в этом случае выбирай
  "subprocess".
- "subprocess" — запускает gigaam_worker.py из изолированного venv
  (по умолчанию ~/.venv_krab_ear_gigaam с Python 3.12 + torch 2.5.1).
  Worker держит модель в памяти и принимает JSON-команды через stdin/stdout.
  См. KrabEar/core/workers/gigaam_worker.py.
- "auto" (default) — пробует in_process; при ImportError → subprocess.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import wave
from typing import Optional

import numpy as np

logger = logging.getLogger("KrabEar.GigaAM")

# Gigaam требует 16 кГц моно PCM
_REQUIRED_SAMPLE_RATE = 16000

# Модели, поддерживаемые адаптером
_VALID_MODES = frozenset({"rnnt", "ctc", "v2_rnnt", "v2_ctc", "v1_rnnt", "v1_ctc"})

# Допустимые значения transport-параметра.
_VALID_TRANSPORTS = frozenset({"auto", "in_process", "subprocess"})

# Default путь к Python в изолированном venv с GigaAM (см. scripts/install_gigaam_venv.command).
_DEFAULT_VENV_PYTHON = os.path.expanduser("~/.venv_krab_ear_gigaam/bin/python")

# Таймаут (секунды) на одну операцию transcribe в subprocess. Нужен запас
# на длинное аудио + первичную загрузку модели.
_SUBPROCESS_TRANSCRIBE_TIMEOUT_SEC = 120.0
_SUBPROCESS_LOAD_TIMEOUT_SEC = 180.0


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

    def __init__(
        self,
        device: str = "mps",
        mode: str = "rnnt",
        transport: str = "auto",
        venv_python_path: Optional[str] = None,
    ) -> None:
        if mode not in _VALID_MODES:
            raise ValueError(
                f"GigaAMAdapter: неподдерживаемый mode={mode!r}. "
                f"Допустимые значения: {sorted(_VALID_MODES)}"
            )
        if transport not in _VALID_TRANSPORTS:
            raise ValueError(
                f"GigaAMAdapter: неподдерживаемый transport={transport!r}. "
                f"Допустимые значения: {sorted(_VALID_TRANSPORTS)}"
            )
        self._device = device
        self._mode = mode
        self._transport_pref = transport
        self._venv_python_path = venv_python_path or _DEFAULT_VENV_PYTHON
        self._model: Optional[object] = None  # lazy in-process load
        self._subprocess: Optional["_GigaAMSubprocessSession"] = None  # lazy subprocess
        # Активный transport определится на первом вызове transcribe (или _resolve_transport).
        self._active_transport: Optional[str] = None

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        longform: bool = False,
        hf_token: str = "",
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
        # Resolve transport единожды (auto → in_process | subprocess).
        transport = self._resolve_transport()

        # Resample выполняем одинаково для обоих транспортов.
        audio_16k = self._ensure_16k(audio, sample_rate)

        # Пишем temp WAV (gigaam в обоих транспортах принимает путь к файлу).
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            self._write_wav(tmp_path, audio_16k)
            if transport == "in_process":
                text, engine_name = self._transcribe_in_process(
                    tmp_path, longform=longform, hf_token=hf_token,
                )
            else:  # subprocess
                text, engine_name = self._transcribe_subprocess(
                    tmp_path, longform=longform, hf_token=hf_token,
                )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        logger.debug(
            "GigaAMAdapter: транскрибировано %d символов (engine=%s, transport=%s)",
            len(text),
            engine_name,
            transport,
        )

        return {
            "text": text.strip(),
            "language": "ru",
            # GigaAM не возвращает логарифмические вероятности сегментов;
            # используем константу 0.9 — типичное качество модели на RU речи.
            "confidence": 0.9,
            "engine": engine_name,
        }

    def is_loaded(self) -> bool:
        """Возвращает True если модель уже загружена (in-process ИЛИ subprocess)."""
        if self._model is not None:
            return True
        if self._subprocess is not None and self._subprocess.is_loaded():
            return True
        return False

    def close(self) -> None:
        """Освобождает ресурсы: subprocess worker (если запущен)."""
        if self._subprocess is not None:
            try:
                self._subprocess.close()
            except Exception as exc:
                logger.debug("GigaAMAdapter.close: %s", exc)
            self._subprocess = None

    def __del__(self) -> None:
        # Best-effort cleanup; не raise — иначе garbage collector ругается.
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Transport resolution + дispatch
    # ------------------------------------------------------------------

    def _resolve_transport(self) -> str:
        """Определяет активный транспорт (in_process | subprocess) и кэширует решение.

        - "in_process" / "subprocess" → используем напрямую.
        - "auto" → пробуем `import gigaam`. ImportError → subprocess.
        """
        if self._active_transport is not None:
            return self._active_transport

        pref = self._transport_pref
        if pref == "in_process" or pref == "subprocess":
            self._active_transport = pref
            return pref

        # auto: проверяем доступность gigaam в текущем процессе.
        try:
            import gigaam  # noqa: F401
            self._active_transport = "in_process"
            logger.info("GigaAMAdapter: transport=auto resolved to in_process")
        except ImportError:
            self._active_transport = "subprocess"
            logger.info(
                "GigaAMAdapter: transport=auto resolved to subprocess "
                "(gigaam недоступен в main venv → используем %s)",
                self._venv_python_path,
            )
        return self._active_transport

    def _transcribe_in_process(
        self, audio_path: str, longform: bool = False, hf_token: str = "",
    ) -> tuple[str, str]:
        """In-process путь: загружает модель в текущий процесс, вызывает .transcribe."""
        model = self._get_model()
        if longform and hasattr(model, "transcribe_longform"):
            if hf_token:
                # Прямое присваивание (не setdefault) — explicit token имеет приоритет.
                os.environ["HF_TOKEN"] = hf_token
                os.environ["HUGGING_FACE_HUB_TOKEN"] = hf_token
            segments = model.transcribe_longform(audio_path)
            text = "\n\n".join(
                (seg.get("transcription") or "").strip()
                for seg in (segments or [])
                if isinstance(seg, dict) and seg.get("transcription")
            )
            return text, f"{self._engine_name()}-longform"
        transcription = model.transcribe(audio_path)
        if isinstance(transcription, str):
            text = transcription
        elif hasattr(transcription, "text"):
            text = transcription.text
        else:
            text = str(transcription)
        return text, self._engine_name()

    def _transcribe_subprocess(
        self, audio_path: str, longform: bool = False, hf_token: str = "",
    ) -> tuple[str, str]:
        """Subprocess путь: ленивый spawn worker'а, отправка transcribe-команды."""
        session = self._get_subprocess_session()
        result = session.transcribe(audio_path, longform=longform, hf_token=hf_token)
        text = str(result.get("text", ""))
        engine = str(result.get("engine") or self._engine_name())
        return text, engine

    def _get_subprocess_session(self) -> "_GigaAMSubprocessSession":
        """Lazy spawn subprocess worker. Idempotent."""
        if self._subprocess is not None:
            return self._subprocess

        if not os.path.exists(self._venv_python_path):
            raise RuntimeError(
                f"GigaAMAdapter[subprocess]: venv Python не найден: {self._venv_python_path}\n"
                "Запусти scripts/install_gigaam_venv.command чтобы создать venv_gigaam, "
                "либо передай явный venv_python_path в GigaAMAdapter(...)."
            )

        worker_path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "workers", "gigaam_worker.py")
        )
        if not os.path.exists(worker_path):
            raise RuntimeError(
                f"GigaAMAdapter[subprocess]: worker script не найден: {worker_path}"
            )

        session = _GigaAMSubprocessSession(
            venv_python=self._venv_python_path,
            worker_path=worker_path,
            mode=self._mode,
            device=self._device,
        )
        session.start()  # spawn + load model
        self._subprocess = session
        return session

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


# ---------------------------------------------------------------------------
# Subprocess transport (B-3, 2026-04-26)
# ---------------------------------------------------------------------------


class _GigaAMSubprocessSession:
    """Управляет долгоживущим subprocess-воркером (gigaam_worker.py).

    Worker запускается из изолированного venv (~/.venv_krab_ear_gigaam) с
    Python 3.12 + torch 2.5.1 + gigaam, потому что main Krab Ear venv
    несовместим с pin'ами gigaam. Общение через stdin/stdout JSON-строками.

    Lifecycle:
        session = _GigaAMSubprocessSession(venv_python, worker_path, mode, device)
        session.start()           # spawn + load model
        result = session.transcribe(audio_path)  # повторяемо
        session.close()           # graceful shutdown

    Threading: внутренний lock сериализует transcribe-вызовы (worker single-threaded).
    """

    def __init__(
        self,
        venv_python: str,
        worker_path: str,
        mode: str,
        device: str,
    ) -> None:
        self._venv_python = venv_python
        self._worker_path = worker_path
        self._mode = mode
        self._device = device
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._loaded = False

    def is_loaded(self) -> bool:
        """True если worker запущен и модель загружена."""
        return self._loaded and self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        """Spawn subprocess + отправить load-команду. Idempotent."""
        if self._proc is not None:
            return
        try:
            self._proc = subprocess.Popen(
                [self._venv_python, "-u", self._worker_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # line-buffered для синхронной работы с readline()
            )
        except (OSError, FileNotFoundError) as exc:
            raise RuntimeError(
                f"_GigaAMSubprocessSession: не удалось запустить worker: {exc}"
            ) from exc

        # Сразу шлём load-команду — модель в памяти к моменту первого transcribe.
        load_response = self._send(
            {"op": "load", "mode": self._mode, "device": self._device},
            timeout_sec=_SUBPROCESS_LOAD_TIMEOUT_SEC,
        )
        if not load_response.get("ok"):
            err = load_response.get("error", "unknown")
            self.close()
            raise RuntimeError(f"_GigaAMSubprocessSession: load failed: {err}")
        self._loaded = True
        logger.info(
            "_GigaAMSubprocessSession: worker готов (mode=%s, device=%s)",
            self._mode,
            self._device,
        )

    def transcribe(
        self,
        audio_path: str,
        longform: bool = False,
        hf_token: str = "",
    ) -> dict:
        """Отправляет transcribe-команду, возвращает {"text": ..., "engine": ...}.

        Параметры:
            audio_path: путь к WAV/M4A/MP3 файлу.
            longform:   bool — для аудио > 30 сек использовать
                        `model.transcribe_longform()` (требует pyannote.audio + HF token).
            hf_token:   HuggingFace API token для pyannote VAD. Если пустой —
                        worker использует cached token (~/.cache/huggingface/token).
        """
        if not self.is_loaded():
            raise RuntimeError("_GigaAMSubprocessSession: worker not started or crashed")
        # Longform может занять 5-10× больше времени чем обычный transcribe — увеличиваем timeout.
        timeout = _SUBPROCESS_TRANSCRIBE_TIMEOUT_SEC * (8 if longform else 1)
        request = {"op": "transcribe", "audio_path": audio_path}
        if longform:
            request["longform"] = True
        if hf_token:
            request["hf_token"] = hf_token
        response = self._send(request, timeout_sec=timeout)
        if not response.get("ok"):
            err = response.get("error", "unknown")
            raise RuntimeError(f"_GigaAMSubprocessSession: transcribe failed: {err}")
        return response

    def close(self) -> None:
        """Graceful shutdown: shutdown-команда + wait + force kill при таймауте."""
        if self._proc is None:
            return
        try:
            # Shutdown — worker отвечает молча (просто exit).
            try:
                if self._proc.stdin and not self._proc.stdin.closed:
                    self._proc.stdin.write(json.dumps({"op": "shutdown"}) + "\n")
                    self._proc.stdin.flush()
                    self._proc.stdin.close()
            except (OSError, BrokenPipeError):
                pass
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        finally:
            self._proc = None
            self._loaded = False

    # ------------------------------------------------------------------
    # Internal protocol helpers
    # ------------------------------------------------------------------

    def _send(self, request: dict, timeout_sec: float) -> dict:
        """Отправляет JSON-запрос, читает одну JSON-строку ответа.

        Сериализует доступ через `_lock` — несколько потоков не могут одновременно
        писать в stdin одного worker'а.
        """
        if self._proc is None:
            raise RuntimeError("_GigaAMSubprocessSession: process not started")
        if self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("_GigaAMSubprocessSession: process pipes missing")

        with self._lock:
            try:
                self._proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise RuntimeError(f"_GigaAMSubprocessSession: stdin write failed: {exc}") from exc

            # Простой read с timeout: используем threading.Timer для kill при зависании.
            # subprocess.Popen API не поддерживает per-readline timeout на текстовых pipes,
            # поэтому простейшая защита — Timer, который терминирует процесс если не успели.
            timer = threading.Timer(timeout_sec, self._timeout_kill)
            timer.start()
            try:
                line = self._proc.stdout.readline()
            finally:
                timer.cancel()

        if not line:
            raise RuntimeError(
                "_GigaAMSubprocessSession: empty response (worker exited or timed out)"
            )
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"_GigaAMSubprocessSession: invalid JSON from worker: {exc}; line={line!r}"
            ) from exc

    def _timeout_kill(self) -> None:
        """Вызывается Timer'ом если worker не ответил вовремя."""
        if self._proc is None or self._proc.poll() is not None:
            return
        logger.warning("_GigaAMSubprocessSession: worker timeout → terminating")
        try:
            self._proc.terminate()
        except Exception:
            pass


def is_subprocess_venv_available(venv_python_path: Optional[str] = None) -> bool:
    """Проверяет существует ли venv с gigaam (worker сможет запуститься).

    Полезно для проактивной проверки перед STT_GIGAAM_ENABLED=True.
    Не запускает реальный subprocess — только проверка filesystem.
    """
    path = venv_python_path or _DEFAULT_VENV_PYTHON
    return os.path.exists(path) and shutil.which(path) is not None or os.path.isfile(path)
