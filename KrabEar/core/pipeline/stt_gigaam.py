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
from collections import deque
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
        # Optional OOM callback: callable(name: str, rc: int, stderr: str)
        # Set by engine.py after adapter creation to forward OOM events to ErrorBus.
        self._oom_callback: Optional[object] = None

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
            _prev_hf: dict[str, str | None] = {}
            if hf_token:
                # SEC MED-1: set token only for the duration of the call, then restore.
                # Avoids the token persisting in the main-process env and leaking into
                # subsequently-spawned child processes (visible via /proc/<pid>/environ).
                for _k in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
                    _prev_hf[_k] = os.environ.get(_k)
                    os.environ[_k] = hf_token
            try:
                segments = model.transcribe_longform(audio_path)
            finally:
                # Restore original values (None → remove entirely).
                for _k, _v in _prev_hf.items():
                    if _v is None:
                        os.environ.pop(_k, None)
                    else:
                        os.environ[_k] = _v
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
        # Forward OOM callback to session so crash events reach the ErrorBus.
        if self._oom_callback is not None:
            session.oom_callback = self._oom_callback
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


def detect_subprocess_oom(returncode: int, stderr: str) -> tuple[bool, str | None]:
    """Return (is_oom, signal_name) for subprocess exit.

    is_oom: True if returncode or stderr indicates OOM/fatal MLX crash.
    signal_name: Human-readable signal name (e.g. 'SIGABRT', 'SIGKILL',
                 'SIGSEGV', 'SIGBUS') OR 'stderr_oom_pattern' OR None.

    Heuristics:
    - returncode == -6 (SIGABRT) — MLX often abort()'s on Metal OOM
    - returncode == -9 (SIGKILL) — kernel OOM killer
    - returncode == -10 (SIGBUS) — memory alignment fault
    - returncode == -11 (SIGSEGV) — MLX/Metal corruption segfault
    - stderr contains "out of memory" / "MallocStackLogging" / "OutOfMemoryError"

    Public so it can be imported by tests without instantiating any class.
    """
    _SIGNAL_NAMES: dict[int, str] = {
        -6: "SIGABRT",
        -9: "SIGKILL",
        -10: "SIGBUS",
        -11: "SIGSEGV",
    }
    if returncode in _SIGNAL_NAMES:
        return (True, _SIGNAL_NAMES[returncode])
    if stderr:
        low = stderr.lower()
        if any(s in low for s in (
            "out of memory",
            "outofmemoryerror",
            "metal out of memory",
            "mallocstacklogging",
        )):
            return (True, "stderr_oom_pattern")
    return (False, None)


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

    OOM detection: set `oom_callback` to a callable(name: str, rc: int, stderr: str)
    to receive notification when the worker crashes with an OOM signal/pattern.
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
        # Optional callback for OOM detection: callable(name, returncode, stderr)
        self.oom_callback: Optional[object] = None
        # Late-injected error bus (Phase B Wave 60). Set by GigaAMAdapter after
        # session construction. If None, no error_bus push for worker timeout.
        self._error_bus: Optional[object] = None
        # H3: ring buffer for stderr drain thread (capped at 200 lines).
        # Prevents 64 KB OS pipe-full backpressure when gigaam_worker writes
        # HuggingFace Hub progress / PyTorch warnings to stderr.
        # _check_proc_oom_on_exit reads from this ring instead of proc.stderr.read().
        self._stderr_ring: deque = deque(maxlen=200)
        self._stderr_drain_thread: Optional[threading.Thread] = None

    def is_loaded(self) -> bool:
        """True если worker запущен и модель загружена."""
        return self._loaded and self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        """Spawn subprocess + отправить load-команду. Idempotent."""
        if self._proc is not None:
            return
        # Wave 64: system.malloc_env_leak — strip MALLOC_STACK_LOGGING from the
        # subprocess environment to prevent macOS from logging the warning
        # "can't turn off malloc stack logging because it was not enabled".
        _env = os.environ.copy()
        if "MALLOC_STACK_LOGGING" in _env:
            _env.pop("MALLOC_STACK_LOGGING")
            _error_bus = getattr(self, "_error_bus", None)
            if _error_bus is not None:
                try:
                    from backend.error_bus import KrabError
                    from backend.error_codes import ERROR_REGISTRY
                    from datetime import datetime, timezone
                    _entry = ERROR_REGISTRY.get("system.malloc_env_leak", {})
                    _err = KrabError(
                        severity=_entry.get("severity", "info"),
                        component="system",
                        code="system.malloc_env_leak",
                        message_user=_entry.get("user_msg_ru", ""),
                        message_debug=(
                            "MALLOC_STACK_LOGGING found in subprocess env; "
                            "stripped before Popen to prevent macOS warning"
                        ),
                        timestamp=datetime.now(timezone.utc),
                        context={},
                        actionable=False,
                        action_id=None,
                    )
                    _error_bus.push(_err)
                except Exception:
                    pass
        try:
            self._proc = subprocess.Popen(
                [self._venv_python, "-u", self._worker_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # line-buffered для синхронной работы с readline()
                env=_env,
            )
        except (OSError, FileNotFoundError) as exc:
            raise RuntimeError(
                f"_GigaAMSubprocessSession: не удалось запустить worker: {exc}"
            ) from exc

        # H3: start stderr drain thread immediately after spawn so pipe never fills up.
        # Must be called before _send(load) because model load emits HF Hub progress
        # to stderr — enough to block the worker on a 64 KB pipe on long downloads.
        self._start_stderr_drain()

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
            # Wave 64: mlx.semaphore_leak — multiprocessing resource_tracker emits
            # a "leaked semaphore objects" warning after subprocess shutdown.
            # Push to error_bus so it appears once per session (dedupe 1800s).
            _error_bus = getattr(self, "_error_bus", None)
            if _error_bus is not None:
                try:
                    from backend.error_bus import KrabError
                    from backend.error_codes import ERROR_REGISTRY
                    from datetime import datetime, timezone
                    _entry = ERROR_REGISTRY.get("mlx.semaphore_leak", {})
                    _err = KrabError(
                        severity=_entry.get("severity", "warn"),
                        component="mlx",
                        code="mlx.semaphore_leak",
                        message_user=_entry.get("user_msg_ru", ""),
                        message_debug="GigaAM worker subprocess shutdown: potential semaphore leak",
                        timestamp=datetime.now(timezone.utc),
                        context={},
                        actionable=False,
                        action_id=None,
                    )
                    _error_bus.push(_err)
                except Exception:
                    pass

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
            # Worker exited without responding — check for OOM before raising.
            self._check_proc_oom_on_exit()
            raise RuntimeError(
                "_GigaAMSubprocessSession: empty response (worker exited or timed out)"
            )
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"_GigaAMSubprocessSession: invalid JSON from worker: {exc}; line={line!r}"
            ) from exc

    def _start_stderr_drain(self) -> None:
        """Start background thread that continuously reads stderr to prevent pipe-full backpressure.

        H3 hypothesis fix (docs/audit/gigaam-worker-memory-2026-05-05.md):
        gigaam_worker writes HuggingFace Hub progress bars and PyTorch warnings to stderr.
        On a long model load these can exceed the 64 KB OS pipe buffer, causing the worker
        to block on the next stderr write and appear hung (no stdout response → timeout kill).

        Mitigation: dedicated daemon thread reads stderr lines into _stderr_ring (capped at
        200 lines) — the pipe stays empty, the worker never blocks, and _check_proc_oom_on_exit
        reads from the ring instead of the now-drained pipe.

        Idempotent: does nothing if _proc is None or stderr is unavailable.
        Never raises.
        """
        if self._proc is None or self._proc.stderr is None:
            return

        proc = self._proc  # capture ref for closure

        def _drain_loop() -> None:
            try:
                while proc.poll() is None:
                    line = proc.stderr.readline()
                    if not line:
                        break
                    # Store in ring buffer for OOM detection; deque.append is thread-safe.
                    self._stderr_ring.append(line if isinstance(line, str) else line.decode(errors="replace"))
                # Drain any remaining lines after process exits.
                try:
                    for line in proc.stderr:
                        if line:
                            self._stderr_ring.append(line if isinstance(line, str) else line.decode(errors="replace"))
                except Exception:
                    pass
            except Exception:
                pass  # must never raise — daemon thread

        thread = threading.Thread(
            target=_drain_loop,
            daemon=True,
            name=f"gigaam-stderr-drain-{proc.pid}",
        )
        thread.start()
        self._stderr_drain_thread = thread

    def _check_proc_oom_on_exit(self) -> None:
        """Check if the worker process exited with OOM and fire oom_callback if so.

        Called whenever the worker produces no response (crash / silent exit).
        Reads stderr from _stderr_ring (populated by drain thread, H3) instead of
        proc.stderr.read() — because the drain thread already consumed the pipe.
        Never raises.
        """
        try:
            if self._proc is None:
                return
            rc = self._proc.poll()
            if rc is None:
                # Process still running — not a crash, skip.
                return
            # Read from ring buffer (H3): drain thread has been accumulating lines.
            # Fall back to direct stderr.read() if ring is empty (e.g. no drain thread).
            if self._stderr_ring:
                stderr_text = "".join(self._stderr_ring)
            else:
                stderr_text = ""
                try:
                    if self._proc.stderr is not None:
                        stderr_text = self._proc.stderr.read() or ""
                except Exception:
                    pass
            is_oom, _signal_name = detect_subprocess_oom(rc, stderr_text)
            if is_oom:
                cb = self.oom_callback
                if callable(cb):
                    try:
                        cb("gigaam_worker", rc, stderr_text)
                    except Exception:
                        pass
        except Exception:
            pass  # must never raise

    def _timeout_kill(self) -> None:
        """Вызывается Timer'ом если worker не ответил вовремя."""
        if self._proc is None or self._proc.poll() is not None:
            return
        logger.warning("_GigaAMSubprocessSession: worker timeout → terminating")
        try:
            self._proc.terminate()
        except Exception:
            pass
        # Wave 60: push stt.gigaam_worker_timeout to error bus (never raises)
        self._push_worker_timeout_error()

    def _push_worker_timeout_error(self) -> None:
        """Push stt.gigaam_worker_timeout to error bus. Never raises."""
        error_bus = self._error_bus
        if error_bus is None:
            return
        try:
            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY
            from datetime import datetime, timezone
            entry = ERROR_REGISTRY.get("stt.gigaam_worker_timeout", {})
            err = KrabError(
                severity=entry.get("severity", "warn"),
                component="stt",
                code="stt.gigaam_worker_timeout",
                message_user=entry.get("user_msg_ru", "GigaAM воркер не ответил вовремя"),
                message_debug="_timeout_kill fired: worker subprocess terminated",
                timestamp=datetime.now(timezone.utc),
                context={},
                actionable=entry.get("actionable", False),
                action_id=entry.get("action_id"),
            )
            error_bus.push(err)
        except Exception:
            logger.exception("_GigaAMSubprocessSession: error_bus.push failed")


def is_subprocess_venv_available(venv_python_path: Optional[str] = None) -> bool:
    """Проверяет существует ли venv с gigaam (worker сможет запуститься).

    Полезно для проактивной проверки перед STT_GIGAAM_ENABLED=True.
    Не запускает реальный subprocess — только проверка filesystem.
    """
    path = venv_python_path or _DEFAULT_VENV_PYTHON
    return os.path.exists(path) and shutil.which(path) is not None or os.path.isfile(path)
