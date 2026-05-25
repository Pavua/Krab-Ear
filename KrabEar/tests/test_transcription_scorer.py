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
from core.transcription_scorer import QualityScore, TranscriptionScorer

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


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


class TestTranscriptionScorerMissingFields(unittest.TestCase):
    """Graceful handling of missing/default fields."""

    def setUp(self) -> None:
        self.scorer = TranscriptionScorer()

    def test_whitespace_only_text_treated_as_empty(self) -> None:
        result = self.scorer.score(text="   ", confidence=0.5, duration_sec=5.0)
        self.assertEqual(result.factors["text_completeness"], 0.0)
        self.assertIsInstance(result.grade, str)

    def test_negative_duration_clamped_to_zero(self) -> None:
        result = self.scorer.score(text="Тест", confidence=0.8, duration_sec=-1.0)
        # duration clamped to 0 → neutral duration_appropriateness (10.0)
        self.assertAlmostEqual(result.factors["duration_appropriateness"], 10.0)

    def test_returns_quality_score_type(self) -> None:
        result = self.scorer.score(text="", confidence=0.0, duration_sec=0.0)
        self.assertIsInstance(result, QualityScore)
        self.assertIn(result.grade, ("A", "B", "C", "D", "F"))
        self.assertGreaterEqual(result.overall_score, 0.0)
        self.assertLessEqual(result.overall_score, 100.0)

    def test_factors_always_present(self) -> None:
        """factors dict always has all 5 keys even for degenerate inputs."""
        result = self.scorer.score(text="", confidence=0.0, duration_sec=0.0)
        for key in (
            "confidence",
            "text_completeness",
            "duration_appropriateness",
            "diarization_bonus",
            "llm_enhancement_bonus",
        ):
            self.assertIn(key, result.factors)


