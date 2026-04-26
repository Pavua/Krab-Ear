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
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Optional


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
    global _MODEL, _MODE

    if _MODEL is None:
        return _err("model_not_loaded: call load first")

    audio_path = params.get("audio_path")
    if not audio_path or not isinstance(audio_path, str):
        return _err("invalid_params: audio_path must be a non-empty string")

    longform = bool(params.get("longform", False))
    hf_token = params.get("hf_token", "")

    # Если HF token передан явно — overwrite env vars (pyannote/HF Hub их читают).
    # Используем прямое присваивание (не setdefault), чтобы explicit token из
    # settings.STT_GIGAAM_HF_TOKEN имел приоритет над shell env (более частый
    # case: env пустой, settings overrides cached ~/.cache/huggingface/token).
    if hf_token and isinstance(hf_token, str):
        import os
        os.environ["HF_TOKEN"] = hf_token
        os.environ["HUGGING_FACE_HUB_TOKEN"] = hf_token

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

    # Имя движка соответствует тому что использует in-process адаптер
    # (core/pipeline/stt_gigaam.py — _engine_name).
    mode_base = (_MODE or "rnnt").replace("v2_", "").replace("v1_", "")
    engine = f"gigaam-{mode_base}"
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

        try:
            response = _process_request(line)
        except Exception as exc:
            # Ловим ВСЁ — воркер не должен падать от неожиданного ввода.
            tb = traceback.format_exc(limit=3)
            response = _err(f"unexpected: {type(exc).__name__}: {exc}\n{tb}")

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
