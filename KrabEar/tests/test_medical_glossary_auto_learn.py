"""Unit tests for GlossaryAutoLearn and GlossaryAutoLearnService."""

import sys
import os
import unittest
from typing import Any, Dict

# Добавляем PROJECT_ROOT в sys.path для standalone-запуска
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from backend.glossary_auto_learn import (
    GlossaryAutoLearn,
    GlossaryAutoLearnService,
    GlossarySuggestion,
    _MEDICAL_KEYWORDS_RU,
    _MEDICAL_KEYWORDS_ES,
)


# ── Вспомогательные фикстуры ────────────────────────────────────────────────


def _make_item(
    source_text: str = "",
    translated_text: str = "",
) -> Dict[str, Any]:
    return {
        "source_text": source_text,
        "translated_text": translated_text,
    }


# ── Тесты GlossaryAutoLearn ─────────────────────────────────────────────────


class TestGlossaryAutoLearnEmpty(unittest.TestCase):
    """Пустая история → пустой список предложений."""

    def setUp(self):
        self.learner = GlossaryAutoLearn()

    def test_empty_items_returns_empty_list(self):
        result = self.learner.suggest(items=[])
        self.assertEqual(result, [])

    def test_items_without_translated_text_returns_empty(self):
        items = [
            {"source_text": "боль в животе", "translated_text": ""},
            {"source_text": "лечение", "translated_text": None},
        ]
        result = self.learner.suggest(items=items)
        self.assertEqual(result, [])


class TestGlossaryAutoLearnFrequency(unittest.TestCase):
    """Термины встречающиеся менее 2 раз не предлагаются."""

    def setUp(self):
        self.learner = GlossaryAutoLearn()

    def test_single_occurrence_skipped(self):
        """Пара появляется только 1 раз → не предлагается (freq < 2)."""
        items = [
            _make_item(
                source_text="антибиотик amoxicillin назначен",
                translated_text="antibiótico amoxicillin recetado",
            ),
        ]
        result = self.learner.suggest(items=items)
        self.assertEqual(result, [])

    def test_two_occurrences_included(self):
        """Пара встречается 2 раза → предлагается."""
        items = [
            _make_item("диагноз бронхит поставлен", "diagnóstico bronquitis establecido"),
            _make_item("повторный диагноз бронхит", "segundo diagnóstico bronquitis"),
        ]
        result = self.learner.suggest(items=items)
        terms = [s.source_term for s in result]
        self.assertTrue(
            any("диагноз" in t or "bronquitis" in t or "diagnóstico" in t for t in terms),
            f"Ожидался хотя бы один повторяющийся термин, получено: {terms}",
        )


class TestGlossaryAutoLearnMedicalDomain(unittest.TestCase):
    """Медицинские ключевые слова → домен 'medical'."""

    def setUp(self):
        self.learner = GlossaryAutoLearn()

    def test_medical_keywords_classified_as_medical(self):
        """Записи с медицинскими словами → domain='medical'."""
        items = [
            _make_item(
                "пациент принимает антибиотик амоксициллин",
                "paciente toma antibiótico amoxicilina",
            ),
            _make_item(
                "пациент принимает антибиотик амоксициллин",
                "paciente toma antibiótico amoxicilina",
            ),
        ]
        result = self.learner.suggest(items=items)
        medical = [s for s in result if s.domain == "medical"]
        self.assertTrue(len(medical) > 0, "Ожидались термины с domain='medical'")

    def test_non_medical_context_classified_as_general(self):
        """Записи без мед. слов → domain='general'."""
        items = [
            _make_item(
                "программирование компьютер интерфейс",
                "programación computadora interfaz",
            ),
            _make_item(
                "программирование компьютер интерфейс",
                "programación computadora interfaz",
            ),
        ]
        result = self.learner.suggest(items=items)
        if result:
            general = [s for s in result if s.domain == "general"]
            self.assertTrue(
                len(general) > 0,
                f"Ожидались термины с domain='general', получено: {[s.domain for s in result]}",
            )


class TestGlossaryAutoLearnExistingGlossary(unittest.TestCase):
    """Термины уже в глоссарии → пропускаются."""

    def setUp(self):
        self.learner = GlossaryAutoLearn()

    def test_already_in_glossary_skipped(self):
        """Если source_term уже в existing_glossary → не предлагается."""
        items = [
            _make_item(
                "пациент антибиотик назначение",
                "paciente antibiótico prescripción",
            ),
            _make_item(
                "пациент антибиотик назначение",
                "paciente antibiótico prescripción",
            ),
        ]
        # Допустим "антибиотик" → "antibiótico" уже в глоссарии
        existing = {"антибиотик": "antibiótico"}
        result = self.learner.suggest(items=items, existing_glossary=existing)
        for s in result:
            self.assertNotEqual(
                s.source_term, "антибиотик",
                "Уже существующий термин не должен предлагаться",
            )

    def test_empty_glossary_all_qualify(self):
        """Пустой глоссарий → все повторяющиеся пары квалифицируются."""
        items = [
            _make_item("симптомы болезни температура", "síntomas enfermedad temperatura"),
            _make_item("симптомы болезни температура", "síntomas enfermedad temperatura"),
        ]
        result = self.learner.suggest(items=items, existing_glossary={})
        self.assertGreater(len(result), 0)


