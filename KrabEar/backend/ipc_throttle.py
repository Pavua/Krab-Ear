"""Throttling для IPC-методов Krab Ear.

Предотвращает злоупотребление тяжёлыми методами (транскрибация, экспорт, суммаризация)
через алгоритм token bucket (token bucket algorithm).

Категории методов и их лимиты (вызовов/минуту):
  heavy  — 5/min  : transcribe_paths, export, summarize (тяжёлые фоновые операции)
  medium — 30/min : search, statistics (поиск и аналитика)
  light  — 120/min: ping, get_settings, start/stop_recording (и все остальные)

Все операции потокобезопасны через threading.Lock.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Set


# ---------------------------------------------------------------------------
# Категории методов
# ---------------------------------------------------------------------------

# Тяжёлые методы: CPU/GPU-интенсивные фоновые операции.
# start/stop_recording не включаем — это обычные пользовательские действия.
HEAVY_METHODS: Set[str] = {
    "transcribe_paths",         # импорт и транскрибация файлов
    "preview_transcribe_paths",  # предпросмотр транскрибации
    "summarize_text",           # LLM суммаризация текста
    "summarize_item",           # LLM суммаризация элемента истории
    "auto_summarize_batch",     # пакетная LLM суммаризация
    "export_history",           # полный экспорт истории
    "export_history_srt",       # SRT экспорт
    "export_history_csv",       # CSV экспорт
    "export_history_json",      # JSON экспорт
    "export_history_markdown",  # Markdown экспорт
    "export_obsidian",          # Obsidian экспорт
    "batch_export",             # пакетный экспорт
    "generate_daily_digest",    # генерация дайджеста
    "check_integrity",          # проверка целостности
    "analyze_quality_trends",   # анализ трендов качества
    "repair_integrity",         # исправление целостности
    "get_waveform",             # генерация waveform-данных
    "extract_action_items",     # LLM извлечение задач/решений/вопросов из транскрипта
}

MEDIUM_METHODS: Set[str] = {
    "search_history",           # текстовый поиск по истории
    "fuzzy_search",             # нечёткий поиск
    "search_by_speaker",        # поиск по спикеру
    "search_by_tag",            # поиск по тегу
    "search_annotations",       # поиск по заметкам
    "get_history_stats",        # статистика истории
    "get_history_overview",     # обзор истории
    "get_history_statistics",   # агрегированная статистика
    "word_frequency_analysis",  # частотный анализ слов
    "get_metrics_dashboard",    # метрики (расчёт перцентилей)
    "get_diagnostics",          # диагностика
    "get_recording_stats",      # статистика записи
    "get_usage_stats",          # статистика использования
    "get_system_info",          # системные ресурсы
    "filter_by_confidence",     # фильтрация по confidence
    "find_duplicates",          # поиск дублей
    "translate_text",           # перевод текста
    "get_glossary_suggestions",  # предложения для глоссария
    "get_vocabulary_suggestions",
    "get_event_log",            # лог событий
    "get_event_stats",          # статистика событий
    "replay_events",            # воспроизведение событий
    "get_error_report",         # отчёт об ошибках
    "get_error_stats",          # статистика ошибок
    "health_check",             # health check подсистем
    "get_disk_status",          # статус дискового пространства
    "get_storage_breakdown",    # разбивка использования диска
}

# Методы, полностью исключённые из throttling.
# Это операции жизненного цикла записи — пользователь должен иметь возможность
# вызывать их без ограничений (в т.ч. в тестах с 1000 циклами).
EXCLUDED_METHODS: Set[str] = {
    "start_recording",
    "stop_recording",
    "get_recording_state",
    "set_paste_status",
    "ping",
    # settings writes: slider drag может бёрстить 20+ событий/сек,
    # это legitimate write, не повод отклонять
    "set_settings",
    "get_settings",
    "apply_profile_preset",
    "list_profile_presets",
    "list_settings_backups",
    "restore_settings_backup",
    "create_manual_settings_backup",
    "translate_selection",  # Phase 2A: вызывается часто при выделении текста
    # Live subtitles: вызывается ~10-30 раз/сек по одному аудио-чанку
    "live_subs_ingest",
    "live_subs_stop",
    # Phase 3 safeguards: polling calls from auto-end monitor loop
    "call_estimate_cost",
    "call_check_auto_end",
}

# Всё остальное — light (120/min)

# Лимиты по категориям (вызовов/минуту)
_LIMITS: Dict[str, int] = {
    "heavy": 5,
    "medium": 30,
    "light": 120,
}


def _classify_method(method: str) -> str:
    """Возвращает категорию метода: 'heavy', 'medium' или 'light'.

    Возвращает None для методов из EXCLUDED_METHODS (throttling не применяется).
    """
    if method in HEAVY_METHODS:
        return "heavy"
    if method in MEDIUM_METHODS:
        return "medium"
    return "light"


# ---------------------------------------------------------------------------
# Token Bucket
# ---------------------------------------------------------------------------

class _TokenBucket:
    """Реализация token bucket для одного rate limit.

    capacity  — максимальное число токенов (= лимит вызовов за окно).
    rate      — скорость пополнения: tokens per second = capacity / 60.
    """

    __slots__ = ("capacity", "rate", "_tokens", "_last_refill")

    def __init__(self, capacity: int) -> None:
        self.capacity: float = float(capacity)
        self.rate: float = capacity / 60.0   # tokens/sec
        self._tokens: float = float(capacity)
        self._last_refill: float = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last_refill = now

    def consume(self) -> bool:
        """Попытка потребить один токен. Возвращает True если разрешено."""
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def wait_time(self) -> float:
        """Секунд до следующего доступного токена."""
        self._refill()
        if self._tokens >= 1.0:
            return 0.0
        deficit = 1.0 - self._tokens
        return deficit / self.rate


# ---------------------------------------------------------------------------
# IPCThrottle
# ---------------------------------------------------------------------------

class IPCThrottle:
    """Потокобезопасный throttle для IPC-методов.

    Использует token bucket per-method. Ведёт статистику вызовов и throttled count.

    Пример использования::

        throttle = IPCThrottle()
        if not throttle.check_rate("transcribe_paths"):
            wait = throttle.get_wait_time("transcribe_paths")
            return error(f"rate_limit: retry in {wait:.1f}s")
    """

    def __init__(
        self,
        limits: Dict[str, int] | None = None,
    ) -> None:
        """
        Args:
            limits: Переопределение лимитов {"heavy": N, "medium": N, "light": N}.
                    Если None — используются дефолтные лимиты.
        """
        self._limits: Dict[str, int] = {**_LIMITS, **(limits or {})}
        self._lock = threading.Lock()

        # Бакеты per-method, создаются лениво
        self._buckets: Dict[str, _TokenBucket] = {}

        # Статистика
        self._call_counts: Dict[str, int] = {}
        self._throttled_counts: Dict[str, int] = {}
        self._total_calls: int = 0
        self._total_throttled: int = 0

    def _get_bucket(self, method: str) -> _TokenBucket:
        """Возвращает (или создаёт) бакет для метода. Вызывать под lock."""
        if method not in self._buckets:
            category = _classify_method(method)
            capacity = self._limits[category]
            self._buckets[method] = _TokenBucket(capacity)
        return self._buckets[method]

    def check_rate(self, method: str) -> bool:
        """Проверяет и потребляет rate-limit токен.

        Методы из EXCLUDED_METHODS всегда разрешены (True, без учёта в статистике).

        Returns:
            True  — вызов разрешён.
            False — вызов отклонён (rate limit exceeded).
        """
        if method in EXCLUDED_METHODS:
            return True

        with self._lock:
            bucket = self._get_bucket(method)
            allowed = bucket.consume()
            self._call_counts[method] = self._call_counts.get(method, 0) + 1
            self._total_calls += 1
            if not allowed:
                self._throttled_counts[method] = self._throttled_counts.get(method, 0) + 1
                self._total_throttled += 1
            return allowed

    def get_wait_time(self, method: str) -> float:
        """Возвращает секунды до следующего разрешённого вызова.

        Returns 0.0 если вызов уже разрешён (токены есть) или метод исключён.
        """
        if method in EXCLUDED_METHODS:
            return 0.0
        with self._lock:
            bucket = self._get_bucket(method)
            return bucket.wait_time()

    def get_throttle_stats(self) -> dict:
        """Возвращает статистику throttling.

        Returns:
            {
                "total_calls": int,
                "total_throttled": int,
                "methods": {
                    "<method>": {
                        "calls": int,
                        "throttled": int,
                        "category": str,
                        "limit_per_minute": int,
                    },
                    ...
                }
            }
        """
        with self._lock:
            all_methods = set(self._call_counts) | set(self._throttled_counts)
            methods_stats = {}
            for m in all_methods:
                category = _classify_method(m)
                methods_stats[m] = {
                    "calls": self._call_counts.get(m, 0),
                    "throttled": self._throttled_counts.get(m, 0),
                    "category": category,
                    "limit_per_minute": self._limits[category],
                }
            return {
                "total_calls": self._total_calls,
                "total_throttled": self._total_throttled,
                "methods": methods_stats,
            }

    def reset_stats(self) -> None:
        """Сбрасывает статистику (полезно для тестов)."""
        with self._lock:
            self._call_counts.clear()
            self._throttled_counts.clear()
            self._total_calls = 0
            self._total_throttled = 0
