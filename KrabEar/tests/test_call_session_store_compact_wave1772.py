"""Wave 1772 — тесты компактирования CallSessionStore.

Проверяют три инварианта исправления unbounded-growth + O(N²):

(a) add_transcript для K=500 реплик НЕ выполняет O(K²) полных парсов файла —
    число парсов за один вызов ограничено (≤ размер файла после K вызовов,
    а не нарастает как K*avg_file_size).  Тест измеряет реальное число
    строк, считанных функцией _iter_records_unlocked, и убеждается, что
    на последнем вызове add_transcript файл содержит не более чем ~501
    строк (1 базовая + 500 дельт), то есть каждый вызов читает O(1) полных
    «сессий», а не O(K) прошлых вызовов.

(b) После compact() файл содержит ровно по одной строке на активную сессию,
    и get() возвращает то же состояние, что и до компактирования.

(c) delete_all() после compact() очищает всё.

(d) maybe_compact() возвращает False при маленьком файле и True при большом.

(e) cost_usd: _apply_delta использует присваивание (=), а не накопление (+=).
    Replay нескольких mark_completed-дельт НЕ удваивает стоимость.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_session import CallStatus  # noqa: E402
from backend.call_session_store import (  # noqa: E402
    CallSessionStore,
    _COMPACT_LINE_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _make_store(tmp: Path) -> CallSessionStore:
    return CallSessionStore(data_dir=tmp)


def _count_file_lines(path: Path) -> int:
    """Подсчитывает непустые строки файла."""
    if not path.exists():
        return 0
    return sum(1 for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip())


def _walk_to_talking(store: CallSessionStore, sid: str) -> None:
    """Переводит сессию в состояние TALKING."""
    for st in [CallStatus.DIALING, CallStatus.CONNECTED, CallStatus.TALKING]:
        store.update_status(sid, st.value)


# ---------------------------------------------------------------------------
# (a) Число парсов при K add_transcript — ограничено, не O(K²)
# ---------------------------------------------------------------------------


class TestAddTranscriptParseBound(unittest.TestCase):
    """Каждый вызов add_transcript читает файл O(1) раз (однократный проход),
    а не O(K) предыдущих вызовов.

    Метрика: после K=500 вызовов файл содержит 1 + K строк.  Последний вызов
    add_transcript читает не более 1+K строк (однократный проход _iter_records).
    Если бы replay вызывался K раз внутри одного add_transcript — мы бы
    наблюдали O(K²) суммарных парсов.  Тест проверяет, что суммарное число
    парсов ≤ K*(K+2)/2 (худший линейный: каждый из K вызовов читает i строк),
    и строго меньше K² / 2.
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmpdir.name))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_parse_count_is_not_quadratic(self) -> None:
        K = 500
        s = self.store.create("+79001234567", "тест масштабирования")
        _walk_to_talking(self.store, s.id)

        # Считаем суммарное число строк, прочитанных _iter_records_unlocked
        # за все K вызовов add_transcript.
        total_lines_read = 0
        _real_iter = self.store._iter_records_unlocked

        for i in range(K):
            # Каждый вызов add_transcript вызывает _replay_session_unlocked →
            # _iter_records_unlocked ровно ОДИН раз.  Фиксируем число строк
            # для этого одного прохода.
            lines_this_call = 0

            def _counting_iter(
                _store=self.store,
                _real=_real_iter,
                _counter=[0],
            ):
                for rec in _real():
                    _counter[0] += 1
                    yield rec

            with patch.object(
                self.store,
                "_iter_records_unlocked",
                side_effect=lambda: _counting_iter(),
            ):
                # Сбрасываем счётчик перед вызовом
                counter_holder: list[int] = [0]
                original_iter = self.store._iter_records_unlocked

                def patched_iter(_ch=counter_holder, _real=_real_iter):
                    for rec in _real():
                        _ch[0] += 1
                        yield rec

                self.store._iter_records_unlocked = patched_iter  # type: ignore[method-assign]
                try:
                    self.store.add_transcript(s.id, "user", f"реплика {i}")
                finally:
                    self.store._iter_records_unlocked = _real_iter  # type: ignore[method-assign]
                lines_this_call = counter_holder[0]

            total_lines_read += lines_this_call

        # Линейный суммарный максимум:
        #   Базовая строка (create) + 3 дельты (walk_to_talking) = 4 строки до первого add_transcript.
        #   После i-го add_transcript в файле 4 + i строк.
        #   Каждый (i+1)-й вызов (i=0..K-1) читает ровно (4+i) строк.
        #   Итого: sum(4+i for i in range(K)) = 4*K + K*(K-1)/2
        linear_max = 4 * K + K * (K - 1) // 2
        # O(K²) граница: грубая квадратичная метрика — K вызовов × K строк
        quadratic_bound = K * K

        self.assertLessEqual(
            total_lines_read,
            linear_max,
            f"Суммарно считано {total_lines_read} строк — превышает линейный максимум {linear_max}",
        )
        # Главная регрессия: убеждаемся, что это НЕ K² * K поведение
        self.assertLess(
            total_lines_read,
            quadratic_bound,
            f"Суммарно считано {total_lines_read} строк — кажется квадратичным (K²={quadratic_bound})",
        )

        # Проверяем, что файл содержит ровно 1 + 3 (walk_to_talking) + K строк
        expected_lines = 1 + 3 + K
        actual_lines = _count_file_lines(self.store.sessions_path)
        self.assertEqual(
            actual_lines,
            expected_lines,
            f"Файл должен содержать {expected_lines} строк, содержит {actual_lines}",
        )

    def test_each_add_transcript_does_single_file_pass(self) -> None:
        """Один вызов add_transcript выполняет ровно один проход по файлу."""
        s = self.store.create("+1", "однократный проход")
        _walk_to_talking(self.store, s.id)
        # Добавляем 10 реплик — убеждаемся, что каждый вызов читает ≤ N строк файла
        for i in range(10):
            lines_before = _count_file_lines(self.store.sessions_path)
            pass_count: list[int] = [0]
            real_iter = self.store._iter_records_unlocked

            def _counting(_ph=pass_count, _real=real_iter):
                for rec in _real():
                    _ph[0] += 1
                    yield rec

            self.store._iter_records_unlocked = _counting  # type: ignore[method-assign]
            try:
                self.store.add_transcript(s.id, "bot", f"ответ {i}")
            finally:
                self.store._iter_records_unlocked = real_iter  # type: ignore[method-assign]

            lines_after = _count_file_lines(self.store.sessions_path)
            # Один проход: не более lines_before + 1 (новая строка ещё не была в файле
            # в момент чтения, но могла попасть в счётчик при нашем monkey-patch
            # в зависимости от порядка).  Гарантируем ≤ lines_after.
            self.assertLessEqual(
                pass_count[0],
                lines_after,
                f"Итерация {i}: считано {pass_count[0]} строк, файл {lines_after}",
            )


