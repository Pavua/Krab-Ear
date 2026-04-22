"""Property-based tests for TextUtils (KrabEar/core/utils.py).

Проверяет инварианты методов очистки транскрипций через рандомные входы
с использованием библиотеки Hypothesis.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_text_utils_property.py -v
"""

import sys
import os
import unicodedata

# Убеждаемся, что KrabEar/ в PYTHONPATH
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
KRAB_EAR_ROOT = os.path.join(os.path.dirname(__file__), "..")
for p in (PROJECT_ROOT, KRAB_EAR_ROOT):
    if p not in sys.path:
        sys.path.insert(0, os.path.abspath(p))

import unittest

try:
    from hypothesis import given, settings, HealthCheck
    from hypothesis import strategies as st
    from core.utils import TextUtils
    _IMPORT_ERROR: Exception | None = None
except (ImportError, OSError) as _err:
    _IMPORT_ERROR = _err

    def given(*_a, **_kw):  # type: ignore[no-redef]
        return lambda fn: fn

    def settings(*_a, **_kw):  # type: ignore[no-redef]
        return lambda fn: fn

    class _HealthCheckMeta(type):
        def __getattr__(cls, _name):
            return None

    class HealthCheck(metaclass=_HealthCheckMeta):  # type: ignore[no-redef]
        pass

    class _ChainableShim:
        def __call__(self, *_a, **_kw):
            return self

        def __getattr__(self, _name):
            return self

    st = _ChainableShim()  # type: ignore[assignment]
    TextUtils = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Стратегии
# ---------------------------------------------------------------------------

# Реалистичный Unicode — буквы, цифры, пробелы, пунктуация; без суррогатов
_SAFE_TEXT = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),  # исключаем суррогатные пары
        blacklist_characters="\x00",   # нулевой байт
    ),
    min_size=0,
    max_size=2000,
)

# Только ASCII — быстрее для boundary-проверок
_ASCII_TEXT = st.text(
    alphabet=st.characters(
        min_codepoint=32,
        max_codepoint=126,
    ),
    min_size=0,
    max_size=2000,
)

# Текст с кириллицей и латиницей (имитирует реальные транскрипции)
_TRANSCRIPT_TEXT = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd", "Zs", "Po"),
        whitelist_characters=" \n\t.,!?-:;()…",
    ),
    min_size=0,
    max_size=2000,
)

_PROFILES = st.sampled_from(["soft", "strict"])


# ---------------------------------------------------------------------------
# Property 1: Идемпотентность cleanup_transcript (soft и strict профили)
# ---------------------------------------------------------------------------

@unittest.skipIf(_IMPORT_ERROR is not None, f"dependency unavailable: {_IMPORT_ERROR}")
class TestIdempotence(unittest.TestCase):
    """cleanup_transcript(cleanup_transcript(x)) == cleanup_transcript(x)"""

    @given(text=_SAFE_TEXT, profile=_PROFILES)
    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    def test_idempotent_unicode(self, text: str, profile: str):
        first = TextUtils.cleanup_transcript(text, profile=profile)
        second = TextUtils.cleanup_transcript(first, profile=profile)
        self.assertEqual(
            first, second,
            f"cleanup_transcript не идемпотентен для profile={profile!r}.\n"
            f"  Входной текст (repr): {text!r}\n"
            f"  После 1-го прохода:   {first!r}\n"
            f"  После 2-го прохода:   {second!r}",
        )

    @given(text=_TRANSCRIPT_TEXT, profile=_PROFILES)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_idempotent_transcript_like(self, text: str, profile: str):
        first = TextUtils.cleanup_transcript(text, profile=profile)
        second = TextUtils.cleanup_transcript(first, profile=profile)
        self.assertEqual(first, second)


# ---------------------------------------------------------------------------
# Property 2: Ограничение длины (cleanup только убирает или нормализует)
# ---------------------------------------------------------------------------

