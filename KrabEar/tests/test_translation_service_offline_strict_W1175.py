"""W1175 regression tests: privacy_mode must use offline_strict (not offline_only).

W1167 F1 HIGH: offline_only is NOT a valid Translator network_mode — it silently
maps to offline_default, which allows network calls.  After the fix, privacy_mode
must pass offline_strict (the strictest valid tier) to Translator.translate().

Covers:
- test_privacy_mode_enforces_offline_strict_network_mode
- test_translate_text_in_privacy_passes_offline_strict_to_translator
- test_translate_selection_in_privacy_passes_offline_strict_to_translator
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.translation_service import TranslationService
from backend.translator import TranslationResult, Translator


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _make_result(**kw: Any) -> TranslationResult:
    defaults: dict[str, Any] = dict(
        text="translated",
        status="ok",
        source_lang="ru",
        target_lang="es",
        mode="ru_es",
        engine="opus_mt",
    )
    defaults.update(kw)
    return TranslationResult(**defaults)


def _make_service(settings: dict[str, Any] | None = None) -> tuple[TranslationService, MagicMock]:
    effective: dict[str, Any] = {
        "network_mode": "online_opt_in",
        "translation_glossary": {},
        "translation_style": "neutral",
    }
    if settings:
        effective.update(settings)

    translator = MagicMock()
    translator.translate.return_value = _make_result()

    store = MagicMock()
    store.get_history_page.return_value = ([], None)
    store.save_settings.side_effect = lambda s: s
    store.load_vocabulary.return_value = []

    cell = [dict(effective)]

    svc = TranslationService(
        translator=translator,
        store=store,
        cached_settings=lambda: dict(cell[0]),
        invalidate_settings_cache=lambda: None,
    )
    return svc, translator


# ──────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────

class PrivacyModeOfflineStrictTestCase(unittest.TestCase):
    """Privacy mode must enforce offline_strict, not offline_only."""

    # 1. _normalize_network_mode rejects offline_only → maps to offline_default
    def test_privacy_mode_enforces_offline_strict_network_mode(self) -> None:
        """Translator._normalize_network_mode accepts offline_strict, rejects offline_only."""
        # offline_strict is a valid tier → round-trips unchanged
        self.assertEqual(
            Translator._normalize_network_mode("offline_strict"),
            "offline_strict",
        )
        # offline_only is NOT in the accepted set → maps to offline_default (the bug)
        self.assertEqual(
            Translator._normalize_network_mode("offline_only"),
            "offline_default",
        )

    # 2. handle_translate_text with privacy_mode_enabled passes offline_strict
    def test_translate_text_in_privacy_passes_offline_strict_to_translator(self) -> None:
        """handle_translate_text must pass network_mode='offline_strict' when privacy_mode_enabled=True."""
        svc, translator = _make_service(settings={"privacy_mode_enabled": True})

        svc.handle_translate_text({"text": "привет", "translation_mode": "ru_es"})

        translator.translate.assert_called_once()
        call_kwargs = translator.translate.call_args.kwargs
        self.assertEqual(
            call_kwargs.get("network_mode"),
            "offline_strict",
            msg=(
                f"Expected network_mode='offline_strict' but got "
                f"'{call_kwargs.get('network_mode')}'. "
                "offline_only silently maps to offline_default and allows network calls."
            ),
        )

    # 3. handle_translate_text without privacy_mode keeps original network_mode
    def test_translate_text_no_privacy_keeps_original_network_mode(self) -> None:
        """Without privacy_mode, the network_mode from settings is passed unchanged."""
        svc, translator = _make_service(settings={
            "privacy_mode_enabled": False,
            "network_mode": "online_opt_in",
        })

        svc.handle_translate_text({"text": "hello", "translation_mode": "auto"})

        call_kwargs = translator.translate.call_args.kwargs
        self.assertEqual(call_kwargs.get("network_mode"), "online_opt_in")

    # 4. handle_translate_selection with privacy_mode_enabled passes offline_strict
    def test_translate_selection_in_privacy_passes_offline_strict_to_translator(self) -> None:
        """handle_translate_selection must pass network_mode='offline_strict' when privacy_mode_enabled=True."""
        svc, translator = _make_service(settings={
            "privacy_mode_enabled": True,
            "network_mode": "online_opt_in",
        })

        svc.handle_translate_selection({
            "text": "привет мир",
            "source_lang": "ru",
            "target_lang": "es",
        })

        translator.translate.assert_called_once()
        call_kwargs = translator.translate.call_args.kwargs
        self.assertEqual(
            call_kwargs.get("network_mode"),
            "offline_strict",
            msg=(
                f"Expected network_mode='offline_strict' but got "
                f"'{call_kwargs.get('network_mode')}'. "
                "Selection translate must block network calls in privacy mode."
            ),
        )

    # 5. handle_translate_selection without privacy_mode keeps original network_mode
    def test_translate_selection_no_privacy_keeps_original_network_mode(self) -> None:
        """Without privacy_mode, handle_translate_selection passes network_mode from settings."""
        svc, translator = _make_service(settings={
            "privacy_mode_enabled": False,
            "network_mode": "offline_default",
        })

        svc.handle_translate_selection({
            "text": "hello world",
            "source_lang": "en",
            "target_lang": "ru",
        })

        call_kwargs = translator.translate.call_args.kwargs
        self.assertEqual(call_kwargs.get("network_mode"), "offline_default")

    # 6. privacy_mode with already-strict setting: no duplicate audit log needed
    def test_translate_text_privacy_already_offline_strict_no_error(self) -> None:
        """If network_mode is already offline_strict and privacy_mode is True, no exception."""
        svc, translator = _make_service(settings={
            "privacy_mode_enabled": True,
            "network_mode": "offline_strict",
        })

        # Should not raise
        svc.handle_translate_text({"text": "тест", "translation_mode": "ru_es"})

        call_kwargs = translator.translate.call_args.kwargs
        self.assertEqual(call_kwargs.get("network_mode"), "offline_strict")


if __name__ == "__main__":
    unittest.main()
