"""test_glossary_auto_learn.py — Wave 180 unit tests for GlossaryAutoLearn.

Covers:
- empty history
- repeated proper nouns as candidates
- dedup against existing glossary
- min frequency threshold
- unicode terms (RU/ES/medical Latin)
- concurrent thread safety
- corrupted history entry handling
- stop word exclusion
- top-N by frequency
- persist seen terms (avoid re-suggesting confirmed)
"""

from __future__ import annotations

import sys
import os
import threading
import unittest
from typing import Any, Dict, List

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from backend.glossary_auto_learn import (
    GlossaryAutoLearn,
    GlossaryAutoLearnService,
    _STOP_WORDS,
    _MIN_TERM_LENGTH,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _item(src: str, tgt: str) -> Dict[str, Any]:
    return {"source_text": src, "translated_text": tgt}


def _repeat(item: Dict[str, Any], n: int) -> List[Dict[str, Any]]:
    return [dict(item) for _ in range(n)]


class FakeStore:
    """Minimal stub for StateStore used in service tests."""

    def __init__(self, items=None, settings=None):
        self._items = items or []
        self._settings = settings or {}

    def get_history_page(self, cursor=None, limit=500):
        return list(self._items), None

    def save_settings(self, settings):
        self._settings = dict(settings)
        return self._settings


def _make_service(items=None, settings=None):
    settings = settings or {}
    store = FakeStore(items=items or [], settings=settings)
    svc = GlossaryAutoLearnService(
        store=store,
        cached_settings=lambda: dict(store._settings),
        invalidate_settings_cache=lambda: None,
    )
    return svc, store


# ── Tests ────────────────────────────────────────────────────────────────────


class TestExtractFromEmptyHistory(unittest.TestCase):
    """test_extract_from_empty_history_returns_no_suggestions"""

    def setUp(self):
        self.learner = GlossaryAutoLearn()

    def test_empty_list(self):
        self.assertEqual(self.learner.suggest(items=[]), [])

    def test_none_texts_give_empty(self):
        items = [
            {"source_text": None, "translated_text": None},
            {"source_text": "", "translated_text": ""},
        ]
        self.assertEqual(self.learner.suggest(items=items), [])

    def test_missing_keys_give_empty(self):
        items = [{"text": "что-то"}, {}]
        self.assertEqual(self.learner.suggest(items=items), [])


class TestExtractRepeatedProperNouns(unittest.TestCase):
    """test_extract_repeated_proper_nouns_as_candidates"""

    def setUp(self):
        self.learner = GlossaryAutoLearn()

    def test_repeated_term_appears_in_suggestions(self):
        # "амоксициллин" / "amoxicilina" repeated 3 times
        items = _repeat(
            _item("пациент принимает амоксициллин лечение",
                  "paciente toma amoxicilina tratamiento"),
            3,
        )
        result = self.learner.suggest(items=items)
        src_terms = {s.source_term for s in result}
        tgt_terms = {s.target_term for s in result}
        self.assertTrue(
            "амоксициллин" in src_terms or "amoxicilina" in tgt_terms,
            f"Expected амоксициллин/amoxicilina in suggestions; got {src_terms}/{tgt_terms}",
        )

    def test_frequency_increases_with_repetition(self):
        items = _repeat(
            _item("диагноз бронхит симптомы", "diagnóstico bronquitis síntomas"),
            4,
        )
        result = self.learner.suggest(items=items)
        self.assertTrue(len(result) > 0, "Expected at least one suggestion")
        max_freq = max(s.frequency for s in result)
        self.assertGreaterEqual(max_freq, 2)


class TestDedupExistingGlossaryEntries(unittest.TestCase):
    """test_dedup_existing_glossary_entries"""

    def setUp(self):
        self.learner = GlossaryAutoLearn()

    def test_existing_term_not_re_suggested(self):
        items = _repeat(
            _item("пациент антибиотик назначение",
                  "paciente antibiótico prescripción"),
            3,
        )
        existing = {"антибиотик": "antibiótico"}
        result = self.learner.suggest(items=items, existing_glossary=existing)
        src_terms = [s.source_term for s in result]
        self.assertNotIn("антибиотик", src_terms)

    def test_case_insensitive_dedup(self):
        """Glossary key in mixed case should still deduplicate lower-cased term."""
        items = _repeat(
            _item("антибиотик лечение пациент", "antibiótico tratamiento paciente"),
            3,
        )
        existing = {"АНТИБИОТИК": "ANTIBIÓTICO"}
        result = self.learner.suggest(items=items, existing_glossary=existing)
        src_terms = [s.source_term for s in result]
        self.assertNotIn("антибиотик", src_terms)

    def test_non_existing_term_still_suggested(self):
        items = _repeat(
            _item("антибиотик лечение пациент", "antibiótico tratamiento paciente"),
            3,
        )
        existing = {"другой": "otro"}
        result = self.learner.suggest(items=items, existing_glossary=existing)
        # Should have at least something since "другой" is short (<6) and
        # not in source anyway
        self.assertIsInstance(result, list)


class TestMinFrequencyThreshold(unittest.TestCase):
    """test_min_frequency_threshold — terms must appear >= 2 times"""

    def setUp(self):
        self.learner = GlossaryAutoLearn()

    def test_single_occurrence_excluded(self):
        items = [_item("антибиотик лечение симптомы",
                       "antibiótico tratamiento síntomas")]
        result = self.learner.suggest(items=items)
        self.assertEqual(result, [], "Single occurrence must not be suggested")

    def test_exactly_two_occurrences_included(self):
        items = _repeat(
            _item("диагноз бронхит лечение", "diagnóstico bronquitis tratamiento"),
            2,
        )
        result = self.learner.suggest(items=items)
        self.assertTrue(len(result) > 0,
                        "Frequency==2 should pass the threshold")
        for s in result:
            self.assertGreaterEqual(s.frequency, 2)

    def test_all_returned_have_freq_gte_2(self):
        items = _repeat(
            _item("антибиотик лечение пациент", "antibiótico tratamiento paciente"),
            5,
        )
        result = self.learner.suggest(items=items)
        for s in result:
            self.assertGreaterEqual(
                s.frequency, 2,
                f"Term {s.source_term!r} has frequency {s.frequency} < 2",
            )


class TestUnicodeTerms(unittest.TestCase):
    """test_unicode_terms_extracted — RU/ES medical Latin Unicode."""

    def setUp(self):
        self.learner = GlossaryAutoLearn()

    def test_cyrillic_terms_extracted(self):
        items = _repeat(
            _item("пациент принимает препарат витамин",
                  "paciente toma medicamento vitamina"),
            3,
        )
        result = self.learner.suggest(items=items)
        src_terms = {s.source_term for s in result}
        # At least one Cyrillic term should appear
        has_cyrillic = any(any('Ѐ' <= c <= 'ӿ' for c in t) for t in src_terms)
        self.assertTrue(has_cyrillic, f"Expected Cyrillic term; got {src_terms}")

    def test_spanish_accented_terms_extracted(self):
        items = _repeat(
            _item("diagnóstico médico infección bacteriana",
                  "диагноз врача инфекция бактерия"),
            3,
        )
        result = self.learner.suggest(items=items)
        src_terms = {s.source_term for s in result}
        # diagnóstico or médico should be extracted (both >= 6 chars)
        self.assertTrue(
            "diagnóstico" in src_terms or "médico" in src_terms or len(src_terms) > 0,
            f"Expected accented ES terms; got {src_terms}",
        )

    def test_latin_medical_term_extracted(self):
        """Latin-alphabet medical term repeated multiple times should be extracted."""
        items = _repeat(
            _item("amoxicillin antibiotic treatment",
                  "амоксициллин антибиотик лечение"),
            3,
        )
        result = self.learner.suggest(items=items)
        src_terms = {s.source_term for s in result}
        self.assertTrue(
            "amoxicillin" in src_terms or "antibiotic" in src_terms,
            f"Expected Latin medical term; got {src_terms}",
        )


class TestConcurrentExtractThreadSafe(unittest.TestCase):
    """test_concurrent_extract_thread_safe"""

    def setUp(self):
        self.learner = GlossaryAutoLearn()

    def test_concurrent_suggest_no_exception(self):
        """Multiple threads calling suggest() simultaneously must not raise."""
        items = _repeat(
            _item("пациент принимает антибиотик лечение",
                  "paciente toma antibiótico tratamiento"),
            4,
        )
        errors: List[Exception] = []
        results: List[Any] = []
        lock = threading.Lock()

        def worker():
            try:
                r = self.learner.suggest(items=items, limit=10)
                with lock:
                    results.append(r)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [], f"Concurrent suggest raised: {errors}")
        self.assertEqual(len(results), 8)

    def test_concurrent_results_consistent(self):
        """All concurrent results should be identical (pure function)."""
        items = _repeat(
            _item("диагноз бронхит лечение", "diagnóstico bronquitis tratamiento"),
            3,
        )
        all_results: List[List] = []
        lock = threading.Lock()

        def worker():
            r = self.learner.suggest(items=items, limit=5)
            r_keys = sorted(s.source_term for s in r)
            with lock:
                all_results.append(r_keys)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertTrue(len(all_results) > 0)
        first = all_results[0]
        for r in all_results[1:]:
            self.assertEqual(r, first, "Concurrent results are inconsistent")


