"""Коллектор метрик для мониторинга производительности и качества Krab Ear.

Служит для сбора данных о задержках (latency) и уверенности модели (confidence)
с последующей агрегацией в квантили (p50, p95, p99).
"""

import threading
import logging
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
        self.errors = 0
        self.total_requests = 0
        self._lock = threading.Lock()

    def record(self, latency_ms: float, confidence: float, is_error: bool = False) -> None:
        """Записывает результат выполнения одного запроса."""
        with self._lock:
            self.total_requests += 1
            if is_error:
                self.errors += 1
            else:
                self.latencies.append(latency_ms)
                self.confidences.append(confidence)

    def get_summary(self) -> Dict[str, Any]:
        """Возвращает агрегированный отчет по текущим метрикам в окне."""
        with self._lock:
            # Копируем данные для стабильности расчета
            lats = np.array(list(self.latencies))
            confs = np.array(list(self.confidences))

            if len(lats) == 0:
                return {
                    "total_requests": self.total_requests,
                    "error_rate": round(self.errors / self.total_requests, 4) if self.total_requests > 0 else 0,
                    "status": "waiting_data"
                }

            # Расчет квантилей и средних значений через numpy
            summary = {
                "total_requests": self.total_requests,
                "error_rate": round(self.errors / self.total_requests, 4),
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
