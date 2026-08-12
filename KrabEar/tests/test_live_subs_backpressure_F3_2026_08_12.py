"""F3 (2026-08-12) — backpressure тесты LiveSubsService: фоновый flush-воркер,
слот "последний выигрывает", таймаут финального flush.

Живой инцидент: `ingest()` выполнял STT синхронно в IPC-треде — при 2x-темпе
видео handle_request висел 180с (backstop) на КАЖДЫЙ чанк, сжигая коннект-
слоты и деградируя весь бэкенд (включая диктовку в то же окно). Фикс: ingest()
никогда не выполняет STT инлайн — снапшот буфера уходит в фоновый воркер через
слот размера 1 ("последний выигрывает"), а ingest() возвращается немедленно.

Спека: docs/superpowers/specs/2026-08-12-live-subs-backpressure-design.md §2.

Ни один тест здесь не ждёт РЕАЛЬНЫЙ STT — везде фейковый (MagicMock) transcriber.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest \
        KrabEar/tests/test_live_subs_backpressure_F3_2026_08_12.py -v
"""

from __future__ import annotations

import base64
import sys
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── path setup ────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import numpy as np  # noqa: E402

import backend.live_subs_service as live_subs_module  # noqa: E402
from backend.live_subs_service import LiveSubsService  # noqa: E402
from backend.translator import TranslationResult  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────

def _pcm_bytes(duration_sec: float, sample_rate: int = 16000) -> bytes:
    n = int(duration_sec * sample_rate)
    return np.zeros(n, dtype=np.int16).tobytes()


def _make_service(
    stt_text: str = "hello",
    translated: str = "привет",
    transcribe_side_effect=None,
) -> LiveSubsService:
    """LiveSubsService с фейковыми (MagicMock) зависимостями — без реального MLX."""
    transcriber = MagicMock()
    if transcribe_side_effect is not None:
        transcriber.transcribe.side_effect = transcribe_side_effect
    else:
        transcriber.transcribe.return_value = {"text": stt_text, "language": "en"}

    tr_result = TranslationResult(
        text=translated, status="ok", source_lang="en", target_lang="ru",
        mode="ru", engine="stub",
    )
    translator = MagicMock()
    translator.translate.return_value = tr_result

    return LiveSubsService(transcriber=transcriber, translator=translator)


# ── DoD: ingest() никогда не блокируется на STT ────────────────────────────────

class TestIngestNeverBlocksOnSTT(unittest.TestCase):
    """DoD: `ingest` возвращается немедленно даже когда STT занят."""

    def test_non_final_ingest_returns_immediately_during_slow_stt(self) -> None:
        """Не-финальный flush-триггер не ждёт STT — воркер обрабатывает его в фоне."""
        stt_started = threading.Event()
        stt_release = threading.Event()

        def _slow_transcribe(audio, **kwargs):  # noqa: ANN001
            stt_started.set()
            stt_release.wait(timeout=5.0)
            return {"text": "slow", "language": "en"}

        svc = _make_service(transcribe_side_effect=_slow_transcribe)
        started_at = time.monotonic()
        result = svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
        elapsed = time.monotonic() - started_at

        self.assertIsNone(result, "non-final ingest() не должен возвращать результат синхронно")
        self.assertLess(elapsed, 1.0, "ingest() не должен блокироваться, пока STT занят")
        self.assertTrue(stt_started.wait(timeout=2.0), "фоновый воркер не начал STT")

        stt_release.set()
        self.assertTrue(svc.wait_until_idle(timeout=3.0))
        svc.close()

    def test_final_ingest_blocks_only_up_to_timeout(self) -> None:
        """is_final=True ждёт синхронно, но не дольше _FINAL_FLUSH_TIMEOUT_SEC."""
        stt_release = threading.Event()

        def _hanging_transcribe(audio, **kwargs):  # noqa: ANN001
            stt_release.wait(timeout=5.0)
            return {"text": "late", "language": "en"}

        svc = _make_service(transcribe_side_effect=_hanging_transcribe)
        with patch.object(live_subs_module, "_FINAL_FLUSH_TIMEOUT_SEC", 0.3):
            started_at = time.monotonic()
            result = svc.ingest(_pcm_bytes(0.5), 16000, "off", True)
            elapsed = time.monotonic() - started_at

        self.assertIsNone(result, "таймаут истёк — ingest() должен вернуть None, не зависший результат")
        self.assertGreaterEqual(elapsed, 0.25, "ingest() не должен вернуться раньше таймаута")
        self.assertLess(elapsed, 2.0, "ingest() не должен ждать дольше явного таймаута")

        stt_release.set()  # отпускаем зависший воркер, чтобы не оставлять поток висящим
        self.assertTrue(svc.wait_until_idle(timeout=3.0))
        self.assertEqual(svc._completed_result["text"], "late", "воркер должен был доработать после release")
        svc.close()

    def test_handle_ingest_is_final_timeout_returns_explicit_status(self) -> None:
        """handle_ingest: is_final=True + таймаут → явный flush_timeout, не тихий accepted."""
        stt_release = threading.Event()

        def _hanging_transcribe(audio, **kwargs):  # noqa: ANN001
            stt_release.wait(timeout=5.0)
            return {"text": "late", "language": "en"}

        svc = _make_service(transcribe_side_effect=_hanging_transcribe)
        params = {
            "audio_chunk": base64.b64encode(_pcm_bytes(0.5)).decode(),
            "target_lang": "off",
            "sample_rate": 16000,
            "is_final": True,
        }
        with patch.object(live_subs_module, "_FINAL_FLUSH_TIMEOUT_SEC", 0.3):
            result = svc.handle_ingest(params)

        self.assertEqual(result["status"], "stopped")
        self.assertFalse(result["flushed"])
        self.assertEqual(result["reason"], "flush_timeout")

        stt_release.set()
        svc.wait_until_idle(timeout=3.0)
        svc.close()


