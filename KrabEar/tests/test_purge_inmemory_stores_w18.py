"""Wave-18: privacy-purge ДОЛЖЕН стирать in-memory PII-хранилища (root-cause guard).

W1768 закрыл file-backed purge-пробелы; этот тест расширяет тот же класс на
RAM-резидентный PII, который ``handle_purge_all_data`` раньше НЕ трогал:

GAP-1 (MED):
  - ``ContextMemory._texts`` — deque последних 50 СЫРЫХ транскриптов (полный PII),
    re-exposable через get_context_memory IPC. Файлового артефакта нет.
  - ``_clipboard_history`` — последние ~20 вставленных транскрипций (полный PII),
    re-exposable через get_clipboard_history / repaste_item IPC.

GAP-2 (MED):
  - ``StateStore._search_index`` (SearchIndex._texts) — полный in-RAM cleartext-слепок
    ВСЕХ транскриптов, построенный на fast-path ``search_history``.
  - ``StateStore._recent_search_index`` (+ signature) — последние ~4000 «стогов».

Тест строит МИНИМАЛЬНЫЕ коллабораторы (реальный StateStore на temp-dir + реальная
ContextMemory; никаких моделей не грузится), наполняет каждое хранилище, вызывает
``handle_purge_all_data`` и проверяет, что все четыре RAM-слепка пусты. Этот тест
ОБЯЗАН падать, если purge когда-либо перестанет очищать любое из них.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore           # noqa: E402
from backend.history_service import HistoryService    # noqa: E402
from core.context_memory import ContextMemory         # noqa: E402


# Распознаваемые PII-маркеры, которые мы потом ищем в RAM-слепках.
_PII_TEXTS = [
    "Иван Петров позвонил по номеру восемь девятьсот",
    "секретный пароль альфабравочарли и адрес офиса",
    "Мария обсуждала перевод денег на счёт компании",
]


class PurgeInMemoryStoresTestCase(unittest.TestCase):
    """Wave-18: purge стирает context_memory + clipboard + StateStore поисковые кэши."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="krabear_purge_w18_")
        self.store = StateStore(data_dir=Path(self._tmpdir))

        # --- наполняем историю реальными PII-записями ---
        for text in _PII_TEXTS:
            self.store.add_history_item(text=text, paste_status="ok")

        # --- наполняем StateStore поисковые кэши через fast-path search_history ---
        # Запрос без доп-фильтров идёт через инвертированный индекс (строит
        # _search_index._texts) И через recent-index (_recent_search_index).
        items, _ = self.store.search_history(query="Иван", cursor=None, limit=50)
        self.assertTrue(items, "search_history должен находить наполненную запись")

        # --- ContextMemory с RAW-транскриптами ---
        self.context_memory = ContextMemory(window_size=50)
        for text in _PII_TEXTS:
            self.context_memory.update(text)

        # --- общий clipboard-список (как передаёт BackendService — по ссылке) ---
        self.clipboard_history: list[dict] = [
            {"text": text, "ts": "2024-01-01T00:00:00+00:00"} for text in _PII_TEXTS
        ]

        # --- HistoryService с late-injected in-memory коллабораторами ---
        # Зеркалирует wiring из BackendService.__init__ (service.py):
        #   self._history._context_memory = self._context_memory
        #   clipboard_history передаётся в конструктор по ссылке.
        self.svc = HistoryService(
            store=self.store,
            clipboard_history=self.clipboard_history,
        )
        self.svc._context_memory = self.context_memory

    # ------------------------------------------------------------------
    # Pre-conditions — убеждаемся, что хранилища ДЕЙСТВИТЕЛЬНО наполнены
    # ------------------------------------------------------------------

    def test_preconditions_stores_are_populated(self) -> None:
        """Защита самого теста: до purge все четыре RAM-слепка непусты."""
        self.assertGreater(len(self.context_memory._texts), 0)
        self.assertGreater(len(self.clipboard_history), 0)
        self.assertGreater(
            len(self.store._search_index._texts), 0,
            "search fast-path должен был наполнить _search_index._texts",
        )
        self.assertGreater(
            len(self.store._recent_search_index), 0,
            "search fast-path должен был наполнить _recent_search_index",
        )

    # ------------------------------------------------------------------
    # GAP-1 — ContextMemory + clipboard
    # ------------------------------------------------------------------

    def test_purge_clears_context_memory(self) -> None:
        """ContextMemory._texts (raw транскрипты) пуст после purge."""
        result = self.svc.handle_purge_all_data({"confirm": True})
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(
            len(self.context_memory._texts), 0,
            "ContextMemory._texts должен быть пуст после purge (GAP-1)",
        )
        # context_memory НЕ должен попасть в secondary_errors
        self.assertNotIn("context_memory", result.get("errors", []))

    def test_purge_clears_clipboard_history(self) -> None:
        """In-memory история буфера обмена пуста после purge."""
        result = self.svc.handle_purge_all_data({"confirm": True})
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(
            len(self.clipboard_history), 0,
            "clipboard_history должен быть пуст после purge (GAP-1)",
        )
        self.assertNotIn("clipboard_history", result.get("errors", []))

    def test_purge_clears_clipboard_in_place_shared_reference(self) -> None:
        """Очистка clipboard происходит in-place — общая ссылка тоже видит пустоту.

        BackendService и HistoryService делят ОДИН список по ссылке. Если бы purge
        переприсваивал поле (self._clipboard_history = []), внешний владелец ссылки
        продолжал бы видеть старый непустой список. Проверяем, что объект тот же.
        """
        self.assertIs(self.svc._clipboard_history, self.clipboard_history)
        self.svc.handle_purge_all_data({"confirm": True})
        # тот же объект, и он пуст
        self.assertIs(self.svc._clipboard_history, self.clipboard_history)
        self.assertEqual(len(self.clipboard_history), 0)

    # ------------------------------------------------------------------
    # GAP-2 — StateStore поисковые кэши
    # ------------------------------------------------------------------

    def test_purge_clears_search_index_texts(self) -> None:
        """SearchIndex._texts (полный cleartext всех записей) пуст после purge."""
        result = self.svc.handle_purge_all_data({"confirm": True})
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(
            len(self.store._search_index._texts), 0,
            "store._search_index._texts должен быть пуст после purge (GAP-2)",
        )
        self.assertEqual(len(self.store._search_index._index), 0)
        self.assertIsNone(self.store._search_index._signature)
        self.assertNotIn("search_caches", result.get("errors", []))

    def test_purge_clears_recent_search_index(self) -> None:
        """_recent_search_index (+ signature) сброшен после purge."""
        result = self.svc.handle_purge_all_data({"confirm": True})
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(
            self.store._recent_search_index, [],
            "store._recent_search_index должен быть [] после purge (GAP-2)",
        )
        self.assertIsNone(self.store._recent_search_index_signature)

    # ------------------------------------------------------------------
    # Все четыре сразу — единый root-cause guard
    # ------------------------------------------------------------------

    def test_purge_clears_all_inmemory_pii_stores(self) -> None:
        """Один проход purge стирает ВСЕ четыре in-memory PII-слепка."""
        result = self.svc.handle_purge_all_data({"confirm": True})
        self.assertTrue(result.get("ok"), result)

        self.assertEqual(len(self.context_memory._texts), 0, "context_memory не очищена")
        self.assertEqual(len(self.clipboard_history), 0, "clipboard_history не очищена")
        self.assertEqual(
            len(self.store._search_index._texts), 0, "_search_index._texts не очищен"
        )
        self.assertEqual(
            self.store._recent_search_index, [], "_recent_search_index не сброшен"
        )

        # Дополнительно: ни один из четырёх шагов не должен зарегистрировать ошибку.
        errs = result.get("errors", [])
        for step in ("search_caches", "context_memory", "clipboard_history"):
            self.assertNotIn(step, errs, f"шаг {step} завершился с ошибкой: {errs}")


