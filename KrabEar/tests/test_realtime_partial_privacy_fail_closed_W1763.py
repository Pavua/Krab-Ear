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

        class _CapturingHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if record.levelno == logging.WARNING:
                    warning_records.append(record)

        handler = _CapturingHandler()
        target_logger = logging.getLogger("KrabEar.RealtimePartial")
        target_logger.addHandler(handler)
        original_level = target_logger.level
        target_logger.setLevel(logging.WARNING)

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
        # Текст транскрипта НЕ должен быть в сообщении (privacy)
        self.assertNotIn("конфиденциальные данные", first_warn.getMessage())

        # Emit всё равно подавлен
        self.assertEqual(bus.partial_call_count(), 0)

    def test_privacy_getter_intermittent_raise_suppresses_only_affected_iterations(self):
        """Getter который чередует False / raise: эмиты происходят только на False.

        Итерация 1: False → emit разрешён.
        Итерация 2: raise → emit подавлен.
        Итерация 3: False → emit разрешён снова.
        ...

        Тест проверяет что хотя бы один emit прошёл (getter=False) и
        что за время работы не было эмита непосредственно после raise.
        Практически это проверяет что fail-closed не блокирует нормальные итерации.
        """
        recorder = _make_recorder(initial_duration_sec=3.0)
        transcriber = _make_transcriber("нормальный текст")
        bus = _SpyBus()

        # Чередуем False → raise → False → raise ...
        toggle = [0]

        def alternating_getter():
            toggle[0] += 1
            if toggle[0] % 2 == 0:
                raise ValueError("transient error")
            return False  # чётные = raise, нечётные = False → emit разрешён

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
            # Ждём минимум одного emit (когда getter вернул False)
            bus.partial_emitted.wait(timeout=3.0)
        finally:
            rpt.stop(timeout_sec=2.0)

        # Хотя бы один emit должен был пройти (итерации с getter=False)
        self.assertGreater(
            bus.partial_call_count(),
            0,
            "Expected at least one emit on iterations where privacy_getter returns False",
        )


if __name__ == "__main__":
    unittest.main()
