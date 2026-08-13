"""Тесты провязки write-пути SearchHistoryManager.record_search() с HistoryService.

Проверяет, что handle_search_history записывает поисковые запросы в
SearchHistoryManager (_entries), который используется IPC-хендлерами
get_recent_searches / get_popular_searches для автодополнения в Swift HistoryPanel.

Связано с:
- backend/history_service.py  — handle_search_history (строка ~315)
- backend/search_history.py   — SearchHistoryManager.record_search() (строка ~97)
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

# Добавляем корень проекта KrabEar в sys.path, чтобы работали импорты вида
# `from backend.xxx import ...` (аналогично test_history_service_extended.py)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from backend.history_service import HistoryService
    from backend.search_history import SearchHistoryManager
    _SKIP = False
except ImportError as _import_err:
    _SKIP = True
    _SKIP_REASON = str(_import_err)


# ---------------------------------------------------------------------------
# Заглушка store — минимальный интерфейс, нужный handle_search_history
# ---------------------------------------------------------------------------

class _FakeStore:
    """Фейковый store с единственным методом search_history.

    Возвращает одну фиктивную запись, чтобы results_count=1 после поиска.
    Метод load_settings нужен HistoryService._is_privacy_mode() при отсутствии
    _cached_settings.
    """

    def __init__(self, privacy: bool = False) -> None:
        # Флаг для имитации privacy mode через load_settings
        self._privacy = privacy

    def search_history(
        self,
        query: str,
        cursor: Any,
        limit: int,
        paste_status: Any,
        translation_mode: Any,
        translation_status: Any,
        from_ts: Any,
        to_ts: Any,
    ) -> tuple[list[dict], None]:
        """Возвращает один фиктивный элемент (чтобы results_count=1)."""
        return [{"id": "fake-1", "text": "тестовая запись", "ts": "2026-01-01T00:00:00Z"}], None

    def load_settings(self, lock_timeout_sec: float | None = None, nowait: bool = False) -> dict[str, Any]:
        """Симулирует load_settings для _is_privacy_mode()."""
        return {"privacy_mode_enabled": self._privacy}


# ---------------------------------------------------------------------------
# Вспомогательная фабрика сервиса
# ---------------------------------------------------------------------------

def _make_svc(
    tmp_dir: str,
    privacy: bool = False,
) -> "HistoryService":
    """Создаёт HistoryService с фейковым store и реальным SearchHistoryManager.

    Args:
        tmp_dir: Временная директория для SearchHistoryManager (персистентность).
        privacy:  Включить ли privacy mode в фейковом store.

    Returns:
        Готовый HistoryService со связанным _search_history_mgr.
    """
    store = _FakeStore(privacy=privacy)
    # _cached_settings=None → _is_privacy_mode() использует store.load_settings()
    svc = HistoryService(store=store)  # type: ignore[arg-type]
    # Late-inject реального SearchHistoryManager (как делает BackendService в проде)
    svc._search_history_mgr = SearchHistoryManager(data_dir=tmp_dir)
    return svc


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP, "HistoryService или SearchHistoryManager недоступны")
class SearchHistoryWiringRecordTestCase(unittest.TestCase):
    """Проверяет write-путь: handle_search_history → record_search → _entries."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    # ------------------------------------------------------------------
    # Тест 1: нормальный запрос записывается
    # ------------------------------------------------------------------

    def test_search_records_query(self) -> None:
        """Вызов handle_search_history с непустым query сохраняет запрос в истории."""
        svc = _make_svc(self.tmp.name)

        result = svc.handle_search_history({"query": "привет"})

        # Сам поиск должен вернуть нормальный ответ
        self.assertIn("items", result)

        # Запрос "привет" должен быть записан в _entries
        recent = svc._search_history_mgr.get_recent_searches()
        queries = [e["query"] for e in recent]
        self.assertIn("привет", queries, f"Ожидали 'привет' в get_recent_searches(), получили: {queries}")

    # ------------------------------------------------------------------
    # Тест 2: пустой запрос не записывается
    # ------------------------------------------------------------------

    def test_empty_query_not_recorded(self) -> None:
        """Вызов handle_search_history с query='' не должен ничего записывать."""
        svc = _make_svc(self.tmp.name)

        svc.handle_search_history({"query": ""})

        # История должна быть пустой — record_search игнорирует пустые строки,
        # а guard `and query` в handle_search_history тоже отсекает пустые
        recent = svc._search_history_mgr.get_recent_searches()
        self.assertEqual(recent, [], f"Ожидали пустую историю, получили: {recent}")

    # ------------------------------------------------------------------
    # Тест 3: privacy mode → запрос НЕ записывается, возвращается privacy-ответ
    # ------------------------------------------------------------------

    def test_privacy_mode_records_nothing(self) -> None:
        """В privacy mode handle_search_history возвращает privacy-ответ и не пишет в историю."""
        svc = _make_svc(self.tmp.name, privacy=True)

        result = svc.handle_search_history({"query": "секретный запрос"})

        # Должен вернуться privacy-ответ (early-return до вызова store.search_history
        # и, соответственно, до record_search)
        self.assertEqual(result.get("reason"), "privacy_mode_active",
                         f"Ожидали privacy_mode_active, получили: {result}")

        # История должна быть пустой
        recent = svc._search_history_mgr.get_recent_searches()
        self.assertEqual(recent, [], f"В privacy mode _entries должны быть пусты, получили: {recent}")

    # ------------------------------------------------------------------
    # Тест 4: сбой record_search не ломает основной поиск
    # ------------------------------------------------------------------

    def test_record_failure_does_not_break_search(self) -> None:
        """Если record_search бросает Exception, handle_search_history всё равно возвращает items."""
        svc = _make_svc(self.tmp.name)

        # Подменяем _search_history_mgr объектом, чей record_search всегда падает
        class _BrokenMgr:
            def record_search(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
                raise RuntimeError("Симулированная ошибка записи")

        svc._search_history_mgr = _BrokenMgr()  # type: ignore[assignment]

        # handle_search_history должен отработать без исключения
        try:
            result = svc.handle_search_history({"query": "тест устойчивости"})
        except Exception as exc:  # noqa: BLE001
            self.fail(f"handle_search_history пробросил исключение вместо best-effort: {exc}")

        # Основной ответ должен быть корректным
        self.assertIn("items", result, f"Ожидали ключ 'items' в ответе, получили: {result}")


if __name__ == "__main__":
    unittest.main()
