"""Action dispatchers for actionable errors (button clicks in toasts/diagnostics).

Each handler signature: handler(*, settings_service, **kwargs) -> dict.
Return shape: {"executed": bool, "reason": str | None, "side_effect": str | None}.

ACTION_HANDLERS maps action_id strings to handler callables.
handle_action() is the single entry point — catches all handler exceptions.
"""
from __future__ import annotations

import logging
import subprocess

from backend.brain_lease import current_lease_holder
from backend.lm_studio_lifecycle import unload_model_async
from typing import Callable

logger = logging.getLogger("KrabEar.Backend.ErrorActions")

# Владелец brain-лизы, которым представляется этот процесс (см. recording_core_service).
_OWN_LEASE_OWNER = "krab_ear"

# Privacy preference URLs (macOS deep links)
_PRIVACY_ACCESSIBILITY_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)


def _open_url(url: str) -> dict:
    try:
        subprocess.run(["open", url], check=True)
        return {"executed": True, "reason": None, "side_effect": f"opened:{url}"}
    except subprocess.CalledProcessError as exc:
        return {"executed": False, "reason": f"open_failed: {exc}", "side_effect": None}


# ---------------------------------------------------------------------------
# Real handlers
# ---------------------------------------------------------------------------

def _open_privacy_settings(*, settings_service, **kwargs) -> dict:
    return _open_url(_PRIVACY_ACCESSIBILITY_URL)


def _open_hf_token_setting(*, settings_service, **kwargs) -> dict:
    # Emit IPC event the Swift side picks up to focus HF Token field in Settings tab.
    return {"executed": True, "reason": None, "side_effect": "swift_focus_hf_token"}


def _disable_rewriter(*, settings_service, **kwargs) -> dict:
    settings_service.handle_set_settings({"llm_rewrite_enabled": False})
    return {"executed": True, "reason": None, "side_effect": "settings_updated"}


# ---------------------------------------------------------------------------
# Stubs (real implementations in later phases)
# ---------------------------------------------------------------------------

def _open_hotkey_settings(*, settings_service, **kwargs) -> dict:
    return {"executed": True, "reason": None, "side_effect": "swift_focus_hotkey_tab"}


def _switch_to_balanced_profile(*, settings_service, **kwargs) -> dict:
    settings_service.handle_set_settings({"quality_profile": "balanced"})
    return {"executed": True, "reason": None, "side_effect": "profile_switched"}


def _retry_history_save(*, settings_service, store=None, **kwargs) -> dict:
    if store is None:
        return {"executed": False, "reason": "no_store_available", "side_effect": None}
    try:
        store.retry_pending_writes()  # method to be added in B.2
        return {"executed": True, "reason": None, "side_effect": "history_retried"}
    except Exception as exc:
        return {"executed": False, "reason": f"retry_failed: {exc}", "side_effect": None}


def _unload_lm_studio_model(*, settings_service, **kwargs) -> dict:
    """Освободить память: выгрузить brain-модель из LM Studio.

    🔴 До 2026-08-19 здесь стояла заглушка `feature_disabled` (Phase B.1, «pending
    separate spec»), которая безусловно ничего не делала — владелец жал кнопку
    «выгрузить» под тостом о нехватке памяти, и не происходило ровным счётом ничего.
    Механизм выгрузки при этом в проекте есть и работает, он просто никогда отсюда
    не звался.

    🔴 Гейт на чужую работу: `brain_lease` — advisory-координация одного Metal GPU
    между Krab Ear и Главным Крабом. Если лизу держит ДРУГОЙ владелец, выгрузка
    оборвала бы его inference, поэтому отказываемся. Направление отказа fail-safe:
    не уверены — не выгружаем (тост остаётся, но чужая работа цела).

    Выгрузка асинхронная (fire-and-forget), подтверждения от LM Studio нет — поэтому
    честно сообщаем «запрошена», а не «выполнена».
    """
    holder = current_lease_holder()
    if holder and holder.get("owner") != _OWN_LEASE_OWNER:
        return {
            "executed": False,
            "reason": f"brain lease held by {holder.get('owner')!r} — чужой inference не обрываем",
            "side_effect": None,
        }

    settings = settings_service.cached_settings() if settings_service else {}
    model_id = (settings or {}).get("llm_brain_model") or ""
    base_url = (settings or {}).get("llm_base_url") or ""
    if not model_id:
        return {
            "executed": False,
            "reason": "llm_brain_model не задан в настройках — нечего выгружать",
            "side_effect": None,
        }

    unload_model_async(base_url, model_id)
    logger.info("mlx.oom action: запрошена выгрузка модели", extra={"model_id": model_id})
    return {
        "executed": True,
        "reason": None,
        "side_effect": f"unload_requested:{model_id}",
    }


