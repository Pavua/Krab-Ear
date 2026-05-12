"""Action dispatchers for actionable errors (button clicks in toasts/diagnostics).

Each handler signature: handler(*, settings_service, **kwargs) -> dict.
Return shape: {"executed": bool, "reason": str | None, "side_effect": str | None}.

ACTION_HANDLERS maps action_id strings to handler callables.
handle_action() is the single entry point — catches all handler exceptions.
"""
from __future__ import annotations

import logging
import subprocess
from typing import Callable

logger = logging.getLogger("KrabEar.Backend.ErrorActions")

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


def _kill_lm_studio_via_telegram(*, settings_service, **kwargs) -> dict:
    # B.1: feature-flagged off. Real Telegram bridge integration pending separate spec.
    return {"executed": False, "reason": "feature_disabled", "side_effect": None}


def _switch_to_stable_rewriter(*, settings_service, **kwargs) -> dict:
    """Switch LLM rewriter model to the historically stable qwen3-4b-abliterated.

    Triggered by the 'rewriter.channel_error' actionable toast — gemma-4-e4b-it-mlx
    (vision-capable MLX) emits tool_calls JSON or triggers mlx_lm UnboundLocalError
    mid-stream, causing LM Studio 'Channel Error' within 1 second of inference start.
    """
    settings_service.handle_set_settings({"llm_model": "qwen3-4b-abliterated"})
    return {"executed": True, "reason": None, "side_effect": "settings_updated"}


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
    "open_hf_token_setting": _open_hf_token_setting,
    "disable_rewriter": _disable_rewriter,
    "open_hotkey_settings": _open_hotkey_settings,
    "switch_to_balanced_profile": _switch_to_balanced_profile,
    "retry_history_save": _retry_history_save,
    "kill_lm_studio_via_telegram": _kill_lm_studio_via_telegram,
    "switch_to_stable_rewriter": _switch_to_stable_rewriter,
    "open_lm_studio_settings": _open_lm_studio_settings,
    # Wave 50 — wire-in for new actionable codes (diarization.vad_gated,
    # agent.binary_drift).
    "open_pyannote_hf_page": _open_pyannote_hf_page,
    "open_terminal_make_release": _open_terminal_make_release,
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
