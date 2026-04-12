"""Профилирование памяти Krab Ear — замер накладных расходов фреймворка без ML-моделей."""

from __future__ import annotations

import sys
import tempfile
import tracemalloc
from pathlib import Path

# Настройка пути — аналогично остальным тестам
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Фейковые коллабораторы (без ML, без сети)
# ---------------------------------------------------------------------------

import numpy as np

from backend.translator import TranslationResult


class FakeRecorder:
    """Минимальный стаб рекордера."""

    def __init__(self) -> None:
        self.is_recording = False
        self.sample_rate = 16000
        self._snapshot_counter = 0
        self.last_stop_trim_ms = 0
        self.last_stop_timeout_sec = 3.0

    def start(self) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        if not self.is_recording:
            return None
        self.is_recording = False
        self.last_stop_timeout_sec = timeout_sec
        self.last_stop_trim_ms = trim_tail_ms
        return np.zeros(16000, dtype=np.float32), 1.0

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        self._snapshot_counter += 1
        return np.ones(32000, dtype=np.float32), float(self._snapshot_counter)


class FakeTranscriber:
    """Стаб транскрибера — всегда возвращает детерминированную строку."""

    def __init__(self) -> None:
        self.counter = 0

    def transcribe(self, audio_data, quality_profile: str = "balanced",
                   cleanup_profile: str = "soft", domain: str = "casual",
                   extra_vocabulary=None, lang_hint=None) -> str:
        self.counter += 1
        return f"тестовая строка #{self.counter}"

    def transcribe_preview(self, audio_data, quality_profile: str = "balanced") -> str:
        return f"preview#{self.counter}"


class FakeTranslator:
    """Стаб переводчика — возвращает text без изменений."""

    def translate(self, text: str, mode: str, network_mode: str,
                  translation_style: str = "neutral",
                  glossary: dict | None = None) -> TranslationResult:
        return TranslationResult(
            text="",
            status="not_requested",
            source_lang="",
            target_lang="",
            mode="off",
            engine="fake",
        )


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _snapshot(label: str) -> tuple[int, int]:
    """Возвращает (current_bytes, peak_bytes) и печатает строку отчёта."""
    current, peak = tracemalloc.get_traced_memory()
    print(f"  {label:<30}  current={current / 1024 / 1024:.2f} MB  peak={peak / 1024 / 1024:.2f} MB")
    return current, peak


# ---------------------------------------------------------------------------
# Основной профилировщик
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 65)
    print("Krab Ear — профилирование памяти (без ML-моделей)")
    print("=" * 65)

    tracemalloc.start()

    # 1. Базовый уровень — после старта tracemalloc, до импортов сервисов
    baseline_current, baseline_peak = _snapshot("baseline (до импортов)")

    # 2. Импорт ключевых модулей
    from backend.service import BackendService  # noqa: F401 — нужен для прогрева
    from backend.state_store import StateStore   # noqa: F401
    from core.config import settings             # noqa: F401

    after_import_current, after_import_peak = _snapshot("после импортов")

    # 3. Создание экземпляра BackendService со стабами
    tmp = tempfile.TemporaryDirectory()
    store = StateStore(Path(tmp.name) / "data")
    service = BackendService(
        store=store,
        recorder=FakeRecorder(),
        transcriber=FakeTranscriber(),
        translator=FakeTranslator(),
    )

    after_init_current, after_init_peak = _snapshot("после создания сервиса")

    # 4. Добавление 1000 фиктивных записей истории
    for i in range(1, 1001):
        service.handle_request({
            "id": f"prof_{i}",
            "method": "add_history_item",
            "params": {
                "text": f"фиктивная запись #{i}: тестирование накладных расходов памяти",
                "source": "profiler",
            },
        })

    after_load_current, after_load_peak = _snapshot("после 1000 записей истории")

    # Завершение tracemalloc
    tracemalloc.stop()
    tmp.cleanup()

    # ---------------------------------------------------------------------------
    # Итоговый отчёт
    # ---------------------------------------------------------------------------
    print()
    print("=" * 65)
    print("Итоговый отчёт (current bytes, без учёта системного overhead)")
    print("=" * 65)

    def mb(b: int) -> str:
        return f"{b / 1024 / 1024:.2f} MB"

    rows = [
        ("baseline",                    baseline_current),
        ("после импортов",              after_import_current),
        ("после создания сервиса",      after_init_current),
        ("после 1000 записей истории",  after_load_current),
    ]

    for label, val in rows:
        print(f"  {label:<35}  {mb(val)}")

    print()
    print("Дельты:")
    print(f"  импорты                           +{mb(after_import_current - baseline_current)}")
    print(f"  инициализация сервиса             +{mb(after_init_current - after_import_current)}")
    print(f"  загрузка 1000 записей истории     +{mb(after_load_current - after_init_current)}")
    print(f"  итого (baseline → 1000 записей)   +{mb(after_load_current - baseline_current)}")
    print()
    print(f"  пиковое значение (за весь прогон)  {mb(after_load_peak)}")
    print("=" * 65)


if __name__ == "__main__":
    main()