# Кандидаты в «стабильный рерайтер», в порядке предпочтения. Список — это
# ПОЖЕЛАНИЕ, а не факт: каждое имя сверяется с живым каталогом LM Studio перед
# записью в настройки.
#
# 🔴 Раньше здесь стояло одно зашитое имя `qwen3-4b-abliterated`, которого в
# каталоге владельца не существует (живая сверка 2026-08-27: 105 моделей).
# Это действие — кнопка «починить» в тосте `rewriter.channel_error`, то есть
# нажатие делало эпизодический сбой рерайтера постоянным.
#
# Порядок — по замеру 2026-08-27 на 12 живых диктовках (тот же промпт и
# параметры, что в проде): huihui-qwen3-14b-abl сохраняет текст дословно
# (9/9 матерных слов, как 26B) при медиане 8.4 с против 24.7 с у 26B.
_STABLE_REWRITER_CANDIDATES = (
    "huihui-qwen3-14b-abl-v2",
    "huihui-qwen3-4b-instruct-2507-abliterated-hi-mlx",
    "gemma-4-26b-a4b-it@4bit",
    "gemma-4-e4b-it-mlx",
)


def _live_model_catalog(llm_ops_svc) -> list:
    """Имена моделей, реально доступных в LM Studio. Пустой список = не знаем."""
    if llm_ops_svc is None:
        return []
    try:
        payload = llm_ops_svc.handle_list_llm_models({}) or {}
    except Exception as exc:
        logger.warning("не удалось получить каталог моделей LM Studio: %s", exc)
        return []
    return [str(m) for m in (payload.get("models") or [])]


def _switch_to_stable_rewriter(*, settings_service, llm_ops_svc=None, **kwargs) -> dict:
    """Переключает рерайтер на модель, которая ЕСТЬ в каталоге LM Studio.

    Вызывается кнопкой тоста 'rewriter.channel_error': gemma-4-e4b-it-mlx
    (vision-capable MLX) отдаёт tool_calls JSON или роняет mlx_lm
    UnboundLocalError на середине потока, что LM Studio показывает как
    'Channel Error' через секунду после старта инференса.

    Направление отказа fail-safe: нет каталога или нет живого кандидата —
    рабочая настройка НЕ трогается.
    """
    catalog = _live_model_catalog(llm_ops_svc)
    if not catalog:
        return {
            "executed": False,
            "reason": "каталог моделей LM Studio недоступен — не меняю настройку вслепую",
            "side_effect": None,
        }

    settings = settings_service.cached_settings() if settings_service else {}
    current = str((settings or {}).get("llm_model") or "")
    available = set(catalog)
    target = next(
        (m for m in _STABLE_REWRITER_CANDIDATES if m in available and m != current),
        None,
    )
    if target is None:
        return {
            "executed": False,
            "reason": "в каталоге LM Studio нет ни одной проверенной модели на замену",
            "side_effect": None,
        }

    settings_service.handle_set_settings({"llm_model": target})
    logger.info("рерайтер переключён на проверенную модель", extra={"model": target})
    return {"executed": True, "reason": None, "side_effect": f"settings_updated:{target}"}


