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


class ReviewFixesTests(unittest.TestCase):
    """Правки по гейту Opus (#2257): призрачное срабатывание после reset, глухота под внезапным шумом."""

    def test_reset_during_inflight_transcription_discards_its_result(self) -> None:
        gate = threading.Event()
        started = threading.Event()

        def slow(_samples):
            started.set()
            gate.wait(5.0)
            return "краб"

        model = sk.SttKeywordModel(slow, name="krab_stt", cooldown_sec=0.0)
        burst = _silence(10) + _tone(6) + _silence(8)
        for i in range(0, len(burst) - FRAME + 1, FRAME):
            model.predict(burst[i:i + FRAME])
        self.assertTrue(started.wait(2.0))
        model.reset()           # пауза/запись: результат ещё летящего распознавания устарел
        gate.set()
        model.drain(timeout=2.0)
        self.assertLess(model.predict(_silence(1)[:FRAME])["krab_stt"], 0.5)
        self.assertEqual(model.stats()["matched"], 0)
        model.close()

    def test_result_after_reset_for_a_new_utterance_still_fires(self) -> None:
        model = sk.SttKeywordModel(lambda _s: "краб", name="krab_stt", cooldown_sec=0.0)
        model.reset()
        burst = _silence(10) + _tone(6) + _silence(8)
        fired = []
        for i in range(0, len(burst) - FRAME + 1, FRAME):
            fired.append(model.predict(burst[i:i + FRAME])["krab_stt"])
            model.drain(timeout=2.0)
        self.assertEqual(sum(1 for v in fired if v >= 0.5), 1)
        model.close()

    @staticmethod
    def _noise(frames: int, rms: float, seed: int = 1) -> list[int]:
        state = seed
        amp = rms * math.sqrt(3)
        out = []
        for _ in range(FRAME * frames):
            state = (state * 1103515245 + 12345) & 0x7FFFFFFF
            out.append(int((state / 0x7FFFFFFF * 2 - 1) * amp))
        return out

    def test_segmenter_recovers_after_sudden_steady_noise(self) -> None:
        seg = sk.UtteranceSegmenter()
        word = [a + b for a, b in zip(_tone(6, amp=2500), self._noise(6, 150.0, seed=7))]
        fan = lambda n, seed: self._noise(n, 150.0, seed=seed)  # noqa: E731
        samples = self._noise(60, 20.0) + fan(300, 3)
        for k in range(5):  # пять слов с паузами «только вентилятор» (не цифровая тишина)
            samples += word + fan(12, 10 + k)
        emitted = _feed(seg, samples)
        self.assertGreaterEqual(len(emitted), 1)

    def test_starting_under_noise_above_initial_threshold_recovers(self) -> None:
        # замечание Codex: фон rms≈100 выше начального порога 60, пол в простое не обновляется (пол=0 с начала)
        seg = sk.UtteranceSegmenter()
        samples = self._noise(200, 100.0, seed=2)
        for k in range(4):
            word = [a + b for a, b in zip(_tone(6, amp=2500), self._noise(6, 100.0, seed=30 + k))]
            samples += word + self._noise(12, 100.0, seed=40 + k)
        self.assertGreaterEqual(len(_feed(seg, samples)), 1)

    def test_same_words_in_quiet_room_still_work(self) -> None:
        seg = sk.UtteranceSegmenter()
        samples = self._noise(60, 20.0)
        for k in range(5):
            samples += _tone(6, amp=2500) + self._noise(12, 20.0, seed=20 + k)
        self.assertEqual(len(_feed(seg, samples)), 5)

    def test_floor_does_not_creep_up_on_a_genuinely_long_speech(self) -> None:
        # длинная речь не должна «поднять пол» до уровня речи и оглушить сегментатор
        seg = sk.UtteranceSegmenter()
        _feed(seg, self._noise(60, 20.0) + _tone(80, amp=2500) + self._noise(30, 20.0, seed=5))
        self.assertGreaterEqual(len(_feed(seg, _silence(10) + _tone(6, amp=2500) + _silence(8))), 1)

    def test_floor_recovers_quickly_after_loud_period(self) -> None:
        # после долгого шума/речи глухота не должна длиться секундами: пол падает быстрее, чем растёт
        seg = sk.UtteranceSegmenter()
        _feed(seg, self._noise(60, 20.0) + _tone(80, amp=2500))
        got = _feed(seg, self._noise(14, 20.0, seed=9) + _tone(6, amp=2500) + self._noise(8, 20.0, seed=10))
        self.assertGreaterEqual(len(got), 1)

    def test_drain_timeout_uses_real_time_not_injected_clock(self) -> None:
        gate = threading.Event()
        model = sk.SttKeywordModel(lambda _s: gate.wait(5.0) or "краб", name="krab_stt", clock=lambda: 0.0)
        burst = _silence(10) + _tone(6) + _silence(8)
        for i in range(0, len(burst) - FRAME + 1, FRAME):
            model.predict(burst[i:i + FRAME])
        start = time.monotonic()
        self.assertFalse(model.drain(timeout=0.3))
        self.assertLess(time.monotonic() - start, 2.0)
        gate.set()
        model.close()

    def test_fragment_hits_are_counted_separately_without_text(self) -> None:
        for text, key in (("Краб", "matched_word"), ("Раб", "matched_fragment")):
            model = sk.SttKeywordModel(lambda _s, t=text: t, name="krab_stt", cooldown_sec=0.0)
            burst = _silence(10) + _tone(6) + _silence(12)
            for i in range(0, len(burst) - FRAME + 1, FRAME):
                model.predict(burst[i:i + FRAME])
                model.drain(timeout=2.0)
            stats = model.stats()
            self.assertEqual(stats[key], 1, (text, stats))
            self.assertEqual(stats["matched"], 1)
            model.close()