# ── DoD: переполнение слота дропает СТАРОЕ окно + счётчик ─────────────────────

class TestBackpressureDropsOldWindow(unittest.TestCase):
    """DoD: слот занят → старое окно ДРОПАЕТСЯ, dropped_windows инкрементится."""

    def test_second_submission_drops_first_while_worker_busy(self) -> None:
        stt_started = threading.Event()
        stt_release = threading.Event()
        texts_seen: list[str] = []

        def _blocking_first_call(audio, **kwargs):  # noqa: ANN001
            if not texts_seen:
                stt_started.set()
                stt_release.wait(timeout=5.0)
            text = f"text-{len(texts_seen)}"
            texts_seen.append(text)
            return {"text": text, "language": "en"}

        svc = _make_service(transcribe_side_effect=_blocking_first_call)

        # Первое окно уходит в воркер и блокируется там (держит слот "занятым").
        svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
        self.assertTrue(stt_started.wait(timeout=2.0), "воркер не начал обработку первого окна")

        # Второе окно садится в pending-слот (тот пуст — воркер уже забрал первое).
        svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
        # Третье окно приходит, пока второе ЕЩЁ не обработано — второе дропается.
        svc.ingest(_pcm_bytes(3.0), 16000, "off", False)

        self.assertEqual(svc.dropped_windows, 1, "ровно одно окно должно быть дропнуто")

        stt_release.set()
        self.assertTrue(svc.wait_until_idle(timeout=3.0))
        # transcribe вызван дважды: за первое (блокирующее) и за третье (последнее
        # засабмиченное) окно — второе дропнуто целиком, без STT.
        self.assertEqual(svc._transcriber.transcribe.call_count, 2)
        svc.close()

    def test_dropped_windows_starts_at_zero(self) -> None:
        svc = _make_service()
        self.assertEqual(svc.dropped_windows, 0)
        svc.close()

    def test_drop_warning_logged_once_per_episode(self) -> None:
        """Лог дропа — один раз на эпизод, а не на каждый дроп (анти лог-шторм)."""
        stt_started = threading.Event()
        stt_release = threading.Event()
        call_n = {"n": 0}

        def _blocking_first_call(audio, **kwargs):  # noqa: ANN001
            call_n["n"] += 1
            if call_n["n"] == 1:
                stt_started.set()
                stt_release.wait(timeout=5.0)
            return {"text": "", "language": "en"}

        svc = _make_service(transcribe_side_effect=_blocking_first_call)
        svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
        self.assertTrue(stt_started.wait(timeout=2.0))

        with self.assertLogs("KrabEar.Backend.LiveSubsService", level="WARNING") as cm:
            svc.ingest(_pcm_bytes(3.0), 16000, "off", False)  # окно #2 — слот пуст, без дропа
            svc.ingest(_pcm_bytes(3.0), 16000, "off", False)  # дропает #2 → warning #1
            svc.ingest(_pcm_bytes(3.0), 16000, "off", False)  # дропает #3 → НЕ логируется повторно

        drop_logs = [line for line in cm.output if "дропнуто" in line]
        self.assertEqual(len(drop_logs), 1, f"Ожидался ровно один лог дропа на эпизод, получено: {drop_logs}")
        self.assertEqual(svc.dropped_windows, 2)

        stt_release.set()
        svc.wait_until_idle(timeout=3.0)
        svc.close()


