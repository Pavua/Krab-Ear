"""OS-subprocess session for mlx_whisper (P0c, 2026-08-16).

``core.mlx_subprocess`` — in-process thread watchdog; SIGSEGV всё равно
убивает PID родителя. Этот модуль выносит inference в child: SEGV убивает
только worker. Протокол — JSON-строка на stdin/stdout, как GigaAM.

Включён: ``KRAB_EAR_MLX_WHISPER_WORKER=1`` или argv содержит ``rest_server.py``.
Юнит-тесты (pytest argv) остаются in-process, пока env явно не включён.
Child получает ``KRAB_EAR_MLX_WHISPER_WORKER=0``, иначе рекурсия.
``start()`` респавнит мёртвый child (``poll() is not None``) — сиблинг GigaAM W1216 F1.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from core.mlx_subprocess import MLXTimeoutError
from core.mlx_lock import MLXLockTimeoutError

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


def peek_session() -> Optional["MLXWhisperSession"]:
    """Читает текущий singleton, НИКОГДА его не создаёт (Memory Conductor —
    сканер пробегает резидентов, не порождая новые ради самого прохода)."""
    with _session_lock:
        return _session


def whisper_residency_snapshot() -> dict:
    """Наблюдает singleton без ожидания его создания/закрытия и без spawn."""
    from core.stt_residency import empty_snapshot

    if not _session_lock.acquire(blocking=False):
        return empty_snapshot("busy", "worker")
    try:
        session = _session
    finally:
        _session_lock.release()
    # Снимок observational: owner может измениться после захвата ссылки.
    return session.residency_snapshot() if session is not None else empty_snapshot(transport="worker")


def reset_mlx_whisper_session() -> None:
    """Тесты / shutdown: закрыть singleton, чтобы следующий get() сделал новый."""
    global _session
    with _session_lock:
        if _session is not None:
            _session.close()
            _session = None


def close_mlx_whisper_session() -> None:
    reset_mlx_whisper_session()


def kill_mlx_whisper_session() -> None:
    """Hard-kill child без shutdown JSON. Для ``os._exit`` после REST 504.

    ``os._exit`` не гоняет atexit и не ждёт 2с на graceful close — child
    иначе остаётся на GPU, launchd поднимает новый REST, два mlx → SEGV.
    """
    global _session
    with _session_lock:
        sess = _session
        _session = None
    if sess is None:
        return
    sess.kill()


def _remaining_sec(deadline_monotonic: float) -> float:
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise MLXLockTimeoutError("Срок Whisper worker истёк до отправки запроса")
    return remaining


def transcribe_via_mlx_worker(
    audio_data: Any,
    mlx_params: dict[str, Any],
    timeout_sec: float,
    model_name: str,
) -> dict[str, Any]:
    """Пишет ndarray в temp WAV и шлёт transcribe в worker."""
    deadline = time.monotonic() + timeout_sec
    _remaining_sec(deadline)
    tmp: str | None = None
    try:
        if isinstance(audio_data, (str, Path)):
            audio_path = str(audio_data)
        else:
            fd, tmp = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            _write_pcm_wav(tmp, audio_data)
            audio_path = tmp
        return get_mlx_whisper_session().transcribe(
            audio_path,
            mlx_params,
            timeout_sec=_remaining_sec(deadline),
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


def _child_rss_mb(pid: int) -> Optional[int]:
    """RSS живого child в МБ; любой сбой — None (метрика не должна ронять снимок)."""
    try:
        import psutil

        return int(psutil.Process(pid).memory_info().rss // (1024 * 1024))
    except Exception:
        return None


class MLXWhisperSession:
    """Долгоживущий mlx_whisper worker: модель остаётся в RSS child."""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen[str]] = None
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stderr_ring: deque[str] = deque(maxlen=200)
        self._stderr_drain_thread: Optional[threading.Thread] = None
        self._inflight = 0
        self._last_used_ts = time.monotonic()
        # Метрики цены выгрузки (канарейка enforce_whisper): только счётчики/
        # метки под коротким _state_lock, без I/O.
        self._cold_start_count = 0
        self._spawn_mono: Optional[float] = None
        self._last_load_sec: Optional[float] = None
        self._awaiting_first = False

    @property
    def inflight(self) -> int:
        """Сколько transcribe() сейчас в полёте (C-INFLIGHT gate для Memory Conductor)."""
        return self._inflight

    @property
    def last_used_ts(self) -> float:
        """monotonic-таймстемп последнего входа/выхода transcribe()."""
        return self._last_used_ts

    @contextmanager
    def _request_lock(self, deadline_monotonic: float | None):
        # Только владелец I/O-lock может отправить JSON или убить свой запрос.
        # Истёкший waiter уходит без воздействия на активный child.
        if deadline_monotonic is None:
            acquired = self._lock.acquire()
        else:
            acquired = self._lock.acquire(timeout=_remaining_sec(deadline_monotonic))
        if not acquired:
            raise MLXLockTimeoutError("Whisper worker занят до истечения срока")
        try:
            if deadline_monotonic is not None:
                _remaining_sec(deadline_monotonic)
            yield
        finally:
            self._lock.release()

    def start(self, deadline_monotonic: float | None = None) -> None:
        """Idempotent spawn. Мёртвый child (poll()!=None) — сиблинг GigaAM W1216 F1."""
        with self._request_lock(deadline_monotonic):
            if self._proc is not None and self._proc.poll() is None:
                return
            if self._proc is not None:
                logger.warning(
                    "mlx_whisper worker dead rc=%s — respawn (W1216 F1 sibling)",
                    self._proc.poll(),
                )
                self._reap_dead_unlocked()
            self._spawn_unlocked()

    def _reap_dead_unlocked(self) -> None:
        """Снять мёртвый Popen без shutdown JSON (pipe уже мёртв)."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.kill()
        except OSError:
            pass
        for pipe in (
            getattr(proc, "stdin", None),
            getattr(proc, "stdout", None),
            getattr(proc, "stderr", None),
        ):
            if pipe is not None:
                try:
                    pipe.close()
                except (OSError, BrokenPipeError):
                    pass

    def _spawn_unlocked(self) -> None:
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
        with self._state_lock:
            self._cold_start_count += 1
            self._spawn_mono = time.monotonic()
            self._awaiting_first = True
        self._start_stderr_drain()

    def transcribe(
        self,
        audio_path: str,
        mlx_params: dict[str, Any],
        timeout_sec: float,
        model_name: str,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_sec
        _remaining_sec(deadline)
        # Короткий state-lock не удерживается через spawn или pipe I/O.
        # Вход до start() сохраняет conductor gate в промежутке start→send.
        with self._state_lock:
            self._inflight += 1
            self._last_used_ts = time.monotonic()
        try:
            self.start(deadline_monotonic=deadline)
            response = self._send(
                {
                    "op": "transcribe",
                    "audio_path": audio_path,
                    "params": mlx_params,
                },
                timeout_sec=timeout_sec,
                model_name=model_name,
                deadline_monotonic=deadline,
            )
            if not response.get("ok"):
                err = str(response.get("error", "unknown"))
                if err.startswith("TypeError"):
                    raise TypeError(err)
                raise RuntimeError(f"mlx_whisper worker: {err}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("mlx_whisper worker: missing result dict")
            with self._state_lock:
                if self._awaiting_first and self._spawn_mono is not None:
                    # Первый успешный ответ после spawn = spawn + загрузка модели
                    # + сам запрос: честная цена холодного старта для оператора.
                    self._last_load_sec = time.monotonic() - self._spawn_mono
                    self._awaiting_first = False
            return result
        finally:
            with self._state_lock:
                self._inflight -= 1
                self._last_used_ts = time.monotonic()

    def residency_snapshot(self) -> dict:
        """Живой child не доказывает загруженную модель: её metadata нет."""
        from core.stt_residency import empty_snapshot

        if not self._lock.acquire(blocking=False):
            return empty_snapshot("busy", "worker")
        try:
            if not self._state_lock.acquire(blocking=False):
                return empty_snapshot("busy", "worker")
            try:
                proc = self._proc
                alive = proc is not None and proc.poll() is None
                result = empty_snapshot("busy" if self._inflight else "unknown" if alive else "unloaded", "worker")
                result.update(
                    pid=proc.pid if alive else None,
                    inflight=self._inflight,
                    cold_start_count=self._cold_start_count,
                    last_load_sec=self._last_load_sec,
                    resident_mb=_child_rss_mb(proc.pid) if alive else None,
                )
                return result
            finally:
                self._state_lock.release()
        finally:
            self._lock.release()

    def close(self, timeout: float = 5.0) -> None:
        """Публичный close(): берёт self._lock С ТАЙМАУТОМ, чтобы залипший
        readline() внутри _send() (до timeout_sec, вплоть до 120с) не вешал
        вызывающих — в первую очередь reset_mlx_whisper_session(), который
        сам держит модульный _session_lock. Тело — в _close_unlocked() для
        вызовов ИЗНУТРИ уже удерживаемого self._lock (см. _send())."""
        if self._proc is None:
            return
        acquired = self._lock.acquire(timeout=timeout)
        if not acquired:
            logger.warning(
                "mlx_whisper session close(): лок не получен за %.1fs "
                "(вероятно активный _send держит его) — close пропущен, "
                "процесс не тронут",
                timeout,
            )
            return
        try:
            self._close_unlocked()
        finally:
            self._lock.release()

    def _close_unlocked(self) -> None:
        """Тело close() БЕЗ захвата self._lock — вызывать только когда
        self._lock уже держит вызывающий (см. _send() write-failure path)."""
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

    def close_if_idle(self, idle_sec: float) -> bool:
        """Memory Conductor C-INFLIGHT: атомарная (под self._lock) проверка
        «резидент простаивает» + закрытие. inflight>0 всегда побеждает —
        даже если формально прошло достаточно времени с last_used_ts (тот
        обновляется и на ВХОДЕ в transcribe(), так что во время работы
        разница почти всегда мала, но именно inflight — авторитетный
        источник правды на границе start()→_send())."""
        with self._lock:
            with self._state_lock:
                if self._proc is None or self._inflight > 0:
                    return False
                if (time.monotonic() - self._last_used_ts) < idle_sec:
                    return False
            self._close_unlocked()
            return True

    def kill(self) -> None:
        """SIGKILL без wait. Не блокировать путь к ``os._exit``."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.kill()
        except OSError:
            pass
        for pipe in (
            getattr(proc, "stdin", None),
            getattr(proc, "stdout", None),
            getattr(proc, "stderr", None),
        ):
            if pipe is not None:
                try:
                    pipe.close()
                except (OSError, BrokenPipeError):
                    pass

    def _send(
        self,
        request: dict[str, Any],
        timeout_sec: float,
        model_name: str,
        deadline_monotonic: float | None = None,
    ) -> dict[str, Any]:
        if deadline_monotonic is None:
            deadline_monotonic = time.monotonic() + timeout_sec
        with self._request_lock(deadline_monotonic):
            proc = self._proc
            if proc is None:
                raise RuntimeError("mlx_whisper worker: process not started")
            if proc.stdin is None or proc.stdout is None:
                raise RuntimeError("mlx_whisper worker: pipes missing")

            # Сериализация и ожидание очереди расходуют тот же бюджет.
            payload = json.dumps(request, ensure_ascii=False) + "\n"
            remaining = _remaining_sec(deadline_monotonic)
            timeout_guard = threading.Lock()
            active = True
            timed_out = False

            def timeout_kill() -> None:
                nonlocal timed_out
                with timeout_guard:
                    if not active:
                        return
                    timed_out = True
                    try:
                        proc.kill()
                    except OSError:
                        pass

            # Вооружить ДО write/flush: полный stdin тоже может блокировать.
            timer = threading.Timer(remaining, timeout_kill)
            timer.start()
            write_error = None
            line = ""
            try:
                try:
                    proc.stdin.write(payload)
                    proc.stdin.flush()
                except (BrokenPipeError, OSError) as exc:
                    write_error = exc
                if write_error is None:
                    line = proc.stdout.readline()
            finally:
                timer.cancel()
                # cancel не останавливает уже вошедший callback. Retire под
                # guard до release I/O-lock защищает следующий запрос того же PID.
                with timeout_guard:
                    active = False

            if write_error is not None or not line:
                rc = proc.poll()
                stderr_tail = "".join(self._stderr_ring)[-2000:]
                # Cleanup принадлежит этому же запросу, пока I/O-lock удерживается.
                self._close_unlocked()
                if timed_out:
                    raise MLXTimeoutError(timeout_sec=timeout_sec, model_name=model_name)
                raise MLXWorkerCrashed(rc, model_name, stderr_tail=stderr_tail) from write_error
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                rc = proc.poll()
                self._close_unlocked()
                raise MLXWorkerCrashed(rc, model_name, stderr_tail=line[:500]) from exc
            if not isinstance(parsed, dict):
                rc = proc.poll()
                self._close_unlocked()
                raise MLXWorkerCrashed(rc, model_name, stderr_tail=line[:500])
            return parsed

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
