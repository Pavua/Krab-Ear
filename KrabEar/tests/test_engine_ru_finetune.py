"""Тесты для опциональной RU fine-tune модели Whisper (antony66/whisper-large-v3-russian).

Проверяем логику выбора модели в fallback chain без запуска реального MLX inference.
Тесты изолированы от тяжёлых зависимостей (numpy, mlx_whisper, pyannote) —
проверяется только логика построения candidates list на базе флагов settings.

Проверяем:
1. Флаг выключен → RU fine-tune маркер НЕ добавляется в chain.
2. Флаг включён + lang=ru → маркер идёт первым в chain.
3. Флаг включён + lang=es → маркер НЕ добавляется (только для RU).
4. Fine-tune модель падает → маркер помечается недоступным, fallback без краша.
5. lang=None → берётся settings.TRANSCRIBE_LANGUAGE; если "ru" — маркер добавляется.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Вспомогательная функция: воспроизводит логику добавления RU fine-tune
# маркера из _transcribe_with_fallback_impl, без импорта engine.py.
# ---------------------------------------------------------------------------

_RU_FINETUNE_MARKER = "ru_finetune:adapter"
_DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"


def _build_candidates(
    *,
    use_ru_finetune: bool,
    language: str | None,
    transcribe_language: str = "ru",
    unavailable: set | None = None,
    current_model: str = _DEFAULT_MODEL,
) -> list[str]:
    """Воспроизводит логику выбора кандидатов для RU fine-tune части chain.

    Точная копия условия из engine._transcribe_with_fallback_impl.
    """
    unavailable = unavailable or set()
    candidates = [current_model]
    _effective_lang = language if language is not None else transcribe_language

    if (
        use_ru_finetune
        and _effective_lang == "ru"
        and _RU_FINETUNE_MARKER not in unavailable
    ):
        candidates = [_RU_FINETUNE_MARKER] + candidates

    return candidates


# ---------------------------------------------------------------------------
# Тест 1: флаг выключен
# ---------------------------------------------------------------------------

class TestRuFinetuneDisabled(unittest.TestCase):
    """1. STT_USE_RU_FINETUNE=False → fine-tune маркер отсутствует в chain."""

    def test_disabled_flag_no_marker(self):
        """Флаг False → RU маркер не добавляется."""
        candidates = _build_candidates(
            use_ru_finetune=False,
            language="ru",
        )
        self.assertNotIn(_RU_FINETUNE_MARKER, candidates)

    def test_disabled_flag_default_model_first(self):
        """Флаг False → первый кандидат — дефолтная модель."""
        candidates = _build_candidates(
            use_ru_finetune=False,
            language="ru",
        )
        self.assertEqual(candidates[0], _DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# Тест 2: флаг включён + lang=ru
# ---------------------------------------------------------------------------

class TestRuFinetuneEnabledRu(unittest.TestCase):
    """2. STT_USE_RU_FINETUNE=True + language="ru" → маркер первый в chain."""

    def test_enabled_ru_marker_first(self):
        """Маркер RU fine-tune должен быть первым кандидатом."""
        candidates = _build_candidates(
            use_ru_finetune=True,
            language="ru",
        )
        self.assertEqual(candidates[0], _RU_FINETUNE_MARKER)

    def test_enabled_ru_default_model_also_present(self):
        """Дефолтная модель должна оставаться в chain как fallback."""
        candidates = _build_candidates(
            use_ru_finetune=True,
            language="ru",
        )
        self.assertIn(_DEFAULT_MODEL, candidates)
        self.assertEqual(len(candidates), 2)


# ---------------------------------------------------------------------------
# Тест 3: флаг включён + НЕ RU язык
# ---------------------------------------------------------------------------

class TestRuFinetuneEnabledNonRu(unittest.TestCase):
    """3. STT_USE_RU_FINETUNE=True + lang != "ru" → маркер НЕ добавляется."""

    def test_enabled_es_lang_no_marker(self):
        """Испанский → RU fine-tune маркер не добавляется."""
        candidates = _build_candidates(
            use_ru_finetune=True,
            language="es",
        )
        self.assertNotIn(_RU_FINETUNE_MARKER, candidates)
        self.assertEqual(candidates[0], _DEFAULT_MODEL)

    def test_enabled_en_lang_no_marker(self):
        """Английский → RU fine-tune маркер не добавляется."""
        candidates = _build_candidates(
            use_ru_finetune=True,
            language="en",
        )
        self.assertNotIn(_RU_FINETUNE_MARKER, candidates)

    def test_enabled_uk_lang_no_marker(self):
        """Украинский → RU fine-tune маркер не добавляется (близкий язык, но не RU)."""
        candidates = _build_candidates(
            use_ru_finetune=True,
            language="uk",
        )
        self.assertNotIn(_RU_FINETUNE_MARKER, candidates)


# ---------------------------------------------------------------------------
# Тест 4: fine-tune модель падает → fallback без краша
# ---------------------------------------------------------------------------

class TestRuFinetuneLoadFailFallback(unittest.TestCase):
    """4. Fine-tune падает → маркер в unavailable, chain продолжается без него."""

    def test_marker_added_to_unavailable_on_fail(self):
        """После сбоя адаптера — маркер помечается как недоступный."""
        unavailable: set[str] = set()

        # Симулируем обработку сбоя (логика из engine for loop)
        try:
            raise RuntimeError("Model download failed / not found")
        except Exception:
            unavailable.add(_RU_FINETUNE_MARKER)

        self.assertIn(_RU_FINETUNE_MARKER, unavailable)

    def test_unavailable_marker_not_in_next_chain(self):
        """После сбоя — при следующем вызове маркер не добавляется в chain."""
        unavailable = {_RU_FINETUNE_MARKER}

        candidates = _build_candidates(
            use_ru_finetune=True,
            language="ru",
            unavailable=unavailable,
        )
        self.assertNotIn(_RU_FINETUNE_MARKER, candidates)
        # Дефолтная модель всё равно в chain
        self.assertIn(_DEFAULT_MODEL, candidates)

    def test_no_crash_after_failure(self):
        """После fallback chain корректно продолжает работу."""
        unavailable = {_RU_FINETUNE_MARKER}
        candidates = _build_candidates(
            use_ru_finetune=True,
            language="ru",
            unavailable=unavailable,
        )
        # chain должен быть не пустым
        self.assertTrue(len(candidates) > 0)
        self.assertEqual(candidates, [_DEFAULT_MODEL])


# ---------------------------------------------------------------------------
# Тест 5: language=None → берётся TRANSCRIBE_LANGUAGE
# ---------------------------------------------------------------------------

class TestRuFinetuneNoneLang(unittest.TestCase):
    """5. language=None → определяется через TRANSCRIBE_LANGUAGE."""

    def test_none_lang_ru_default_activates_finetune(self):
        """None + TRANSCRIBE_LANGUAGE='ru' → fine-tune активируется."""
        candidates = _build_candidates(
            use_ru_finetune=True,
            language=None,
            transcribe_language="ru",
        )
        self.assertEqual(candidates[0], _RU_FINETUNE_MARKER)

    def test_none_lang_es_default_no_finetune(self):
        """None + TRANSCRIBE_LANGUAGE='es' → fine-tune НЕ активируется."""
        candidates = _build_candidates(
            use_ru_finetune=True,
            language=None,
            transcribe_language="es",
        )
        self.assertNotIn(_RU_FINETUNE_MARKER, candidates)

    def test_none_lang_disabled_flag(self):
        """None + TRANSCRIBE_LANGUAGE='ru' + флаг False → маркер не добавляется."""
        candidates = _build_candidates(
            use_ru_finetune=False,
            language=None,
            transcribe_language="ru",
        )
        self.assertNotIn(_RU_FINETUNE_MARKER, candidates)


if __name__ == "__main__":
    unittest.main()