# ── DoD: жизненный цикл воркера (ленивый старт, stop()/reset()/close()) ───────

class TestWorkerLazyLifecycle(unittest.TestCase):
    def test_worker_not_started_before_first_flush(self) -> None:
        svc = _make_service()
        self.assertIsNone(svc._worker_thread, "воркер не должен существовать до первого flush")
        svc.ingest(_pcm_bytes(1.0), 16000, "off", False)  # ниже порога — flush не запускается
        self.assertIsNone(svc._worker_thread, "накопление ниже порога не должно стартовать воркер")
        svc.close()

    def test_worker_starts_on_first_threshold_flush(self) -> None:
        svc = _make_service()
        svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
        self.assertIsNotNone(svc._worker_thread)
        self.assertTrue(svc._worker_thread.is_alive())
        self.assertTrue(svc._worker_thread.daemon, "воркер обязан быть daemon-потоком")
        svc.close()

    def test_worker_stopped_by_stop(self) -> None:
        svc = _make_service()
        svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
        self.assertIsNotNone(svc._worker_thread)
        svc.stop()
        self.assertIsNone(svc._worker_thread, "stop() должен остановить воркер")

    def test_worker_stopped_by_reset(self) -> None:
        svc = _make_service()
        svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
        svc.wait_until_idle(timeout=2.0)
        svc.reset()
        self.assertIsNone(svc._worker_thread, "reset() должен остановить воркер")

    def test_worker_stopped_by_close(self) -> None:
        svc = _make_service()
        svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
        svc.wait_until_idle(timeout=2.0)
        svc.close()
        self.assertIsNone(svc._worker_thread, "close() должен остановить воркер")

    def test_close_is_idempotent(self) -> None:
        svc = _make_service()
        svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
        svc.wait_until_idle(timeout=2.0)
        svc.close()
        svc.close()  # не должен бросить

    def test_close_on_never_started_worker_is_noop(self) -> None:
        svc = _make_service()
        svc.close()  # воркер никогда не стартовал — не должен бросить

    def test_worker_restarts_lazily_after_stop(self) -> None:
        """Новая сессия после stop() снова лениво стартует воркер (self-heal)."""
        svc = _make_service()
        svc.ingest(_pcm_bytes(1.0), 16000, "off", False)
        svc.stop()
        self.assertIsNone(svc._worker_thread)

        svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
        self.assertIsNotNone(svc._worker_thread)
        self.assertTrue(svc._worker_thread.is_alive())
        svc.close()

    def test_wait_until_idle_true_when_nothing_submitted(self) -> None:
        """wait_until_idle() на свежем сервисе без ingest() возвращает True немедленно."""
        svc = _make_service()
        self.assertTrue(svc.wait_until_idle(timeout=1.0))
        svc.close()

    def test_wait_until_idle_returns_false_on_genuine_timeout(self) -> None:
        stt_release = threading.Event()

        def _hanging_transcribe(audio, **kwargs):  # noqa: ANN001
            stt_release.wait(timeout=5.0)
            return {"text": "x", "language": "en"}

        svc = _make_service(transcribe_side_effect=_hanging_transcribe)
        svc.ingest(_pcm_bytes(3.0), 16000, "off", False)

        self.assertFalse(svc.wait_until_idle(timeout=0.2), "воркер занят — должен вернуть False")

        stt_release.set()
        self.assertTrue(svc.wait_until_idle(timeout=3.0))
        svc.close()


# ── DoD: воркер переживает исключения внутри STT/translate/emit ───────────────

class TestWorkerSurvivesExceptions(unittest.TestCase):
    """Fail-safe: воркер не должен умирать молча — следующее окно обрабатывается нормально."""

    def test_worker_continues_after_processing_exception(self) -> None:
        call_n = {"n": 0}

        def _flaky_transcribe(audio, **kwargs):  # noqa: ANN001
            call_n["n"] += 1
            if call_n["n"] == 1:
                raise RuntimeError("boom")
            return {"text": "recovered", "language": "en"}

        svc = _make_service(transcribe_side_effect=_flaky_transcribe)

        svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
        self.assertTrue(svc.wait_until_idle(timeout=2.0))
        self.assertIsNotNone(svc._worker_thread)
        self.assertTrue(svc._worker_thread.is_alive(), "воркер должен пережить исключение в _process_window")

        svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
        self.assertTrue(svc.wait_until_idle(timeout=2.0))
        self.assertEqual(svc._completed_result["text"], "recovered")
        svc.close()


# ── DoD: reset()/close() выкидывают ещё не обработанное окно ──────────────────

