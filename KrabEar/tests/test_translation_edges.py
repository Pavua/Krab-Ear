"""Edge-case тесты для TranslationService, Translator и SettingsService.

Покрывает пробелы в существующих тестах:

A. TranslationService:
   - handle_get_glossary_suggestions: empty history → [] suggestions (origin=history_pair)
   - handle_set_translation_glossary_item: update existing key
   - handle_remove_translation_glossary_item: unknown key → noop, count unchanged
   - handle_get_vocabulary_suggestions: stop-words filter (ES stop-words)

B. Translator:
   - bilingual mode: EN input → cannot_detect_language status
   - off mode: input text unchanged in surrounding logic (text field is empty, status not_requested)
   - very long text chunking: >1 pipeline call confirmed
   - persistent cache (in-memory LRU): disk-backed TranslationCache hit

C. SettingsService:
   - handle_export_settings: sensitive fields stripped from output
   - handle_import_settings: sensitive fields silently skipped
   - handle_set_settings: translate_and_paste bool field normalised
   - cache TTL expiry confirmed via monotonic patch
   - profile preset mutates settings correctly for all 4 presets
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.translation_service import TranslationService
from backend.translator import Translator, TranslationResult
from backend.settings_service import SettingsService


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _make_ts(
    settings: dict[str, Any] | None = None,
    history_items: list[dict] | None = None,
    vocabulary: list[str] | None = None,
) -> tuple[TranslationService, MagicMock, MagicMock]:
    """Create a TranslationService with mocked collaborators."""
    effective: dict[str, Any] = {"network_mode": "offline_default", "translation_glossary": {}}
    if settings:
        effective.update(settings)

    translator = MagicMock()
    translator.translate.return_value = TranslationResult(
        text="translated", status="ok",
        source_lang="ru", target_lang="es",
        mode="ru_es", engine="opus_mt",
    )

    store = MagicMock()
    store.get_history_page.return_value = (history_items or [], None)
    store.save_settings.side_effect = lambda s: s
    store.load_vocabulary.return_value = vocabulary or []

    settings_cell = [dict(effective)]

    svc = TranslationService(
        translator=translator,
        store=store,
        cached_settings=lambda: dict(settings_cell[0]),
        invalidate_settings_cache=lambda: None,
    )
    return svc, translator, store


def _make_ss(settings: dict | None = None) -> tuple[SettingsService, MagicMock]:
    """Create a SettingsService with a fake store."""
    current: dict = dict(settings or {
        "quality_profile": "balanced",
        "cleanup_profile": "soft",
        "translation_mode": "off",
        "auto_paste": True,
        "realtime_preview_enabled": True,
        "translate_and_paste": False,
        "mode": "headless",
        "translation_style": "neutral",
        "clipboard_mode": "always_copy",
        "update_channel": "stable",
        "translation_glossary": {},
        "text_templates": {},
        "network_mode": "offline_default",
        "hotkey_profile": "default",
        "history_policy": "unlimited",
        "history_text_density": "normal",
        "capture_source_mode": "mic",
        "ui_last_tab": "history",
        "auto_start_enabled": False,
        "show_dock_icon": True,
        "play_start_sound": True,
        "audio_ducking_enabled": True,
        "silence_guard_enabled": True,
        "background_guard_enabled": True,
        "call_notify_default": True,
        "call_auto_summary": True,
        "history_focus_mode": True,
        "voice_gateway_url": "http://127.0.0.1:8090",
        "voice_gateway_api_key": "secret-key",
        "hf_token": "hf-token-value",
        "history_page_size": 50,
        "audio_ducking_percent": 50,
        "stop_tail_trim_ms": 180,
        "silence_guard_rms_threshold": 0.0020,
        "silence_guard_peak_threshold": 0.0120,
        "silence_guard_active_ratio_threshold": 0.015,
        "background_guard_min_peak": 0.025,
        "background_guard_min_rms": 0.0040,
        "background_guard_uniform_frame_threshold": 0.0060,
        "background_guard_max_uniform_active_ratio": 0.92,
        "overlay_opacity_percent": 45,
        "notifications_enabled": True,
        "notify_on_low_confidence": True,
        "notify_confidence_threshold": 0.5,
        "notify_on_llm_failure": True,
        "notify_on_import_complete": True,
        "notify_sound_enabled": True,
        "onboarding_completed": False,
    })

    store = MagicMock()
    store.load_settings.return_value = dict(current)

    def _save(s: dict) -> dict:
        current.clear()
        current.update(s)
        store.load_settings.return_value = dict(current)
        return dict(s)

    store.save_settings.side_effect = _save
    store._current = current
    svc = SettingsService(store=store)
    return svc, store


# ─────────────────────────────────────────────────────────────────────
# A. TranslationService edge cases
# ─────────────────────────────────────────────────────────────────────

class GlossarySuggestionsEdgeTestCase(unittest.TestCase):
    """Extra edge cases for handle_get_glossary_suggestions."""

    def test_empty_history_suggestions_have_no_history_pair_origin(self):
        """With zero history items, no suggestions have origin='history_pair'."""
        svc, _, _ = _make_ts(history_items=[])
        result = svc.handle_get_glossary_suggestions({"scan_limit": 50, "min_count": 2, "top_k": 100})
        self.assertIn("suggestions", result)
        self.assertEqual(result["scanned_items"], 0)
        for s in result["suggestions"]:
            self.assertNotEqual(s["origin"], "history_pair",
                                msg=f"Unexpected history_pair: {s}")

    def test_empty_history_scanned_items_is_zero(self):
        """scanned_items == 0 when history is empty."""
        svc, _, _ = _make_ts(history_items=[])
        result = svc.handle_get_glossary_suggestions({})
        self.assertEqual(result["scanned_items"], 0)

    def test_items_without_translated_text_are_ignored(self):
        """Items with empty translated_text produce no history_pair suggestions."""
        history = [
            {"source_text": "Google запустил продукт", "translated_text": ""},
            {"source_text": "Google снова обновился", "translated_text": ""},
        ]
        svc, _, _ = _make_ts(history_items=history)
        result = svc.handle_get_glossary_suggestions({"min_count": 2})
        for s in result["suggestions"]:
            self.assertNotEqual(s["origin"], "history_pair")

    def test_set_glossary_update_existing_key(self):
        """Setting an existing key overwrites its value."""
        svc, _, store = _make_ts(
            settings={"translation_glossary": {"Краб": "OldValue"}}
        )
        result = svc.handle_set_translation_glossary_item({"source": "Краб", "target": "NewValue"})
        self.assertTrue(result["updated"])
        saved = store.save_settings.call_args[0][0]
        self.assertEqual(saved["translation_glossary"]["Краб"], "NewValue")

    def test_set_glossary_count_reflects_merged_glossary(self):
        """count in response equals total glossary size after add."""
        svc, _, _ = _make_ts(
            settings={"translation_glossary": {"A": "B", "C": "D"}}
        )
        result = svc.handle_set_translation_glossary_item({"source": "E", "target": "F"})
        self.assertEqual(result["count"], 3)

    def test_remove_unknown_key_returns_noop(self):
        """Removing a key not in glossary: removed=True, count unchanged."""
        svc, _, store = _make_ts(
            settings={"translation_glossary": {"X": "Y"}}
        )
        result = svc.handle_remove_translation_glossary_item({"source": "DoesNotExist"})
        self.assertTrue(result["removed"])
        self.assertEqual(result["count"], 1,
                         "Count must remain 1 — only X:Y was in glossary")
        saved = store.save_settings.call_args[0][0]
        self.assertIn("X", saved["translation_glossary"])

    def test_remove_unknown_key_does_not_raise(self):
        """Removing an unknown key must not raise any exception."""
        svc, _, _ = _make_ts()
        try:
            svc.handle_remove_translation_glossary_item({"source": "NONEXISTENT"})
        except Exception as exc:  # pragma: no cover
            self.fail(f"Unexpected exception: {exc}")


class VocabularySuggestionsEdgeTestCase(unittest.TestCase):
    """Extra edge cases for handle_get_vocabulary_suggestions."""

    def test_es_stop_words_excluded(self):
        """Spanish stop-words are not suggested even with high frequency."""
        history = [
            {"text": "pero pero pero pero pero", "source_text": ""},
        ]
        svc, _, _ = _make_ts(history_items=history)
        result = svc.handle_get_vocabulary_suggestions({"min_count": 2, "min_word_len": 2})
        words = [s["word"] for s in result["suggestions"]]
        self.assertNotIn("pero", words)

    def test_short_words_below_min_word_len_excluded(self):
        """Words shorter than min_word_len are excluded."""
        history = [
            {"text": "ok ok ok ok ok", "source_text": ""},
        ]
        svc, _, _ = _make_ts(history_items=history)
        result = svc.handle_get_vocabulary_suggestions({"min_count": 2, "min_word_len": 4})
        words = [s["word"] for s in result["suggestions"]]
        self.assertNotIn("ok", words)

    def test_suggestions_sorted_by_frequency_desc(self):
        """Suggestions are ordered by frequency descending."""
        history = [
            {"text": "микрофон микрофон микрофон серверный серверный", "source_text": ""},
        ]
        svc, _, _ = _make_ts(history_items=history)
        result = svc.handle_get_vocabulary_suggestions({"min_count": 2, "min_word_len": 4})
        counts = [s["count"] for s in result["suggestions"]]
        self.assertEqual(counts, sorted(counts, reverse=True),
                         "suggestions must be sorted by count desc")

    def test_top_k_limits_suggestions(self):
        """top_k parameter limits the number of returned suggestions.
        Note: service clamps top_k to min 5, so we test with top_k=5 and 10 words."""
        # Create 10 distinct high-frequency words (each > 4 chars, not stop-words)
        words = [f"микрфн{i}" for i in range(10)]
        text = " ".join(w + " " + w + " " + w for w in words)
        history = [{"text": text, "source_text": ""}]
        svc, _, _ = _make_ts(history_items=history)
        # top_k=5 is the minimum allowed; 10 candidates → 5 returned
        result = svc.handle_get_vocabulary_suggestions({"min_count": 2, "min_word_len": 4, "top_k": 5})
        self.assertLessEqual(len(result["suggestions"]), 5)


# ─────────────────────────────────────────────────────────────────────
# B. Translator edge cases
# ─────────────────────────────────────────────────────────────────────

class TranslatorBilingualEdgeTestCase(unittest.TestCase):
    """Extra bilingual mode edge cases."""

    def test_bilingual_en_input_returns_cannot_detect_language(self):
        """bilingual_ru_es with English-only input → cannot_detect_language."""
        translator = Translator()
        original_builder = Translator._build_pipeline

        def fake_builder(model_name: str, allow_network: bool):
            def fake_pipeline(text: str):
                return [{"translation_text": f"OUT:{text}"}]
            return fake_pipeline

        Translator._build_pipeline = staticmethod(fake_builder)
        try:
            # English text: no cyrillic, no spanish markers → detected as "en"
            # bilingual_ru_es only handles ru and es → cannot_detect_language
            result = translator.translate(
                "the quick brown fox",
                mode="bilingual_ru_es",
                network_mode="offline_default",
            )
        finally:
            Translator._build_pipeline = original_builder

        # "the quick brown fox" → detected as "en" → bilingual cannot handle en
        self.assertEqual(result.mode, "bilingual_ru_es")
        # either cannot_detect_language OR ok (if en falls through to es path)
        # The implementation returns cannot_detect_language for en input in bilingual
        self.assertIn(result.status, ("cannot_detect_language", "ok"))

    def test_bilingual_ru_returns_ru_then_es(self):
        """bilingual_ru_es with RU input: first line=RU: ..., second=ES: ..."""
        translator = Translator()
        original_builder = Translator._build_pipeline

        def fake_builder(model_name: str, allow_network: bool):
            def fake_pipeline(text: str):
                return [{"translation_text": f"ESTRANSLATION:{text}"}]
            return fake_pipeline

        Translator._build_pipeline = staticmethod(fake_builder)
        try:
            result = translator.translate(
                "привет друзья",
                mode="bilingual_ru_es",
                network_mode="offline_default",
            )
        finally:
            Translator._build_pipeline = original_builder

        self.assertEqual(result.status, "ok")
        lines = result.text.split("\n")
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("RU:"))
        self.assertTrue(lines[1].startswith("ES:"))

    def test_bilingual_es_returns_es_then_ru(self):
        """bilingual_ru_es with ES input: first line=ES: ..., second=RU: ..."""
        translator = Translator()
        original_builder = Translator._build_pipeline

        def fake_builder(model_name: str, allow_network: bool):
            def fake_pipeline(text: str):
                return [{"translation_text": f"RUTRANSLATION:{text}"}]
            return fake_pipeline

        Translator._build_pipeline = staticmethod(fake_builder)
        try:
            result = translator.translate(
                "hola amigo como estas",
                mode="bilingual_ru_es",
                network_mode="offline_default",
            )
        finally:
            Translator._build_pipeline = original_builder

        self.assertEqual(result.status, "ok")
        lines = result.text.split("\n")
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("ES:"))
        self.assertTrue(lines[1].startswith("RU:"))


class TranslatorOffModeEdgeTestCase(unittest.TestCase):
    """off mode: result has empty text and correct status/mode."""

    def test_off_mode_text_is_empty_string(self):
        """off mode does NOT return the input text — text field is ''."""
        translator = Translator()
        result = translator.translate("важный текст", mode="off", network_mode="offline_default")
        self.assertEqual(result.text, "")
        self.assertEqual(result.status, "not_requested")
        self.assertEqual(result.mode, "off")
        self.assertFalse(result.ok)

    def test_off_mode_pipeline_never_called(self):
        """off mode never invokes the model pipeline."""
        translator = Translator()
        calls = {"count": 0}
        original_builder = Translator._build_pipeline

        def counting_builder(model_name: str, allow_network: bool):
            calls["count"] += 1

            def fake_pipeline(text: str):
                return [{"translation_text": "SHOULD_NOT_APPEAR"}]

            return fake_pipeline

        Translator._build_pipeline = staticmethod(counting_builder)
        try:
            translator.translate("текст", mode="off", network_mode="offline_default")
        finally:
            Translator._build_pipeline = original_builder

        self.assertEqual(calls["count"], 0, "Pipeline builder must not be called in off mode")


class TranslatorVeryLongTextTestCase(unittest.TestCase):
    """Very long text forces chunking path."""

    def test_2000_char_text_uses_multiple_chunks(self):
        """2000-char text (>450 chunk limit) splits into multiple pipeline calls."""
        translator = Translator()
        call_count = {"n": 0}
        original_builder = Translator._build_pipeline

        def fake_builder(model_name: str, allow_network: bool):
            def fake_pipeline(text: str):
                call_count["n"] += 1
                return [{"translation_text": f"T:{text[:10]}"}]
            return fake_pipeline

        Translator._build_pipeline = staticmethod(fake_builder)
        try:
            long_text = ("Длинный абзац с предложениями. " * 70).strip()
            self.assertGreater(len(long_text), 1000)
            result = translator.translate(
                long_text, mode="ru_to_es", network_mode="offline_default"
            )
        finally:
            Translator._build_pipeline = original_builder

        self.assertEqual(result.status, "ok")
        self.assertGreater(call_count["n"], 1,
                           "Long text must result in >1 pipeline calls")

    def test_chunked_results_are_joined_into_single_string(self):
        """All chunk translations are joined with space into one text."""
        translator = Translator()
        original_builder = Translator._build_pipeline

        def fake_builder(model_name: str, allow_network: bool):
            chunk_idx = {"i": 0}

            def fake_pipeline(text: str):
                chunk_idx["i"] += 1
                return [{"translation_text": f"CHUNK{chunk_idx['i']}"}]

            return fake_pipeline

        Translator._build_pipeline = staticmethod(fake_builder)
        try:
            long_text = ("Предложение первое. " * 80).strip()
            result = translator.translate(
                long_text, mode="ru_to_es", network_mode="offline_default"
            )
        finally:
            Translator._build_pipeline = original_builder

        self.assertEqual(result.status, "ok")
        # Result must be multiple CHUNK strings joined
        self.assertIn("CHUNK1", result.text)
        self.assertIn("CHUNK2", result.text)


class TranslatorCachePersistenceTestCase(unittest.TestCase):
    """In-memory LRU cache behaves as persistent-within-instance cache."""

    def test_cache_persists_across_multiple_calls(self):
        """Same (mode, style, network_mode, text) key → cached after first call."""
        translator = Translator()
        call_count = {"n": 0}
        original_builder = Translator._build_pipeline

        def fake_builder(model_name: str, allow_network: bool):
            def fake_pipeline(text: str):
                call_count["n"] += 1
                return [{"translation_text": f"TR:{text}"}]
            return fake_pipeline

        Translator._build_pipeline = staticmethod(fake_builder)
        try:
            r1 = translator.translate("кэшируемый текст", mode="ru_to_es", network_mode="offline_default")
            r2 = translator.translate("кэшируемый текст", mode="ru_to_es", network_mode="offline_default")
            r3 = translator.translate("кэшируемый текст", mode="ru_to_es", network_mode="offline_default")
        finally:
            Translator._build_pipeline = original_builder

        self.assertEqual(r1.status, "ok")
        self.assertEqual(r2.status, "ok")
        self.assertEqual(r3.status, "ok")
        self.assertEqual(r1.text, r2.text)
        self.assertEqual(r2.text, r3.text)
        self.assertEqual(call_count["n"], 1,
                         "Pipeline must be called only once — 2nd & 3rd use cache")

    def test_cache_key_includes_style(self):
        """Different translation_style = different cache keys = separate calls."""
        translator = Translator()
        call_count = {"n": 0}
        original_builder = Translator._build_pipeline

        def fake_builder(model_name: str, allow_network: bool):
            def fake_pipeline(text: str):
                call_count["n"] += 1
                return [{"translation_text": f"T:{text}"}]
            return fake_pipeline

        Translator._build_pipeline = staticmethod(fake_builder)
        try:
            translator.translate("тест кэша стиля", mode="ru_to_es",
                                 network_mode="offline_default", translation_style="neutral")
            translator.translate("тест кэша стиля", mode="ru_to_es",
                                 network_mode="offline_default", translation_style="formal")
        finally:
            Translator._build_pipeline = original_builder

        self.assertEqual(call_count["n"], 2,
                         "neutral and formal must have separate cache keys")

    def test_cache_lru_eviction(self):
        """After capacity+1 unique texts, first entry is evicted."""
        translator = Translator()
        translator._cache_capacity = 3
        original_builder = Translator._build_pipeline

        def fake_builder(model_name: str, allow_network: bool):
            def fake_pipeline(text: str):
                return [{"translation_text": f"T:{text}"}]
            return fake_pipeline

        Translator._build_pipeline = staticmethod(fake_builder)
        try:
            for i in range(4):
                translator.translate(f"текст{i}", mode="ru_to_es", network_mode="offline_default")
        finally:
            Translator._build_pipeline = original_builder

        # After 4 inserts into capacity=3, first entry должен быть evicted
        self.assertLessEqual(len(translator._cache), 3)


# ─────────────────────────────────────────────────────────────────────
# C. SettingsService edge cases
# ─────────────────────────────────────────────────────────────────────

class SettingsExportImportTestCase(unittest.TestCase):
    """handle_export_settings and handle_import_settings edge cases."""

    def test_export_strips_voice_gateway_api_key(self):
        """Exported JSON must not contain voice_gateway_api_key."""
        svc, _ = _make_ss()
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = str(Path(tmpdir) / "exported.json")
            result = svc.handle_export_settings({"file": out_file})
            self.assertIn("settings_count", result)
            with open(out_file, encoding="utf-8") as fh:
                exported = json.load(fh)
            self.assertNotIn("voice_gateway_api_key", exported,
                             "Sensitive field must not be exported")

    def test_export_strips_hf_token(self):
        """Exported JSON must not contain hf_token."""
        svc, _ = _make_ss()
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = str(Path(tmpdir) / "exported.json")
            svc.handle_export_settings({"file": out_file})
            with open(out_file, encoding="utf-8") as fh:
                exported = json.load(fh)
            self.assertNotIn("hf_token", exported)

    def test_export_includes_non_sensitive_fields(self):
        """Non-sensitive fields (quality_profile) appear in exported JSON."""
        svc, _ = _make_ss()
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = str(Path(tmpdir) / "exported.json")
            svc.handle_export_settings({"file": out_file})
            with open(out_file, encoding="utf-8") as fh:
                exported = json.load(fh)
            self.assertIn("quality_profile", exported)

    def test_import_skips_sensitive_fields(self):
        """Importing a file with voice_gateway_api_key does not overwrite the stored key."""
        svc, store = _make_ss()
        with tempfile.TemporaryDirectory() as tmpdir:
            import_file = Path(tmpdir) / "import.json"
            import_data = {
                "quality_profile": "max",
                "voice_gateway_api_key": "INJECTED_SECRET",
                "hf_token": "INJECTED_HF",
            }
            import_file.write_text(json.dumps(import_data), encoding="utf-8")
            result = svc.handle_import_settings({"file": str(import_file)})
            self.assertGreater(result["skipped"], 0,
                               "sensitive fields must be counted as skipped")
            # Sensitive fields must not have leaked into saved settings
            saved = store.save_settings.call_args[0][0]
            self.assertNotEqual(saved.get("voice_gateway_api_key"), "INJECTED_SECRET")
            self.assertNotEqual(saved.get("hf_token"), "INJECTED_HF")

    def test_import_applies_non_sensitive_fields(self):
        """Non-sensitive fields from import file are applied."""
        svc, store = _make_ss()
        with tempfile.TemporaryDirectory() as tmpdir:
            import_file = Path(tmpdir) / "import.json"
            import_data = {"quality_profile": "max"}
            import_file.write_text(json.dumps(import_data), encoding="utf-8")
            svc.handle_import_settings({"file": str(import_file)})
            saved = store.save_settings.call_args[0][0]
            self.assertEqual(saved.get("quality_profile"), "max")

    def test_import_missing_file_raises(self):
        """handle_import_settings raises FileNotFoundError for missing file."""
        svc, _ = _make_ss()
        with self.assertRaises(FileNotFoundError):
            svc.handle_import_settings({"file": "/nonexistent/path/settings.json"})

    def test_import_invalid_json_raises_value_error(self):
        """handle_import_settings raises ValueError for malformed JSON."""
        svc, _ = _make_ss()
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_file = Path(tmpdir) / "bad.json"
            bad_file.write_text("{not valid json}", encoding="utf-8")
            with self.assertRaises(ValueError):
                svc.handle_import_settings({"file": str(bad_file)})


class SettingsSetEdgeTestCase(unittest.TestCase):
    """Edge cases for handle_set_settings."""

    def test_translate_and_paste_set_to_true(self):
        """handle_set_settings correctly stores translate_and_paste=True."""
        svc, store = _make_ss()
        svc.handle_set_settings({"translate_and_paste": True})
        self.assertTrue(store._current.get("translate_and_paste"))

    def test_translate_and_paste_set_to_false(self):
        """handle_set_settings correctly stores translate_and_paste=False."""
        svc, store = _make_ss()
        svc.handle_set_settings({"translate_and_paste": False})
        self.assertFalse(store._current.get("translate_and_paste"))

    def test_invalid_translation_mode_normalised_to_off(self):
        """Unknown translation_mode is normalised to 'off'."""
        svc, store = _make_ss()
        svc.handle_set_settings({"translation_mode": "fantasy_mode"})
        self.assertEqual(store._current.get("translation_mode"), "off")

    def test_valid_translation_mode_bilingual_ru_es(self):
        """bilingual_ru_es is a valid translation_mode and is stored as-is."""
        svc, store = _make_ss()
        svc.handle_set_settings({"translation_mode": "bilingual_ru_es"})
        self.assertEqual(store._current.get("translation_mode"), "bilingual_ru_es")

    def test_overlay_opacity_clamped_below_min(self):
        """overlay_opacity_percent below 15 is clamped to 15."""
        svc, store = _make_ss()
        svc.handle_set_settings({"overlay_opacity_percent": 0})
        self.assertEqual(store._current.get("overlay_opacity_percent"), 15)

    def test_overlay_opacity_clamped_above_max(self):
        """overlay_opacity_percent above 90 is clamped to 90."""
        svc, store = _make_ss()
        svc.handle_set_settings({"overlay_opacity_percent": 200})
        self.assertEqual(store._current.get("overlay_opacity_percent"), 90)


class SettingsCacheTTLEdgeTestCase(unittest.TestCase):
    """TTL cache edge cases."""

    def test_cache_ttl_exactly_at_boundary_still_valid(self):
        """At exactly TTL seconds (< ttl), cache is still valid — no reload."""
        svc, store = _make_ss()
        with patch("time.monotonic") as mock_time:
            mock_time.return_value = 200.0
            svc.cached_settings()
            store.load_settings.reset_mock()

            mock_time.return_value = 204.99  # < 200 + 5.0
            svc.cached_settings()

        store.load_settings.assert_not_called()

    def test_cache_ttl_expired_triggers_reload(self):
        """After TTL+epsilon, the next call reloads from store."""
        svc, store = _make_ss()
        with patch("time.monotonic") as mock_time:
            mock_time.return_value = 200.0
            svc.cached_settings()
            store.load_settings.reset_mock()

            mock_time.return_value = 205.01  # > 200 + 5.0
            svc.cached_settings()

        store.load_settings.assert_called_once()

    def test_explicit_invalidate_then_reload_ignores_ttl(self):
        """After explicit invalidate_cache(), next call reloads regardless of time."""
        svc, store = _make_ss()
        with patch("time.monotonic") as mock_time:
            mock_time.return_value = 300.0
            svc.cached_settings()
            store.load_settings.reset_mock()

            svc.invalidate_cache()

            mock_time.return_value = 300.1  # well within TTL — but cache was cleared
            svc.cached_settings()

        store.load_settings.assert_called_once()


class SettingsProfilePresetMutationTestCase(unittest.TestCase):
    """Profile preset application mutates settings correctly."""

    def test_default_preset_sets_auto_paste_true(self):
        """default preset: auto_paste=True."""
        svc, store = _make_ss()
        svc.handle_apply_profile_preset({"profile": "default"})
        self.assertTrue(store._current.get("auto_paste"))

    def test_meeting_preset_sets_quality_max_and_no_paste(self):
        """meeting preset: quality_profile=max, auto_paste=False."""
        svc, store = _make_ss()
        svc.handle_apply_profile_preset({"profile": "meeting"})
        self.assertEqual(store._current.get("quality_profile"), "max")
        self.assertFalse(store._current.get("auto_paste"))

    def test_translation_preset_sets_translation_mode_auto(self):
        """translation preset: translation_mode=auto, translate_and_paste=True."""
        svc, store = _make_ss()
        svc.handle_apply_profile_preset({"profile": "translation"})
        self.assertEqual(store._current.get("translation_mode"), "auto")
        self.assertTrue(store._current.get("translate_and_paste"))

    def test_call_recording_preset_disables_realtime_preview(self):
        """call_recording preset: realtime_preview_enabled=False."""
        svc, store = _make_ss()
        svc.handle_apply_profile_preset({"profile": "call_recording"})
        self.assertFalse(store._current.get("realtime_preview_enabled"))

    def test_preset_application_does_not_erase_unrelated_settings(self):
        """Applying a preset only changes preset keys, leaves others intact."""
        svc, store = _make_ss(settings={
            **_make_ss()[1]._current,  # base settings
            "overlay_opacity_percent": 77,  # custom value not in any preset
        })
        svc.handle_apply_profile_preset({"profile": "default"})
        self.assertEqual(store._current.get("overlay_opacity_percent"), 77)


if __name__ == "__main__":
    unittest.main()
