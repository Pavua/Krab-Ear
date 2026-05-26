"""LM Studio lifecycle management — load/unload моделей через REST + CLI fallback.

Цель: на M4 Max 36GB unified memory можно держать одновременно:
  - rewriter (always-on, 4-9B Q4, ~5 GB)
  - brain (on-demand, 30-35B-A3B Q4, ~19-20 GB) — для Voice Assistant

Когда STT (Whisper + pyannote) активен, brain должен быть выгружен чтобы
освободить ~19 GB Metal-памяти. После окончания записи brain reload'ится.

Strategy:
  1. LM Studio REST API endpoints (newer versions 0.3.x):
     - POST /api/v0/models/{model_id}/unload
     - POST /api/v0/models/{model_id}/load
  2. Fallback: shell `lms unload <model>` / `lms load <model>` (CLI tool).
  3. Last resort: log warning, не падаем (recording flow важнее).

Безопасность: если LM Studio недоступен или endpoint не поддерживается,
мы НЕ ломаем recording. Hook fire-and-forget с timeout 1 сек.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from urllib.parse import quote

logger = logging.getLogger("KrabEar.LMStudioLifecycle")

# Короткие таймауты — мы не блокируем recording flow, fire-and-forget.
_REST_TIMEOUT_SEC = 1.5
_CLI_TIMEOUT_SEC = 2.0

# Safety cap: model IDs longer than this are rejected before any network call.
_MODEL_ID_MAX_LEN = 256


def _try_rest_unload(base_url: str, model_id: str) -> bool:
    """LM Studio REST: POST /api/v0/models/{model}/unload. Returns True on 2xx."""
    # base_url типа "http://localhost:1234/v1" — отбрасываем "/v1"
    api_root = base_url.rstrip("/").removesuffix("/v1")
    url = f"{api_root}/api/v0/models/{quote(model_id, safe='')}/unload"
    try:
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req, timeout=_REST_TIMEOUT_SEC) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            return False  # endpoint недоступен — silent fallback
        logger.debug("LM Studio REST unload %s: HTTP %s", model_id, e.code)
        return False
    except Exception as exc:
        logger.debug("LM Studio REST unload %s: %s", model_id, exc)
        return False


def _try_rest_load(base_url: str, model_id: str) -> bool:
    """LM Studio REST: POST /api/v0/models/load. Returns True on 2xx."""
    api_root = base_url.rstrip("/").removesuffix("/v1")
    url = f"{api_root}/api/v0/models/load"
    try:
        body = json.dumps({"model": model_id}).encode()
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_REST_TIMEOUT_SEC) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            return False
        logger.debug("LM Studio REST load %s: HTTP %s", model_id, e.code)
        return False
    except Exception as exc:
        logger.debug("LM Studio REST load %s: %s", model_id, exc)
        return False


def _try_cli(action: str, model_id: str) -> bool:
    """Fallback на `lms <action> <model_id>` shell command."""
    lms = shutil.which("lms")
    if not lms:
        return False
    try:
        result = subprocess.run(
            [lms, action, model_id],
            timeout=_CLI_TIMEOUT_SEC,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except Exception as exc:
        logger.debug("LM Studio CLI %s %s: %s", action, model_id, exc)
        return False


def unload_model_async(base_url: str, model_id: str) -> None:
    """Fire-and-forget unload. Не блокирует caller, errors silently logged.

    Args:
        base_url: LLM_BASE_URL из settings (например "http://localhost:1234/v1").
        model_id: model identifier как видит LM Studio (lowercase, e.g. "qwen3.6-35b-a3b").
    """
    if not model_id:
        return
    if len(model_id) > _MODEL_ID_MAX_LEN:
        logger.warning(
            "LM Studio: model_id too long (%d chars, max %d) — unload skipped",
            len(model_id), _MODEL_ID_MAX_LEN,
        )
        return

    def _worker() -> None:
        if _try_rest_unload(base_url, model_id):
            logger.info("LM Studio: unloaded brain model '%s' via REST", model_id)
            return
        if _try_cli("unload", model_id):
            logger.info("LM Studio: unloaded brain model '%s' via CLI", model_id)
            return
        logger.debug(
            "LM Studio: unload '%s' not supported (REST 404 + no CLI). "
            "Brain memory will be freed on TTL eviction.",
            model_id,
        )

    threading.Thread(
        target=_worker,
        name=f"LMStudio-unload-{model_id[:20]}",
        daemon=True,
    ).start()


def load_model_async(base_url: str, model_id: str) -> None:
    """Fire-and-forget load. Не блокирует caller.

    Используется после stop_recording чтобы pre-warm brain к моменту когда
    user может открыть Voice Assistant.
    """
    if not model_id:
        return
    if len(model_id) > _MODEL_ID_MAX_LEN:
        logger.warning(
            "LM Studio: model_id too long (%d chars, max %d) — load skipped",
            len(model_id), _MODEL_ID_MAX_LEN,
        )
        return

    def _worker() -> None:
        if _try_rest_load(base_url, model_id):
            logger.info("LM Studio: pre-loaded brain model '%s' via REST", model_id)
            return
        if _try_cli("load", model_id):
            logger.info("LM Studio: pre-loaded brain model '%s' via CLI", model_id)
            return
        logger.debug(
            "LM Studio: load '%s' not supported. Will load on first request (cold start)",
            model_id,
        )

    threading.Thread(
        target=_worker,
        name=f"LMStudio-load-{model_id[:20]}",
        daemon=True,
    ).start()
