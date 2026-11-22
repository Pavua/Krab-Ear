"""Regression tests for fail-closed privacy guard in RealtimePartialTranscriber
(W1763 — MED privacy fail-open fix).

Before the fix, if privacy_getter() raised an exception the except block only
logged a debug message and fell through to self._event_bus.emit(), leaking the
partial transcript text.

After the fix the except block emits a structured logger.warning and hits
``continue`` — the emit is suppressed (fail closed).

Covers:
  - test_privacy_getter_raises_suppresses_emit  (primary regression test)
  - test_privacy_getter_raises_logs_warning_not_transcript
  - test_privacy_getter_intermittent_raise_suppresses_only_affected_iterations
"""

from __future__ import annotations

import logging
import sys
import os
import threading
import time
import unittest
from unittest.mock import MagicMock

# Path setup — нужен для запуска standalone из корня репо.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_KRAB_EAR = os.path.join(_PROJECT_ROOT, "KrabEar")
if _KRAB_EAR not in sys.path:
    sys.path.insert(0, _KRAB_EAR)

import numpy as np

from backend.realtime_partial import RealtimePartialTranscriber, _REALTIME_PARTIAL_TYPE


# ---------------------------------------------------------------------------
# Helpers (переиспользуем паттерн из test_realtime_partial_privacy_W1200.py)
# ---------------------------------------------------------------------------

def _make_audio(duration_sec: float = 3.0, sr: int = 16000) -> np.ndarray:
    return np.zeros(int(sr * duration_sec), dtype=np.float32)


def _make_recorder(initial_duration_sec: float = 3.0) -> MagicMock:
    """Recorder чей snapshot_audio возвращает нарастающую длительность.

    Гарантирует прохождение gate ``(duration_sec - last_transcribed_duration) >= 0.5``
    на каждой итерации воркера.
    """
    recorder = MagicMock()
    recorder.sample_rate = 16000

    call_count = [0]

    def _snapshot(max_duration_sec: float = 8.0):
        call_count[0] += 1
        dur = initial_duration_sec + call_count[0] * 1.0
        audio = _make_audio(min(dur, max_duration_sec))
        return (audio, dur)

    recorder.snapshot_audio.side_effect = _snapshot
    return recorder


def _make_transcriber(text: str = "секретный частичный транскрипт") -> MagicMock:
    t = MagicMock()
    t.transcribe_preview.return_value = {"text": text}
    return t


