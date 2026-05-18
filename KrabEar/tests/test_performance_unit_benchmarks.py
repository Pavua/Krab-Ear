"""Lightweight performance benchmarks for CI regression detection.

Each test runs <100ms and verifies that hot-path operations
stay within established latency budgets.

Budgets are measured on an M4 Max MacBook Pro and scaled 5× for CI runners.
Run with SKIP_BENCH=1 to skip all benchmarks in resource-constrained envs.

Usage:
    PYTHONPATH=KrabEar python -m pytest KrabEar/tests/test_performance_unit_benchmarks.py -v -s
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
import time
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

_SKIP_BENCH = bool(os.environ.get("SKIP_BENCH"))

# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------


def _elapsed_ms(fn, iters: int) -> float:
    """Return total elapsed ms for *iters* calls to *fn*."""
    t0 = time.perf_counter()
    for _ in range(iters):
        fn()
    return (time.perf_counter() - t0) * 1000.0


def _budget(budget_ms: float, mult: float = 5.0) -> float:
    """Return CI budget = budget_ms * mult (generous for slower runners)."""
    return budget_ms * mult


# ---------------------------------------------------------------------------
# 1. TextUtils.cleanup_transcript — soft profile, 1000 sentences
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_BENCH, "SKIP_BENCH set")
class BenchTextUtilsSoft(unittest.TestCase):
    """1000 calls to TextUtils.cleanup_transcript(soft) must finish in <2000 ms.

    Soft profile runs 45 pre-compiled brand-name regexes per call (~0.4 ms/call).
    Budget: M4 Max baseline ~407ms, CI macos-15-arm64 observed ~1200ms (3×). 2000ms
    gives ~5× headroom over local and ~1.7× over worst CI observation.
    """

    BUDGET_MS = 2000.0

    def setUp(self):
        from core.utils import TextUtils  # noqa: PLC0415
        self.TextUtils = TextUtils

    def test_cleanup_soft_1000_sentences(self):
        text = (
            "Привет, меня зовут Павел. "
            "Сегодня обсуждаем Krab Ear. "
            "Транскрипция через Whisper. "
            "Диаризация Pyannote."
        )
        elapsed = _elapsed_ms(
            lambda: self.TextUtils.cleanup_transcript(text, "soft"),
            iters=1000,
        )
        budget = self.BUDGET_MS  # already includes CI headroom
        print(f"\n  [bench] TextUtils.soft 1000× : {elapsed:.1f}ms  (budget {budget:.0f}ms)")
        self.assertLess(elapsed, budget,
                        f"TextUtils.cleanup_soft 1000× took {elapsed:.1f}ms > {budget:.0f}ms")


# ---------------------------------------------------------------------------
# 2. TextUtils.cleanup_transcript — strict profile, 1000 sentences
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_BENCH, "SKIP_BENCH set")
class BenchTextUtilsStrict(unittest.TestCase):
    """1000 calls to TextUtils.cleanup_transcript(strict) must finish in <2000 ms.

    Strict profile measured ~395ms/1000 calls on M4 Max. CI macos-15-arm64 observed
    ~810ms; 2000ms gives ~5× over local and ~2.5× over worst CI observation.
    """

    BUDGET_MS = 2000.0

    def setUp(self):
        from core.utils import TextUtils  # noqa: PLC0415
        self.TextUtils = TextUtils

    def test_cleanup_strict_1000_sentences(self):
        text = (
            "Привет, меня зовут Павел. "
            "Сегодня обсуждаем Krab Ear. "
            "Транскрипция через Whisper. "
            "Диаризация Pyannote."
        )
        elapsed = _elapsed_ms(
            lambda: self.TextUtils.cleanup_transcript(text, "strict"),
            iters=1000,
        )
        budget = self.BUDGET_MS  # already includes CI headroom
        print(f"\n  [bench] TextUtils.strict 1000× : {elapsed:.1f}ms  (budget {budget:.0f}ms)")
        self.assertLess(elapsed, budget,
                        f"TextUtils.cleanup_strict 1000× took {elapsed:.1f}ms > {budget:.0f}ms")


# ---------------------------------------------------------------------------
# 3. Regex precompile A2 win — precompiled vs inline re.search tight loop
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_BENCH, "SKIP_BENCH set")
class BenchRegexPrecompile(unittest.TestCase):
    """Precompiled regex must be at least as fast as inline re.search (A2 regression guard).

    Compares 5000 iterations of precompiled vs inline on a realistic pattern.
    The ratio precompiled/inline should stay ≤ 1.5 (precompiled is never slower).
    """

    def test_precompiled_not_slower_than_inline(self):
        pattern = r"[.!?]+"
        texts = [
            "Привет мир. Как дела?",
            "Hello world! This is a test...",
            "Нет знаков конца",
            "Ещё одно предложение с точкой.",
        ]
        compiled = re.compile(pattern)
        iters = 5000

        # Precompiled
        t0 = time.perf_counter()
        for _ in range(iters):
            for t in texts:
                compiled.search(t)
        elapsed_compiled = (time.perf_counter() - t0) * 1000.0

        # Inline
        t0 = time.perf_counter()
        for _ in range(iters):
            for t in texts:
                re.search(pattern, t)
        elapsed_inline = (time.perf_counter() - t0) * 1000.0

        ratio = elapsed_compiled / max(elapsed_inline, 0.001)
        print(
            f"\n  [bench] regex precompiled {elapsed_compiled:.1f}ms "
            f"vs inline {elapsed_inline:.1f}ms  ratio={ratio:.2f}"
        )
        # Precompiled should never be more than 50% slower (generous headroom)
        self.assertLessEqual(ratio, 1.5,
                             f"Precompiled regex ({elapsed_compiled:.1f}ms) "
                             f"unexpectedly > 1.5× inline ({elapsed_inline:.1f}ms)")


# ---------------------------------------------------------------------------
# 4. SearchIndex.build_index — 100 items
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_BENCH, "SKIP_BENCH set")
class BenchSearchIndexBuild(unittest.TestCase):
    """SearchIndex.build_index(100 items) must finish in <30 ms."""

    BUDGET_MS = 30.0

    def setUp(self):
        from core.search_index import SearchIndex  # noqa: PLC0415
        self.SearchIndex = SearchIndex

    def _make_items(self, n: int) -> list[dict]:
        return [
            {
                "id": f"item-{i}",
                "text": f"Запись номер {i}: привет мир Krab Ear транскрипция",
                "ts": f"2025-01-{(i % 28) + 1:02d}T12:00:00",
            }
            for i in range(n)
        ]

    def test_build_index_100_items(self):
        idx = self.SearchIndex()
        items = self._make_items(100)
        elapsed = _elapsed_ms(lambda: idx.build_index(items), iters=1)
        budget = _budget(self.BUDGET_MS)
        print(f"\n  [bench] SearchIndex.build_index(100) : {elapsed:.1f}ms  (budget {budget:.0f}ms)")
        self.assertLess(elapsed, budget,
                        f"SearchIndex.build_index(100) took {elapsed:.1f}ms > {budget:.0f}ms")


# ---------------------------------------------------------------------------
# 5. SearchIndex.search — 1000-item index
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_BENCH, "SKIP_BENCH set")
class BenchSearchIndexSearch(unittest.TestCase):
    """SearchIndex.search on a 1000-item index must finish in <50 ms per query."""

    BUDGET_MS = 50.0

    def setUp(self):
        from core.search_index import SearchIndex  # noqa: PLC0415
        idx = SearchIndex()
        items = [
            {
                "id": f"item-{i}",
                "text": f"Запись {i}: транскрипция Whisper Krab Ear привет мир",
                "ts": f"2025-01-{(i % 28) + 1:02d}T12:00:00",
            }
            for i in range(1000)
        ]
        idx.build_index(items)
        self.idx = idx

    def test_search_1000_item_index(self):
        query = "транскрипция Krab"
        elapsed = _elapsed_ms(lambda: self.idx.search(query, limit=20), iters=1)
        budget = _budget(self.BUDGET_MS)
        print(f"\n  [bench] SearchIndex.search(1000 items) : {elapsed:.1f}ms  (budget {budget:.0f}ms)")
        self.assertLess(elapsed, budget,
                        f"SearchIndex.search(1000 items) took {elapsed:.1f}ms > {budget:.0f}ms")


# ---------------------------------------------------------------------------
# 6. NumberNormalizer.normalize — 100 RU phrases
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_BENCH, "SKIP_BENCH set")
class BenchNumberNormalizer(unittest.TestCase):
    """100 calls to NumberNormalizer.normalize(ru) must finish in <50 ms."""

    BUDGET_MS = 50.0

    _PHRASES = [
        "встреча в два часа дня",
        "пятьсот рублей за штуку",
        "три тысячи двести пятьдесят один рубль",
        "первый этаж, второй подъезд",
        "двадцать пятое января две тысячи двадцать пятого года",
    ] * 20  # 100 phrases total

    def setUp(self):
        from core.number_normalizer import NumberNormalizer  # noqa: PLC0415
        self.nn = NumberNormalizer()

    def test_normalize_100_ru_phrases(self):
        phrases = self._PHRASES
        nn = self.nn
        elapsed = _elapsed_ms(
            lambda: [nn.normalize(p, language="ru") for p in phrases],
            iters=1,
        )
        budget = _budget(self.BUDGET_MS)
        print(f"\n  [bench] NumberNormalizer 100 RU phrases : {elapsed:.1f}ms  (budget {budget:.0f}ms)")
        self.assertLess(elapsed, budget,
                        f"NumberNormalizer 100× took {elapsed:.1f}ms > {budget:.0f}ms")


# ---------------------------------------------------------------------------
# 7. DateTimeNormalizer.normalize — 100 RU phrases
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_BENCH, "SKIP_BENCH set")
class BenchDateTimeNormalizer(unittest.TestCase):
    """100 calls to DateTimeNormalizer.normalize(ru) must finish in <50 ms."""

    BUDGET_MS = 50.0

    _PHRASES = [
        "встреча в пятнадцать ноль ноль",
        "созвон в восемь тридцать утра",
        "дедлайн первого февраля две тысячи двадцать пятого года",
        "в среду в полдень",
        "третье марта в девять утра",
    ] * 20  # 100 phrases total

    def setUp(self):
        from core.datetime_normalizer import DateTimeNormalizer  # noqa: PLC0415
        self.dn = DateTimeNormalizer()

    def test_normalize_100_ru_phrases(self):
        phrases = self._PHRASES
        dn = self.dn
        elapsed = _elapsed_ms(
            lambda: [dn.normalize(p, language="ru") for p in phrases],
            iters=1,
        )
        budget = _budget(self.BUDGET_MS)
        print(f"\n  [bench] DateTimeNormalizer 100 RU phrases : {elapsed:.1f}ms  (budget {budget:.0f}ms)")
        self.assertLess(elapsed, budget,
                        f"DateTimeNormalizer 100× took {elapsed:.1f}ms > {budget:.0f}ms")


# ---------------------------------------------------------------------------
# 8. EmotionDetector.detect — 100 sentences
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_BENCH, "SKIP_BENCH set")
class BenchEmotionDetector(unittest.TestCase):
    """100 calls to EmotionDetector.detect must finish in <80 ms."""

    BUDGET_MS = 80.0

    _SENTENCES = [
        "Отличный результат, очень рад!",
        "Это ужасно, я в шоке.",
        "Всё прошло нормально.",
        "Супер! Кайф!",
        "Грустно и тяжело на душе.",
        "Hello world, good morning!",
        "Это было неожиданно приятно.",
        "Ничего особенного.",
        "Полный провал, катастрофа.",
        "Просто текст без эмоций.",
    ] * 10  # 100 sentences total

    def setUp(self):
        from core.emotion_detector import EmotionDetector  # noqa: PLC0415
        self.ed = EmotionDetector()

    def test_detect_100_sentences(self):
        sentences = self._SENTENCES
        ed = self.ed
        elapsed = _elapsed_ms(
            lambda: [ed.detect(s) for s in sentences],
            iters=1,
        )
        budget = _budget(self.BUDGET_MS)
        print(f"\n  [bench] EmotionDetector 100 sentences : {elapsed:.1f}ms  (budget {budget:.0f}ms)")
        self.assertLess(elapsed, budget,
                        f"EmotionDetector 100× took {elapsed:.1f}ms > {budget:.0f}ms")


# ---------------------------------------------------------------------------
# 9. LanguageDetector.detect — 100 mixed RU/EN/ES sentences
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_BENCH, "SKIP_BENCH set")
class BenchLanguageDetector(unittest.TestCase):
    """100 calls to LanguageDetector.detect must finish in <30 ms."""

    BUDGET_MS = 30.0

    _TEXTS = [
        "Привет, как дела?",
        "Hello, how are you?",
        "Hola, cómo estás?",
        "Это тестовое предложение на русском.",
        "This is a test sentence in English.",
        "Esta es una frase de prueba en español.",
        "Транскрипция голоса через нейросеть.",
        "Voice transcription via neural network.",
        "Transcripción de voz mediante red neuronal.",
        "MLX Whisper работает быстро.",
    ] * 10  # 100 texts total

    def setUp(self):
        from core.language_detector import LanguageDetector  # noqa: PLC0415
        self.ld = LanguageDetector()

    def test_detect_100_mixed_texts(self):
        texts = self._TEXTS
        ld = self.ld
        elapsed = _elapsed_ms(
            lambda: [ld.detect(t) for t in texts],
            iters=1,
        )
        budget = _budget(self.BUDGET_MS)
        print(f"\n  [bench] LanguageDetector 100 mixed : {elapsed:.1f}ms  (budget {budget:.0f}ms)")
        self.assertLess(elapsed, budget,
                        f"LanguageDetector 100× took {elapsed:.1f}ms > {budget:.0f}ms")


# ---------------------------------------------------------------------------
# 10. SettingsService cache hit — 1000 cached get_setting() calls
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_BENCH, "SKIP_BENCH set")
class BenchSettingsServiceCacheHit(unittest.TestCase):
    """1000 cached_settings() calls (all cache hits) must finish in <10 ms.

    Verifies that the 5s TTL cache short-circuits disk I/O correctly.
    """

    BUDGET_MS = 10.0

    def setUp(self):
        import pathlib
        import tempfile
        from backend.state_store import StateStore  # noqa: PLC0415
        from backend.settings_service import SettingsService  # noqa: PLC0415

        self._tmpdir = tempfile.TemporaryDirectory()
        store = StateStore(pathlib.Path(self._tmpdir.name))
        store.save_settings({"stt_model": "balanced", "language": "ru", "llm_rewriter": True})
        self.svc = SettingsService(store)
        # Prime the cache with one call
        self.svc.cached_settings()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_cached_settings_1000_cache_hits(self):
        svc = self.svc
        elapsed = _elapsed_ms(svc.cached_settings, iters=1000)
        budget = _budget(self.BUDGET_MS)
        print(f"\n  [bench] SettingsService.cached_settings 1000× : {elapsed:.1f}ms  (budget {budget:.0f}ms)")
        self.assertLess(elapsed, budget,
                        f"cached_settings 1000× took {elapsed:.1f}ms > {budget:.0f}ms")


# ---------------------------------------------------------------------------
# 11. StateStore.add_history_item — 100 items (mock disk)
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_BENCH, "SKIP_BENCH set")
class BenchStateStoreAppend(unittest.TestCase):
    """StateStore.add_history_item 100× must finish in <100 ms (includes file I/O)."""

    BUDGET_MS = 100.0

    def test_append_100_items(self):
        import pathlib
        import tempfile
        from backend.state_store import StateStore  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(pathlib.Path(tmpdir))
            texts = [f"Запись номер {i} — тестовое предложение для бенчмарка." for i in range(100)]

            elapsed = _elapsed_ms(
                lambda: store.add_history_item(
                    text=texts[0],
                    paste_status="ok",
                ),
                iters=100,
            )

        budget = _budget(self.BUDGET_MS)
        print(f"\n  [bench] StateStore.add_history_item 100× : {elapsed:.1f}ms  (budget {budget:.0f}ms)")
        self.assertLess(elapsed, budget,
                        f"StateStore.add_history_item 100× took {elapsed:.1f}ms > {budget:.0f}ms")


# ---------------------------------------------------------------------------
# 12. IPCThrottle.check_rate — 1000 calls
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_BENCH, "SKIP_BENCH set")
class BenchIPCThrottle(unittest.TestCase):
    """IPCThrottle.check_rate 1000 calls must finish in <20 ms."""

    BUDGET_MS = 20.0

    def setUp(self):
        from backend.ipc_throttle import IPCThrottle  # noqa: PLC0415
        # Use high limits so no calls are throttled — pure overhead measurement
        self.throttle = IPCThrottle(limits={"heavy": 10000, "medium": 10000, "light": 10000})

    def test_check_rate_1000_calls(self):
        throttle = self.throttle
        elapsed = _elapsed_ms(
            lambda: throttle.check_rate("get_history"),
            iters=1000,
        )
        budget = _budget(self.BUDGET_MS)
        print(f"\n  [bench] IPCThrottle.check_rate 1000× : {elapsed:.1f}ms  (budget {budget:.0f}ms)")
        self.assertLess(elapsed, budget,
                        f"IPCThrottle.check_rate 1000× took {elapsed:.1f}ms > {budget:.0f}ms")


if __name__ == "__main__":
    unittest.main(verbosity=2)
