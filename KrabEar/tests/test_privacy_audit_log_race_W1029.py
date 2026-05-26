"""Тест для W1027 F1 HIGH — race condition в log_event() на _last_hash.

Проверяет что при конкурентных вызовах log_event из 20 потоков цепочка
HMAC-SHA256 остаётся валидной (verify_chain() возвращает valid=True).

Fix: threading.Lock(_log_lock) в PrivacyAuditLogger.__init__ сериализует
весь цикл read→compute→write→update внутри процесса.
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


class TestConcurrentLogEventChainValid(unittest.TestCase):
    """W1027 F1 HIGH — concurrent log_event must keep chain valid."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "privacy_audit.log"
        PrivacyAuditLogger.reset_instance()
        self.audit = PrivacyAuditLogger(log_path=self.log_path)

    def tearDown(self):
        PrivacyAuditLogger.reset_instance()

    def test_concurrent_log_event_chain_stays_valid(self):
        """20 потоков вызывают log_event конкурентно; verify_chain() должен вернуть valid=True."""
        n_threads = 20
        barrier = threading.Barrier(n_threads)
        errors: list[Exception] = []

        def worker(idx: int) -> None:
            try:
                barrier.wait()  # синхронный старт — максимизируем конкуренцию
                self.audit.log_event(
                    category="test",
                    action="concurrent_write",
                    details={"thread": idx},
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertFalse(errors, f"Потоки бросили исключения: {errors}")

        # Все 20 записей должны быть на диске
        entries = self.audit.read_entries(limit=100)
        self.assertEqual(
            len(entries),
            n_threads,
            f"Ожидали {n_threads} записей, получили {len(entries)}",
        )

        # Цепочка должна быть целостной
        result = self.audit.verify_chain()
        self.assertTrue(
            result["valid"],
            f"verify_chain() вернул invalid после конкурентной записи: {result}",
        )
        self.assertIsNone(
            result["first_broken_index"],
            f"Обнаружен разрыв цепочки на индексе {result.get('first_broken_index')}: {result}",
        )
        self.assertEqual(
            result["checked"],
            n_threads,
            f"verify_chain() проверил {result['checked']} записей вместо {n_threads}",
        )

    def test_log_lock_exists(self):
        """PrivacyAuditLogger должен иметь атрибут _log_lock (threading.Lock)."""
        self.assertTrue(
            hasattr(self.audit, "_log_lock"),
            "PrivacyAuditLogger не имеет атрибута _log_lock",
        )
        self.assertIsInstance(
            self.audit._log_lock,
            type(threading.Lock()),
            "_log_lock должен быть threading.Lock",
        )

    def test_sequential_chain_valid_after_fix(self):
        """Последовательные вызовы log_event дают валидную цепочку (smoke)."""
        for i in range(5):
            self.audit.log_event("audit", "sequential", {"i": i})

        result = self.audit.verify_chain()
        self.assertTrue(result["valid"], f"Последовательная цепочка невалидна: {result}")
        self.assertEqual(result["checked"], 5)


if __name__ == "__main__":
    unittest.main()
