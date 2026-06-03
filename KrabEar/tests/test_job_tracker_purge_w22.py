"""Wave-22: privacy-purge ДОЛЖЕН стирать in-memory JobTracker._jobs (root-cause guard).

FINDING (MED): terminal-задачи хранят полный текст транскрипций (items[].text) в
JobTracker._jobs (RAM-only). Задачи могут жить до 1 часа после завершения
(max_age_sec=3600). Без этого шага transcript text переживает handle_purge_all_data.

Тест строит минимальные коллабораторы:
  - реальный StateStore на temp-dir (без моделей)
  - реальный JobTracker
  - HistoryService с late-injected _job_tracker

Наполняет JobTracker terminal-задачей с текстом транскрипции, вызывает
handle_purge_all_data и проверяет, что _jobs пуст после.

Этот тест ОБЯЗАН падать, если purge когда-либо перестанет вызывать job_tracker.clear().
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.job_tracker import JobTracker          # noqa: E402
from backend.state_store import StateStore          # noqa: E402
from backend.history_service import HistoryService  # noqa: E402


_PII_TEXT = "Иван Петров обсуждал секретный контракт с компанией Альфа на 50 миллионов"


class JobTrackerClearTestCase(unittest.TestCase):
    """Юнит-тест JobTracker.clear() — изолированно от HistoryService."""

    def test_clear_empties_all_dicts(self) -> None:
        """clear() обнуляет _jobs, _cancel_events, _evict_times, _cancel_events_ts."""
        jt = JobTracker()

        # Создаём job
        job_id = jt.create_job(total_files=1)
        # Помечаем done с PII-текстом
        jt.mark_done(job_id, items=[{"text": _PII_TEXT}], errors=[])

        # Убеждаемся, что до clear() запись есть и содержит PII
        state = jt.get(job_id)
        self.assertIsNotNone(state)
        self.assertEqual(state["items"][0]["text"], _PII_TEXT)  # type: ignore[index]
        self.assertIn(job_id, jt._jobs)
        self.assertIn(job_id, jt._cancel_events)

        # --- действие ---
        count = jt.clear()

        # --- постусловия ---
        self.assertEqual(count, 1, "clear() должна вернуть кол-во удалённых задач")
        self.assertEqual(len(jt._jobs), 0, "_jobs должен быть пуст")
        self.assertEqual(len(jt._cancel_events), 0, "_cancel_events должен быть пуст")
        self.assertEqual(len(jt._evict_times), 0, "_evict_times должен быть пуст")
        self.assertEqual(len(jt._cancel_events_ts), 0, "_cancel_events_ts должен быть пуст")
        self.assertIsNone(jt.get(job_id), "get() должен вернуть None после clear()")

    def test_clear_on_empty_tracker_returns_zero(self) -> None:
        """clear() на пустом реестре безопасна и возвращает 0."""
        jt = JobTracker()
        self.assertEqual(jt.clear(), 0)

    def test_clear_sets_cancel_event_for_active_job(self) -> None:
        """clear() устанавливает cancel_event до удаления — воркер увидит отмену."""
        jt = JobTracker()
        job_id = jt.create_job(total_files=2)
        # Активная задача (не terminal) — cancel_event не установлен
        event = jt.get_cancel_event(job_id)
        self.assertIsNotNone(event)
        self.assertFalse(event.is_set(), "event не должен быть установлен до clear()")  # type: ignore[union-attr]

        jt.clear()

        # После clear(): event SET (воркер должен завершиться), объект ещё доступен
        # через сохранённую ссылку, но в реестре уже нет.
        self.assertTrue(event.is_set(), "event должен быть установлен после clear()")  # type: ignore[union-attr]

    def test_clear_idempotent(self) -> None:
        """Повторный вызов clear() безопасен."""
        jt = JobTracker()
        jt.create_job(total_files=1)
        jt.clear()
        jt.clear()  # второй раз — не должно бросать
        self.assertEqual(len(jt._jobs), 0)

    def test_clear_returns_correct_count_multiple_jobs(self) -> None:
        """clear() корректно считает несколько задач в разных статусах."""
        jt = JobTracker()
        id1 = jt.create_job(total_files=1)
        id2 = jt.create_job(total_files=2)
        jt.create_job(total_files=3)  # third job remains queued

        jt.mark_done(id1, items=[{"text": "transcription 1"}], errors=[])
        jt.mark_failed(id2, error="ошибка STT")
        # third job остаётся в статусе queued

        count = jt.clear()
        self.assertEqual(count, 3, "clear() должна вернуть 3 для 3 задач")
        self.assertEqual(len(jt._jobs), 0)


class JobTrackerPurgeWiringTestCase(unittest.TestCase):
    """Wave-22: handle_purge_all_data должен вызывать job_tracker.clear().

    Это root-cause regression guard: если purge когда-либо перестанет очищать
    JobTracker._jobs, тест упадёт с явным сообщением об ошибке.
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="krabear_purge_jt_w22_")
        self.store = StateStore(data_dir=Path(self._tmpdir))

        # Добавляем запись в историю, чтобы purge не выглядел пустым
        self.store.add_history_item(text=_PII_TEXT, paste_status="ok")

        # Реальный JobTracker с terminal-задачей, несущей PII-текст
        self.job_tracker = JobTracker()
        job_id = self.job_tracker.create_job(total_files=1)
        self.job_tracker.mark_done(
            job_id,
            items=[{"text": _PII_TEXT, "id": "test-item-1", "ts": "2024-01-01T00:00:00"}],
            errors=[],
        )
        # Убеждаемся, что до purge задача в реестре
        self.assertIn(job_id, self.job_tracker._jobs)
        self._job_id = job_id

        # HistoryService с late-injected _job_tracker (зеркалирует BackendService wiring)
        self.svc = HistoryService(store=self.store)
        self.svc._job_tracker = self.job_tracker

    def test_purge_clears_job_tracker_jobs(self) -> None:
        """handle_purge_all_data опустошает JobTracker._jobs (root-cause guard).

        Этот тест ДОЛЖЕН ПАДАТЬ, если purge перестанет вызывать job_tracker.clear().
        """
        result = self.svc.handle_purge_all_data({"confirm": True})

        self.assertTrue(result.get("ok"), f"purge вернул ok=False: {result}")
        self.assertEqual(
            len(self.job_tracker._jobs),
            0,
            "JobTracker._jobs должен быть пуст после purge (Wave-22 MED purge-gap)",
        )

    def test_purge_job_tracker_not_in_secondary_errors(self) -> None:
        """Шаг job_tracker не должен попадать в secondary_errors при успешной очистке."""
        result = self.svc.handle_purge_all_data({"confirm": True})
        self.assertNotIn(
            "job_tracker",
            result.get("errors", []),
            f"job_tracker оказался в secondary_errors: {result.get('errors')}",
        )

    def test_purge_requires_confirmation(self) -> None:
        """purge без confirm не должен трогать JobTracker."""
        result = self.svc.handle_purge_all_data({})  # без confirm
        self.assertFalse(result.get("ok", True), "должен вернуть ошибку без confirm")
        # Задача должна остаться
        self.assertIn(
            self._job_id,
            self.job_tracker._jobs,
            "JobTracker._jobs не должен меняться без confirm",
        )

    def test_purge_clears_all_sibling_dicts(self) -> None:
        """Все четыре dict JobTracker пусты после purge."""
        self.svc.handle_purge_all_data({"confirm": True})
        self.assertEqual(len(self.job_tracker._jobs), 0, "_jobs не пуст")
        self.assertEqual(len(self.job_tracker._cancel_events), 0, "_cancel_events не пуст")
        self.assertEqual(len(self.job_tracker._evict_times), 0, "_evict_times не пуст")
        self.assertEqual(len(self.job_tracker._cancel_events_ts), 0, "_cancel_events_ts не пуст")

    def test_purge_with_none_job_tracker_is_safe(self) -> None:
        """Если _job_tracker не wired (None), purge не падает (backward-compat)."""
        svc = HistoryService(store=self.store)
        # _job_tracker остаётся None (не wired)
        result = svc.handle_purge_all_data({"confirm": True})
        # Должен завершиться без исключений; job_tracker не в errors
        self.assertNotIn("job_tracker", result.get("errors", []))


if __name__ == "__main__":
    unittest.main()
