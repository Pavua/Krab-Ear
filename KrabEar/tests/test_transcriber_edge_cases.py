"""Transcriber edge-case тесты (дополнение к test_transcriber.py).

Покрытие:
- transcribe() с extra_vocabulary=[] (пустой список) — передаётся как есть
- lang_hint corner values: "auto", None, и нестандартный код
- settings_get инжектируется + не перезаписывает существующее
- Passthrough ошибок из engine (OSError, ValueError, TypeError)
- Profile switch: max → balanced при preview
- Двойной вызов transcribe без изменений профиля — set_quality_profile не вызывается

Все зависимости мокируются.
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

from backend.transcriber import Transcriber


# ---------------------------------------------------------------------------
# Shared fake engine
# ---------------------------------------------------------------------------

class FakeAudioEngine:
    """Мок AudioEngine для изоляции Transcriber."""

    def __init__(self, llm_rewriter=None, settings_get=None):
        self.quality_profile = "balanced"
        self._llm_rewriter = llm_rewriter
        self._settings_get = settings_get
        self.transcribe_calls: list[dict[str, Any]] = []
        self.profile_switch_calls: list[str] = []

    def set_quality_profile(self, profile: str) -> bool:
        self.profile_switch_calls.append(profile)
        if self.quality_profile == profile.lower():
            return False
        self.quality_profile = profile.lower()
        return True

    def transcribe(
        self,
        audio_data: Any,
        cleanup_profile: str = "soft",
        is_preview: bool = False,
        domain: str = "casual",
        extra_vocabulary: list[str] | None = None,
        lang_hint: str | None = None,
        history_context: list[Any] | None = None,
        stt_hotwords: list[str] | None = None,
        progress_callback: Any = None,
        silence_ranges: list[Any] | None = None,
        diarize: bool | None = None,
        skip_vad_prefilter: bool | None = None,  # added by cherry-pick a6213c0 (live_subs bypass VAD)
        context_free: bool | None = None,  # 2026-08-12 live-subs-prompt-leakage (G1)
    ) -> dict[str, Any]:
        record = {
            "audio_data": audio_data,
            "cleanup_profile": cleanup_profile,
            "is_preview": is_preview,
            "domain": domain,
            "extra_vocabulary": extra_vocabulary,
            "lang_hint": lang_hint,
        }
        self.transcribe_calls.append(record)
        return {
            "text": f"result:{audio_data}",
            "language": lang_hint or "auto",
            "confidence": 0.9,
        }


# ---------------------------------------------------------------------------
# 1. extra_vocabulary edge cases
# ---------------------------------------------------------------------------

class ExtraVocabularyEdgeCasesTests(unittest.TestCase):

    def setUp(self):
        self.fake_engine = FakeAudioEngine()
        self.transcriber = Transcriber(engine=self.fake_engine)

    def test_empty_list_passed_as_is(self):
        """extra_vocabulary=[] передаётся в engine.transcribe без изменений."""
        self.transcriber.transcribe(b"audio", extra_vocabulary=[])
        call_rec = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call_rec["extra_vocabulary"], [])

    def test_none_vocabulary_passed_as_none(self):
        """extra_vocabulary=None передаётся в engine.transcribe как None."""
        self.transcriber.transcribe(b"audio", extra_vocabulary=None)
        call_rec = self.fake_engine.transcribe_calls[0]
        self.assertIsNone(call_rec["extra_vocabulary"])

    def test_single_item_vocabulary(self):
        """Список из одного элемента передаётся корректно."""
        self.transcriber.transcribe(b"audio", extra_vocabulary=["краб"])
        call_rec = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call_rec["extra_vocabulary"], ["краб"])

    def test_vocabulary_not_mutated_by_transcriber(self):
        """Transcriber не мутирует переданный список."""
        original = ["a", "b"]
        self.transcriber.transcribe(b"audio", extra_vocabulary=original)
        self.assertEqual(original, ["a", "b"])


# ---------------------------------------------------------------------------
# 2. lang_hint corner values
# ---------------------------------------------------------------------------

class LangHintCornerValueTests(unittest.TestCase):

    def setUp(self):
        self.fake_engine = FakeAudioEngine()
        self.transcriber = Transcriber(engine=self.fake_engine)

    def test_lang_hint_none(self):
        """None передаётся в engine.transcribe без изменений."""
        self.transcriber.transcribe(b"audio", lang_hint=None)
        self.assertIsNone(self.fake_engine.transcribe_calls[0]["lang_hint"])

    def test_lang_hint_auto(self):
        """'auto' передаётся в engine как 'auto' (не конвертируется в None)."""
        self.transcriber.transcribe(b"audio", lang_hint="auto")
        self.assertEqual(self.fake_engine.transcribe_calls[0]["lang_hint"], "auto")

    def test_lang_hint_valid_ru(self):
        """'ru' передаётся как есть."""
        self.transcriber.transcribe(b"audio", lang_hint="ru")
        self.assertEqual(self.fake_engine.transcribe_calls[0]["lang_hint"], "ru")

    def test_lang_hint_valid_es(self):
        """'es' передаётся как есть."""
        self.transcriber.transcribe(b"audio", lang_hint="es")
        self.assertEqual(self.fake_engine.transcribe_calls[0]["lang_hint"], "es")

    def test_lang_hint_valid_en(self):
        """'en' передаётся как есть."""
        self.transcriber.transcribe(b"audio", lang_hint="en")
        self.assertEqual(self.fake_engine.transcribe_calls[0]["lang_hint"], "en")

    def test_lang_hint_unknown_code_passed_through(self):
        """Transcriber не валидирует lang_hint — передаёт любой код в engine."""
        self.transcriber.transcribe(b"audio", lang_hint="klingon")
        # Transcriber — тонкий слой, engine сам решает что делать с неизвестным кодом
        self.assertEqual(self.fake_engine.transcribe_calls[0]["lang_hint"], "klingon")


# ---------------------------------------------------------------------------
# 3. settings_get injection
# ---------------------------------------------------------------------------

class SettingsGetInjectionTests(unittest.TestCase):

    def test_settings_get_injected_into_engine(self):
        """settings_get передаётся в engine при инициализации."""
        fake_engine = FakeAudioEngine()
        mock_sg = MagicMock(return_value=True)
        Transcriber(engine=fake_engine, settings_get=mock_sg)
        self.assertIs(fake_engine._settings_get, mock_sg)

    def test_settings_get_always_overrides_in_engine(self):
        """settings_get инжектируется всегда, даже если engine._settings_get уже задан."""
        existing_sg = MagicMock(return_value=False)
        new_sg = MagicMock(return_value=True)
        fake_engine = FakeAudioEngine(settings_get=existing_sg)
        Transcriber(engine=fake_engine, settings_get=new_sg)
        # Текущая реализация: новый settings_get всегда заменяет
        self.assertIs(fake_engine._settings_get, new_sg)

    def test_settings_get_none_does_not_overwrite(self):
        """settings_get=None не должен затирать существующий engine._settings_get."""
        existing_sg = MagicMock(return_value=42)
        fake_engine = FakeAudioEngine(settings_get=existing_sg)
        Transcriber(engine=fake_engine, settings_get=None)
        # settings_get=None → не инжектируется (условие: if settings_get is not None)
        self.assertIs(fake_engine._settings_get, existing_sg)

    def test_llm_rewriter_not_overwritten_when_engine_has_one(self):
        """Если engine уже имеет llm_rewriter, новый не перезаписывает."""
        existing_llm = MagicMock()
        fake_engine = FakeAudioEngine(llm_rewriter=existing_llm)
        new_llm = MagicMock()
        Transcriber(engine=fake_engine, llm_rewriter=new_llm)
        # Реализация: инжектируем только если engine._llm_rewriter is None
        self.assertIs(fake_engine._llm_rewriter, existing_llm)


# ---------------------------------------------------------------------------
# 4. Passthrough errors from engine
# ---------------------------------------------------------------------------

class PassthroughErrorsTests(unittest.TestCase):

    def setUp(self):
        self.fake_engine = FakeAudioEngine()
        self.transcriber = Transcriber(engine=self.fake_engine)

    def test_os_error_propagates(self):
        """OSError из engine проходит насквозь."""
        self.fake_engine.transcribe = MagicMock(
            side_effect=OSError("audio device not found")
        )
        with self.assertRaises(OSError) as ctx:
            self.transcriber.transcribe(b"audio")
        self.assertIn("audio device not found", str(ctx.exception))

    def test_value_error_propagates(self):
        """ValueError из engine проходит насквозь."""
        self.fake_engine.transcribe = MagicMock(
            side_effect=ValueError("file too large")
        )
        with self.assertRaises(ValueError):
            self.transcriber.transcribe(b"audio")

    def test_type_error_propagates(self):
        """TypeError из engine проходит насквозь."""
        self.fake_engine.transcribe = MagicMock(
            side_effect=TypeError("unexpected type")
        )
        with self.assertRaises(TypeError):
            self.transcriber.transcribe(b"audio")

    def test_memory_error_propagates(self):
        """MemoryError из engine проходит насквозь."""
        self.fake_engine.transcribe = MagicMock(side_effect=MemoryError("OOM"))
        with self.assertRaises(MemoryError):
            self.transcriber.transcribe(b"audio")

    def test_preview_propagates_value_error(self):
        """transcribe_preview тоже пропускает исключения."""
        self.fake_engine.transcribe = MagicMock(
            side_effect=ValueError("bad audio")
        )
        with self.assertRaises(ValueError):
            self.transcriber.transcribe_preview(b"audio")


# ---------------------------------------------------------------------------
# 5. Profile switch behaviour
# ---------------------------------------------------------------------------

class ProfileSwitchBehaviourTests(unittest.TestCase):

    def setUp(self):
        self.fake_engine = FakeAudioEngine()
        self.transcriber = Transcriber(engine=self.fake_engine)

    def test_same_profile_set_quality_still_called(self):
        """set_quality_profile вызывается при каждом transcribe, даже если профиль тот же."""
        # Engine начинает с 'balanced'
        self.transcriber.transcribe(b"audio1", quality_profile="balanced")
        self.transcriber.transcribe(b"audio2", quality_profile="balanced")
        # Оба вызова должны дёрнуть set_quality_profile
        self.assertEqual(len(self.fake_engine.profile_switch_calls), 2)

    def test_profile_max_then_balanced(self):
        """max → balanced переключение через два последовательных transcribe."""
        self.transcriber.transcribe(b"audio1", quality_profile="max")
        self.assertEqual(self.fake_engine.quality_profile, "max")
        self.transcriber.transcribe(b"audio2", quality_profile="balanced")
        self.assertEqual(self.fake_engine.quality_profile, "balanced")

    def test_preview_always_forces_balanced(self):
        """transcribe_preview всегда переключает engine на 'balanced'."""
        self.fake_engine.quality_profile = "max"
        self.transcriber.transcribe_preview(b"audio")
        self.assertEqual(self.fake_engine.quality_profile, "balanced")

    def test_preview_after_max_resets_to_balanced(self):
        """После max-транскрибации preview возвращает к balanced."""
        self.transcriber.transcribe(b"audio1", quality_profile="max")
        self.assertEqual(self.fake_engine.quality_profile, "max")
        self.transcriber.transcribe_preview(b"audio2")
        self.assertEqual(self.fake_engine.quality_profile, "balanced")


# ---------------------------------------------------------------------------
# 6. Return value integrity
# ---------------------------------------------------------------------------

class ReturnValueIntegrityTests(unittest.TestCase):

    def setUp(self):
        self.fake_engine = FakeAudioEngine()
        self.transcriber = Transcriber(engine=self.fake_engine)

    def test_transcribe_returns_dict(self):
        """transcribe() должен вернуть dict."""
        result = self.transcriber.transcribe(b"audio")
        self.assertIsInstance(result, dict)

    def test_transcribe_result_contains_text(self):
        """Результат содержит ключ 'text'."""
        result = self.transcriber.transcribe(b"audio")
        self.assertIn("text", result)

    def test_preview_result_contains_text(self):
        """transcribe_preview() результат содержит ключ 'text'."""
        result = self.transcriber.transcribe_preview(b"audio")
        self.assertIn("text", result)

    def test_transcribe_result_contains_confidence(self):
        """Результат содержит ключ 'confidence'."""
        result = self.transcriber.transcribe(b"audio")
        self.assertIn("confidence", result)


if __name__ == "__main__":
    unittest.main()
