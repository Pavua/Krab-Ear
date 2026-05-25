"""Тесты для ContextMemory — контекстной памяти STT.

Покрывает:
- update(): добавление транскрибаций и извлечение слов
- get_context_words(): возврат наиболее частых контекстных слов
- get_recent_topics(): извлечение тем из последних N транскрибаций
- clear(): очистка памяти
- Скользящее окно (вытеснение старых записей)
- Thread-safety (базовый concurrent-тест)
"""

from __future__ import annotations
from core.context_memory import ContextMemory, _extract_notable_words

import sys
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestExtractNotableWords(unittest.TestCase):
    """Юнит-тесты вспомогательной функции _extract_notable_words."""

    # ------------------------------------------------------------------
    # 1. Аббревиатуры извлекаются
    # ------------------------------------------------------------------
    def test_extracts_abbreviations(self) -> None:
        words = _extract_notable_words("Запускаем API через IPC сервер")
        self.assertIn("API", words)
        self.assertIn("IPC", words)

    # ------------------------------------------------------------------
    # 2. CamelCase идентификаторы извлекаются
    # ------------------------------------------------------------------
    def test_extracts_camel_case(self) -> None:
        words = _extract_notable_words("BackendService использует AudioEngine")
        self.assertIn("BackendService", words)
        self.assertIn("AudioEngine", words)

    # ------------------------------------------------------------------
    # 3. Технические термины с цифрами извлекаются
    # ------------------------------------------------------------------
    def test_extracts_tech_with_digits(self) -> None:
        words = _extract_notable_words("Используем GPT4 и Python3 для проекта")
        lower = [w.lower() for w in words]
        self.assertTrue(any("gpt" in w or "python" in w for w in lower))

    # ------------------------------------------------------------------
    # 4. Стоп-слова не попадают в результат
    # ------------------------------------------------------------------
    def test_excludes_stop_words(self) -> None:
        words = _extract_notable_words("это и то но или также всегда")
        lower = [w.lower() for w in words]
        stop = {"это", "и", "но", "или", "также", "всегда"}
        for w in lower:
            self.assertNotIn(w, stop)

    # ------------------------------------------------------------------
    # 5. Пустой текст возвращает пустой список
    # ------------------------------------------------------------------
    def test_empty_text_returns_empty(self) -> None:
        self.assertEqual(_extract_notable_words(""), [])
        self.assertEqual(_extract_notable_words("   "), [])