# ---------------------------------------------------------------------------
# (b) compact() оставляет по одной строке на сессию, get() возвращает то же
# ---------------------------------------------------------------------------


class TestCompactCorrectness(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmpdir.name))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_compact_collapses_to_one_line_per_session(self) -> None:
        """После compact() на каждую активную сессию приходится ровно одна строка."""
        s1 = self.store.create("+1", "звонок 1")
        s2 = self.store.create("+2", "звонок 2")
        _walk_to_talking(self.store, s1.id)
        _walk_to_talking(self.store, s2.id)
        for i in range(20):
            self.store.add_transcript(s1.id, "user", f"реплика {i}")
        for i in range(15):
            self.store.add_transcript(s2.id, "bot", f"ответ {i}")

        lines_before = _count_file_lines(self.store.sessions_path)
        # Должно быть гораздо больше 2 строк
        self.assertGreater(lines_before, 10)

        stats = self.store.compact()
        self.assertEqual(stats["lines_before"], lines_before)
        self.assertEqual(stats["sessions_kept"], 2)

        lines_after = _count_file_lines(self.store.sessions_path)
        # Ровно 2 строки — по одной на сессию
        self.assertEqual(lines_after, 2)
        self.assertEqual(stats["lines_after"], 2)

    def test_get_returns_same_state_after_compact(self) -> None:
        """get() после compact() возвращает то же состояние, что до компактирования."""
        s = self.store.create("+79001234567", "проверка состояния")
        _walk_to_talking(self.store, s.id)
        for i in range(50):
            self.store.add_transcript(s.id, "user", f"слово {i}")

        # Состояние ДО
        before = self.store.get(s.id)
        assert before is not None
        status_before = before.status
        transcript_len_before = len(before.transcript_history)

        self.store.compact()

        # Состояние ПОСЛЕ
        after = self.store.get(s.id)
        assert after is not None
        self.assertEqual(after.status, status_before)
        self.assertEqual(len(after.transcript_history), transcript_len_before)
        self.assertEqual(after.id, s.id)

    def test_compact_excludes_deleted_sessions(self) -> None:
        """Удалённые сессии не попадают в компактированный файл."""
        s1 = self.store.create("+1", "живая")
        s2 = self.store.create("+2", "удалённая")
        self.store.delete(s2.id)

        self.store.compact()

        lines = _count_file_lines(self.store.sessions_path)
        self.assertEqual(lines, 1)

        self.assertIsNotNone(self.store.get(s1.id))
        self.assertIsNone(self.store.get(s2.id))

    def test_compact_empty_store(self) -> None:
        """compact() на пустом хранилище не падает."""
        stats = self.store.compact()
        self.assertEqual(stats["sessions_kept"], 0)
        self.assertEqual(stats["lines_after"], 0)

    def test_compact_preserves_transcript_history(self) -> None:
        """Транскрипт полностью сохраняется после компактирования."""
        s = self.store.create("+7", "транскрипт")
        _walk_to_talking(self.store, s.id)
        texts = [f"реплика номер {i}" for i in range(30)]
        for t in texts:
            self.store.add_transcript(s.id, "user", t)

        self.store.compact()

        fetched = self.store.get(s.id)
        assert fetched is not None
        fetched_texts = [e.text for e in fetched.transcript_history]
        self.assertEqual(fetched_texts, texts)

    def test_list_sessions_correct_after_compact(self) -> None:
        """list_sessions() после compact() возвращает все активные сессии."""
        ids = set()
        for i in range(5):
            s = self.store.create(f"+{i}", f"цель {i}")
            ids.add(s.id)

        self.store.compact()

        sessions = self.store.list_sessions(limit=10)
        self.assertEqual(len(sessions), 5)
        returned_ids = {d["id"] for d in sessions}
        self.assertEqual(returned_ids, ids)

    def test_compact_after_mark_completed_reduces_lines(self) -> None:
        """compact() после mark_completed сворачивает дельты в одну строку."""
        s = self.store.create("+1", "завершённый звонок")
        for st in [CallStatus.DIALING, CallStatus.CONNECTED, CallStatus.TALKING, CallStatus.ENDING]:
            self.store.update_status(s.id, st.value)
        for i in range(10):
            self.store.add_transcript(s.id, "user", f"реплика {i}")

        # mark_completed вызывает maybe_compact; порог 500 не достигнут —
        # здесь вызываем compact() явно
        self.store.mark_completed(s.id, "goal_reached", cost_usd=1.50)

        self.store.compact()
        lines = _count_file_lines(self.store.sessions_path)
        self.assertEqual(lines, 1)

        fetched = self.store.get(s.id)
        assert fetched is not None
        self.assertEqual(fetched.status, CallStatus.COMPLETED.value)
        self.assertAlmostEqual(fetched.cost_usd, 1.50, places=4)


