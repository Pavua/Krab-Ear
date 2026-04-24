"""E2E performance baseline benchmarks for daily-use regression detection.

Each test measures a real end-to-end flow with mocked STT/MLX engine and
prints timing in a standardised format:
    [BENCH] <name>: <elapsed:.3f>s (<ops/s> op/s)

CI limits are 3-5x typical local timing to avoid flaking on shared runners.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import numpy as np  # noqa: E402

from backend.service import BackendService  # noqa: E402
from backend.settings_service import SettingsService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
from backend.translation_service import TranslationService  # noqa: E402
from backend.translator import TranslationResult  # noqa: E402
from backend.html_report import HTMLReportGenerator  # noqa: E402
from core.utils import TextUtils  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fake collaborators
# ---------------------------------------------------------------------------

class _FakeRecorder:
    """Записывает fake audio без реального микрофона."""

    def __init__(self) -> None:
        self.is_recording = False
        self.sample_rate = 16000

    def start(self) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        if not self.is_recording:
            return None
        self.is_recording = False
        # Return speech-like audio (not pure silence, to pass silence guard)
        t = np.linspace(0.0, 1.0, 16000, endpoint=False, dtype=np.float32)
        audio = (0.06 * np.sin(2.0 * np.pi * 210.0 * t)).astype(np.float32)
        return audio, 1.0

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        return np.ones(32000, dtype=np.float32), 2.0


class _FakeRecorderLong(_FakeRecorder):
    """Returns 60s of fake speech-like audio."""

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        if not self.is_recording:
            return None
        self.is_recording = False
        t = np.linspace(0.0, 60.0, 16000 * 60, endpoint=False, dtype=np.float32)
        audio = (0.06 * np.sin(2.0 * np.pi * 210.0 * t)).astype(np.float32)
        return audio, 60.0


class _FakeTranscriber:
    """Returns a canned transcript immediately (no MLX inference)."""

    def __init__(self, delay: float = 0.0) -> None:
        self._counter = 0
        self._delay = delay

    def transcribe(
        self,
        audio_data: Any,
        quality_profile: str = "balanced",
        cleanup_profile: str = "soft",
        domain: str = "casual",
        extra_vocabulary=None,
        lang_hint=None,
    ) -> str:
        if self._delay:
            time.sleep(self._delay)
        self._counter += 1
        return f"fake transcript #{self._counter}"

    def transcribe_preview(self, audio_data: Any, quality_profile: str = "balanced") -> str:
        return f"preview #{self._counter}"


class _FakeTranslator:
    """Returns a canned translation immediately."""

    def translate(self, text: str, mode: str, network_mode: str,
                  translation_style: str = "neutral",
                  glossary=None) -> Any:
        return TranslationResult(
            text=f"FAKE:{text}",
            status="ok",
            source_lang="ru",
            target_lang="es",
            mode=mode,
            engine="fake",
        )


def _unique_text(i: int) -> str:
    return f"Транскрипция номер {i}: тест {uuid.uuid4().hex[:8]} деталях проекта"


def _make_service(
    tmp_path: Path,
    recorder=None,
    transcriber=None,
) -> BackendService:
    store = StateStore(tmp_path / "data")
    return BackendService(
        store=store,
        recorder=recorder or _FakeRecorder(),
        transcriber=transcriber or _FakeTranscriber(),
        translator=_FakeTranslator(),
    )


# ---------------------------------------------------------------------------
# 1. Record → transcribe (short, 1s fake audio)
# ---------------------------------------------------------------------------

class RecordTranscribeShortBenchmark(unittest.TestCase):
    """E2E: start_recording → stop_recording (1s audio) → transcript latency."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = _make_service(Path(self.tmp.name))

    def _request(self, method: str, params=None) -> dict:
        return self.service.handle_request(
            {"id": "bench", "method": method, "params": params or {}}
        )

    def test_record_to_transcript_e2e_short(self) -> None:
        """1s fake audio → complete record+transcribe cycle < 8s (CI)."""
        start = self._request("start_recording")
        self.assertTrue(start.get("ok"), f"start_recording failed: {start}")

        t0 = time.perf_counter()
        stop = self._request("stop_recording")
        elapsed = time.perf_counter() - t0

        self.assertTrue(stop.get("ok"), f"stop_recording failed: {stop}")
        print(
            f"\n[BENCH] record_to_transcript_e2e_short: {elapsed:.3f}s"
            f"  (goal <2s, CI limit <8s)"
        )
        self.assertLess(elapsed, 8.0,
                        f"E2E short record+transcribe took {elapsed:.3f}s (CI limit 8.0s)")


