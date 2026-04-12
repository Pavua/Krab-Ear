"""Профайлер производительности для Krab Ear.

Отслеживает время выполнения методов и pipeline-стадий через:
- декоратор @profile
- контекстный менеджер start_span()
- скользящее окно 1000 последних вызовов на метод
"""

import time
import threading
import functools
import logging
from collections import deque
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

import numpy as np

logger = logging.getLogger("KrabEar.Backend.Profiler")


class SpanContext:
    """Контекстный менеджер для ручного отслеживания span'а."""

    def __init__(self, profiler: "PerformanceProfiler", name: str) -> None:
        self._profiler = profiler
        self._name = name
        self._start: float = 0.0

    def __enter__(self) -> "SpanContext":
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        elapsed_ms = (time.perf_counter() - self._start) * 1000.0
        self._profiler._record(self._name, elapsed_ms)


class PerformanceProfiler:
    """Потокобезопасный профайлер со скользящим окном (1000 вызовов на метод)."""

    def __init__(self, window_size: int = 1000) -> None:
        self._window_size = window_size
        # {method_name: deque of float (ms)}
        self._data: Dict[str, deque] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def profile(self, func):
        """Декоратор: автоматически записывает время выполнения метода."""
        name = func.__qualname__

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                self._record(name, elapsed_ms)

        return wrapper

    def start_span(self, name: str) -> SpanContext:
        """Возвращает контекстный менеджер для ручного трекинга span'а.

        Использование::

            with profiler.start_span("stt"):
                result = run_stt(audio)
        """
        return SpanContext(self, name)

    def get_profile_report(self) -> Dict[str, Any]:
        """Возвращает агрегированный отчёт по всем отслеживаемым методам."""
        with self._lock:
            snapshot = {name: list(timings) for name, timings in self._data.items()}

        methods: Dict[str, Dict[str, Any]] = {}
        total_time_ms = 0.0

        for name, timings in snapshot.items():
            if not timings:
                continue
            arr = np.array(timings)
            avg_ms = float(np.mean(arr))
            total_time_ms += avg_ms * len(arr)
            methods[name] = {
                "calls": len(arr),
                "avg_ms": round(avg_ms, 2),
                "p50_ms": round(float(np.percentile(arr, 50)), 2),
                "p95_ms": round(float(np.percentile(arr, 95)), 2),
                "max_ms": round(float(np.max(arr)), 2),
            }

        slowest = sorted(methods.keys(), key=lambda n: methods[n]["avg_ms"], reverse=True)[:10]

        return {
            "methods": methods,
            "slowest_methods": slowest,
            "total_profiled_time_sec": round(total_time_ms / 1000.0, 3),
        }

    def reset(self) -> None:
        """Сбрасывает все накопленные данные."""
        with self._lock:
            self._data.clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _record(self, name: str, elapsed_ms: float) -> None:
        with self._lock:
            if name not in self._data:
                self._data[name] = deque(maxlen=self._window_size)
            self._data[name].append(elapsed_ms)


# Глобальный синглтон для использования во всём приложении
profiler = PerformanceProfiler()