# ---------------------------------------------------------------------------
# (c) delete_all() после compact() очищает всё
# ---------------------------------------------------------------------------


class TestDeleteAllAfterCompact(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmpdir.name))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_delete_all_after_compact(self) -> None:
        for i in range(5):
            self.store.create(f"+{i}", f"цель {i}")
        self.store.compact()

        count = self.store.delete_all()
        self.assertEqual(count, 5)
        self.assertEqual(self.store.list_sessions(), [])
        self.assertEqual(_count_file_lines(self.store.sessions_path), 0)


# ---------------------------------------------------------------------------
# (d) maybe_compact() — корректный возврат True/False
# ---------------------------------------------------------------------------


class TestMaybeCompact(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmpdir.name))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_maybe_compact_returns_false_below_threshold(self) -> None:
        """maybe_compact() возвращает False если строк меньше порога."""
        for i in range(5):
            s = self.store.create(f"+{i}", f"g{i}")
            self.store.add_transcript(s.id, "u", "текст")
        result = self.store.maybe_compact()
        self.assertFalse(result)

    def test_maybe_compact_returns_true_above_threshold(self) -> None:
        """maybe_compact() возвращает True и выполняет compact при превышении порога."""
        # Патчим порог до маленького значения
        import backend.call_session_store as _mod
        original_threshold = _mod._COMPACT_LINE_THRESHOLD
        _mod._COMPACT_LINE_THRESHOLD = 5  # type: ignore[attr-defined]
        try:
            s = self.store.create("+1", "цель")
            _walk_to_talking(self.store, s.id)
            for i in range(10):
                self.store.add_transcript(s.id, "u", f"реплика {i}")

            lines_before = _count_file_lines(self.store.sessions_path)
            self.assertGreater(lines_before, 5)

            result = self.store.maybe_compact()
            self.assertTrue(result)

            lines_after = _count_file_lines(self.store.sessions_path)
            self.assertLess(lines_after, lines_before)
            self.assertEqual(lines_after, 1)
        finally:
            _mod._COMPACT_LINE_THRESHOLD = original_threshold  # type: ignore[attr-defined]

    def test_mark_completed_triggers_compact_above_threshold(self) -> None:
        """mark_completed автоматически компактирует при превышении порога."""
        import backend.call_session_store as _mod
        original_threshold = _mod._COMPACT_LINE_THRESHOLD
        _mod._COMPACT_LINE_THRESHOLD = 5  # type: ignore[attr-defined]
        try:
            s = self.store.create("+1", "авто-компакт")
            for st in [CallStatus.DIALING, CallStatus.CONNECTED, CallStatus.TALKING, CallStatus.ENDING]:
                self.store.update_status(s.id, st.value)
            for i in range(10):
                self.store.add_transcript(s.id, "u", f"р {i}")

            lines_before = _count_file_lines(self.store.sessions_path)
            self.assertGreater(lines_before, 5)

            self.store.mark_completed(s.id, "done")

            lines_after = _count_file_lines(self.store.sessions_path)
            self.assertLess(lines_after, lines_before)
        finally:
            _mod._COMPACT_LINE_THRESHOLD = original_threshold  # type: ignore[attr-defined]

    def test_mark_failed_triggers_compact_above_threshold(self) -> None:
        """mark_failed автоматически компактирует при превышении порога."""
        import backend.call_session_store as _mod
        original_threshold = _mod._COMPACT_LINE_THRESHOLD
        _mod._COMPACT_LINE_THRESHOLD = 5  # type: ignore[attr-defined]
        try:
            s = self.store.create("+1", "авто-компакт fail")
            self.store.update_status(s.id, CallStatus.DIALING.value)
            for i in range(10):
                self.store.add_transcript(s.id, "u", f"р {i}")

            lines_before = _count_file_lines(self.store.sessions_path)
            self.assertGreater(lines_before, 5)

            self.store.mark_failed(s.id, "no_answer")

            lines_after = _count_file_lines(self.store.sessions_path)
            self.assertLess(lines_after, lines_before)
        finally:
            _mod._COMPACT_LINE_THRESHOLD = original_threshold  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# (e) cost_usd: _apply_delta использует = не +=