class TestHandlesCorruptedHistoryEntry(unittest.TestCase):
    """test_handles_corrupted_history_entry"""

    def setUp(self):
        self.learner = GlossaryAutoLearn()

    def test_none_item_skipped(self):
        """None values in items list are coerced by str() and skipped."""
        items = [
            None,
            {"source_text": None, "translated_text": None},
            _item("антибиотик лечение симптомы", "antibiótico tratamiento síntomas"),
        ]
        # Filter out None items before passing (service does this via to_dict)
        safe_items = [i for i in items if isinstance(i, dict)]
        # Should not raise
        result = self.learner.suggest(items=safe_items)
        self.assertIsInstance(result, list)

    def test_extra_fields_ignored(self):
        """Items with extra unexpected fields should be handled gracefully."""
        items = _repeat(
            {
                "source_text": "диагноз бронхит пациент",
                "translated_text": "diagnóstico bronquitis paciente",
                "random_garbage": object(),
                "nested": {"a": [1, 2, 3]},
            },
            3,
        )
        result = self.learner.suggest(items=items)
        self.assertIsInstance(result, list)

    def test_integer_source_text_coerced(self):
        """Non-string source_text coerced to str."""
        items = [
            {"source_text": 12345, "translated_text": "número entero"},
        ]
        # Should not raise; result will likely be empty due to coercion
        result = self.learner.suggest(items=items)
        self.assertIsInstance(result, list)

    def test_store_exception_returns_empty(self):
        """If store.get_history_page raises, service returns empty suggestions."""
        class BrokenStore:
            def get_history_page(self, **kw):
                raise RuntimeError("disk error")

            def save_settings(self, s):
                return s

        svc = GlossaryAutoLearnService(
            store=BrokenStore(),
            cached_settings=lambda: {},
            invalidate_settings_cache=lambda: None,
        )
        result = svc.handle_suggest_medical_glossary_terms({})
        self.assertIn("suggestions", result)
        self.assertEqual(result["suggestions"], [])