class TestIPCHandlerScoreTranscription(unittest.TestCase):
    """Проверяет IPC-обработчик _handle_score_transcription в BackendService."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(self._tmp.name))

        # Минимальные заглушки, чтобы не инициализировать AudioEngine
        class _FakeRecorder:
            is_recording = False

            def start(self): pass

            def stop(self): return b""

            def snapshot_audio(self, max_duration_sec: float = 12.0):
                import numpy as np
                return np.zeros(int(max_duration_sec * 16000), dtype=np.float32), max_duration_sec

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


class TestTranscriptionScorerWave116Required(unittest.TestCase):
    """Wave 116 — required named tests for TranscriptionScorer."""

    def setUp(self) -> None:
        self.scorer = TranscriptionScorer()

    def test_high_confidence_long_audio_grade_A(self) -> None:
        """Высокая уверенность + длинный текст + бонусы → оценка A."""
        # 30 слов за 10 секунд = 3 вел/с (в зоне нормального темпа)
        text = " ".join(["слово"] * 30)
        result = self.scorer.score(
            text=text,
            confidence=1.0,
            duration_sec=10.0,
            has_diarization=True,
            has_llm_enhancement=True,
        )
        self.assertEqual(result.grade, "A")
        self.assertGreaterEqual(result.overall_score, 90.0)

    def test_low_confidence_short_audio_grade_F(self) -> None:
        """Нулевая уверенность + пустой текст → оценка F."""
        result = self.scorer.score(
            text="",
            confidence=0.0,
            duration_sec=2.0,
            has_diarization=False,
            has_llm_enhancement=False,
        )
        self.assertEqual(result.grade, "F")
        self.assertLess(result.overall_score, 60.0)

    def test_diarization_bonus(self) -> None:
        """has_diarization=True добавляет ровно 10 к итоговому баллу."""
        text = "Тест диаризации спикеров для проверки бонуса."
        base = self.scorer.score(text=text, confidence=0.8, duration_sec=5.0,
                                 has_diarization=False)
        bonused = self.scorer.score(text=text, confidence=0.8, duration_sec=5.0,
                                    has_diarization=True)
        self.assertAlmostEqual(bonused.factors["diarization_bonus"], 10.0, places=2)
        self.assertAlmostEqual(base.factors["diarization_bonus"], 0.0, places=2)
        self.assertAlmostEqual(bonused.overall_score - base.overall_score, 10.0, places=1)

    def test_llm_penalty_on_hallucination_flag(self) -> None:
        """has_llm_enhancement=False → нет бонуса LLM; has_llm_enhancement=True → +10."""
        text = "Проверка флага LLM-обработки транскрипции."
        without = self.scorer.score(text=text, confidence=0.85, duration_sec=5.0,
                                    has_llm_enhancement=False)
        with_llm = self.scorer.score(text=text, confidence=0.85, duration_sec=5.0,
                                     has_llm_enhancement=True)
        self.assertAlmostEqual(without.factors["llm_enhancement_bonus"], 0.0)
        self.assertAlmostEqual(with_llm.factors["llm_enhancement_bonus"], 10.0)
        self.assertGreater(with_llm.overall_score, without.overall_score)

    def test_score_in_0_100_range(self) -> None:
        """Итоговый балл всегда в диапазоне [0, 100] для любых входных данных."""
        test_cases = [
            ("", 0.0, 0.0, False, False),
            ("Тест", 1.5, 5.0, True, True),  # confidence > 1 → clamped
            ("Слово", -0.5, -1.0, False, False),  # negative inputs
            (" ".join(["слово"] * 200), 1.0, 60.0, True, True),  # очень много слов
        ]
        for text, conf, dur, diar, llm in test_cases:
            result = self.scorer.score(text=text, confidence=conf, duration_sec=dur,
                                       has_diarization=diar, has_llm_enhancement=llm)
            self.assertGreaterEqual(result.overall_score, 0.0,
                                    f"Score below 0 for: {(text[:20], conf, dur)}")
            self.assertLessEqual(result.overall_score, 100.0,
                                 f"Score above 100 for: {(text[:20], conf, dur)}")

    def test_grade_letter_thresholds(self) -> None:
        """Проверяет буквенные оценки на граничных значениях: A>=90, B>=80, C>=70, D>=60, F<60."""
        from core.transcription_scorer import _grade
        self.assertEqual(_grade(90.0), "A")
        self.assertEqual(_grade(89.9), "B")
        self.assertEqual(_grade(80.0), "B")
        self.assertEqual(_grade(79.9), "C")
        self.assertEqual(_grade(70.0), "C")
        self.assertEqual(_grade(69.9), "D")
        self.assertEqual(_grade(60.0), "D")
        self.assertEqual(_grade(59.9), "F")
        self.assertEqual(_grade(0.0), "F")
        self.assertEqual(_grade(100.0), "A")

    def test_empty_metadata(self) -> None:
        """Пустой текст + duration=0 возвращает валидный QualityScore без исключений."""
        result = self.scorer.score(text="", confidence=0.0, duration_sec=0.0)
        self.assertIsInstance(result, QualityScore)
        self.assertIn(result.grade, ("A", "B", "C", "D", "F"))
        self.assertGreaterEqual(result.overall_score, 0.0)
        self.assertLessEqual(result.overall_score, 100.0)
        # Все факторы присутствуют
        for key in ("confidence", "text_completeness", "duration_appropriateness",
                    "diarization_bonus", "llm_enhancement_bonus"):
            self.assertIn(key, result.factors)

    def test_concurrent_score(self) -> None:
        """Параллельные вызовы score() не вызывают исключений и возвращают корректные результаты."""
        import threading
        results = []
        errors = []
        text = "Параллельная транскрипция для проверки потокобезопасности скорера."

        def worker():
            try:
                for _ in range(50):
                    res = self.scorer.score(text=text, confidence=0.8, duration_sec=5.0,
                                            has_diarization=True, has_llm_enhancement=True)
                    results.append(res)
                    assert 0.0 <= res.overall_score <= 100.0
                    assert res.grade in ("A", "B", "C", "D", "F")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent errors: {errors}")
        self.assertEqual(len(results), 200)


if __name__ == "__main__":
    unittest.main()
