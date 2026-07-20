"""GigaAM-RNNT subprocess worker для Krab Ear.

Запускается из изолированного venv (~/.venv_krab_ear_gigaam) — потому что
пакет gigaam пинит torch<=2.5.1 / onnxruntime<=1.23.x, что несовместимо с
main Krab Ear venv (Python 3.14 + torch 2.11). Worker сам импортирует gigaam,
держит модель в памяти и отвечает на JSON-команды через stdin/stdout.

Protocol (одна JSON строка на запрос, одна на ответ):

    Request:  {"op": "load", "mode": "rnnt", "device": "mps"}
    Response: {"ok": true}

    Request:  {"op": "transcribe", "audio_path": "/tmp/x.wav"}
    Response: {"ok": true, "text": "...", "engine": "gigaam-rnnt"}

    Request:  {"op": "shutdown"}
    Response: (нет — процесс exit)

При любой ошибке: {"ok": false, "error": "<тип>: <сообщение>"}.

Запуск (вручную для тестирования):
    ~/.venv_krab_ear_gigaam/bin/python -u KrabEar/core/workers/gigaam_worker.py

Использование из main backend — через core/pipeline/stt_gigaam.py
(класс GigaAMAdapter с transport="subprocess").

Memory profiling (opt-in, zero overhead when off):
    KRAB_EAR_TRACE_GIGAAM_MEM=1 python gigaam_worker.py
    Logs RSS after each transcribe + top-10 tracemalloc allocations every 10 requests.
    See docs/audit/gigaam-worker-memory-2026-05-05.md for details.
"""

from __future__ import annotations

import gc
import json
import os
import sys
import traceback
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Wave 525: singleton guard — один процесс gigaam_worker на всю систему.
# Если уже запущен другой экземпляр → новый exit(0) немедленно.
# Использует эксклюзивный fcntl flock на PID-файл вместо pgrep (race-free).
# ---------------------------------------------------------------------------

def _engine_name_from_mode(mode: Optional[str]) -> str:
    """Имя движка из режима модели: срезает версионный префикс.

    v3_e2e_rnnt / v3_rnnt / v2_rnnt / v1_rnnt → "gigaam-rnnt" (движок один
    независимо от версии). Порядок replace важен: v3_e2e_ ДО v3_.
    """
    mode_base = (
        (mode or "rnnt")
        .replace("v3_e2e_", "")
        .replace("v3_", "")
        .replace("v2_", "")
        .replace("v1_", "")
    )
    return f"gigaam-{mode_base}"


def _acquire_singleton_lock() -> None:
    """Попытка захватить эксклюзивный lock на PID-файл.

    При успехе — пишет PID и возвращает управление (lock держится до exit).
    При неудаче (другой воркер уже держит lock) — печатает предупреждение в stderr
    и завершает процесс с кодом 0 (не ошибка — просто не нужен дубликат).

    Пропускается под KRAB_EAR_GIGAAM_WORKER_NO_SINGLETON=1 — чтобы юнит-тесты,
    импортирующие этот модуль, не хватали прод-lock (иначе живой воркер → sys.exit(0)
    убьёт тест-процесс) и не создавали побочный flock.
    """
    if os.environ.get("KRAB_EAR_GIGAAM_WORKER_NO_SINGLETON") == "1":
        return

    import fcntl
    import tempfile

    lock_path = os.path.join(
        tempfile.gettempdir(), "krab_ear_gigaam_worker.lock"
    )
    try:
        # Открываем/создаём PID-файл. Держим fd открытым всё время жизни процесса —
        # при завершении (нормальном или по сигналу) ядро автоматически снимет flock.
        fd = open(lock_path, "w")  # noqa: WPS515  — intentionally kept open
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid()))
        fd.flush()
        # Не закрываем fd — пока процесс живёт, lock держится.
        # CPython держит fd в глобальном __builtins__ GC, поэтому не нужен глобал,
        # но для явности сохраним в module-level переменную.
        globals()["_singleton_lock_fd"] = fd
    except OSError:
        # LOCK_NB → EWOULDBLOCK если другой процесс держит lock.
        existing_pid = "(unknown)"
        try:
            with open(lock_path) as _lf:
                existing_pid = _lf.read().strip()
        except Exception:
            pass
        sys.stderr.write(
            f"gigaam_worker: singleton guard — уже запущен PID {existing_pid}, "
            "этот экземпляр завершается (duplicate prevention, Wave 525)\n"
        )
        sys.stderr.flush()
        sys.exit(0)