@unittest.skipIf(_IMPORT_ERROR is not None, f"dependency unavailable: {_IMPORT_ERROR}")
class TestLengthBound(unittest.TestCase):
    """len(cleanup_transcript(x)) <= len(x) + safety_margin.

    Небольшой допуск: brand-нормализация может заменить кириллическую строку
    (например «Меркадонна» = 10 символов) на латинскую («Mercadona» = 9),
    но никогда не должна значительно удлинять текст.  Задаём margin как 10%
    от исходной длины + 50 символов для коротких строк.
    """

    MARGIN_FACTOR = 1.10
    MARGIN_ABS = 50

    @given(text=_SAFE_TEXT, profile=_PROFILES)
    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    def test_length_does_not_explode(self, text: str, profile: str):
        result = TextUtils.cleanup_transcript(text, profile=profile)
        max_allowed = int(len(text) * self.MARGIN_FACTOR) + self.MARGIN_ABS
        self.assertLessEqual(
            len(result), max_allowed,
            f"Длина после cleanup превысила допустимый предел.\n"
            f"  Вход ({len(text)} симв.): {text!r}\n"
            f"  Выход ({len(result)} симв.): {result!r}\n"
            f"  Максимально допустимо: {max_allowed}",
        )

    @given(text=_TRANSCRIPT_TEXT, profile=_PROFILES)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_length_does_not_explode_transcript(self, text: str, profile: str):
        result = TextUtils.cleanup_transcript(text, profile=profile)
        max_allowed = int(len(text) * self.MARGIN_FACTOR) + self.MARGIN_ABS
        self.assertLessEqual(len(result), max_allowed)


# ---------------------------------------------------------------------------
# Property 3: Корректность Unicode (нет mojibake, результат — валидный Unicode)
# ---------------------------------------------------------------------------

@unittest.skipIf(_IMPORT_ERROR is not None, f"dependency unavailable: {_IMPORT_ERROR}")
class TestUnicodePreservation(unittest.TestCase):
    """Валидный Unicode на входе → валидный Unicode на выходе."""

    @given(text=_SAFE_TEXT, profile=_PROFILES)
    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    def test_output_is_valid_unicode(self, text: str, profile: str):
        result = TextUtils.cleanup_transcript(text, profile=profile)
        # Попытка encode/decode как UTF-8 — проверяем отсутствие surrogates
        try:
            encoded = result.encode("utf-8")
            decoded = encoded.decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError) as exc:
            self.fail(
                f"Результат cleanup_transcript содержит невалидные символы Unicode: {exc}\n"
                f"  Вход: {text!r}\n"
                f"  Выход: {result!r}"
            )
        self.assertEqual(result, decoded)

    @given(text=_SAFE_TEXT, profile=_PROFILES)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_no_surrogate_chars_in_output(self, text: str, profile: str):
        result = TextUtils.cleanup_transcript(text, profile=profile)
        for ch in result:
            cat = unicodedata.category(ch)
            self.assertNotEqual(
                cat, "Cs",
                f"Суррогатный символ {ch!r} обнаружен в выводе cleanup_transcript.\n"
                f"  Профиль: {profile!r}",
            )


# ---------------------------------------------------------------------------
# Property 4: normalize_phrase + cleanup: коммутативность нормализации
#
# normalize_phrase(cleanup(x)) == normalize_phrase(cleanup(normalize_phrase(x)))
# если normalize_phrase сам идемпотентен (что тоже проверяется).
# ---------------------------------------------------------------------------

@unittest.skipIf(_IMPORT_ERROR is not None, f"dependency unavailable: {_IMPORT_ERROR}")
class TestNormalizePhraseInvariant(unittest.TestCase):
    """normalize_phrase идемпотентен и commutes с cleanup на нормализованных входах."""

    @given(text=_TRANSCRIPT_TEXT)
    @settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
    def test_normalize_phrase_idempotent(self, text: str):
        first = TextUtils.normalize_phrase(text)
        second = TextUtils.normalize_phrase(first)
        self.assertEqual(
            first, second,
            f"normalize_phrase не идемпотентен.\n"
            f"  Вход:  {text!r}\n"
            f"  1-й проход: {first!r}\n"
            f"  2-й проход: {second!r}",
        )

    @given(text=_TRANSCRIPT_TEXT, profile=_PROFILES)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_normalize_then_cleanup_commutes(self, text: str, profile: str):
        """Нормализация перед cleanup и после дают одинаковый norm результат.

        normalize_phrase(cleanup(text)) == normalize_phrase(cleanup(normalize_phrase(text)))
        """
        clean_then_norm = TextUtils.normalize_phrase(
            TextUtils.cleanup_transcript(text, profile=profile)
        )
        norm_clean_norm = TextUtils.normalize_phrase(
            TextUtils.cleanup_transcript(
                TextUtils.normalize_phrase(text), profile=profile
            )
        )
        self.assertEqual(
            clean_then_norm, norm_clean_norm,
            f"normalize∘cleanup ≠ normalize∘cleanup∘normalize.\n"
            f"  Профиль: {profile!r}\n"
            f"  Вход: {text!r}\n"
            f"  norm(cleanup(x)):        {clean_then_norm!r}\n"
            f"  norm(cleanup(norm(x))): {norm_clean_norm!r}",
        )


