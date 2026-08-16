"""OS-subprocess session for mlx_whisper (P0c, 2026-08-16).

``core.mlx_subprocess`` — in-process thread watchdog; SIGSEGV всё равно
убивает PID родителя. Этот модуль выносит inference в child: SEGV убивает
только worker. Протокол — JSON-строка на stdin/stdout, как GigaAM.

Включён: ``KRAB_EAR_MLX_WHISPER_WORKER=1`` или argv содержит ``rest_server.py``.
Юнит-тесты (pytest argv) остаются in-process, пока env явно не включён.
Child получает ``KRAB_EAR_MLX_WHISPER_WORKER=0``, иначе рекурсия.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import wave
from collections import deque
from pathlib import Path
from typing import Any, Optional

from core.mlx_subprocess import MLXTimeoutError

logger = logging.getLogger("KrabEar.MLXWhisperWorker")

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_WORKER_SCRIPT = Path(__file__).resolve().parent / "workers" / "mlx_whisper_worker.py"
_REQUIRED_SAMPLE_RATE = 16000

_session_lock = threading.Lock()
_session: Optional["MLXWhisperSession"] = None


class MLXWorkerCrashed(RuntimeError):
    """Worker вышел без JSON-ответа (SIGSEGV/SIGBUS/OOM-kill)."""

    def __init__(
        self,
        returncode: int | None,
        model_name: str,
        stderr_tail: str = "",
    ) -> None:
        self.returncode = returncode
        self.model_name = model_name
        self.stderr_tail = stderr_tail
        super().__init__(
            f"mlx_whisper worker crashed rc={returncode} model={model_name}"
        )


def mlx_whisper_worker_enabled(argv: list[str] | None = None) -> bool:
    """True, если inference должен идти в OS-worker, а не в PID родителя."""
    env = os.environ.get("KRAB_EAR_MLX_WHISPER_WORKER", "").strip().lower()
    if env in _FALSE:
        return False
    if env in _TRUE:
        return True
    args = argv if argv is not None else sys.argv
    if any("rest_server.py" in str(a).replace("\\", "/") for a in args):
        return True
    try:
        from core.config import settings

        return bool(getattr(settings, "MLX_WHISPER_WORKER_ENABLED", False))
    except Exception:
        return False


def get_mlx_whisper_session() -> "MLXWhisperSession":
    global _session
    with _session_lock:
        if _session is None:
            _session = MLXWhisperSession()
        return _session


def reset_mlx_whisper_session() -> None:
    """Тесты / shutdown: закрыть singleton, чтобы следующий get() сделал новый."""
    global _session
    with _session_lock:
        if _session is not None:
            _session.close()
            _session = None


def close_mlx_whisper_session() -> None:
    reset_mlx_whisper_session()


def transcribe_via_mlx_worker(
    audio_data: Any,
    mlx_params: dict[str, Any],
    timeout_sec: float,
    model_name: str,
) -> dict[str, Any]:
    """Пишет ndarray в temp WAV и шлёт transcribe в worker."""
    tmp: str | None = None
    if isinstance(audio_data, (str, Path)):
        audio_path = str(audio_data)
    else:
        fd, tmp = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        _write_pcm_wav(tmp, audio_data)
        audio_path = tmp
    try:
        return get_mlx_whisper_session().transcribe(
            audio_path,
            mlx_params,
            timeout_sec=timeout_sec,
            model_name=model_name,
        )
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _write_pcm_wav(path: str, audio: Any) -> None:
    import numpy as np

    arr = np.asarray(audio)
    if arr.ndim > 1:
        arr = arr[:, 0] if arr.shape[-1] < arr.shape[0] else arr[0]
    arr = np.clip(arr.astype(np.float32), -1.0, 1.0)
    pcm = (arr * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_REQUIRED_SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())


class MLXWhisperSession:
    """Долгоживущий mlx_whisper worker: модель остаётся в RSS child."""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen[str]] = None
        self._lock = threading.Lock()
        self._stderr_ring: deque[str] = deque(maxlen=200)
        self._stderr_drain_thread: Optional[threading.Thread] = None
        self._timed_out = False

    def start(self) -> None:
        if self._proc is not None:
            return
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["KRAB_EAR_MLX_WHISPER_WORKER"] = "0"
        env.pop("MALLOC_STACK_LOGGING", None)
        krab_root = str(Path(__file__).resolve().parents[1])
        py_path = env.get("PYTHONPATH", "")
        if krab_root not in py_path.split(os.pathsep):
            env["PYTHONPATH"] = krab_root + (os.pathsep + py_path if py_path else "")
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-u", str(_WORKER_SCRIPT)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        except (OSError, FileNotFoundError) as exc:
            raise RuntimeError(f"mlx_whisper worker spawn failed: {exc}") from exc
        self._start_stderr_drain()

    def transcribe(
        self,
        audio_path: str,
        mlx_params: dict[str, Any],
        timeout_sec: float,
        model_name: str,
    ) -> dict[str, Any]:
        self.start()
        response = self._send(
            {
                "op": "transcribe",
                "audio_path": audio_path,
                "params": mlx_params,
            },
            timeout_sec=timeout_sec,
            model_name=model_name,
        )
        if not response.get("ok"):
            err = str(response.get("error", "unknown"))
            if err.startswith("TypeError"):
                raise TypeError(err)
            raise RuntimeError(f"mlx_whisper worker: {err}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("mlx_whisper worker: missing result dict")
        return result

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                try:
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
            for pipe in (
                getattr(self._proc, "stdin", None),
                getattr(self._proc, "stdout", None),
                getattr(self._proc, "stderr", None),
            ):
                if pipe is not None:
                    try:
                        pipe.close()
                    except (OSError, BrokenPipeError):
                        pass
            self._proc = None

    def _send(
        self,
        request: dict[str, Any],
        timeout_sec: float,
        model_name: str,
    ) -> dict[str, Any]:
        if self._proc is None:
            raise RuntimeError("mlx_whisper worker: process not started")
        if self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("mlx_whisper worker: pipes missing")

        with self._lock:
            self._timed_out = False
            try:
                self._proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                rc = self._proc.poll()
                self.close()
                raise MLXWorkerCrashed(rc, model_name) from exc

            timer = threading.Timer(timeout_sec, self._timeout_kill)
            timer.start()
            try:
                line = self._proc.stdout.readline()
            finally:
                timer.cancel()

        if not line:
            rc = self._proc.poll() if self._proc is not None else None
            stderr_tail = "".join(self._stderr_ring)[-2000:]
            timed_out = self._timed_out
            self.close()
            if timed_out:
                raise MLXTimeoutError(timeout_sec=timeout_sec, model_name=model_name)
            raise MLXWorkerCrashed(rc, model_name, stderr_tail=stderr_tail)
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"mlx_whisper worker: invalid JSON: {exc}; line={line!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("mlx_whisper worker: response is not an object")
        return parsed

    def _timeout_kill(self) -> None:
        self._timed_out = True
        proc = self._proc
        if proc is None:
            return
        try:
            proc.kill()
        except OSError:
            pass

    def _start_stderr_drain(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        proc = self._proc

        def _drain_loop() -> None:
            try:
                while proc.poll() is None:
                    line = proc.stderr.readline()
                    # MagicMock/не-строка → стоп, иначе вечный цикл в юнит-тестах.
                    if not isinstance(line, (str, bytes)) or not line:
                        break
                    if isinstance(line, bytes):
                        line = line.decode(errors="replace")
                    self._stderr_ring.append(line)
                try:
                    leftover = proc.stderr.read()
                    if isinstance(leftover, bytes):
                        leftover = leftover.decode(errors="replace")
                    if isinstance(leftover, str) and leftover:
                        self._stderr_ring.append(leftover)
                except Exception:
                    pass
            except Exception:
                pass

        thread = threading.Thread(
            target=_drain_loop,
            daemon=True,
            name=f"mlx-whisper-stderr-{getattr(proc, 'pid', 0)}",
        )
        thread.start()
        self._stderr_drain_thread = thread