class TestDiscardPendingWindow(unittest.TestCase):
    """reset()/close() не должны дать уже засабмиченному окну доехать до STT/emit
    (privacy-purge race — окно, принятое ДО явного сброса состояния)."""

    def test_reset_discards_pending_window(self) -> None:
        svc = _make_service()
        with svc._worker_cond:
            svc._pending_window = {
                "seq": 99, "audio": np.zeros(10, dtype=np.float32),
                "sample_rate": 16000, "target_lang": "off",
                "start_ts": 0.0, "end_ts": 1.0,
            }
        svc.reset()
        with svc._worker_cond:
            self.assertIsNone(svc._pending_window, "reset() должен выкинуть окно из слота")

    def test_close_discards_pending_window(self) -> None:
        svc = _make_service()
        with svc._worker_cond:
            svc._pending_window = {
                "seq": 1, "audio": np.zeros(1, dtype=np.float32),
                "sample_rate": 16000, "target_lang": "off",
                "start_ts": 0.0, "end_ts": 0.0,
            }
        svc.close()
        with svc._worker_cond:
            self.assertIsNone(svc._pending_window, "close() должен выкинуть окно из слота")


# ── stop() flush_timeout контракт ──────────────────────────────────────────────

class TestBackendServiceCloseWiresLiveSubs(unittest.TestCase):
    """F3: BackendService.close() обязан остановить LiveSubsService flush-воркер —
    тот же класс CI daemon-thread-флейка, что и transcriber/disk_monitor/
    recap_scheduler (feedback_backendservice_teardown_ci.md). Конструируем
    BackendService через __new__ + минимальные стабы (паттерн
    test_live_subs_privacy_wiring_W1714.py::_build_minimal_backend), плюс
    стаб _meeting_svc — единственная НЕ-getattr-guarded зависимость в close().
    """

    def _build_service(self):
        from backend.service import BackendService
        from backend.state_store import StateStore

        tmp = Path(tempfile.mkdtemp())
        store = StateStore(data_dir=tmp)

        service = BackendService.__new__(BackendService)
        service.store = store
        service.recorder = MagicMock()
        service.transcriber = MagicMock()
        service.translator = MagicMock()
        service._meeting_svc = MagicMock()
        service._meeting_svc.close.return_value = True
        service._live_subs = LiveSubsService(
            transcriber=MagicMock(), translator=MagicMock(),
        )
        return service

    def test_close_calls_live_subs_close(self) -> None:
        service = self._build_service()
        mock_close = MagicMock()
        service._live_subs.close = mock_close

        service.close()

        mock_close.assert_called_once()

    def test_close_does_not_raise_when_live_subs_worker_never_started(self) -> None:
        service = self._build_service()
        service.close()  # не должен бросить — воркер ни разу не стартовал

    def test_close_stops_real_live_subs_worker_thread(self) -> None:
        """Не мок, а реальный LiveSubsService: close() реально гасит фоновый поток."""
        service = self._build_service()
        service._live_subs.ingest(_pcm_bytes(3.0), 16000, "off", False)
        self.assertIsNotNone(service._live_subs._worker_thread)

        service.close()

        self.assertIsNone(service._live_subs._worker_thread, "close() должен остановить воркер live-subs")


class TestStopFlushTimeout(unittest.TestCase):
    def test_stop_returns_flush_timeout_when_worker_too_slow(self) -> None:
        stt_release = threading.Event()

        def _hanging_transcribe(audio, **kwargs):  # noqa: ANN001
            stt_release.wait(timeout=5.0)
            return {"text": "late", "language": "en"}

        svc = _make_service(transcribe_side_effect=_hanging_transcribe)
        svc.ingest(_pcm_bytes(1.0), 16000, "off", False)  # < 3с — буфер копится, не флашится

        with patch.object(live_subs_module, "_FINAL_FLUSH_TIMEOUT_SEC", 0.2), \
             patch.object(live_subs_module, "_WORKER_JOIN_TIMEOUT_SEC", 0.2):
            result = svc.stop()

        self.assertEqual(result["status"], "stopped")
        self.assertFalse(result["flushed"])
        self.assertEqual(result.get("reason"), "flush_timeout")

        stt_release.set()  # отпускаем зависший воркер

    def test_stop_flushed_true_on_normal_completion(self) -> None:
        svc = _make_service(stt_text="normal stop")
        svc.ingest(_pcm_bytes(1.0), 16000, "off", False)
        result = svc.stop()
        self.assertEqual(result, {"status": "stopped", "flushed": True})


if __name__ == "__main__":
    unittest.main()