class _SpyBus:
    """Минимальная EventBus заглушка, записывающая все вызовы emit."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._lock = threading.Lock()
        self.partial_emitted = threading.Event()

    def emit(self, event_type: str, payload: dict) -> None:
        with self._lock:
            self.calls.append((event_type, payload))
        if event_type == _REALTIME_PARTIAL_TYPE:
            self.partial_emitted.set()

    def partial_call_count(self) -> int:
        with self._lock:
            return sum(1 for et, _ in self.calls if et == _REALTIME_PARTIAL_TYPE)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRealtimePartialPrivacyFailClosed(unittest.TestCase):
    """Проверяет поведение privacy guard при исключении в privacy_getter.

    До исправления W1763: except блок логировал debug и проваливался насквозь →
    emit вызывался → утечка транскрипта.

    После исправления: except логирует WARNING + continue → emit подавлен.
    """

    def test_privacy_getter_raises_suppresses_emit(self):
        """Когда privacy_getter() бросает исключение, emit НЕ должен вызываться.

        Это основной регрессионный тест для W1763.
        Fail-before: emit вызывался (fail-open).
        Pass-after:  emit не вызывается (fail-closed).
        """
        recorder = _make_recorder(initial_duration_sec=3.0)
        transcriber = _make_transcriber("приватный текст который не должен утечь")
        bus = _SpyBus()

        # Getter всегда бросает — состояние приватности неизвестно
        def always_raises():
            raise RuntimeError("settings service unavailable")

        rpt = RealtimePartialTranscriber(
            transcriber=transcriber,
            recorder=recorder,
            event_bus=bus,
            interval_sec=0.05,
            buffer_sec=2.0,
            privacy_getter=always_raises,
        )
        rpt.start(session_id="sess-w1763-raises")
        try:
            # Даём воркеру несколько итераций — если была бы утечка, emit произошёл бы
            time.sleep(0.4)
        finally:
            rpt.stop(timeout_sec=2.0)

        # ГЛАВНАЯ ПРОВЕРКА: никаких partial_transcript событий не должно быть
        partial_count = bus.partial_call_count()
        self.assertEqual(
            partial_count,
            0,
            f"Privacy getter raised but realtime.partial_transcript was emitted "
            f"{partial_count} time(s) — fail-open privacy leak! (W1763 regression)",
        )

    def test_privacy_getter_raises_logs_warning_not_transcript(self):
        """Проверяет что warning логируется БЕЗ текста транскрипта.

        Требование: extra dict содержит 'error' (тип исключения), не текст.
        """
        recorder = _make_recorder(initial_duration_sec=3.0)
        transcriber = _make_transcriber("конфиденциальные данные")
        bus = _SpyBus()

        warning_records: list[logging.LogRecord] = []

        all_records: list[logging.LogRecord] = []

        class _CapturingHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                all_records.append(record)  # ВСЕ уровни — утечка может быть не в WARNING
                if record.levelno == logging.WARNING:
                    warning_records.append(record)

        handler = _CapturingHandler()
        target_logger = logging.getLogger("KrabEar.RealtimePartial")
        target_logger.addHandler(handler)
        original_level = target_logger.level
        target_logger.setLevel(logging.DEBUG)

        class _PermissionError(PermissionError):
            pass

        def raises_permission_error():
            raise _PermissionError("DB locked")

        rpt = RealtimePartialTranscriber(
            transcriber=transcriber,
            recorder=recorder,
            event_bus=bus,
            interval_sec=0.05,
            buffer_sec=2.0,
            privacy_getter=raises_permission_error,
        )
        rpt.start(session_id="sess-w1763-log")
        try:
            time.sleep(0.3)
        finally:
            rpt.stop(timeout_sec=2.0)
            target_logger.removeHandler(handler)
            target_logger.setLevel(original_level)

        # Должен быть хотя бы один warning
        self.assertGreater(
            len(warning_records),
            0,
            "Expected at least one WARNING log record when privacy_getter raises",
        )

        # Warning message должен упоминать fail-safe, а не текст транскрипта
        first_warn = warning_records[0]
        self.assertIn("failing safe", first_warn.getMessage())
        # extra должен содержать 'error' с типом исключения
        self.assertEqual(getattr(first_warn, "error", None), "_PermissionError")
        # Текст транскрипта НЕ должен быть нигде: ни в сообщениях, ни в structured-полях,
        # ни в exc_info/stack_info — по ВСЕМ записям и уровням.
        marker = "конфиденциальные данные"
        self.assertTrue(all_records, "логгер не записал ни одной записи")
        for rec in all_records:
            blob = " ".join(
                [rec.getMessage(), repr(rec.args), repr(rec.exc_info), str(rec.stack_info)]
                + [repr(v) for k, v in vars(rec).items()
                   if k not in logging.LogRecord("", 0, "", 0, "", (), None).__dict__]
            )
            self.assertNotIn(marker, blob, f"текст транскрипта просочился в лог: {rec.getMessage()!r}")

        # Emit всё равно подавлен
        self.assertEqual(bus.partial_call_count(), 0)

    def test_privacy_getter_intermittent_raise_suppresses_only_affected_iterations(self):
        """Getter чередует False / raise: emit происходит ТОЛЬКО на итерациях с False.

        Каждый emit связан с решением privacy-проверки той же итерации: транскрайбер выдаёт
        разный текст на каждой итерации, getter записывает, какой итерации (тексту) какое
        решение досталось. Итог: множество эмитнутых текстов == тексты итераций с False;
        итерации с исключением не эмитят, а после них эмит восстанавливается.
        """
        recorder = _make_recorder(initial_duration_sec=3.0)
        bus = _SpyBus()

        n_transcriptions = [0]

        def _transcribe(*_a, **_k):
            n_transcriptions[0] += 1
            return {"text": f"text-{n_transcriptions[0]}"}

        transcriber = MagicMock()
        transcriber.transcribe_preview.side_effect = _transcribe

        decisions: list[tuple[int, str]] = []  # (номер итерации/текста, решение)
        done = threading.Event()
        calls = [0]

        def alternating_getter():
            calls[0] += 1
            it = n_transcriptions[0]  # transcribe предшествует getter в той же итерации
            if calls[0] % 2 == 0:
                decisions.append((it, "raise"))
                if len(decisions) >= 8:
                    done.set()
                raise ValueError("transient error")
            decisions.append((it, "ok"))
            if len(decisions) >= 8:
                done.set()
            return False

        rpt = RealtimePartialTranscriber(
            transcriber=transcriber,
            recorder=recorder,
            event_bus=bus,
            interval_sec=0.05,
            buffer_sec=2.0,
            privacy_getter=alternating_getter,
        )
        rpt.start(session_id="sess-w1763-alternating")
        try:
            self.assertTrue(done.wait(timeout=5.0), "worker не сделал нужного числа итераций")
        finally:
            rpt.stop(timeout_sec=2.0)

        snapshot = list(decisions)
        raised = {it for it, d in snapshot if d == "raise"}
        allowed = {it for it, d in snapshot if d == "ok"}
        self.assertGreaterEqual(len(raised), 2, "исключение getter не возникало — сценарий не выполнен")
        self.assertGreaterEqual(len(allowed), 2, "не было итераций с разрешением")

        emitted = {p["text"] for et, p in bus.calls if et == _REALTIME_PARTIAL_TYPE}
        for it in raised:
            self.assertNotIn(f"text-{it}", emitted, f"emit на итерации {it}, где getter бросил исключение")
        for it in allowed:
            self.assertIn(f"text-{it}", emitted, f"нет emit на итерации {it}, где getter вернул False")


if __name__ == "__main__":
    unittest.main()
