"""Поиск слова «Краб» по распознаванию речи: сегментатор коротких реплик, сопоставление, модель-обёртка."""

from __future__ import annotations

import ast
import math
import os
import sys
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend import stt_keyword_spotter as sk  # noqa: E402

FRAME = sk.FRAME_SAMPLES


def _silence(frames: int) -> list[int]:
    return [0] * (FRAME * frames)


def _tone(frames: int, amp: int = 3000) -> list[int]:
    n = FRAME * frames
    return [int(amp * math.sin(2 * math.pi * 220 * i / sk.SAMPLE_RATE)) for i in range(n)]


def _feed(segmenter: sk.UtteranceSegmenter, samples: list[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for i in range(0, len(samples) - FRAME + 1, FRAME):
        got = segmenter.push(samples[i:i + FRAME])
        if got is not None:
            out.append(list(got))
    return out


class KeywordMatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.m = sk.KeywordMatcher()

    def test_accepts_the_word_in_common_forms(self) -> None:
        for text in ("Краб", "краб.", "Эй, краб!", "Краб, открой историю.", "Эй, Краб", "КРАБ?"):
            self.assertTrue(self.m.matches(text), text)

    def test_accepts_clipped_fragments_but_not_lookalikes(self) -> None:
        for text in ("Кра.", "Раб, помоги.", "Айкрап."):
            self.assertTrue(self.m.matches(text), text)
        for text in ("Кран.", "Эй, Кран", "Грабли", "Крупа", "Брат", "Карп", "Эй, Карл", "Рабочий", "Привет", ""):
            self.assertFalse(self.m.matches(text), text)

    def test_ignores_none_and_non_text(self) -> None:
        self.assertFalse(self.m.matches(None))  # type: ignore[arg-type]

    def test_word_inside_longer_sentence_is_rejected(self) -> None:
        # Длинная реплика — разговор, а не обращение: слово должно быть в КОРОТКОЙ фразе.
        self.assertFalse(self.m.matches("Сегодня мы ели краб салат на обед в ресторане у моря"))


class SegmenterTests(unittest.TestCase):
    def test_isolated_short_utterance_is_emitted_with_padding(self) -> None:
        seg = sk.UtteranceSegmenter()
        got = _feed(seg, _silence(10) + _tone(6) + _silence(8))
        self.assertEqual(len(got), 1)
        self.assertGreaterEqual(len(got[0]), FRAME * 6)
        self.assertLessEqual(len(got[0]), FRAME * (6 + sk.PRE_ROLL_FRAMES + sk.TRAIL_FRAMES))

    def test_long_speech_is_ignored_as_conversation(self) -> None:
        seg = sk.UtteranceSegmenter()
        self.assertEqual(_feed(seg, _silence(10) + _tone(40) + _silence(10)), [])

    def test_speech_right_after_other_speech_is_not_isolated(self) -> None:
        seg = sk.UtteranceSegmenter()
        # две реплики с паузой 0.16 с (2 кадра) — это одна фраза, а при длине > лимита она отбрасывается;
        # короткая вторая реплика без достаточной тишины ПЕРЕД ней не должна считаться отдельным обращением
        samples = _silence(10) + _tone(6) + _silence(sk.TRAIL_FRAMES) + _tone(4) + _silence(8)
        got = _feed(seg, samples)
        self.assertEqual(len(got), 1)  # только первая: вторая началась сразу за хвостом первой

    def test_pause_inside_utterance_is_tolerated(self) -> None:
        seg = sk.UtteranceSegmenter()
        got = _feed(seg, _silence(10) + _tone(3) + _silence(2) + _tone(4) + _silence(8))
        self.assertEqual(len(got), 1)

    def test_too_short_blip_is_ignored(self) -> None:
        seg = sk.UtteranceSegmenter()
        self.assertEqual(_feed(seg, _silence(10) + _tone(1) + _silence(8)), [])

    def test_continuous_noise_never_emits(self) -> None:
        seg = sk.UtteranceSegmenter()
        self.assertEqual(_feed(seg, _tone(200)), [])

    def test_quiet_room_noise_is_not_speech(self) -> None:
        seg = sk.UtteranceSegmenter()
        noise = [((i * 7919) % 41) - 20 for i in range(FRAME * 100)]
        self.assertEqual(_feed(seg, noise), [])

    def test_wrong_frame_size_is_rejected(self) -> None:
        seg = sk.UtteranceSegmenter()
        with self.assertRaises(ValueError):
            seg.push([0] * (FRAME - 1))

    def test_reset_drops_partial_utterance(self) -> None:
        seg = sk.UtteranceSegmenter()
        _feed(seg, _silence(10) + _tone(3))
        seg.reset()
        self.assertEqual(_feed(seg, _silence(8)), [])


class SttKeywordModelTests(unittest.TestCase):
    def _model(self, transcript: str = "Краб", **kw) -> tuple[sk.SttKeywordModel, list[int]]:
        calls: list[int] = []

        def transcribe(samples):
            calls.append(len(samples))
            return transcript

        return sk.SttKeywordModel(transcribe, name="krab_stt", **kw), calls

    def _run(self, model: sk.SttKeywordModel, samples: list[int]) -> list[dict]:
        out = []
        for i in range(0, len(samples) - FRAME + 1, FRAME):
            out.append(model.predict(samples[i:i + FRAME]))
            model.drain(timeout=2.0)
        return out

    def test_match_fires_score_once_with_model_name(self) -> None:
        model, calls = self._model("Эй, краб!")
        scores = self._run(model, _silence(10) + _tone(6) + _silence(8) + _silence(4))
        fired = [s for s in scores if s.get("krab_stt", 0) >= 0.5]
        self.assertEqual(len(fired), 1)
        self.assertEqual(len(calls), 1)
        model.close()

    def test_non_matching_transcript_never_fires(self) -> None:
        model, calls = self._model("Привет")
        scores = self._run(model, _silence(10) + _tone(6) + _silence(12))
        self.assertTrue(all(s.get("krab_stt", 0) < 0.5 for s in scores))
        self.assertEqual(len(calls), 1)
        model.close()

    def test_predict_never_blocks_on_slow_transcriber(self) -> None:
        gate = threading.Event()

        def slow(_samples):
            gate.wait(5.0)
            return "краб"

        model = sk.SttKeywordModel(slow, name="krab_stt")
        samples = _silence(10) + _tone(6) + _silence(8)
        start = time.monotonic()
        for i in range(0, len(samples) - FRAME + 1, FRAME):
            model.predict(samples[i:i + FRAME])
        self.assertLess(time.monotonic() - start, 1.0)  # транскрибер ждёт gate — predict не ждёт его
        gate.set()
        model.close()

    def test_busy_worker_drops_new_utterance_instead_of_queueing(self) -> None:
        gate = threading.Event()
        started = threading.Event()
        seen: list[int] = []

        def slow(samples):
            seen.append(len(samples))
            started.set()
            gate.wait(5.0)
            return "привет"

        model = sk.SttKeywordModel(slow, name="krab_stt")
        burst = _silence(10) + _tone(6) + _silence(8)
        for i in range(0, len(burst) - FRAME + 1, FRAME):
            model.predict(burst[i:i + FRAME])
        self.assertTrue(started.wait(2.0))
        for i in range(0, len(burst) - FRAME + 1, FRAME):  # вторая реплика, пока воркер занят
            model.predict(burst[i:i + FRAME])
        gate.set()
        model.drain(timeout=2.0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(model.stats()["dropped_busy"], 1)
        model.close()

    def test_cooldown_suppresses_repeated_fire(self) -> None:
        model, _ = self._model("краб", cooldown_sec=60.0)
        burst = _silence(10) + _tone(6) + _silence(12)
        first = [s for s in self._run(model, burst) if s.get("krab_stt", 0) >= 0.5]
        second = [s for s in self._run(model, burst) if s.get("krab_stt", 0) >= 0.5]
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)
        model.close()

    def test_transcriber_exception_is_not_a_match_and_does_not_kill_worker(self) -> None:
        state = {"n": 0}

        def flaky(_samples):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("boom")
            return "краб"

        model = sk.SttKeywordModel(flaky, name="krab_stt", cooldown_sec=0.0)
        burst = _silence(10) + _tone(6) + _silence(12)
        self.assertEqual(len([s for s in self._run(model, burst) if s.get("krab_stt", 0) >= 0.5]), 0)
        self.assertEqual(len([s for s in self._run(model, burst) if s.get("krab_stt", 0) >= 0.5]), 1)
        self.assertEqual(model.stats()["errors"], 1)
        model.close()

    def test_rate_limit_caps_transcriptions_per_minute(self) -> None:
        model, calls = self._model("привет", max_per_minute=2, cooldown_sec=0.0)
        burst = _silence(10) + _tone(6) + _silence(12)
        for _ in range(5):
            self._run(model, burst)
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(model.stats()["dropped_rate"], 1)
        model.close()

    def test_reset_clears_pending_fire(self) -> None:
        model, _ = self._model("краб")
        burst = _silence(10) + _tone(6) + _silence(8)
        for i in range(0, len(burst) - FRAME + 1, FRAME):
            model.predict(burst[i:i + FRAME])
        model.drain(timeout=2.0)
        model.reset()
        self.assertTrue(model.predict(_silence(1))["krab_stt"] < 0.5)
        model.close()

    def test_predict_accepts_numpy_like_int16_chunks(self) -> None:
        try:
            import numpy as np
        except ImportError:  # pragma: no cover
            self.skipTest("numpy недоступен")
        model, _ = self._model("краб")
        out = model.predict(np.zeros(FRAME, dtype=np.int16))
        self.assertIn("krab_stt", out)
        model.close()

    def test_close_is_idempotent_and_stops_worker(self) -> None:
        model, _ = self._model("краб")
        model.close()
        model.close()
        self.assertFalse(model.is_alive())


class PrivacyContractTests(unittest.TestCase):
    """Аудио и тексты реплик живут только в памяти: модуль не пишет на диск и не логирует транскрипт."""

    SRC = Path(PROJECT_ROOT, "backend", "stt_keyword_spotter.py").read_text(encoding="utf-8")

    def test_module_has_no_file_writes_or_network(self) -> None:
        tree = ast.parse(self.SRC)
        banned_calls = {"open", "write_text", "write_bytes", "mkdir", "unlink"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                self.assertNotIn(name, banned_calls)
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertFalse(imported & {"socket", "requests", "urllib", "http", "wave", "sqlite3", "pathlib"})

    def test_logging_never_receives_the_transcript(self) -> None:
        tree = ast.parse(self.SRC)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in {"debug", "info", "warning", "error", "exception"}:
                text = ast.dump(node)
                self.assertNotIn("transcript", text)
                self.assertNotIn("'text'", text)


class EvalScriptTests(unittest.TestCase):
    """Чистая часть скрипта оценки: числа и порог приёмки; в отчёте нет имён файлов и транскриптов."""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util

        path = Path(PROJECT_ROOT).parent / "scripts" / "eval_stt_wake_spotter.py"
        spec = importlib.util.spec_from_file_location("eval_stt_wake_spotter_under_test", path)
        assert spec and spec.loader
        cls.mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.mod
        spec.loader.exec_module(cls.mod)

    def test_accepts_good_run_and_counts_word_blocks_separately(self) -> None:
        results = {("pos", "pos_normal"): (20, 18), ("pos", "pos_short"): (16, 16),
                   ("neg", "neg_ru_speech"): (24, 0), ("neg", "neg_lookalikes"): (16, 3)}
        summary = self.mod.summarize(results)
        self.assertEqual((summary["pos_hit"], summary["pos_total"]), (34, 36))
        self.assertEqual(summary["neg_fire"], 0)
        self.assertEqual((summary["word_clip_fires"], summary["word_clips"]), (3, 16))
        self.assertTrue(self.mod.accepted(summary))

    def test_rejects_low_recall_any_false_fire_or_empty_sets(self) -> None:
        low = self.mod.summarize({("pos", "a"): (10, 8), ("neg", "b"): (10, 0)})
        self.assertFalse(self.mod.accepted(low))
        false_fire = self.mod.summarize({("pos", "a"): (10, 10), ("neg", "b"): (10, 1)})
        self.assertFalse(self.mod.accepted(false_fire))
        self.assertFalse(self.mod.accepted(self.mod.summarize({})))
        no_neg = self.mod.summarize({("pos", "a"): (10, 10)})
        self.assertFalse(self.mod.accepted(no_neg))

    def test_report_contains_only_numbers_by_block(self) -> None:
        results = {("pos", "pos_normal"): (20, 18), ("neg", "neg_ru_speech"): (24, 0)}
        report = self.mod.format_report(results, self.mod.summarize(results))
        self.assertIn("pos pos_normal | 18 / 20", report)
        self.assertNotIn(".wav", report)


if __name__ == "__main__":
    unittest.main()
