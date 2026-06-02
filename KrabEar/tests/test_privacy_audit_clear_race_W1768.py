"""W1768 MED — data race в PrivacyAuditLogger.clear() на _last_hash.

FINDING (privacy_audit.py): clear() мутировал self._last_hash и удалял лог-файл
БЕЗ удержания self._log_lock, конкурируя с log_event(), который тоже читает/пишет
_last_hash под этим же замком → порча HMAC хеш-цепочки / interleaved writes.

FIX (W1768): body метода clear() обёрнут в `with self._log_lock:`. Замок —
non-reentrant threading.Lock; clear() НЕ вызывает log_event(), поэтому deadlock'а нет.

Тесты:
  test_clear_acquires_log_lock          — clear() реально берёт _log_lock (spy).
  test_interleaved_clear_and_log_consistent
                                         — конкурентные clear()+log_event() не бросают
                                           исключений, _last_hash well-defined, цепочка
                                           уцелевших записей валидна (verify_chain()).
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.privacy_audit import PrivacyAuditLogger  # noqa: E402


class TestPrivacyAuditClearRaceW1768(unittest.TestCase):
    """W1768 MED — clear() должен держать _log_lock как log_event()."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "privacy_audit.log"
        PrivacyAuditLogger.reset_instance()
        self.audit = PrivacyAuditLogger(log_path=self.log_path)

    def tearDown(self) -> None:
        PrivacyAuditLogger.reset_instance()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------
    # test_clear_acquires_log_lock
    # ------------------------------------------------------------------
    def test_clear_acquires_log_lock(self) -> None:
        """clear() должен вызвать acquire на self._log_lock (spy-обёртка)."""
        real_lock = self.audit._log_lock
        acquired: list[bool] = []

        class _SpyLock:
            """Прозрачная обёртка над реальным замком — фиксирует acquire."""

            def __enter__(self_inner):
                acquired.append(True)
                return real_lock.__enter__()

            def __exit__(self_inner, *exc):
                return real_lock.__exit__(*exc)

            def acquire(self_inner, *a, **kw):
                acquired.append(True)
                return real_lock.acquire(*a, **kw)

            def release(self_inner):
                return real_lock.release()

        self.audit._log_lock = _SpyLock()

        # Пишем запись и очищаем
        # (log_event тоже берёт замок, поэтому записываем ДО подмены spy уже поздно —
        #  здесь нас интересует только то, что clear() трогает замок)
        self.audit.clear()

        self.assertTrue(
            acquired,
            "clear() не захватил _log_lock — гонка с log_event() остаётся открытой",
        )

    # ------------------------------------------------------------------
    # test_interleaved_clear_and_log_consistent
    # ------------------------------------------------------------------
    def test_interleaved_clear_and_log_consistent(self) -> None:
        """Конкурентные clear()+log_event() не ломают цепочку и не бросают исключений."""
        n_writers = 16
        n_clearers = 4
        barrier = threading.Barrier(n_writers + n_clearers)
        errors: list[Exception] = []

        def writer(idx: int) -> None:
            try:
                barrier.wait()
                for j in range(5):
                    self.audit.log_event(
                        category="test",
                        action="interleaved_write",
                        details={"writer": idx, "seq": j},
                    )
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        def clearer(idx: int) -> None:
            try:
                barrier.wait()
                for _ in range(3):
                    self.audit.clear()
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(i,)) for i in range(n_writers)
        ] + [
            threading.Thread(target=clearer, args=(i,)) for i in range(n_clearers)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertFalse(errors, f"Потоки бросили исключения при гонке: {errors}")

        # _last_hash должен быть well-defined: либо None (после clear),
        # либо непустая hex-строка (после успешной записи).
        last = self.audit._last_hash
        self.assertTrue(
            last is None or (isinstance(last, str) and len(last) > 0),
            f"_last_hash в неопределённом состоянии: {last!r}",
        )

        # Уцелевшие на диске записи должны образовывать валидную цепочку:
        # под общим замком ни одна запись не пишется наполовину и не чередуется
        # с unlink, поэтому остаток лога всегда консистентен.
        result = self.audit.verify_chain()
        self.assertTrue(
            result["valid"],
            f"verify_chain() невалиден после гонки clear()+log_event(): {result}",
        )
        self.assertIsNone(
            result["first_broken_index"],
            f"Разрыв цепочки на индексе {result.get('first_broken_index')}: {result}",
        )

    # ------------------------------------------------------------------
    # test_clear_then_log_starts_fresh_chain
    # ------------------------------------------------------------------
    def test_clear_then_log_starts_fresh_chain(self) -> None:
        """После clear() новая запись начинает свежую цепочку (prev_hash=None)."""
        self.audit.log_event("audit", "before_clear", {"i": 1})
        self.audit.clear()
        self.assertIsNone(self.audit._last_hash, "clear() должен сбросить _last_hash в None")

        self.audit.log_event("audit", "after_clear", {"i": 2})
        entries = self.audit.read_entries(limit=10)
        self.assertEqual(len(entries), 1, "После clear() на диске должна быть одна запись")
        self.assertIsNone(
            entries[0].get("prev_hash"),
            "Первая запись после clear() должна иметь prev_hash=None",
        )

        result = self.audit.verify_chain()
        self.assertTrue(result["valid"], f"Свежая цепочка невалидна: {result}")


if __name__ == "__main__":
    unittest.main()