# ---------------------------------------------------------------------------
# 2. Record → transcribe (long, 60s fake audio with slight delay)
# ---------------------------------------------------------------------------

class RecordTranscribeLongBenchmark(unittest.TestCase):
    """E2E: start_recording → stop_recording (60s audio, mocked) < 30s (CI)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Slight delay simulates chunked-transcription overhead
        self.service = _make_service(
            Path(self.tmp.name),
            recorder=_FakeRecorderLong(),
            transcriber=_FakeTranscriber(delay=0.01),
        )

    def _request(self, method: str, params=None) -> dict:
        return self.service.handle_request(
            {"id": "bench", "method": method, "params": params or {}}
        )

    def test_record_to_transcript_e2e_long(self) -> None:
        """60s fake audio → complete record+transcribe cycle < 30s (CI)."""
        self._request("start_recording")

        t0 = time.perf_counter()
        stop = self._request("stop_recording")
        elapsed = time.perf_counter() - t0

        self.assertTrue(stop.get("ok"), f"stop_recording failed: {stop}")
        print(
            f"\n[BENCH] record_to_transcript_e2e_long: {elapsed:.3f}s"
            f"  (goal <15s, CI limit <30s)"
        )
        self.assertLess(elapsed, 30.0,
                        f"E2E long record+transcribe took {elapsed:.3f}s (CI limit 30.0s)")


# ---------------------------------------------------------------------------
# 3. History query p99 — 5000 items, 100 queries
# ---------------------------------------------------------------------------

class HistoryQueryP99Benchmark(unittest.TestCase):
    """5000 items, 100 search queries — p99 latency < 500ms (CI)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        for i in range(5000):
            store.add_history_item(text=_unique_text(i), paste_status="ok")
        self.store = store

    def test_history_query_p99(self) -> None:
        queries = [f"проект {i % 50}" for i in range(100)]
        latencies: list[float] = []
        for q in queries:
            t0 = time.perf_counter()
            self.store.search_history(query=q, cursor=None, limit=50)
            latencies.append(time.perf_counter() - t0)

        latencies.sort()
        p99 = latencies[int(0.99 * len(latencies)) - 1]
        mean = sum(latencies) / len(latencies)
        print(
            f"\n[BENCH] history_query_p99: p99={p99 * 1000:.1f}ms"
            f"  mean={mean * 1000:.1f}ms  (goal p99 <50ms, CI <500ms)"
        )
        self.assertLess(p99, 0.5,
                        f"History query p99={p99 * 1000:.1f}ms exceeded 500ms CI limit")


# ---------------------------------------------------------------------------
# 4. Translation round-trip — 100 strings
# ---------------------------------------------------------------------------

