"""mlx_whisper subprocess worker для Krab Ear (P0c).

Тот же интерпретатор, что у родителя (mlx-whisper в основном venv).
stdout — ТОЛЬКО однострочный JSON. Progress MLX/HF → stderr (drain в родителе).

Protocol:
    Request:  {"op": "transcribe", "audio_path": "/tmp/x.wav", "params": {...}}
    Response: {"ok": true, "result": {<mlx_whisper dict>}}

    Request:  {"op": "shutdown"}
    Response: (нет — процесс exit)

При ошибке: {"ok": false, "error": "<тип>: <сообщение>"}.
"""
from __future__ import annotations

import faulthandler
import json
import math
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

_KRAB_EAR_ROOT = Path(__file__).resolve().parents[2]
if str(_KRAB_EAR_ROOT) not in sys.path:
    sys.path.insert(0, str(_KRAB_EAR_ROOT))


def _err(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def json_safe(obj: Any) -> Any:
    """Сериализация ответа mlx_whisper (numpy scalars / NaN/Inf → JSON)."""
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    if isinstance(obj, float):
        if not math.isfinite(obj):
            return 0.0
        return obj
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(x) for x in obj]
    try:
        import numpy as np

        if isinstance(obj, np.generic):
            return json_safe(obj.item())
        if isinstance(obj, np.ndarray):
            return json_safe(obj.tolist())
    except Exception:
        pass
    return str(obj)


def _handle_transcribe(params: dict[str, Any]) -> dict[str, Any]:
    audio_path = params.get("audio_path")
    if not audio_path or not isinstance(audio_path, str):
        return _err("invalid_params: audio_path must be a non-empty string")
    mlx_params = params.get("params") or {}
    if not isinstance(mlx_params, dict):
        return _err("invalid_params: params must be an object")
    try:
        with redirect_stdout(sys.stderr):
            import mlx_whisper  # type: ignore[import]
            result = mlx_whisper.transcribe(audio_path, **mlx_params)
        try:
            import mlx.core as mx

            mx.clear_cache()
        except Exception:
            pass
        return {"ok": True, "result": json_safe(result)}
    except ImportError as exc:
        return _err(f"mlx_whisper_not_installed: {exc}")
    except TypeError as exc:
        return _err(f"TypeError: {exc}")
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


def _process_request(line: str) -> dict[str, Any] | None:
    try:
        request = json.loads(line)
    except json.JSONDecodeError as exc:
        return _err(f"invalid_json: {exc}")
    if not isinstance(request, dict):
        return _err("invalid_json: expected object")
    op = request.get("op")
    if op == "shutdown":
        return None
    if op == "transcribe":
        return _handle_transcribe(request)
    return _err(f"unknown_op: {op!r}")


def main() -> int:
    # Signal-safe dump всех потоков в stderr при native crash. НЕ signal.signal(SIGSEGV).
    try:
        faulthandler.enable(all_threads=True)
    except Exception:
        pass
    while True:
        try:
            line = sys.stdin.readline()
        except Exception as exc:
            sys.stderr.write(f"mlx_whisper_worker: stdin read failed: {exc}\n")
            return 1
        if not line:
            return 0
        try:
            response = _process_request(line)
        except Exception as exc:
            tb = traceback.format_exc(limit=3)
            response = _err(f"unexpected: {type(exc).__name__}: {exc}\n{tb}")
        if response is None:
            return 0
        try:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception as exc:
            sys.stderr.write(f"mlx_whisper_worker: stdout write failed: {exc}\n")
            return 1


if __name__ == "__main__":
    sys.exit(main())