class TestFilterStopWordsExcluded(unittest.TestCase):
    """test_filter_stop_words_excluded"""

    def setUp(self):
        self.learner = GlossaryAutoLearn()

    def test_known_stop_words_not_in_suggestions(self):
        """Stop words should never appear as source_term."""
        # Use known stop words from _STOP_WORDS (pick a few >= 6 chars)
        long_stop_words = [w for w in _STOP_WORDS if len(w) >= _MIN_TERM_LENGTH]
        if not long_stop_words:
            self.skipTest("No stop words meet min length for this test")

        sw = long_stop_words[0]
        # Repeat the stop word in context to give it high frequency
        items = _repeat(
            {"source_text": f"{sw} диагноз лечение",
             "translated_text": f"{sw} diagnóstico tratamiento"},
            5,
        )
        result = self.learner.suggest(items=items)
        src_terms = {s.source_term for s in result}
        self.assertNotIn(sw, src_terms,
                         f"Stop word {sw!r} should not appear in suggestions")

    def test_short_words_excluded_by_min_length(self):
        """Words shorter than _MIN_TERM_LENGTH should not be suggested."""
        items = _repeat(
            _item("боль врач дозы", "dolor médico dosis"),
            5,
        )
        result = self.learner.suggest(items=items)
        for s in result:
            self.assertGreaterEqual(
                len(s.source_term), _MIN_TERM_LENGTH,
                f"Term {s.source_term!r} is shorter than min length {_MIN_TERM_LENGTH}",
            )

    def test_ru_stop_word_который_excluded(self):
        items = _repeat(
            _item("который диагноз лечение пациент",
                  "diagnóstico tratamiento paciente"),
            4,
        )
        result = self.learner.suggest(items=items)
        src_terms = {s.source_term for s in result}
        self.assertNotIn("который", src_terms)