class TestContextMemoryBasic(unittest.TestCase):
    """Базовые тесты ContextMemory."""

    def setUp(self) -> None:
        self.mem = ContextMemory(window_size=10)

    # ------------------------------------------------------------------
    # 6. После update() размер увеличивается
    # ------------------------------------------------------------------
    def test_size_increases_after_update(self) -> None:
        self.assertEqual(self.mem.size(), 0)
        self.mem.update("Запускаем API для проекта")
        self.assertEqual(self.mem.size(), 1)
        self.mem.update("AudioEngine инициализирован")
        self.assertEqual(self.mem.size(), 2)

    # ------------------------------------------------------------------
    # 7. get_context_words() возвращает список строк
    # ------------------------------------------------------------------
    def test_get_context_words_returns_list(self) -> None:
        self.mem.update("Используем API и GPT4 для обработки")
        words = self.mem.get_context_words()
        self.assertIsInstance(words, list)
        self.assertTrue(len(words) >= 1)

    # ------------------------------------------------------------------
    # 8. get_context_words() не превышает max_words
    # ------------------------------------------------------------------
    def test_get_context_words_respects_max_words(self) -> None:
        for i in range(5):
            self.mem.update(
                f"Транскрибация{i} AudioEngine BackendService API GPT4 CamelCase TEST ABC XYZ"
            )
        words = self.mem.get_context_words(max_words=3)
        self.assertLessEqual(len(words), 3)

    # ------------------------------------------------------------------
    # 9. Слова, встречающиеся чаще, идут первыми
    # ------------------------------------------------------------------
    def test_frequent_words_come_first(self) -> None:
        # "API" встречается во всех 5 транскрибациях
        # "UniqueWord" только в одной
        for _ in range(5):
            self.mem.update("Используем API для запроса")
        self.mem.update("UniqueWord встретился один раз")
        words = self.mem.get_context_words(max_words=10)
        lower = [w.lower() for w in words]
        self.assertIn("api", lower)
        # API должен быть раньше UniqueWord (или единственным)
        if "uniqueword" in lower:
            self.assertLess(lower.index("api"), lower.index("uniqueword"))

    # ------------------------------------------------------------------
    # 10. clear() сбрасывает всё
    # ------------------------------------------------------------------
    def test_clear_resets_memory(self) -> None:
        self.mem.update("API BackendService GPT4")
        self.mem.update("AudioEngine CamelCase")
        self.assertGreater(self.mem.size(), 0)
        self.mem.clear()
        self.assertEqual(self.mem.size(), 0)
        self.assertEqual(self.mem.get_context_words(), [])

    # ------------------------------------------------------------------
    # 11. Скользящее окно: старые записи вытесняются
    # ------------------------------------------------------------------
    def test_sliding_window_eviction(self) -> None:
        mem = ContextMemory(window_size=3)
        mem.update("OldWord1 OldWord2 API")
        mem.update("OldWord1 OldWord2 API")
        mem.update("OldWord1 OldWord2 API")
        # Теперь добавляем 3 новые — все старые должны быть вытеснены
        mem.update("NewTermA здесь")
        mem.update("NewTermB здесь")
        mem.update("NewTermC здесь")
        # Размер не превышает window_size
        self.assertEqual(mem.size(), 3)
        words_lower = [w.lower() for w in mem.get_context_words(max_words=20)]
        # OldWord должен исчезнуть из счётчиков
        self.assertNotIn("oldword1", words_lower)
        self.assertNotIn("oldword2", words_lower)

    # ------------------------------------------------------------------
    # 12. get_recent_topics() возвращает список строк
    # ------------------------------------------------------------------
    def test_get_recent_topics_returns_list(self) -> None:
        self.mem.update("Krab AudioEngine API транскрибация")
        self.mem.update("BackendService API Python")
        topics = self.mem.get_recent_topics()
        self.assertIsInstance(topics, list)
        self.assertTrue(len(topics) >= 1)

    # ------------------------------------------------------------------
    # 13. get_recent_topics() не превышает max_topics
    # ------------------------------------------------------------------
    def test_get_recent_topics_respects_max_topics(self) -> None:
        for i in range(8):
            self.mem.update(
                f"Term{i} API BackendService AudioEngine GPT4"
            )
        topics = self.mem.get_recent_topics(max_topics=3)
        self.assertLessEqual(len(topics), 3)

    # ------------------------------------------------------------------
    # 14. update() игнорирует пустой и пробельный текст
    # ------------------------------------------------------------------
    def test_update_ignores_empty_text(self) -> None:
        self.mem.update("")
        self.mem.update("   ")
        self.mem.update("\n\t")
        self.assertEqual(self.mem.size(), 0)

    # ------------------------------------------------------------------
    # 15. to_dict() возвращает ожидаемую структуру
    # ------------------------------------------------------------------
    def test_to_dict_structure(self) -> None:
        self.mem.update("BackendService API GPT4")
        d = self.mem.to_dict()
        self.assertIn("window_size", d)
        self.assertIn("current_size", d)
        self.assertIn("context_words", d)
        self.assertIn("recent_topics", d)
        self.assertIn("top_words", d)
        self.assertEqual(d["window_size"], 10)
        self.assertEqual(d["current_size"], 1)
        self.assertIsInstance(d["context_words"], list)
        self.assertIsInstance(d["recent_topics"], list)


class TestContextMemoryThreadSafety(unittest.TestCase):
    """Базовый тест thread-safety: параллельные update() не ломают состояние."""

    # ------------------------------------------------------------------
    # 16. Параллельные update() не вызывают исключений
    # ------------------------------------------------------------------
    def test_concurrent_updates_no_exception(self) -> None:
        mem = ContextMemory(window_size=20)
        errors: list[Exception] = []

        def worker(idx: int) -> None:
            try:
                for _ in range(10):
                    mem.update(f"Транскрибация {idx} BackendService API GPT4")
                    mem.get_context_words(max_words=10)
                    mem.get_recent_topics()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Исключения в потоках: {errors}")
        # Размер не превышает window_size
        self.assertLessEqual(mem.size(), 20)


