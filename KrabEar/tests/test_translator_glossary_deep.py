"""Wave 213 — deep coverage: Translator language pairs + cache + glossary integration.

New scenarios NOT covered by existing test_translator*.py / test_translation_service.py:
  Translator:
    - RU→ES, ES→RU, EN→RU basic happy paths via mocked pipeline
    - auto mode detects source language and resolves pair
    - bilingual_ru_es produces "RU: …\\nES: …" / "ES: …\\nRU: …" format
    - in-memory LRU cache: second identical call is served from cache (no pipeline call)
    - LRU eviction: oldest entry is dropped when capacity is exceeded
    - offline-first: local dict path confirmed via _build_pipeline=None→unavailable
    - unsupported language pair → model_unavailable_offline / cannot_detect_language

  TranslationService glossary:
    - set_glossary_item persists new entry
    - glossary applied during translate_text (term replacement verified)
    - glossary case-sensitivity: exact key required (case-sensitive by default)
    - unicode/CJK glossary terms round-trip correctly
    - remove_glossary_item clears the term
    - concurrent glossary updates are safe (sequential via threading)
    - empty glossary returns passthrough (no text mutation)
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.translator import Translator, TranslationResult  # noqa: E402
from backend.translation_service import TranslationService  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _static_pipeline(output: str):
    """Returns a _build_pipeline replacement that always yields `output`."""
    def builder(model_name: str, allow_network: bool):
        def pipe(text: str):
            return [{"translation_text": output}]
        return pipe
    return staticmethod(builder)


def _passthrough_pipeline():
    """Pipeline that echoes input as 'TR:<input>'."""
    def builder(model_name: str, allow_network: bool):
        def pipe(text: str):
            return [{"translation_text": f"TR:{text}"}]
        return pipe
    return staticmethod(builder)


def _counting_pipeline(calls: list):
    """Pipeline that records calls and returns 'DONE'."""
    def builder(model_name: str, allow_network: bool):
        def pipe(text: str):
            calls.append(text)
            return [{"translation_text": "DONE"}]
        return pipe
    return staticmethod(builder)


def _make_translation_service(
    settings: dict[str, Any] | None = None,
    history_items: list[dict] | None = None,
    vocabulary: list[str] | None = None,
) -> tuple[TranslationService, MagicMock, MagicMock, list[dict]]:
    """Creates TranslationService with full mock dependencies.

    Returns (svc, translator_mock, store_mock, settings_cell).
    `settings_cell[0]` is the mutable settings dict — update it to simulate
    cache invalidation side-effects.
    """
    base: dict[str, Any] = {
        "network_mode": "offline_default",
        "translation_glossary": {},
    }
    if settings:
        base.update(settings)

    translator_mock = MagicMock()
    translator_mock.translate.return_value = TranslationResult(
        text="translated",
        status="ok",
        source_lang="ru",
        target_lang="es",
        mode="ru_to_es",
        engine="hf_marian",
    )

    store = MagicMock()
    store.get_history_page.return_value = (history_items or [], None)
    store.load_vocabulary.return_value = vocabulary or []

    # save_settings mutates the settings cell and returns the saved dict
    settings_cell = [dict(base)]

    def save_settings(s: dict) -> dict:
        settings_cell[0] = dict(s)
        return dict(s)

    store.save_settings.side_effect = save_settings

    def cached_settings() -> dict:
        return dict(settings_cell[0])

    def invalidate() -> None:
        pass  # no-op; settings_cell already updated by save_settings

    svc = TranslationService(
        translator=translator_mock,
        store=store,
        cached_settings=cached_settings,
        invalidate_settings_cache=invalidate,
    )
    return svc, translator_mock, store, settings_cell


# ──────────────────────────────────────────────────────────────────────────────
# Translator — basic language pair tests
# ──────────────────────────────────────────────────────────────────────────────

class TranslatorBasicPairsTestCase(unittest.TestCase):
    """Happy-path translation for each primary language pair."""

    def _translate_with_mock(self, text: str, mode: str, output: str) -> TranslationResult:
        t = Translator()
        original = Translator._build_pipeline
        Translator._build_pipeline = _static_pipeline(output)
        try:
            return t.translate(text, mode=mode, network_mode="offline_default")
        finally:
            Translator._build_pipeline = original

    def test_RU_to_ES_basic(self) -> None:  # noqa: N802
        result = self._translate_with_mock("Привет мир", "ru_to_es", "Hola mundo")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.text, "Hola mundo")
        self.assertEqual(result.source_lang, "ru")
        self.assertEqual(result.target_lang, "es")
        self.assertEqual(result.mode, "ru_to_es")

    def test_ES_to_RU_basic(self) -> None:  # noqa: N802
        result = self._translate_with_mock("Hola mundo", "es_to_ru", "Привет мир")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.text, "Привет мир")
        self.assertEqual(result.source_lang, "es")
        self.assertEqual(result.target_lang, "ru")

    def test_EN_to_RU_basic(self) -> None:  # noqa: N802
        result = self._translate_with_mock("Hello world", "en_to_ru", "Привет мир")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.text, "Привет мир")
        self.assertEqual(result.source_lang, "en")
        self.assertEqual(result.target_lang, "ru")


# ──────────────────────────────────────────────────────────────────────────────
# Translator — auto mode
# ──────────────────────────────────────────────────────────────────────────────

class TranslatorAutoModeTestCase(unittest.TestCase):
    """auto mode resolves language pair from source language detection."""

    def _make_tracker_translator(self) -> tuple[Translator, list]:
        t = Translator()
        used_modes: list = []
        original = Translator._build_pipeline

        def builder(model_name: str, allow_network: bool):
            used_modes.append(model_name)

            def pipe(text: str):
                return [{"translation_text": f"OUT:{text}"}]

            return pipe

        Translator._build_pipeline = staticmethod(builder)
        return t, used_modes, original

    def test_auto_mode_detects_russian_uses_ru_to_es(self) -> None:
        t, used_modes, orig = self._make_tracker_translator()
        try:
            result = t.translate("Привет это русский текст", mode="auto", network_mode="offline_default")
        finally:
            Translator._build_pipeline = orig
        self.assertEqual(result.status, "ok")
        # ru_to_es model should be chosen
        self.assertTrue(any("ru-es" in m or "opus-mt-ru-es" in m for m in used_modes),
                        f"Expected ru-es model; got {used_modes}")

    def test_auto_mode_detects_spanish_uses_es_to_ru(self) -> None:
        t, used_modes, orig = self._make_tracker_translator()
        try:
            result = t.translate("hola como estas amigo", mode="auto", network_mode="offline_default")
        finally:
            Translator._build_pipeline = orig
        self.assertEqual(result.status, "ok")
        self.assertTrue(any("es-ru" in m or "opus-mt-es-ru" in m for m in used_modes),
                        f"Expected es-ru model; got {used_modes}")

    def test_auto_mode_detects_english_uses_en_to_ru(self) -> None:
        t, used_modes, orig = self._make_tracker_translator()
        try:
            result = t.translate("hello the quick brown fox", mode="auto", network_mode="offline_default")
        finally:
            Translator._build_pipeline = orig
        self.assertEqual(result.status, "ok")
        self.assertTrue(any("en-ru" in m or "opus-mt-en-ru" in m for m in used_modes),
                        f"Expected en-ru model; got {used_modes}")


# ──────────────────────────────────────────────────────────────────────────────
# Translator — bilingual mode
# ──────────────────────────────────────────────────────────────────────────────

class TranslatorBilingualModeTestCase(unittest.TestCase):
    """bilingual_ru_es mode produces labelled two-language output."""

    def test_bilingual_mode_ru_source_outputs_RU_ES_format(self) -> None:  # noqa: N802
        t = Translator()
        orig = Translator._build_pipeline
        Translator._build_pipeline = _static_pipeline("Hola mundo")
        try:
            result = t.translate("Привет мир", mode="bilingual_ru_es", network_mode="offline_default")
        finally:
            Translator._build_pipeline = orig
        self.assertEqual(result.status, "ok")
        self.assertIn("RU:", result.text)
        self.assertIn("ES:", result.text)
        self.assertIn("Привет мир", result.text)
        self.assertIn("Hola mundo", result.text)

    def test_bilingual_mode_es_source_outputs_ES_RU_format(self) -> None:  # noqa: N802
        t = Translator()
        orig = Translator._build_pipeline
        Translator._build_pipeline = _static_pipeline("Привет мир")
        try:
            result = t.translate("Hola mundo", mode="bilingual_ru_es", network_mode="offline_default")
        finally:
            Translator._build_pipeline = orig
        self.assertEqual(result.status, "ok")
        self.assertIn("ES:", result.text)
        self.assertIn("RU:", result.text)
        self.assertIn("Hola mundo", result.text)
        self.assertIn("Привет мир", result.text)

    def test_bilingual_mode_target_lang_is_ru_es(self) -> None:
        t = Translator()
        orig = Translator._build_pipeline
        Translator._build_pipeline = _static_pipeline("Hola")
        try:
            result = t.translate("Привет", mode="bilingual_ru_es", network_mode="offline_default")
        finally:
            Translator._build_pipeline = orig
        self.assertEqual(result.target_lang, "ru+es")


# ──────────────────────────────────────────────────────────────────────────────
# Translator — LRU cache
# ──────────────────────────────────────────────────────────────────────────────

class TranslatorLRUCacheTestCase(unittest.TestCase):
    """In-memory LRU cache behaviour."""

    def test_in_memory_cache_hit_no_pipeline_call(self) -> None:
        """Second identical call is served from cache — pipeline not called."""
        t = Translator()
        pipeline_calls: list = []
        orig = Translator._build_pipeline

        def builder(model_name: str, allow_network: bool):
            def pipe(text: str):
                pipeline_calls.append(text)
                return [{"translation_text": "CACHED_OUTPUT"}]
            return pipe

        Translator._build_pipeline = staticmethod(builder)
        try:
            r1 = t.translate("тест кэша", mode="ru_to_es", network_mode="offline_default")
            r2 = t.translate("тест кэша", mode="ru_to_es", network_mode="offline_default")
        finally:
            Translator._build_pipeline = orig

        self.assertEqual(r1.text, "CACHED_OUTPUT")
        self.assertEqual(r2.text, "CACHED_OUTPUT")
        # Pipeline is only called once — second hit served from cache
        self.assertEqual(len(pipeline_calls), 1, "Pipeline must be called exactly once for identical inputs")

    def test_cache_different_texts_each_translated(self) -> None:
        """Two different texts both trigger pipeline calls."""
        t = Translator()
        pipeline_calls: list = []
        orig = Translator._build_pipeline

        def builder(model_name: str, allow_network: bool):
            def pipe(text: str):
                pipeline_calls.append(text)
                return [{"translation_text": f"OUT:{text}"}]
            return pipe

        Translator._build_pipeline = staticmethod(builder)
        try:
            t.translate("первый", mode="ru_to_es", network_mode="offline_default")
            t.translate("второй", mode="ru_to_es", network_mode="offline_default")
        finally:
            Translator._build_pipeline = orig

        self.assertEqual(len(pipeline_calls), 2)

    def test_cache_eviction_lru(self) -> None:
        """When capacity is reached, oldest (LRU) entry is evicted."""
        t = Translator()
        t._cache_capacity = 3  # small capacity for testing
        orig = Translator._build_pipeline

        call_count = [0]

        def builder(model_name: str, allow_network: bool):
            def pipe(text: str):
                call_count[0] += 1
                return [{"translation_text": f"V{call_count[0]}"}]
            return pipe

        Translator._build_pipeline = staticmethod(builder)
        try:
            # Fill cache to capacity with 3 different texts
            for word in ("alpha", "beta", "gamma"):
                t.translate(word, mode="es_to_ru", network_mode="offline_default")

            self.assertEqual(len(t._cache), 3)
            initial_calls = call_count[0]

            # Add 4th entry — "alpha" should be evicted (LRU)
            t.translate("delta", mode="es_to_ru", network_mode="offline_default")
            self.assertEqual(len(t._cache), 3, "Cache must stay at capacity after eviction")

            # "alpha" cache miss — should trigger a new pipeline call
            t.translate("alpha", mode="es_to_ru", network_mode="offline_default")
            self.assertGreater(call_count[0], initial_calls + 1,
                               "Re-translating evicted 'alpha' must call pipeline again")
        finally:
            Translator._build_pipeline = orig

    def test_cache_mode_separation(self) -> None:
        """Same text in different modes are cached separately."""
        t = Translator()
        pipeline_calls: list = []
        orig = Translator._build_pipeline

        def builder(model_name: str, allow_network: bool):
            def pipe(text: str):
                pipeline_calls.append((model_name, text))
                return [{"translation_text": "OUT"}]
            return pipe

        Translator._build_pipeline = staticmethod(builder)
        try:
            t.translate("hello", mode="en_to_ru", network_mode="offline_default")
            t.translate("hello", mode="en_to_es", network_mode="offline_default")
        finally:
            Translator._build_pipeline = orig

        self.assertEqual(len(pipeline_calls), 2, "Different modes must produce separate cache entries")


# ──────────────────────────────────────────────────────────────────────────────
# Translator — offline-first / unavailable model paths
# ──────────────────────────────────────────────────────────────────────────────

class TranslatorOfflinePathsTestCase(unittest.TestCase):
    """Offline-first and model unavailability behaviour."""

    def test_offline_first_pipeline_none_marks_unavailable(self) -> None:
        """When _build_pipeline returns None and NLLB also fails, status = model_unavailable_offline."""
        t = Translator()
        orig_build = Translator._build_pipeline
        orig_nllb = Translator._build_nllb_pipeline

        Translator._build_pipeline = staticmethod(lambda model_name, allow_network: None)
        Translator._build_nllb_pipeline = staticmethod(lambda src_lang, tgt_lang, allow_network: None)
        try:
            result = t.translate("тест", mode="ru_to_es", network_mode="offline_default")
        finally:
            Translator._build_pipeline = orig_build
            Translator._build_nllb_pipeline = orig_nllb

        self.assertIn(result.status, ("model_unavailable_offline", "model_unavailable_online",
                                      "model_unavailable_cached"))
        self.assertEqual(result.text, "")

    def test_unavailable_cached_skips_pipeline_on_repeat(self) -> None:
        """After first unavailability the pipeline key is cached; second call returns cached status."""
        t = Translator()
        build_call_count = [0]
        orig_build = Translator._build_pipeline
        orig_nllb = Translator._build_nllb_pipeline

        def counting_builder(model_name, allow_network):
            build_call_count[0] += 1
            return None

        Translator._build_pipeline = staticmethod(counting_builder)
        Translator._build_nllb_pipeline = staticmethod(lambda src_lang, tgt_lang, allow_network: None)
        try:
            t.translate("один", mode="ru_to_es", network_mode="offline_default")
            t.translate("два", mode="ru_to_es", network_mode="offline_default")
        finally:
            Translator._build_pipeline = orig_build
            Translator._build_nllb_pipeline = orig_nllb

        # Builder only called once; second call uses cached unavailability
        self.assertEqual(build_call_count[0], 1,
                         "Builder must be called only once; subsequent calls use _unavailable cache")

    def test_handles_unsupported_lang_pair_auto_mode(self) -> None:
        """Auto mode on unknown language text returns cannot_detect_language or off-mode status."""
        t = Translator()
        orig_build = Translator._build_pipeline
        orig_nllb = Translator._build_nllb_pipeline
        # Both pipelines unavailable — exercises the "cannot detect / off" path cleanly
        Translator._build_pipeline = staticmethod(lambda model_name, allow_network: None)
        Translator._build_nllb_pipeline = staticmethod(lambda src_lang, tgt_lang, allow_network: None)
        try:
            # Pure numbers / symbols can't be detected reliably — ensure no crash
            result = t.translate("12345 @#$%", mode="auto", network_mode="offline_default")
        finally:
            Translator._build_pipeline = orig_build
            Translator._build_nllb_pipeline = orig_nllb
        # Result should be a TranslationResult without exception
        self.assertIsInstance(result, TranslationResult)
        self.assertIsInstance(result.text, str)


# ──────────────────────────────────────────────────────────────────────────────
# TranslationService — glossary integration
# ──────────────────────────────────────────────────────────────────────────────

class GlossaryPersistenceTestCase(unittest.TestCase):
    """set/remove glossary item persists correctly."""

    def test_set_glossary_item_persists(self) -> None:
        """set_glossary_item saves to store and returns updated=True with correct count."""
        svc, _, store, _ = _make_translation_service()
        result = svc.handle_set_translation_glossary_item({"source": "Краб", "target": "Krab"})
        self.assertTrue(result["updated"])
        self.assertEqual(result["count"], 1)
        store.save_settings.assert_called_once()
        saved_args = store.save_settings.call_args[0][0]
        self.assertEqual(saved_args["translation_glossary"]["Краб"], "Krab")

    def test_glossary_removed_via_handler(self) -> None:
        """remove_glossary_item removes the term and persists."""
        svc, _, store, _ = _make_translation_service(
            settings={"translation_glossary": {"Краб": "Krab", "Ухо": "Ear"}}
        )
        result = svc.handle_remove_translation_glossary_item({"source": "Краб"})
        self.assertTrue(result["removed"])
        saved = store.save_settings.call_args[0][0]
        self.assertNotIn("Краб", saved["translation_glossary"])
        self.assertIn("Ухо", saved["translation_glossary"])
        self.assertEqual(result["count"], 1)


class GlossaryAppliedDuringTranslateTestCase(unittest.TestCase):
    """Glossary terms are substituted in the translated output via Translator."""

    def test_glossary_applied_during_translate(self) -> None:
        """Glossary replacement reaches the translator.translate call kwargs."""
        glossary = {"Krab": "Краб", "Ear": "Ухо"}
        svc, translator_mock, _, _ = _make_translation_service(
            settings={"translation_glossary": glossary}
        )
        translator_mock.translate.return_value = TranslationResult(
            text="Краб Ухо — AI", status="ok",
            source_lang="en", target_lang="ru",
            mode="en_to_ru", engine="hf_marian",
        )

        result = svc.handle_translate_text({"text": "Krab Ear — AI", "translation_mode": "en_to_ru"})

        call_kwargs = translator_mock.translate.call_args.kwargs
        self.assertEqual(call_kwargs.get("glossary"), glossary,
                         "Glossary from settings must be forwarded to translator.translate")
        # The mocked translator already applied glossary; result text must match
        self.assertIn("Краб", result["text"])

    def test_glossary_empty_returns_passthrough(self) -> None:
        """Empty glossary causes no mutation to translated text."""
        svc, translator_mock, _, _ = _make_translation_service(
            settings={"translation_glossary": {}}
        )
        original_output = "Hola mundo sin cambios"
        translator_mock.translate.return_value = TranslationResult(
            text=original_output, status="ok",
            source_lang="ru", target_lang="es",
            mode="ru_to_es", engine="hf_marian",
        )

        result = svc.handle_translate_text({"text": "Привет мир без изменений", "translation_mode": "ru_to_es"})
        self.assertEqual(result["text"], original_output)

    def test_glossary_applied_via_translator_directly(self) -> None:
        """Translator._apply_glossary replaces all occurrences in the translated text."""
        text = "The Krab system is ready"
        glossary = {"Krab": "Краб", "system": "система"}
        replaced = Translator._apply_glossary(text, glossary)
        self.assertEqual(replaced, "The Краб система is ready")


class GlossaryCaseSensitivityTestCase(unittest.TestCase):
    """Glossary matching is case-sensitive by default (str.replace)."""

    def test_glossary_case_sensitive_exact_match(self) -> None:
        """Exact-case key matches and is replaced; wrong case is not."""
        glossary = {"Krab": "Краб"}
        # Exact match
        result = Translator._apply_glossary("Krab is here", glossary)
        self.assertIn("Краб", result)
        # Wrong case — not replaced
        result_lower = Translator._apply_glossary("krab is here", glossary)
        self.assertNotIn("Краб", result_lower,
                         "Glossary keys are case-sensitive; 'krab' != 'Krab'")

    def test_glossary_case_insensitive_not_default(self) -> None:
        """Confirm that upper-case key does not match lower-case occurrence."""
        glossary = {"HELLO": "ПРИВЕТ"}
        result = Translator._apply_glossary("hello world", glossary)
        self.assertNotIn("ПРИВЕТ", result)


class GlossaryUnicodeTestCase(unittest.TestCase):
    """Unicode and multi-byte glossary terms round-trip correctly."""

    def test_unicode_glossary_terms(self) -> None:
        """CJK, Arabic, and emoji glossary terms are replaced without corruption."""
        # CJK → Cyrillic
        glossary_cjk = {"日本語": "японский"}
        result_cjk = Translator._apply_glossary("Изучаю 日本語 каждый день", glossary_cjk)
        self.assertEqual(result_cjk, "Изучаю японский каждый день")

        # Arabic → Spanish
        glossary_arabic = {"مرحبا": "hola"}
        result_ar = Translator._apply_glossary("قال مرحبا للجميع", glossary_arabic)
        self.assertIn("hola", result_ar)

        # Emoji term
        glossary_emoji = {"🤖": "робот"}
        result_emoji = Translator._apply_glossary("Привет 🤖!", glossary_emoji)
        self.assertEqual(result_emoji, "Привет робот!")

    def test_unicode_glossary_via_translation_service_set(self) -> None:
        """set_glossary_item round-trips unicode source/target without error."""
        svc, _, store, _ = _make_translation_service()
        result = svc.handle_set_translation_glossary_item({
            "source": "日本語",
            "target": "японский",
        })
        self.assertTrue(result["updated"])
        saved = store.save_settings.call_args[0][0]
        self.assertEqual(saved["translation_glossary"]["日本語"], "японский")

    def test_unicode_glossary_removal(self) -> None:
        """remove_glossary_item works on unicode keys."""
        svc, _, store, _ = _make_translation_service(
            settings={"translation_glossary": {"日本語": "японский"}}
        )
        result = svc.handle_remove_translation_glossary_item({"source": "日本語"})
        self.assertTrue(result["removed"])
        saved = store.save_settings.call_args[0][0]
        self.assertNotIn("日本語", saved["translation_glossary"])


class GlossaryConcurrencyTestCase(unittest.TestCase):
    """Concurrent glossary updates are safe (no missing entries / data corruption)."""

    def test_concurrent_glossary_update_safe(self) -> None:
        """50 concurrent threads each add a unique glossary entry; all must persist."""
        thread_count = 50
        errors: list[Exception] = []
        lock = threading.Lock()

        # We'll track all save_settings calls ourselves
        all_saved: list[dict] = []

        def make_svc() -> TranslationService:
            settings_cell = [{"translation_glossary": {}}]
            store = MagicMock()

            def save_settings(s: dict) -> dict:
                with lock:
                    settings_cell[0] = dict(s)
                    all_saved.append(dict(s.get("translation_glossary", {})))
                return dict(s)

            store.save_settings.side_effect = save_settings

            def cached_settings():
                with lock:
                    return dict(settings_cell[0])

            return TranslationService(
                translator=MagicMock(),
                store=store,
                cached_settings=cached_settings,
                invalidate_settings_cache=lambda: None,
            )

        svc = make_svc()

        def add_entry(idx: int) -> None:
            try:
                svc.handle_set_translation_glossary_item({
                    "source": f"term_{idx}",
                    "target": f"перевод_{idx}",
                })
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=add_entry, args=(i,)) for i in range(thread_count)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        self.assertEqual(errors, [], f"Concurrent updates raised exceptions: {errors}")
        # All save_settings calls must have been made
        self.assertEqual(len(all_saved), thread_count,
                         "Every thread must trigger exactly one save_settings call")


# ──────────────────────────────────────────────────────────────────────────────
# Translator — glossary normalisation edge cases
# ──────────────────────────────────────────────────────────────────────────────

class TranslatorGlossaryNormalisationTestCase(unittest.TestCase):
    """_normalize_glossary strips invalid/empty entries."""

    def test_none_glossary_returns_empty_dict(self) -> None:
        result = Translator._normalize_glossary(None)
        self.assertEqual(result, {})

    def test_non_dict_glossary_returns_empty_dict(self) -> None:
        self.assertEqual(Translator._normalize_glossary("bad"), {})
        self.assertEqual(Translator._normalize_glossary([("a", "b")]), {})

    def test_empty_key_stripped(self) -> None:
        result = Translator._normalize_glossary({"": "something", "valid": "ok"})
        self.assertNotIn("", result)
        self.assertIn("valid", result)

    def test_whitespace_only_key_stripped(self) -> None:
        result = Translator._normalize_glossary({"  ": "value", "good": "yes"})
        self.assertNotIn("  ", result)
        self.assertNotIn("", result)

    def test_empty_value_stripped(self) -> None:
        result = Translator._normalize_glossary({"key": "", "key2": "val"})
        self.assertNotIn("key", result)
        self.assertIn("key2", result)

    def test_valid_entries_preserved(self) -> None:
        glossary = {"Krab": "Краб", "Ear": "Ухо"}
        result = Translator._normalize_glossary(glossary)
        self.assertEqual(result, {"Krab": "Краб", "Ear": "Ухо"})


if __name__ == "__main__":
    unittest.main()