# ---------------------------------------------------------------------------
# Property 5: Фильтрация галлюцинаций — безопасные входы не изменяются
#
# Если текст НЕ содержит hallucination-паттерны и не является кандидатом
# на повторы (нет дублированных предложений), cleanup сохраняет содержимое
# (mod whitespace normalization и brand-нормализации).
# ---------------------------------------------------------------------------

# Простые тексты без специальных символов, паттернов и повторов
_SAFE_CLEAN_TEXT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    min_size=3,
    max_size=200,
).filter(lambda t: t.strip() != "")


_HALLUCINATION_KEYWORDS = [
    "спасибо за просмотр",
    "спасибо за внимание",
    "субтитры сделал",
    "подписывайтесь на канал",
    "до новых встреч",
    "продолжение следует",
    "to be continued",
    "ставьте лайки",
    "смотрите в описании",
    "поддержите канал",
    "приятного просмотра",
    "увидимся в следующем видео",
    "всем пока",
]


@unittest.skipIf(_IMPORT_ERROR is not None, f"dependency unavailable: {_IMPORT_ERROR}")
class TestHallucinationStripping(unittest.TestCase):
    """Тексты без hallucination-паттернов не удаляются полностью."""

    @given(text=_SAFE_CLEAN_TEXT)
    @settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
    def test_no_hallucination_means_no_empty_output(self, text: str):
        """Если в тексте нет известных галлюцинаций, cleanup не обнуляет его."""
        lowered = text.lower()
        has_hallucination = any(kw in lowered for kw in _HALLUCINATION_KEYWORDS)
        if has_hallucination:
            return  # пропускаем — не наш случай

        result = TextUtils.cleanup_transcript(text, profile="soft")
        if text.strip():
            # Допускается, что результат непустой: cleanup может убрать повторы,
            # но не должен обнулять явно «безопасные» тексты.
            # Если текст нетривиальный (>1 слова) — должно что-то остаться.
            words = text.strip().split()
            if len(words) > 2:
                self.assertTrue(
                    len(result) > 0,
                    f"cleanup_transcript вернул пустую строку для безопасного текста.\n"
                    f"  Вход: {text!r}\n"
                    f"  Выход: {result!r}",
                )

    @given(text=_SAFE_CLEAN_TEXT)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_single_sentence_no_repetition_preserved(self, text: str):
        """Одно предложение без повторов и галлюцинаций сохраняется после cleanup (mod whitespace)."""
        # Нормализуем whitespace сами для корректного сравнения
        normalized_input = " ".join(text.split())
        if not normalized_input:
            return

        lowered = normalized_input.lower()
        has_hallucination = any(kw in lowered for kw in _HALLUCINATION_KEYWORDS)
        if has_hallucination:
            return

        result = TextUtils.cleanup_transcript(normalized_input, profile="soft")
        # Результат должен быть либо равен входу (после нормализации whitespace),
        # либо это brand-замена — в любом случае длина не должна быть 0
        if len(normalized_input.split()) > 2:
            self.assertGreater(
                len(result), 0,
                f"Одиночное предложение без галлюцинаций исчезло.\n"
                f"  Вход: {normalized_input!r}"
            )


# ---------------------------------------------------------------------------
# Property 6: normalize_entities идемпотентен
# ---------------------------------------------------------------------------

@unittest.skipIf(_IMPORT_ERROR is not None, f"dependency unavailable: {_IMPORT_ERROR}")
class TestNormalizeEntities(unittest.TestCase):
    """normalize_entities(normalize_entities(x)) == normalize_entities(x)."""

    @given(text=_SAFE_TEXT)
    @settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
    def test_normalize_entities_idempotent(self, text: str):
        first = TextUtils.normalize_entities(text)
        second = TextUtils.normalize_entities(first)
        self.assertEqual(
            first, second,
            f"normalize_entities не идемпотентен.\n"
            f"  Вход:  {text!r}\n"
            f"  1-й:   {first!r}\n"
            f"  2-й:   {second!r}",
        )

    @given(text=_TRANSCRIPT_TEXT)
    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    def test_normalize_entities_idempotent_transcript(self, text: str):
        first = TextUtils.normalize_entities(text)
        second = TextUtils.normalize_entities(first)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
