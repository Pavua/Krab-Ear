"""retry_strategy.py — Smart retry logic for STT failures.

Предоставляет RetryConfig и RetryStrategy для повторных попыток при ошибках
транскрибации (таймаут, model_error и т.п.) с экспоненциальным backoff.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class RetryConfig:
    """Конфигурация стратегии повторных попыток."""

    max_retries: int = 2
    backoff_factor: float = 1.5
    # Типы ошибок, при которых выполняется retry
    retry_on: list[str] = field(default_factory=lambda: ["timeout", "model_error"])


class RetryStrategy:
    """Стратегия повторных попыток с экспоненциальным backoff.

    Пример использования::

        cfg = RetryConfig(max_retries=3, backoff_factor=2.0)
        strategy = RetryStrategy(cfg)
        result = strategy.execute_with_retry(my_stt_fn, audio_data)
    """

    def __init__(self, config: RetryConfig | None = None) -> None:
        self.config = config or RetryConfig()
        self._total_attempts: int = 0
        self._total_successes: int = 0
        self._total_retries: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_retry(self, error: Exception, attempt: int) -> bool:
        """Возвращает True, если ошибку стоит повторить на попытке *attempt*.

        *attempt* — номер уже выполненных попыток (0-based), т.е. значение
        до следующей попытки.  Повтор разрешён, если:
        1. attempt < max_retries
        2. тип ошибки присутствует в retry_on
        """
        if attempt >= self.config.max_retries:
            return False
        return self._classify_error(error) in self.config.retry_on

    def get_delay(self, attempt: int) -> float:
        """Возвращает задержку перед попыткой *attempt* (секунды).

        Формула: backoff_factor ^ attempt  (attempt 0 → 1.0×, attempt 1 → factor^1, …)
        """
        return self.config.backoff_factor ** attempt

    def execute_with_retry(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Выполняет *fn* с повторными попытками согласно конфигу.

        Raises:
            Exception: последняя пойманная ошибка, если все попытки исчерпаны.
        """
        last_error: Exception | None = None
        self._total_attempts += 1

        for attempt in range(self.config.max_retries + 1):
            try:
                result = fn(*args, **kwargs)
                self._total_successes += 1
                self._total_retries += attempt  # сколько retry до успеха
                return result
            except Exception as exc:
                last_error = exc
                if not self.should_retry(exc, attempt):
                    logger.debug(
                        "retry_strategy: нет смысла повторять (%s), попытка %d/%d",
                        type(exc).__name__, attempt + 1, self.config.max_retries + 1,
                    )
                    break
                delay = self.get_delay(attempt)
                logger.warning(
                    "retry_strategy: ошибка %s (%s), повтор через %.2fs (попытка %d/%d)",
                    type(exc).__name__, exc, delay, attempt + 1, self.config.max_retries,
                )
                time.sleep(delay)

        raise last_error  # type: ignore[misc]

    def get_retry_stats(self) -> dict[str, Any]:
        """Возвращает статистику использования стратегии.

        Keys:
            total_calls       — количество вызовов execute_with_retry
            total_retries     — суммарное число retry-попыток (не первых)
            total_successes   — количество успешных завершений
            success_rate      — доля успешных вызовов (0.0–1.0)
            avg_retries_per_success — среднее число retry на успешный вызов
        """
        success_rate = (
            self._total_successes / self._total_attempts
            if self._total_attempts > 0
            else 0.0
        )
        avg_retries = (
            self._total_retries / self._total_successes
            if self._total_successes > 0
            else 0.0
        )
        return {
            "total_calls": self._total_attempts,
            "total_retries": self._total_retries,
            "total_successes": self._total_successes,
            "success_rate": success_rate,
            "avg_retries_per_success": avg_retries,
        }

    def reset_stats(self) -> None:
        """Сбрасывает накопленную статистику."""
        self._total_attempts = 0
        self._total_successes = 0
        self._total_retries = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_error(error: Exception) -> str:
        """Классифицирует исключение в строковый тип для retry_on."""
        import concurrent.futures

        if isinstance(error, concurrent.futures.TimeoutError):
            return "timeout"
        if isinstance(error, TimeoutError):
            return "timeout"
        name = type(error).__name__.lower()
        if "timeout" in name:
            return "timeout"
        if isinstance(error, (MemoryError, OSError)):
            return "model_error"
        if isinstance(error, RuntimeError):
            return "model_error"
        return "unknown"