class TestGlossaryAutoLearnLimit(unittest.TestCase):
    """Параметр limit ограничивает количество результатов."""

    def setUp(self):
        self.learner = GlossaryAutoLearn()

    def test_limit_applied(self):
        """limit=2 → не более 2 предложений."""
        # Создаём много пар с высокой частотой
        base_items = [
            _make_item(
                "симптомы болезнь лечение препарат анализ рецепт",
                "síntomas enfermedad tratamiento medicamento análisis receta",
            )
        ] * 5  # каждая запись повторяется 5 раз
        result = self.learner.suggest(items=base_items, limit=2)
        self.assertLessEqual(len(result), 2)


class TestGlossarySuggestionStructure(unittest.TestCase):
    """GlossarySuggestion содержит нужные поля."""

    def test_suggestion_fields(self):
        s = GlossarySuggestion(
            id="test",
            source_term="test",
            target_term="prueba",
            frequency=3,
            domain="medical",
            confidence=0.75,
        )
        self.assertEqual(s.id, "test")
        self.assertEqual(s.source_term, "test")
        self.assertEqual(s.target_term, "prueba")
        self.assertEqual(s.frequency, 3)
        self.assertEqual(s.domain, "medical")
        self.assertAlmostEqual(s.confidence, 0.75)


class TestMedicalKeywords(unittest.TestCase):
    """Базовые проверки медицинских словарей."""

    def test_ru_keywords_non_empty(self):
        self.assertGreater(len(_MEDICAL_KEYWORDS_RU), 10)

    def test_es_keywords_non_empty(self):
        self.assertGreater(len(_MEDICAL_KEYWORDS_ES), 10)

    def test_ru_has_expected_words(self):
        self.assertIn("врач", _MEDICAL_KEYWORDS_RU)
        self.assertIn("лечение", _MEDICAL_KEYWORDS_RU)
        self.assertIn("диагноз", _MEDICAL_KEYWORDS_RU)

    def test_es_has_expected_words(self):
        self.assertIn("médico", _MEDICAL_KEYWORDS_ES)
        self.assertIn("tratamiento", _MEDICAL_KEYWORDS_ES)
        self.assertIn("diagnóstico", _MEDICAL_KEYWORDS_ES)


# ── Тесты GlossaryAutoLearnService ─────────────────────────────────────────


class FakeStore:
    """Минимальный stub для StateStore."""

    def __init__(self, items=None, settings=None):
        self._items = items or []
        self._settings = settings or {}

    def get_history_page(self, cursor=None, limit=500):
        return self._items, None

    def save_settings(self, settings):
        self._settings = dict(settings)
        return self._settings


class TestGlossaryAutoLearnServiceHandlers(unittest.TestCase):
    """IPC-обработчики GlossaryAutoLearnService."""

    def _make_service(self, items=None, settings=None):
        settings = settings or {}
        store = FakeStore(items=items, settings=settings)
        svc = GlossaryAutoLearnService(
            store=store,
            cached_settings=lambda: dict(store._settings),
            invalidate_settings_cache=lambda: None,
        )
        svc._store = store
        return svc, store

    def test_suggest_empty_history(self):
        svc, _ = self._make_service(items=[])
        result = svc.handle_suggest_medical_glossary_terms({})
        self.assertIn("suggestions", result)
        self.assertEqual(result["suggestions"], [])

    def test_suggest_returns_list(self):
        items = [
            {
                "source_text": "пациент принимает лекарство антибиотик",
                "translated_text": "paciente toma medicamento antibiótico",
            },
            {
                "source_text": "пациент принимает лекарство антибиотик",
                "translated_text": "paciente toma medicamento antibiótico",
            },
        ]
        svc, _ = self._make_service(items=items)
        result = svc.handle_suggest_medical_glossary_terms({"limit": 10})
        self.assertIn("suggestions", result)
        self.assertIsInstance(result["suggestions"], list)

    def test_apply_empty_selected_ids(self):
        svc, _ = self._make_service()
        result = svc.handle_apply_glossary_suggestions({"selected_ids": []})
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["skipped"], 0)

    def test_apply_adds_to_glossary(self):
        svc, store = self._make_service(settings={"translation_glossary": {}})
        suggestions = [
            {"source_term": "diagnóstico", "target_term": "диагноз"},
            {"source_term": "tratamiento", "target_term": "лечение"},
        ]
        result = svc.handle_apply_glossary_suggestions({
            "selected_ids": ["diagnóstico", "tratamiento"],
            "suggestions": suggestions,
        })
        self.assertEqual(result["applied"], 2)
        self.assertEqual(result["skipped"], 0)
        self.assertGreaterEqual(result["total_glossary"], 2)

    def test_apply_skips_missing_target(self):
        svc, _ = self._make_service(settings={"translation_glossary": {}})
        # selected_id без соответствующего target_term в suggestions
        result = svc.handle_apply_glossary_suggestions({
            "selected_ids": ["неизвестный"],
            "suggestions": [],
        })
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
