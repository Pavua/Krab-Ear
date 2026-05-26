"""Коллектор метрик для мониторинга производительности и качества Krab Ear.

Служит для сбора данных о задержках (latency) и уверенности модели (confidence)
с последующей агрегацией в квантили (p50, p95, p99).
"""

import threading
import logging
import time
from collections import deque
from typing import Any, Dict

import numpy as np

logger = logging.getLogger("KrabEar.Backend.Metrics")


class MetricsCollector:
    """Потокобезопасный сборщик метрик со скользящим окном."""

    def __init__(self, window_size: int = 1000):
        """Инициализация коллектора с заданным размером окна истории."""
        self.window_size = window_size
        self.latencies = deque(maxlen=window_size)
        self.confidences = deque(maxlen=window_size)
        # error_events — bounded deque of timestamps for sliding-window error_rate.
        # Bounded by the same window_size so old errors expire as the window fills.
        self.error_events: deque = deque(maxlen=window_size)
        self.total_requests = 0
        self._lock = threading.Lock()

    @property
    def errors(self) -> int:
        """Обратная совместимость: число ошибок в текущем окне."""
        return len(self.error_events)

    def record(self, latency_ms: float, confidence: float, is_error: bool = False) -> None:
        """Записывает результат выполнения одного запроса."""
        with self._lock:
            self.total_requests += 1
            if is_error:
                self.error_events.append(time.monotonic())
            else:
                self.latencies.append(latency_ms)
                self.confidences.append(confidence)

    def _error_rate(self, n_lats: int) -> float:
        """Вычисляет error_rate относительно скользящего окна.

        Знаменатель = len(latencies) + len(error_events), т.е. суммарное
        количество событий в обоих bounded deque'ах окна. Оба ограничены
        maxlen=window_size, поэтому rate органически падает по мере заполнения
        окна чистыми запросами: числитель (error_events) стабилен или растёт
        медленнее, а знаменатель увеличивается.

        При полностью чистом окне len(error_events)=0 → rate=0.
        """
        window_count = n_lats + len(self.error_events)
        if window_count == 0:
            return 0.0
        return round(len(self.error_events) / window_count, 4)

    def get_summary(self) -> Dict[str, Any]:
        """Возвращает агрегированный отчет по текущим метрикам в окне."""
        with self._lock:
            # Копируем данные для стабильности расчета
            lats = np.array(list(self.latencies))
            confs = np.array(list(self.confidences))

            if len(lats) == 0:
                return {
                    "total_requests": self.total_requests,
                    "error_rate": self._error_rate(0),
                    "status": "waiting_data"
                }

            # Расчет квантилей и средних значений через numpy
            summary = {
                "total_requests": self.total_requests,
                "error_rate": self._error_rate(len(lats)),
                "window_size": len(lats),
                "stt_metrics": {
                    "latency_ms": {
                        "p50": round(float(np.percentile(lats, 50)), 2),
                        "p95": round(float(np.percentile(lats, 95)), 2),
                        "p99": round(float(np.percentile(lats, 99)), 2),
                        "avg": round(float(np.mean(lats)), 2)
                    },
                    "confidence": {
                        "avg": round(float(np.mean(confs)), 3),
                        "min": round(float(np.min(confs)), 3),
                        "max": round(float(np.max(confs)), 3)
                    }
                }
            }
            return summary


# Глобальный инстанс (Singleton) для использования во всем приложении
metrics = MetricsCollector()