class TestContextMemorySpecNames(unittest.TestCase):
    """Wave-126 spec-named tests для ContextMemory."""

    def setUp(self) -> None:
        self.mem = ContextMemory(window_size=5)

    # ------------------------------------------------------------------
    # 17. test_add_words_to_context
    # ------------------------------------------------------------------
    def test_add_words_to_context(self) -> None:
        self.mem.update("AudioEngine BackendService API")
        words = self.mem.get_context_words()
        lower = [w.lower() for w in words]
        # At least one notable word extracted and stored
        self.assertTrue(len(words) >= 1)
        self.assertTrue(
            any(x in lower for x in ("audioengine", "backendservice", "api"))
        )

    # ------------------------------------------------------------------
    # 18. test_sliding_window_drops_oldest
    # ------------------------------------------------------------------
    def test_sliding_window_drops_oldest(self) -> None:
        mem = ContextMemory(window_size=2)
        mem.update("OldTermAlpha здесь")
        mem.update("OldTermBeta тут")
        # Both in window; add 2 more to push both out
        mem.update("NewTermGamma один")
        mem.update("NewTermDelta два")
        self.assertEqual(mem.size(), 2)
        words_lower = [w.lower() for w in mem.get_context_words(max_words=30)]
        self.assertNotIn("oldtermalpha", words_lower)
        self.assertNotIn("oldtermbeta", words_lower)
        self.assertIn("newtermdelta", words_lower)

    # ------------------------------------------------------------------
    # 19. test_topic_extraction
    # ------------------------------------------------------------------
    def test_topic_extraction(self) -> None:
        self.mem.update("Нейросеть обрабатывает AudioEngine данные API")
        self.mem.update("AudioEngine снова вызван для обработки")
        topics = self.mem.get_recent_topics(max_topics=5)
        self.assertIsInstance(topics, list)
        self.assertTrue(len(topics) >= 1)
        lower = [t.lower() for t in topics]
        # AudioEngine appears twice → should surface as a top topic
        self.assertIn("audioengine", lower)

    # ------------------------------------------------------------------
    # 20. test_get_hint_for_stt
    # ------------------------------------------------------------------
    def test_get_hint_for_stt(self) -> None:
        self.mem.update("GPT4 модель используется для API запроса")
        self.mem.update("GPT4 снова вызывается для BackendService")
        hints = self.mem.get_context_words(max_words=20)
        lower = [h.lower() for h in hints]
        # Repeated GPT4 should be top hint
        self.assertIn("gpt4", lower)
        # Count should be 2 in internal counter
        self.assertEqual(self.mem._word_counter.get("gpt4", 0), 2)

    # ------------------------------------------------------------------
    # 21. test_unicode_words
    # ------------------------------------------------------------------
    def test_unicode_words(self) -> None:
        # Cyrillic CamelCase-like and tech terms with unicode chars
        self.mem.update("Архитектура системы использует ВоисГейтвей GPT4")
        self.mem.update("Трансляция ТелеграмБридж активна API")
        words = self.mem.get_context_words(max_words=30)
        # Should not raise, returns a list
        self.assertIsInstance(words, list)
        # Tech term GPT4 must survive unicode-heavy text
        lower = [w.lower() for w in words]
        self.assertIn("gpt4", lower)

    # ------------------------------------------------------------------
    # 22. test_concurrent_add
    # ------------------------------------------------------------------
    def test_concurrent_add(self) -> None:
        mem = ContextMemory(window_size=50)
        errors: list[Exception] = []

        def worker(n: int) -> None:
            try:
                for i in range(20):
                    mem.update(f"Thread{n} BackendService{i} API{i} GPT4")
                    _ = mem.get_context_words()
                    _ = mem.get_recent_topics()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread errors: {errors}")
        self.assertLessEqual(mem.size(), 50)
        # Internal counter must remain non-negative
        for key, count in mem._word_counter.items():
            self.assertGreater(count, 0, f"Non-positive count for '{key}': {count}")

    # ------------------------------------------------------------------
    # 23. test_clear_context
    # ------------------------------------------------------------------
    def test_clear_context(self) -> None:
        self.mem.update("BackendService API AudioEngine")
        self.mem.update("GPT4 CamelCase XYZ")
        self.assertGreater(self.mem.size(), 0)
        self.assertGreater(len(self.mem.get_context_words()), 0)

        self.mem.clear()

        self.assertEqual(self.mem.size(), 0)
        self.assertEqual(self.mem.get_context_words(), [])
        self.assertEqual(self.mem.get_recent_topics(), [])
        d = self.mem.to_dict()
        self.assertEqual(d["current_size"], 0)
        self.assertEqual(d["context_words"], [])
        self.assertEqual(d["top_words"], [])


if __name__ == "__main__":
    unittest.main()