class TranslationRoundTripBenchmark(unittest.TestCase):
    """TranslationService.handle_translate_text × 100 < 2s total (CI)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        settings_svc = SettingsService(store=store)
        self.svc = TranslationService(
            translator=_FakeTranslator(),
            store=store,
            cached_settings=settings_svc.cached_settings,
            invalidate_settings_cache=settings_svc.invalidate_cache,
        )

    def test_translation_round_trip(self) -> None:
        texts = [f"Тестовая фраза номер {i} для перевода" for i in range(100)]
        t0 = time.perf_counter()
        for text in texts:
            result = self.svc.handle_translate_text(
                {"text": text, "translation_mode": "ru_to_es"}
            )
            self.assertIn("text", result)
        elapsed = time.perf_counter() - t0
        mean_ms = elapsed / 100 * 1000
        ops_per_sec = 100 / elapsed if elapsed > 0 else float("inf")
        print(
            f"\n[BENCH] translation_round_trip 100x: {elapsed:.3f}s"
            f"  mean={mean_ms:.1f}ms/op  {ops_per_sec:.0f} op/s"
            f"  (goal <100ms/op, CI total <2s)"
        )
        self.assertLess(elapsed, 2.0,
                        f"Translation 100x took {elapsed:.3f}s (CI limit 2.0s)")


# ---------------------------------------------------------------------------
# 5. Settings set/get round-trip — 1000 cycles
# ---------------------------------------------------------------------------

class SettingsSetGetBenchmark(unittest.TestCase):
    """set_settings → get_settings × 1000 < 10s total / 10ms each (CI)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.svc = SettingsService(store=store)

    def test_settings_set_get_round_trip(self) -> None:
        t0 = time.perf_counter()
        for i in range(1000):
            self.svc.handle_set_settings({"quality_profile": "balanced"})
            got = self.svc.handle_get_settings({})
            self.assertIn("quality_profile", got)
        elapsed = time.perf_counter() - t0
        mean_ms = elapsed / 1000 * 1000
        print(
            f"\n[BENCH] settings_set_get_round_trip 1000x: {elapsed:.3f}s"
            f"  mean={mean_ms:.2f}ms/op"
            f"  (goal <5ms/op, CI total <10s)"
        )
        self.assertLess(elapsed, 10.0,
                        f"Settings 1000x took {elapsed:.3f}s (CI limit 10.0s)")


# ---------------------------------------------------------------------------
# 6. Audio normalization throughput — 100 fake audio arrays
# ---------------------------------------------------------------------------

class AudioNormalizationThroughputBenchmark(unittest.TestCase):
    """TextUtils.cleanup_strict (text normalization proxy) × 100 files."""

    def test_audio_normalization_throughput(self) -> None:
        # Generate 100 fake "raw transcript" texts that simulate post-STT cleanup
        samples = [
            f"  Привет, это тест номер {i}!  Повторяю, тест {i}.  " * 3
            for i in range(100)
        ]

        t0 = time.perf_counter()
        results = [TextUtils.cleanup_transcript(s, profile="strict") for s in samples]
        elapsed = time.perf_counter() - t0

        self.assertEqual(len(results), 100)
        throughput = 100 / elapsed if elapsed > 0 else float("inf")
        print(
            f"\n[BENCH] audio_normalization_throughput 100x:"
            f" {elapsed:.3f}s  {throughput:.0f} files/s"
            f"  (goal >50 files/s, CI elapsed <2s)"
        )
        self.assertLess(elapsed, 2.0,
                        f"Text normalization 100x took {elapsed:.3f}s (CI limit 2.0s)")


# ---------------------------------------------------------------------------
# 7. HTML report generation — dashboard from 500 history items
# ---------------------------------------------------------------------------

class HtmlReportGenerationBenchmark(unittest.TestCase):
    """HTMLReportGenerator.generate_report(500 items) < 5s (CI)."""

    def setUp(self) -> None:
        self.generator = HTMLReportGenerator()
        self.items = [
            {
                "id": str(uuid.uuid4()),
                "text": _unique_text(i),
                "translated_text": f"ES:{i}",
                "created_at": f"2026-04-{(i % 28) + 1:02d}T10:00:00",
                "paste_status": "ok",
                "source_lang": "ru",
                "duration_sec": float(i % 120 + 5),
                "confidence": 0.85,
            }
            for i in range(500)
        ]

    def test_html_report_generation(self) -> None:
        t0 = time.perf_counter()
        html = self.generator.generate_report(
            self.items, title="Benchmark Report"
        )
        elapsed = time.perf_counter() - t0

        self.assertIn("<html", html.lower())
        size_kb = len(html) / 1024
        print(
            f"\n[BENCH] html_report_generation 500 items:"
            f" {elapsed:.3f}s  size={size_kb:.0f} KB"
            f"  (goal <1s, CI <5s)"
        )
        self.assertLess(elapsed, 5.0,
                        f"HTML report generation took {elapsed:.3f}s (CI limit 5.0s)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