_acquire_singleton_lock()

# ---------------------------------------------------------------------------
# Memory tracing — opt-in via KRAB_EAR_TRACE_GIGAAM_MEM=1
# Zero overhead when env var absent: the check happens once at module load.
# ---------------------------------------------------------------------------

_TRACE_MEM: bool = os.environ.get("KRAB_EAR_TRACE_GIGAAM_MEM") == "1"

if _TRACE_MEM:
    import tracemalloc as _tracemalloc
    _tracemalloc.start()
    try:
        sys.stderr.write("gigaam_worker: tracemalloc started (KRAB_EAR_TRACE_GIGAAM_MEM=1)\n")
        sys.stderr.flush()
    except Exception:
        pass


def _log_rss(label: str = "") -> None:
    """Log current process RSS to stderr. No-op when tracing disabled."""
    if not _TRACE_MEM:
        return
    try:
        import resource as _resource
        usage = _resource.getrusage(_resource.RUSAGE_SELF)
        # ru_maxrss is bytes on Linux, KB on macOS
        rss_raw = usage.ru_maxrss
        # On macOS (darwin) ru_maxrss is in bytes
        import platform
        if platform.system() == "Darwin":
            rss_mb = rss_raw / 1024 / 1024
        else:
            rss_mb = rss_raw / 1024
        tag = f" [{label}]" if label else ""
        sys.stderr.write(f"[mem{tag}] rss={rss_mb:.1f} MB pid={os.getpid()}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _log_tracemalloc_snapshot(request_count: int) -> None:
    """Log top-10 tracemalloc allocations every 10 requests. No-op when off."""
    if not _TRACE_MEM:
        return
    if request_count % 10 != 0:
        return
    try:
        snap = _tracemalloc.take_snapshot()
        top = snap.statistics("lineno")[:10]
        sys.stderr.write(f"[tmalloc] === snapshot at request #{request_count} ===\n")
        for i, stat in enumerate(top, 1):
            sys.stderr.write(f"[tmalloc] #{i}: {stat}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _free_mps_pool() -> None:
    """Release PyTorch MPS Metal buffer pool + run gc.collect.

    H1 hypothesis fix (docs/audit/gigaam-worker-memory-2026-05-05.md):
    PyTorch keeps a ~1 GB Metal buffer pool alive after warmup. Calling
    torch.mps.empty_cache() returns those buffers to the OS allocator.
    gc.collect() releases any pyannote/longform intermediate Python objects.

    Both calls are wrapped in try/except — never raise even if torch.mps
    API changes or is unavailable (e.g. CPU-only env, future PyTorch versions).
    Safe to call on every cycle — idempotent, negligible overhead when empty.

    Bypassed if KRAB_EAR_DISABLE_MPS_POOL_FREE=1 (for A/B validation).
    Never raises.
    """
    if os.environ.get("KRAB_EAR_DISABLE_MPS_POOL_FREE") == "1":
        return  # control mode — leak as-was (C.1 A/B validation)

    try:
        import torch  # type: ignore[import]
        if hasattr(torch, "mps") and hasattr(torch.mps, "empty_cache"):
            torch.mps.empty_cache()
    except Exception:
        pass

    try:
        gc.collect()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Worker global state
# ---------------------------------------------------------------------------

# Глобальное состояние воркера: одна загруженная модель на жизнь процесса.
# Перезагрузка модели при изменении mode/device — отдельная команда "load".
_MODEL: Optional[Any] = None
_MODE: Optional[str] = None


def _err(message: str) -> dict:
    """Стандартный формат ошибки для ответа клиенту."""
    return {"ok": False, "error": message}


def _handle_load(params: dict) -> dict:
    """Загружает модель GigaAM. Lazy import gigaam — поднимаем только при первом load.

    Параметры:
        mode:   "rnnt" | "ctc" | "v2_rnnt" | "v2_ctc" | "v1_rnnt" | "v1_ctc"
        device: "mps" | "cuda" | "cpu"
    """
    global _MODEL, _MODE

    mode = str(params.get("mode", "rnnt"))
    device = str(params.get("device", "mps"))

    try:
        import gigaam  # type: ignore[import]
    except ImportError as exc:
        return _err(f"gigaam_not_installed: {exc}")

    try:
        model = gigaam.load_model(mode)
    except Exception as exc:
        return _err(f"load_failed: {type(exc).__name__}: {exc}")

    # Перенос на устройство (если поддерживается).
    if device != "cpu" and hasattr(model, "to"):
        try:
            import torch  # type: ignore[import]
            if device == "mps" and torch.backends.mps.is_available():
                model = model.to(torch.device("mps"))
            elif device == "cuda" and torch.cuda.is_available():
                model = model.to(torch.device("cuda"))
            # Если device запрошен но недоступен — оставляем на CPU без ошибки.
        except Exception:
            # Failover на CPU — не критично, модель работает.
            pass

    _MODEL = model
    _MODE = mode
    return {"ok": True, "mode": mode, "device": device}


def _handle_transcribe(params: dict) -> dict:
    """Транскрибирует аудиофайл. Модель должна быть предварительно загружена.

    Параметры:
        audio_path: str (required) — путь к WAV/M4A/MP3 файлу.
        longform:   bool (optional, default=False) — использовать
                    `model.transcribe_longform()` для аудио > 30 сек. Требует
                    pyannote.audio + HF token (берётся из ~/.cache/huggingface/token
                    или передаётся в `hf_token` параметре).
        hf_token:   str (optional) — HuggingFace API token для pyannote VAD.
                    Если пустой — fallback на cached token.
    """
    if _MODEL is None:
        return _err("model_not_loaded: call load first")

    audio_path = params.get("audio_path")
    if not audio_path or not isinstance(audio_path, str):
        return _err("invalid_params: audio_path must be a non-empty string")

    longform = bool(params.get("longform", False))
    hf_token = params.get("hf_token", "")

    # SEC MED-1: set HF token only for the duration of the transcribe call, then
    # restore the original env values (None → remove). This prevents the token from
    # persisting across subsequent transcribe requests inside the same worker process.
    _prev_hf: dict = {}
    if hf_token and isinstance(hf_token, str):
        import os as _os
        for _k in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
            _prev_hf[_k] = _os.environ.get(_k)
            _os.environ[_k] = hf_token

    try:
        if longform and hasattr(_MODEL, "transcribe_longform"):
            # Возвращает list[dict] с `transcription`, `boundaries` (start, end).
            segments = _MODEL.transcribe_longform(audio_path)
            # Склеиваем в единый текст (с двойным переводом строк между сегментами).
            text = "\n\n".join(
                (seg.get("transcription") or "").strip()
                for seg in (segments or [])
                if isinstance(seg, dict) and seg.get("transcription")
            )
            result_meta = {
                "longform": True,
                "segments_count": len(segments) if segments else 0,
            }
            # H2: free pyannote segments + pyannote intermediates held in segments list.
            # pyannote.audio stores diarization output (embeddings, numpy arrays) inside
            # each segment dict — these are not freed until GC sweeps the list.
            # del + gc.collect() immediately releases ~tens of MB per longform call.
            # Wrapped in try/except so naming mismatches never raise.
            try:
                del segments
            except NameError:
                pass
            try:
                gc.collect()
            except Exception:
                pass
        else:
            result = _MODEL.transcribe(audio_path)
            # gigaam.transcribe() может вернуть строку или объект с .text — адаптируем.
            if isinstance(result, str):
                text = result
            elif hasattr(result, "text"):
                text = result.text
            else:
                text = str(result)
            result_meta = {"longform": False}
    except Exception as exc:
        return _err(f"transcribe_failed: {type(exc).__name__}: {exc}")
    finally:
        # SEC MED-1: restore env vars regardless of success/failure.
        import os as _os
        for _k, _v in _prev_hf.items():
            if _v is None:
                _os.environ.pop(_k, None)
            else:
                _os.environ[_k] = _v

    # Memory tracing (opt-in: KRAB_EAR_TRACE_GIGAAM_MEM=1).
    # Logs RSS after inference so we can track MPS/PyTorch buffer pool growth.
    _log_rss(label="after_transcribe")

    # Free Metal buffer pool — H1 hypothesis fix.
    # Called after every transcribe response (normal + longform). Idempotent.
    # See docs/audit/gigaam-worker-memory-2026-05-05.md for motivation.
    _free_mps_pool()

    # Имя движка соответствует тому что использует in-process адаптер
    # (core/pipeline/stt_gigaam.py — _engine_name). v3_e2e_rnnt → "gigaam-rnnt".
    engine = _engine_name_from_mode(_MODE)
    if longform:
        engine = f"{engine}-longform"

    return {"ok": True, "text": text.strip(), "engine": engine, **result_meta}


def _process_request(line: str) -> Optional[dict]:
    """Разбирает одну строку запроса. Возвращает dict-ответ или None если shutdown."""
    line = line.strip()
    if not line:
        return _err("empty_request")

    try:
        request = json.loads(line)
    except json.JSONDecodeError as exc:
        return _err(f"json_decode_error: {exc}")

    if not isinstance(request, dict):
        return _err("invalid_request: must be a JSON object")

    op = request.get("op")
    if op == "load":
        return _handle_load(request)
    if op == "transcribe":
        return _handle_transcribe(request)
    if op == "shutdown":
        return None  # сигнал клиенту что мы выходим
    if op == "ping":
        # Удобный keep-alive: позволяет клиенту убедиться что воркер жив.
        return {"ok": True, "pong": True, "model_loaded": _MODEL is not None}

    return _err(f"unknown_op: {op}")


def main() -> int:
    """Главный loop: читаем stdin построчно, отвечаем построчно в stdout."""
    # Build / runtime info — одной debug-строкой в stderr (не парсится клиентом).
    try:
        sys.stderr.write(f"gigaam_worker: started (python={sys.version.split()[0]})\n")
        sys.stderr.flush()
    except Exception:
        pass

    # Request counter — used for periodic tracemalloc snapshots (opt-in).
    _request_count = 0

    while True:
        try:
            line = sys.stdin.readline()
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            sys.stderr.write(f"gigaam_worker: stdin read failed: {exc}\n")
            return 1

        if not line:
            # EOF — клиент закрыл pipe, выходим.
            return 0

        _request_count += 1

        try:
            response = _process_request(line)
        except Exception as exc:
            # Ловим ВСЁ — воркер не должен падать от неожиданного ввода.
            tb = traceback.format_exc(limit=3)
            response = _err(f"unexpected: {type(exc).__name__}: {exc}\n{tb}")

        # Periodic tracemalloc snapshot — every 10 requests (opt-in only).
        _log_tracemalloc_snapshot(_request_count)

        if response is None:
            # shutdown — exit cleanly без ответа.
            return 0

        try:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception as exc:
            sys.stderr.write(f"gigaam_worker: stdout write failed: {exc}\n")
            return 1


if __name__ == "__main__":
    sys.exit(main())