# ---------------------------------------------------------------------------


class TestCostUsdNotAccumulated(unittest.TestCase):
    """cost_usd при replay нескольких дельт не должна удваиваться."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmpdir.name))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_cost_usd_is_assigned_not_accumulated(self) -> None:
        """Вручную пишем две дельта-строки с cost_usd и проверяем replay."""
        s = self.store.create("+1", "тест стоимости")
        # Имитируем два mark_completed-дельты в NDJSON напрямую
        # (второй перезаписывает первый — стоимость должна быть из последней дельты)
        delta1 = {"id": s.id, "_update": True, "status": "completed", "cost_usd": 1.0}
        delta2 = {"id": s.id, "_update": True, "cost_usd": 2.5}
        with self.store.sessions_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(delta1, ensure_ascii=False) + "\n")
            fh.write(json.dumps(delta2, ensure_ascii=False) + "\n")

        fetched = self.store.get(s.id)
        assert fetched is not None
        # Если бы применялось +=: cost_usd = 0 + 1.0 + 2.5 = 3.5 (НЕПРАВИЛЬНО)
        # Если =: cost_usd = 2.5 (последняя дельта) — ПРАВИЛЬНО
        self.assertAlmostEqual(
            fetched.cost_usd,
            2.5,
            places=4,
            msg=(
                f"cost_usd={fetched.cost_usd!r} — похоже, применяется += "
                "вместо = (накопление при replay)"
            ),
        )

    def test_mark_completed_cost_correct_after_multiple_replays(self) -> None:
        """mark_completed с cost_usd=1.23 → после двух get() стоимость не удваивается."""
        s = self.store.create("+2", "повторный get")
        for st in [CallStatus.DIALING, CallStatus.CONNECTED, CallStatus.TALKING, CallStatus.ENDING]:
            self.store.update_status(s.id, st.value)
        self.store.mark_completed(s.id, "done", cost_usd=1.23)

        # Несколько get() — каждый делает полный replay
        for _ in range(5):
            fetched = self.store.get(s.id)
            assert fetched is not None
            self.assertAlmostEqual(
                fetched.cost_usd,
                1.23,
                places=4,
                msg=f"cost_usd изменилась при повторном get(): {fetched.cost_usd!r}",
            )

    def test_cost_zero_by_default(self) -> None:
        """cost_usd=0 по умолчанию не должна стать ненулевой после replay."""
        s = self.store.create("+3", "нулевая стоимость")
        self.store.update_status(s.id, CallStatus.DIALING.value)
        self.store.mark_failed(s.id, "busy", cost_usd=0.0)

        fetched = self.store.get(s.id)
        assert fetched is not None
        self.assertAlmostEqual(fetched.cost_usd, 0.0, places=6)


# ---------------------------------------------------------------------------
# (f) compact() атомарен: состояние корректно при повреждении tmp-файла
# ---------------------------------------------------------------------------


class TestCompactAtomicity(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmpdir.name))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_compact_tmp_cleaned_on_exception(self) -> None:
        """При сбое в compact() tmp-файл удаляется, оригинал не тронут."""
        s = self.store.create("+1", "атомарность")
        _walk_to_talking(self.store, s.id)
        for i in range(5):
            self.store.add_transcript(s.id, "u", f"т{i}")

        lines_before = _count_file_lines(self.store.sessions_path)
        tmp = self.store.sessions_path.with_suffix(".ndjson.tmp")

        original_dump = json.dumps

        def _failing_dump(*args, **kwargs):
            raise OSError("disk full simulation")

        with patch("backend.call_session_store.json.dumps", side_effect=_failing_dump):
            try:
                self.store.compact()
            except OSError:
                pass

        # tmp-файл должен быть удалён
        self.assertFalse(tmp.exists(), "tmp-файл не удалён после сбоя compact()")
        # Оригинальный файл не изменился
        lines_after = _count_file_lines(self.store.sessions_path)
        self.assertEqual(lines_after, lines_before)


# ---------------------------------------------------------------------------
# (g) compact() + reload — новый экземпляр store читает компактированный файл
# ---------------------------------------------------------------------------


class TestCompactPersistReload(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_compact_then_reload_preserves_state(self) -> None:
        """Новый экземпляр store после compact() читает корректное состояние."""
        store1 = _make_store(self.data_dir)
        s = store1.create("+79001234567", "перезагрузка после compact")
        _walk_to_talking(store1, s.id)
        for i in range(10):
            store1.add_transcript(s.id, "user", f"реплика {i}")
        store1.compact()

        store2 = _make_store(self.data_dir)
        fetched = store2.get(s.id)
        assert fetched is not None
        self.assertEqual(fetched.id, s.id)
        self.assertEqual(fetched.status, CallStatus.TALKING.value)
        self.assertEqual(len(fetched.transcript_history), 10)

    def test_compact_preserves_multiple_sessions_on_reload(self) -> None:
        """Несколько сессий корректно восстанавливаются после compact() + reload."""
        store1 = _make_store(self.data_dir)
        session_ids = set()
        for i in range(6):
            s = store1.create(f"+{i}", f"цель {i}")
            session_ids.add(s.id)
            store1.add_transcript(s.id, "u", f"привет {i}")
        store1.compact()

        store2 = _make_store(self.data_dir)
        sessions = store2.list_sessions(limit=10)
        self.assertEqual(len(sessions), 6)
        self.assertEqual({d["id"] for d in sessions}, session_ids)


if __name__ == "__main__":
    unittest.main()