class RateLimitRefundTests(unittest.TestCase):
    """Лимит 20/мин защищает от шторма СОВПАДЕНИЙ, а не от шума: пустые реплики лимит не едят (иначе шум комнаты
    оглушает споттер и настоящий «Краб» попадает в dropped_rate)."""

    def _bursts(self, model, n, amp=3000):
        for _ in range(n):
            burst = _silence(10) + _tone(6, amp=amp) + _silence(12)
            for i in range(0, len(burst) - FRAME + 1, FRAME):
                model.predict(burst[i:i + FRAME])
                model.drain(timeout=2.0)

    def test_empty_transcripts_do_not_consume_the_rate_limit(self) -> None:
        calls = []
        model = sk.SttKeywordModel(lambda s: calls.append(1) or "", name="krab_stt", max_per_minute=2, cooldown_sec=0.0)
        self._bursts(model, 6)
        stats = model.stats()
        self.assertEqual(len(calls), 6)
        self.assertEqual((stats["dropped_rate"], stats["empty"]), (0, 6))
        model.close()

    def test_unavailable_recognizer_does_not_consume_the_rate_limit(self) -> None:
        calls = []

        def busy(_s):
            calls.append(1)
            raise sk.TranscriberUnavailable("mlx_busy")

        model = sk.SttKeywordModel(busy, name="krab_stt", max_per_minute=2, cooldown_sec=0.0)
        self._bursts(model, 5)
        self.assertEqual(len(calls), 5)
        self.assertEqual(model.stats()["dropped_rate"], 0)
        model.close()

    def test_real_words_still_consume_the_limit(self) -> None:
        calls = []
        model = sk.SttKeywordModel(lambda s: calls.append(1) or "привет", name="krab_stt", max_per_minute=2, cooldown_sec=0.0)
        self._bursts(model, 5)
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(model.stats()["dropped_rate"], 1)
        model.close()

    def test_real_wake_word_is_recognized_after_a_noise_storm(self) -> None:
        texts = iter([""] * 25 + ["Краб"])
        model = sk.SttKeywordModel(lambda s: next(texts), name="krab_stt", max_per_minute=20, cooldown_sec=0.0)
        fired = []
        for _ in range(26):
            burst = _silence(10) + _tone(6) + _silence(12)
            for i in range(0, len(burst) - FRAME + 1, FRAME):
                fired.append(model.predict(burst[i:i + FRAME])["krab_stt"])
                model.drain(timeout=2.0)
        self.assertEqual(sum(1 for v in fired if v >= 0.5), 1)   # слово после 25 пустых реплик не потеряно
        model.close()

    def test_loudness_bucket_uses_the_loudest_frame_not_the_diluted_average(self) -> None:
        # кадр с речью rms ~495 (корзина 400–1000); в реплике ~половина тишины, среднее по ней ~324 (корзина 150–400)
        model = sk.SttKeywordModel(lambda s: "", name="krab_stt", max_per_minute=100, cooldown_sec=0.0)
        self._bursts(model, 1, amp=700)
        stats = model.stats()
        self.assertEqual((stats["rms_400_1000"], stats["rms_150_400"]), (1, 0))
        model.close()

    def test_loudness_buckets_are_counters_only(self) -> None:
        model = sk.SttKeywordModel(lambda s: "", name="krab_stt", max_per_minute=100, cooldown_sec=0.0)
        self._bursts(model, 1, amp=3000)    # rms ~2100 → ge1000
        self._bursts(model, 1, amp=120)     # rms ~85 → lt150
        stats = model.stats()
        self.assertEqual((stats["rms_ge1000"], stats["rms_lt150"]), (1, 1))
        self.assertEqual(stats["rms_150_400"] + stats["rms_400_1000"], 0)
        model.close()


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

    def test_word_exemption_is_per_clip_not_per_block(self) -> None:
        label = self.mod.block_label
        self.assertEqual(label("neg_lookalikes", "Краб-салат"), "neg_lookalikes:содержит_слово")
        self.assertEqual(label("neg_lookalikes", "Крабы"), "neg_lookalikes:содержит_слово")
        for prompt in ("Грабли", "Драп", "Кроп", "Карп", "Брат", "Кран", "Кроб", "Крупа", "", None):
            self.assertEqual(label("neg_lookalikes", prompt), "neg_lookalikes")
        # срабатывание на «Драп» в том же блоке остаётся ложным и валит приёмку
        results = {("pos", "pos_normal"): (20, 20), ("neg", "neg_lookalikes"): (13, 1),
                   ("neg", "neg_lookalikes:содержит_слово"): (3, 3)}
        self.assertFalse(self.mod.accepted(self.mod.summarize(results)))

    def test_homophone_krap_is_a_separate_row_not_a_false_fire(self) -> None:
        label = self.mod.block_label
        self.assertEqual(label("neg_lookalikes", "Крап"), "neg_lookalikes:омофон")
        self.assertEqual(label("neg_lookalikes", "эй, крап!"), "neg_lookalikes:омофон")
        results = {("pos", "pos_normal"): (20, 19), ("neg", "neg_ru_speech"): (24, 0),
                   ("neg", "neg_lookalikes"): (13, 0), ("neg", "neg_lookalikes:омофон"): (1, 1),
                   ("neg", "neg_lookalikes:содержит_слово"): (2, 2)}
        summary = self.mod.summarize(results)
        self.assertEqual((summary["homophone_fires"], summary["homophone_clips"]), (1, 1))
        self.assertEqual((summary["neg_fire"], summary["neg_total"]), (0, 37))
        self.assertTrue(self.mod.accepted(summary))
        report = self.mod.format_report(results, summary)
        self.assertIn("Омофон «крап» (звучит как «краб»): сработало 1 из 1", report)
        self.assertIn("neg_lookalikes:омофон | 1 / 1", report)

    def test_accepts_good_run_and_counts_word_blocks_separately(self) -> None:
        results = {("pos", "pos_normal"): (20, 18), ("pos", "pos_short"): (16, 16),
                   ("neg", "neg_ru_speech"): (24, 0), ("neg", "neg_lookalikes:содержит_слово"): (16, 3)}
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
