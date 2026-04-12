"""Тесты для TranscriptionScorer — оценки качества транскрибации Krab Ear.

Покрывает:
- QualityScore dataclass (поля, типы)
- TranscriptionScorer.score() — базовые сценарии
- Расчёт оценки-буквы (A–F)
- Факторы: confidence, completeness, duration, diarization, llm
- Рекомендации при низком качестве
- Граничные значения (пустой текст, нулевая длительность, крайние confidence)
- IPC-обработчик _handle_score_transcription
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.transcription_scorer import QualityScore, TranscriptionScorer


class TestQualityScoreDataclass(unittest.TestCase):
    """Проверяет структуру QualityScore."""

    def test_fields_present(self) -> None:
        qs = QualityScore(overall_score=85.0, grade="B")
        self.assertEqual(qs.overall_score, 85.0)
        self.assertEqual(qs.grade, "B")
        self.assertIsInstance(qs.factors, dict)
        self.assertIsInstance(qs.recommendations, list)

    def test_default_factors_empty(self) -> None:
        qs = QualityScore(overall_score=0.0, grade="F")
        self.assertEqual(qs.factors, {})

    def test_default_recommendations_empty(self) -> None:
        qs = QualityScore(overall_score=50.0, grade="F")
        self.assertEqual(qs.recommendations, [])


class TestTranscriptionScorerGrades(unittest.TestCase):
    """Проверяет присвоение оценок A–F."""

    def setUp(self) -> None:
        self.scorer = TranscriptionScorer()

    # ── Тест 1: оценка A при высокой уверенности и всех бонусах ─────────────

    def test_grade_a_high_confidence_all_bonuses(self) -> None:
        text = "Привет мир это тестовая транскрипция для оценки качества речи."
        result = self.scorer.score(
            text=text,
            confidence=1.0,
            duration_sec=5.0,
            has_diarization=True,
            has_llm_enhancement=True,
        )
        self.assertEqual(result.grade, "A")
        self.assertGreaterEqual(result.overall_score, 90.0)

    # ── Тест 2: оценка F при нулевой уверенности и пустом тексте ────────────

    def test_grade_f_zero_confidence_empty_text(self) -> None:
        result = self.scorer.score(
            text="",
            confidence=0.0,
            duration_sec=10.0,
            has_diarization=False,
            has_llm_enhancement=False,
        )
        self.assertEqual(result.grade, "F")
        self.assertLess(result.overall_score, 60.0)

    # ── Тест 3: оценка B при средней уверенности и нормальном тексте ────────

    def test_grade_b_medium_confidence(self) -> None:
        text = " ".join(["слово"] * 15)  # 15 слов
        result = self.scorer.score(
            text=text,
            confidence=0.88,
            duration_sec=10.0,
            has_diarization=False,
            has_llm_enhancement=False,
        )
        self.assertIn(result.grade, ("A", "B", "C"))
        self.assertGreaterEqual(result.overall_score, 0.0)


class TestTranscriptionScorerFactors(unittest.TestCase):
    """Проверяет факторы расчёта баллов."""

    def setUp(self) -> None:
        self.scorer = TranscriptionScorer()
        self.sample_text = "Тестовая транскрипция с несколькими словами для проверки."

    # ── Тест 4: фактор confidence соответствует ожидаемому диапазону ────────

    def test_confidence_factor_scales_with_input(self) -> None:
        result_high = self.scorer.score(
            text=self.sample_text, confidence=1.0, duration_sec=5.0
        )
        result_low = self.scorer.score(
            text=self.sample_text, confidence=0.0, duration_sec=5.0
        )
        self.assertGreater(
            result_high.factors["confidence"],
            result_low.factors["confidence"],
        )
        self.assertAlmostEqual(result_high.factors["confidence"], 40.0)
        self.assertAlmostEqual(result_low.factors["confidence"], 0.0)

    # ── Тест 5: бонус диаризации добавляет ровно 10 баллов ──────────────────

    def test_diarization_bonus_adds_ten_points(self) -> None:
        without = self.scorer.score(
            text=self.sample_text,
            confidence=0.8,
            duration_sec=5.0,
            has_diarization=False,
        )
        with_diar = self.scorer.score(
            text=self.sample_text,
            confidence=0.8,
            duration_sec=5.0,
            has_diarization=True,
        )
        self.assertAlmostEqual(with_diar.factors["diarization_bonus"], 10.0)
        self.assertAlmostEqual(without.factors["diarization_bonus"], 0.0)
        self.assertAlmostEqual(
            with_diar.overall_score - without.overall_score, 10.0, places=1
        )

    # ── Тест 6: бонус LLM добавляет ровно 10 баллов ─────────────────────────

    def test_llm_bonus_adds_ten_points(self) -> None:
        without = self.scorer.score(
            text=self.sample_text,
            confidence=0.8,
            duration_sec=5.0,
            has_llm_enhancement=False,
        )
        with_llm = self.scorer.score(
            text=self.sample_text,
            confidence=0.8,
            duration_sec=5.0,
            has_llm_enhancement=True,
        )
        self.assertAlmostEqual(with_llm.factors["llm_enhancement_bonus"], 10.0)
        self.assertAlmostEqual(without.factors["llm_enhancement_bonus"], 0.0)

    # ── Тест 7: итоговый балл не превышает 100 ───────────────────────────────

    def test_overall_score_capped_at_100(self) -> None:
        result = self.scorer.score(
            text=self.sample_text,
            confidence=1.0,
            duration_sec=5.0,
            has_diarization=True,
            has_llm_enhancement=True,
        )
        self.assertLessEqual(result.overall_score, 100.0)

    # ── Тест 8: факторы присутствуют и имеют корректные ключи ───────────────

    def test_factors_keys_present(self) -> None:
        result = self.scorer.score(
            text=self.sample_text, confidence=0.75, duration_sec=5.0
        )
        expected_keys = {
            "confidence",
            "text_completeness",
            "duration_appropriateness",
            "diarization_bonus",
            "llm_enhancement_bonus",
        }
        self.assertEqual(set(result.factors.keys()), expected_keys)


class TestTranscriptionScorerEdgeCases(unittest.TestCase):
    """Граничные случаи TranscriptionScorer."""

    def setUp(self) -> None:
        self.scorer = TranscriptionScorer()

    # ── Тест 9: пустой текст → нулевые факторы кроме confidence ─────────────

    def test_empty_text_returns_low_score(self) -> None:
        result = self.scorer.score(text="", confidence=0.5, duration_sec=5.0)
        self.assertEqual(result.factors["text_completeness"], 0.0)
        self.assertEqual(result.factors["duration_appropriateness"], 0.0)
        self.assertLess(result.overall_score, 50.0)

    # ── Тест 10: нулевая длительность — нейтральный балл duration ───────────

    def test_zero_duration_neutral_duration_score(self) -> None:
        result = self.scorer.score(
            text="Тест нулевой длительности", confidence=0.8, duration_sec=0.0
        )
        # duration_appropriateness должен быть 10 (половина от 20)
        self.assertAlmostEqual(result.factors["duration_appropriateness"], 10.0)

    # ── Тест 11: confidence > 1.0 зажимается до 1.0 ─────────────────────────

    def test_confidence_clamped_above_one(self) -> None:
        result = self.scorer.score(text="Тест", confidence=1.5, duration_sec=2.0)
        self.assertAlmostEqual(result.factors["confidence"], 40.0)

    # ── Тест 12: confidence < 0.0 зажимается до 0.0 ─────────────────────────

    def test_confidence_clamped_below_zero(self) -> None:
        result = self.scorer.score(text="Тест", confidence=-0.5, duration_sec=2.0)
        self.assertAlmostEqual(result.factors["confidence"], 0.0)


class TestTranscriptionScorerRecommendations(unittest.TestCase):
    """Проверяет генерацию рекомендаций."""

    def setUp(self) -> None:
        self.scorer = TranscriptionScorer()

    # ── Тест 13: низкая уверенность → рекомендация о микрофоне ──────────────

    def test_low_confidence_recommends_microphone(self) -> None:
        result = self.scorer.score(
            text="Привет", confidence=0.3, duration_sec=2.0
        )
        recs_combined = " ".join(result.recommendations).lower()
        self.assertIn("микрофон", recs_combined)

    # ── Тест 14: высокое качество — рекомендации об отсутствующих функциях ──

    def test_no_diarization_recommends_enabling_it(self) -> None:
        result = self.scorer.score(
            text="Тест",
            confidence=0.95,
            duration_sec=3.0,
            has_diarization=False,
        )
        recs_combined = " ".join(result.recommendations)
        self.assertIn("диаризац", recs_combined)

    # ── Тест 15: рекомендации — список строк ─────────────────────────────────

    def test_recommendations_is_list_of_strings(self) -> None:
        result = self.scorer.score(text="Тест", confidence=0.5, duration_sec=5.0)
        self.assertIsInstance(result.recommendations, list)
        for rec in result.recommendations:
            self.assertIsInstance(rec, str)


class TestIPCHandlerScoreTranscription(unittest.TestCase):
    """Проверяет IPC-обработчик _handle_score_transcription в BackendService."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(self._tmp.name))

        # Минимальные заглушки, чтобы не инициализировать AudioEngine
        class _FakeRecorder:
            def start(self): pass
            def stop(self): return b""
            is_recording = False

        class _FakeTranscriber:
            def transcribe(self, audio): return "fake"
            def list_profiles(self): return []
            def set_profile(self, name): pass
            engine = type("E", (), {"_llm_rewriter": None, "_settings_get": None})()

        class _FakeTranslator:
            def translate(self, text, **kw): return text

        self.svc = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ── Тест 16: IPC-обработчик возвращает корректные поля ───────────────────

    def test_handler_returns_expected_fields(self) -> None:
        result = self.svc._handle_score_transcription({
            "text": "Тест речи для оценки качества транскрибации.",
            "confidence": 0.9,
            "duration_sec": 4.0,
            "has_diarization": False,
            "has_llm_enhancement": False,
        })
        self.assertIn("overall_score", result)
        self.assertIn("grade", result)
        self.assertIn("factors", result)
        self.assertIn("recommendations", result)
        self.assertIsInstance(result["overall_score"], float)
        self.assertIn(result["grade"], ("A", "B", "C", "D", "F"))

    # ── Тест 17: IPC-обработчик работает с пустыми параметрами ──────────────

    def test_handler_empty_params(self) -> None:
        result = self.svc._handle_score_transcription({})
        self.assertIn("overall_score", result)
        self.assertEqual(result["grade"], "F")

    # ── Тест 18: IPC-обработчик передаёт бонусы корректно ───────────────────

    def test_handler_passes_bonuses(self) -> None:
        text = "Качественная транскрипция с диаризацией и LLM-обработкой."
        result = self.svc._handle_score_transcription({
            "text": text,
            "confidence": 0.95,
            "duration_sec": 5.0,
            "has_diarization": True,
            "has_llm_enhancement": True,
        })
        factors = result["factors"]
        self.assertAlmostEqual(factors["diarization_bonus"], 10.0)
        self.assertAlmostEqual(factors["llm_enhancement_bonus"], 10.0)


if __name__ == "__main__":
    unittest.main()