class StateStoreResetSearchCachesTestCase(unittest.TestCase):
    """Юнит-тест низкоуровневого хука StateStore.reset_search_caches()."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="krabear_reset_caches_w18_")
        self.store = StateStore(data_dir=Path(self._tmpdir))
        for text in _PII_TEXTS:
            self.store.add_history_item(text=text, paste_status="ok")
        self.store.search_history(query="Мария", cursor=None, limit=50)

    def test_reset_search_caches_empties_both(self) -> None:
        self.assertGreater(len(self.store._search_index._texts), 0)
        self.assertGreater(len(self.store._recent_search_index), 0)

        self.store.reset_search_caches()

        self.assertEqual(len(self.store._search_index._texts), 0)
        self.assertEqual(len(self.store._search_index._index), 0)
        self.assertIsNone(self.store._search_index._signature)
        self.assertEqual(self.store._recent_search_index, [])
        self.assertIsNone(self.store._recent_search_index_signature)

    def test_search_index_clear_is_idempotent(self) -> None:
        """SearchIndex.clear() безопасно вызывать повторно/на пустом индексе."""
        idx = self.store._search_index
        idx.clear()
        idx.clear()  # второй раз — не должно бросать
        self.assertEqual(len(idx._texts), 0)
        self.assertEqual(len(idx._index), 0)
        self.assertIsNone(idx._signature)


if __name__ == "__main__":
    unittest.main()
