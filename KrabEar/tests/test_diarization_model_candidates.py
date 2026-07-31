"""W-B волны gigaam-mlx-diar: цикл кандидатов моделей диаризации.

Тесты не загружают HF-модели: Pipeline.from_pretrained замокан, метод
_load_diarization_pipeline вызывается на «голом» AudioEngine.__new__
с минимальным набором атрибутов (паттерн test_engine_diarization: только
локальная логика, без тяжёлой инициализации движка).
"""
import threading
import unittest
from unittest import mock

import core.engine as engine_mod
from core.engine import AudioEngine


class _FakePipeline:
    def __init__(self, name):
        self.name = name
        self.device = None

    def to(self, device):
        self.device = device
        return self


def _bare_engine():
    eng = AudioEngine.__new__(AudioEngine)
    eng._diarization_pipeline = None
    eng._diarization_load_error = None
    eng._diarization_active_model = None
    eng._diarization_load_lock = threading.RLock()
    eng._resolve_diarization_device = lambda: "cpu"
    return eng


class TestDiarizationCandidates(unittest.TestCase):
    def _patch(self, candidates_value, from_pretrained):
        patches = [
            mock.patch.object(
                engine_mod.settings, "DIARIZATION_MODEL_CANDIDATES",
                candidates_value, create=True,
            ),
            mock.patch.object(
                engine_mod.settings, "DIARIZATION_MODEL",
                "pyannote/speaker-diarization-3.1",
            ),
            mock.patch.object(engine_mod, "Pipeline"),
        ]
        started = [p.start() for p in patches]
        started[2].from_pretrained = from_pretrained
        for p in patches:
            self.addCleanup(p.stop)

    def test_empty_candidates_uses_single_model(self):
        calls = []

        def fake(name, **kwargs):
            calls.append(name)
            return _FakePipeline(name)

        self._patch("", fake)
        eng = _bare_engine()
        pipeline = eng._load_diarization_pipeline()
        self.assertEqual(calls, ["pyannote/speaker-diarization-3.1"])
        self.assertEqual(eng._diarization_active_model, "pyannote/speaker-diarization-3.1")
        self.assertIs(pipeline, eng._diarization_pipeline)

    def test_first_candidate_wins(self):
        def fake(name, **kwargs):
            return _FakePipeline(name)

        self._patch("pyannote/speaker-diarization-community-1,pyannote/speaker-diarization-3.1", fake)
        eng = _bare_engine()
        eng._load_diarization_pipeline()
        self.assertEqual(
            eng._diarization_active_model, "pyannote/speaker-diarization-community-1"
        )

    def test_failed_candidate_falls_back_without_latch(self):
        def fake(name, **kwargs):
            if "community" in name:
                raise RuntimeError("gated repo")
            return _FakePipeline(name)

        self._patch("pyannote/speaker-diarization-community-1,pyannote/speaker-diarization-3.1", fake)
        eng = _bare_engine()
        eng._load_diarization_pipeline()
        self.assertEqual(eng._diarization_active_model, "pyannote/speaker-diarization-3.1")
        self.assertIsNone(eng._diarization_load_error, "фолбэк не должен ставить латч")

    def test_all_failed_sets_latch_once(self):
        def fake(name, **kwargs):
            raise RuntimeError("offline")

        self._patch("a/x,b/y", fake)
        eng = _bare_engine()
        with self.assertRaises(RuntimeError):
            eng._load_diarization_pipeline()
        self.assertIn("a/x", eng._diarization_load_error)
        self.assertIn("b/y", eng._diarization_load_error)
        # Повторный вызов бьётся о латч, не пытаясь грузить заново.
        with self.assertRaises(RuntimeError):
            eng._load_diarization_pipeline()


if __name__ == "__main__":
    unittest.main(verbosity=2)