def _open_lm_studio_settings(*, settings_service, **kwargs) -> dict:
    """Return a hint to the Swift agent to navigate to LM Studio API key settings.

    The Swift agent picks up side_effect='swift_focus_lm_studio_api_key' and
    highlights the LM Studio API Key field in Settings tab.
    As a convenience, also attempt to open LM Studio if it is installed.
    """
    try:
        subprocess.run(["open", "-a", "LM Studio"], check=False)
    except Exception:
        pass
    return {
        "executed": True,
        "reason": None,
        "side_effect": "swift_focus_lm_studio_api_key",
    }


# ---------------------------------------------------------------------------
# Dispatch table (9 entries)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Wave 50 handlers — for new actionable codes (diarization.vad_gated,
# agent.binary_drift). Both open external resource (HF page / Terminal).
# ---------------------------------------------------------------------------

def _open_pyannote_hf_page(*, settings_service, **kwargs) -> dict:
    """Open the gated pyannote VAD model page on Hugging Face so user can
    accept terms. Memory: blocker_pyannote_gated_2026-04-26.md."""
    return _open_url("https://huggingface.co/pyannote/voice-activity-detection")


def _open_logs(*, settings_service, **kwargs) -> dict:
    """Open the KrabEar logs/data directory in Finder so user can delete old files.

    Used by disk.low_space actionable toast — Wave 60.
    """
    import os
    logs_dir = os.path.expanduser("~/Library/Application Support/KrabEar")
    try:
        subprocess.run(["open", logs_dir], check=True)
        return {
            "executed": True,
            "reason": None,
            "side_effect": f"opened_finder_at:{logs_dir}",
        }
    except subprocess.CalledProcessError as exc:
        return {"executed": False, "reason": f"open_failed: {exc}", "side_effect": None}


def _open_terminal_make_release(*, settings_service, **kwargs) -> dict:
    """Open Terminal at the repo root so user can run `make release` to
    sync the two-binary drift. Cannot run `make release` directly —
    requires interactive codesign + dSYM upload may prompt for keychain."""
    import os
    repo = os.path.expanduser("~/Antigravity_AGENTS/Krab Ear")
    try:
        # `open -a Terminal "<path>"` opens a new Terminal window at that cwd
        subprocess.run(["open", "-a", "Terminal", repo], check=True)
        return {
            "executed": True,
            "reason": None,
            "side_effect": f"opened_terminal_at:{repo}",
        }
    except subprocess.CalledProcessError as exc:
        return {"executed": False, "reason": f"open_failed: {exc}", "side_effect": None}


ACTION_HANDLERS: dict[str, Callable] = {
    "open_privacy_settings": _open_privacy_settings,
    "unload_lm_studio_model": _unload_lm_studio_model,
    "open_hf_token_setting": _open_hf_token_setting,
    "disable_rewriter": _disable_rewriter,
    "open_hotkey_settings": _open_hotkey_settings,
    "switch_to_balanced_profile": _switch_to_balanced_profile,
    "retry_history_save": _retry_history_save,
    "switch_to_stable_rewriter": _switch_to_stable_rewriter,
    "open_lm_studio_settings": _open_lm_studio_settings,
    # Wave 50 — wire-in for new actionable codes (diarization.vad_gated,
    # agent.binary_drift).
    "open_pyannote_hf_page": _open_pyannote_hf_page,
    "open_terminal_make_release": _open_terminal_make_release,
    # Wave 60 — open KrabEar data dir for disk.low_space actionable.
    "open_logs": _open_logs,
}


def handle_action(action_id: str, **kwargs) -> dict:
    """Dispatch an action by ID.

    Returns a dict with keys: executed (bool), reason (str | None),
    side_effect (str | None).  Never raises.
    """
    handler = ACTION_HANDLERS.get(action_id)
    if handler is None:
        return {
            "executed": False,
            "reason": f"unknown action_id: {action_id}",
            "side_effect": None,
        }
    try:
        return handler(**kwargs)
    except Exception as exc:
        logger.exception("action handler raised: action_id=%s", action_id)
        return {"executed": False, "reason": f"handler_raised: {exc}", "side_effect": None}