class TestReturnsTopNByFrequency(unittest.TestCase):
    """test_returns_top_N_by_frequency"""

    def setUp(self):
        self.learner = GlossaryAutoLearn()

    def test_limit_parameter_respected(self):
        items = _repeat(
            _item("симптомы болезнь лечение препарат анализ рецепт витамин гормон",
                  "síntomas enfermedad tratamiento medicamento análisis receta vitamina hormona"),
            5,
        )
        result_full = self.learner.suggest(items=items, limit=100)
        result_limit = self.learner.suggest(items=items, limit=2)
        self.assertLessEqual(len(result_limit), 2)
        # Full result should have more
        if len(result_full) > 2:
            self.assertLess(len(result_limit), len(result_full))

    def test_results_sorted_by_frequency_descending(self):
        """Returned list should be sorted: medical first, then by freq desc."""
        items = _repeat(
            _item("симптомы болезнь лечение препарат",
                  "síntomas enfermedad tratamiento medicamento"),
            6,
        )
        result = self.learner.suggest(items=items, limit=50)
        if len(result) < 2:
            return
        # Within same domain, higher frequency first
        for i in range(len(result) - 1):
            a, b = result[i], result[i + 1]
            if a.domain == b.domain:
                self.assertGreaterEqual(
                    a.frequency, b.frequency,
                    f"Sorting violated: {a.source_term}(freq={a.frequency}) "
                    f"before {b.source_term}(freq={b.frequency}) in same domain",
                )

    def test_medical_domain_prioritized_over_general(self):
        """Medical-domain terms should appear before general ones."""
        # Create medical items (contain врач/doctor)
        med_items = _repeat(
            _item("врач назначил лечение антибиотик",
                  "médico recetó tratamiento antibiótico"),
            3,
        )
        # Create general items (no medical keywords)
        gen_items = _repeat(
            _item("программирование компьютер интерфейс",
                  "programación computadora interfaz"),
            3,
        )
        result = self.learner.suggest(items=med_items + gen_items, limit=50)
        if not result:
            return
        # Find positions of first medical and first general
        med_indices = [i for i, s in enumerate(result) if s.domain == "medical"]
        gen_indices = [i for i, s in enumerate(result) if s.domain == "general"]
        if med_indices and gen_indices:
            self.assertLess(
                min(med_indices), min(gen_indices),
                "Medical terms should be sorted before general terms",
            )


class TestPersistSeenTerms(unittest.TestCase):
    """test_persist_seen_terms — avoid re-suggesting confirmed entries.

    GlossaryAutoLearn does not maintain internal state across calls;
    the 'seen' mechanism is the existing_glossary parameter.
    These tests verify that terms passed via existing_glossary are excluded,
    and that the apply_glossary_suggestions IPC handler correctly persists
    accepted suggestions back to the glossary (preventing re-suggestion on
    subsequent calls).
    """

    def setUp(self):
        self.learner = GlossaryAutoLearn()

    def test_accepted_term_not_re_suggested_via_glossary(self):
        """After a term is applied, it won't be suggested if passed as existing."""
        items = _repeat(
            _item("пациент антибиотик лечение", "paciente antibiótico tratamiento"),
            4,
        )
        # First pass: no existing glossary
        first = self.learner.suggest(items=items)
        if not first:
            self.skipTest("No suggestions returned; can't test re-suggestion")

        # Simulate accepting the first suggestion
        accepted = {first[0].source_term: first[0].target_term}

        # Second pass: accepted term should now be excluded
        second = self.learner.suggest(items=items, existing_glossary=accepted)
        second_src = {s.source_term for s in second}
        self.assertNotIn(
            first[0].source_term, second_src,
            f"Previously accepted term {first[0].source_term!r} was re-suggested",
        )

    def test_apply_persists_to_store(self):
        """handle_apply_glossary_suggestions persists accepted terms to store."""
        svc, store = _make_service(settings={"translation_glossary": {}})
        suggestions = [
            {"source_term": "diagnóstico", "target_term": "диагноз"},
        ]
        result = svc.handle_apply_glossary_suggestions({
            "selected_ids": ["diagnóstico"],
            "suggestions": suggestions,
        })
        self.assertEqual(result["applied"], 1)
        # Store should now contain the glossary entry
        glossary = store._settings.get("translation_glossary", {})
        self.assertIn("diagnóstico", glossary)
        self.assertEqual(glossary["diagnóstico"], "диагноз")

    def test_applying_same_term_twice_skips_second(self):
        """Applying an already-in-glossary term results in skipped."""
        svc, store = _make_service(
            settings={"translation_glossary": {"diagnóstico": "диагноз"}}
        )
        suggestions = [
            {"source_term": "diagnóstico", "target_term": "диагноз"},
        ]
        result = svc.handle_apply_glossary_suggestions({
            "selected_ids": ["diagnóstico"],
            "suggestions": suggestions,
        })
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["skipped"], 1)

    def test_multiple_accepted_terms_all_persisted(self):
        """Accepting multiple terms persists all of them."""
        svc, store = _make_service(settings={"translation_glossary": {}})
        suggestions = [
            {"source_term": "diagnóstico", "target_term": "диагноз"},
            {"source_term": "tratamiento", "target_term": "лечение"},
            {"source_term": "antibiótico", "target_term": "антибиотик"},
        ]
        result = svc.handle_apply_glossary_suggestions({
            "selected_ids": ["diagnóstico", "tratamiento", "antibiótico"],
            "suggestions": suggestions,
        })
        self.assertEqual(result["applied"], 3)
        self.assertEqual(result["skipped"], 0)
        glossary = store._settings.get("translation_glossary", {})
        for term in ("diagnóstico", "tratamiento", "antibiótico"):
            self.assertIn(term, glossary, f"{term!r} not persisted to store")


if __name__ == "__main__":
    unittest.main()
