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


def _open_log_file(*, settings_service, **kwargs) -> dict:
    import os
    log_path = os.path.expanduser(
        "~/Library/Application Support/KrabEar/backend.log"
    )
    return _open_url(log_path)


# ---------------------------------------------------------------------------
# Dispatch table (8 entries)
# ---------------------------------------------------------------------------

ACTION_HANDLERS: dict[str, Callable] = {
    "open_privacy_settings": _open_privacy_settings,
    "open_hf_token_setting": _open_hf_token_setting,
    "disable_rewriter": _disable_rewriter,
    "open_hotkey_settings": _open_hotkey_settings,
    "switch_to_balanced_profile": _switch_to_balanced_profile,
    "retry_history_save": _retry_history_save,
    "kill_lm_studio_via_telegram": _kill_lm_studio_via_telegram,
    "open_log_file": _open_log_file,
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
