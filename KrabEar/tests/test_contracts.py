"""Тесты контрактных моделей событий Krab Ear."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from contracts.stt_events import SttPartial, SttFinal, SttFailed
from contracts.translation_events import TranslationCompleted, TranslationFailed
from pydantic import ValidationError


class SttPartialTest(unittest.TestCase):

    def test_valid_minimal(self):
        e = SttPartial(text="hello")
        self.assertEqual(e.text, "hello")
        self.assertIsNone(e.duration_sec)

    def test_valid_full(self):
        e = SttPartial(text="hello", duration_sec=1.5)
        self.assertEqual(e.duration_sec, 1.5)

    def test_missing_text_raises(self):
        with self.assertRaises(ValidationError):
            SttPartial()


class SttFinalTest(unittest.TestCase):

    def test_valid_minimal(self):
        e = SttFinal(history_id="abc-123", text="hello world", duration_sec=2.3)
        self.assertEqual(e.history_id, "abc-123")
        self.assertEqual(e.text, "hello world")
        self.assertEqual(e.duration_sec, 2.3)
        self.assertIsNone(e.language)
        self.assertIsNone(e.confidence)
        self.assertEqual(e.segments, [])

    def test_valid_full(self):
        e = SttFinal(
            history_id="abc-123",
            text="hello",
            duration_sec=2.3,
            language="ru",
            confidence=0.95,
            segments=[{"start": 0.0, "end": 1.0, "text": "hello"}],
        )
        self.assertEqual(e.language, "ru")
        self.assertEqual(e.confidence, 0.95)
        self.assertEqual(len(e.segments), 1)

    def test_missing_required_raises(self):
        with self.assertRaises(ValidationError):
            SttFinal(text="hello")  # missing history_id, duration_sec


class SttFailedTest(unittest.TestCase):

    def test_valid_minimal(self):
        e = SttFailed(reason="timeout")
        self.assertEqual(e.reason, "timeout")
        self.assertEqual(e.duration_sec, 0.0)

    def test_valid_with_duration(self):
        e = SttFailed(reason="model_unavailable", duration_sec=1.2)
        self.assertEqual(e.duration_sec, 1.2)

    def test_missing_reason_raises(self):
        with self.assertRaises(ValidationError):
            SttFailed()


class TranslationCompletedTest(unittest.TestCase):

    def test_valid(self):
        e = TranslationCompleted(
            history_id="abc-123",
            source_text="hola mundo",
            translated_text="hello world",
            source_lang="es",
            target_lang="en",
            engine="local",
            mode="es_en",
        )
        self.assertEqual(e.source_text, "hola mundo")
        self.assertEqual(e.translated_text, "hello world")
        self.assertEqual(e.engine, "local")

    def test_missing_required_raises(self):
        with self.assertRaises(ValidationError):
            TranslationCompleted(
                history_id="abc",
                source_text="hola",
            )


class TranslationFailedTest(unittest.TestCase):

    def test_valid_minimal(self):
        e = TranslationFailed(source_text="hola", reason="engine_unavailable")
        self.assertEqual(e.reason, "engine_unavailable")
        self.assertIsNone(e.history_id)
        self.assertIsNone(e.source_lang)

    def test_valid_full(self):
        e = TranslationFailed(
            history_id="abc-123",
            source_text="hola",
            reason="timeout",
            source_lang="es",
            target_lang="en",
        )
        self.assertEqual(e.history_id, "abc-123")

    def test_missing_required_raises(self):
        with self.assertRaises(ValidationError):
            TranslationFailed()


if __name__ == "__main__":
    unittest.main()
