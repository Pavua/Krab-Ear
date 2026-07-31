"""S3/Задача 2 — документирующий тест: два ``StateStore`` на одном каталоге
самозаклинивают один и тот же тред НАВСЕГДА.

🔴 Этот тест НЕ проходит цикл красный → зелёный. Он документирует ИЗВЕСТНЫЙ
класс бага, а не проверяет фикс: ``state_store.py`` в рамках этой задачи не
меняется (см. план волны S3, Задача 2, «Чего НЕ делать»). Если когда-нибудь
реентерабельность ``_lock()`` станет keyed-by-path (а не per-instance), этот
тест нужно УДАЛИТЬ, а не чинить — он перестанет отражать реальность.

Почему это важно (контекст фикса дедлока #1872,
``test_state_store_lock_reentrancy_W_deadlock_fix.py``): per-thread
depth-counter реентерабельности (``self._lock_depth: dict[int, int]``) живёт
в ПОЛЕ ЭКЗЕМПЛЯРА. Тред, который держит лок экземпляра A и пытается войти в
лок экземпляра B (на ТОМ ЖЕ файле), не увидит своей записи в ``B._lock_depth``
— решит, что лок свободен, возьмёт ``fcntl.flock`` на НОВОМ файловом
дескрипторе и заблокируется навечно, ожидая освобождения лока, который сам
же держит через экземпляр A (flock не привязан к треду, поэтому тред
блокирует сам себя без исключения — самозаклин, а не корректный wait).

Это ровно та мина, которую устраняет S3/Задача 2 — избавляясь от собственных
module-level ``StateStore`` в ``cloud_stt.py``/``cloud_rewriter.py`` в
принципе, а не пытаясь научить ``_lock()`` быть реентерабельным между
экземплярами.
"""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import unittest  # noqa: E402

from backend.state_store import StateStore  # noqa: E402


class TestCrossInstanceLockSelfDeadlocks(unittest.TestCase):
    """Один тред, два ``StateStore`` на одном каталоге — самозаклин."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_same_thread_two_instances_deadlocks_forever(self):
        store_a = StateStore(self.data_dir)
        store_b = StateStore(self.data_dir)

        entered_b = threading.Event()

        def _run():
            with store_a._lock():
                # Тот же тред, ДРУГОЙ экземпляр StateStore на том же пути —
                # per-thread depth-counter store_b его не видит, поэтому
                # store_b._lock() реально берёт fcntl.flock на новом fd и
                # блокируется навсегда (лок уже держит этот же тред через
                # store_a — flock не привязан к треду, поэтому это
                # самозаклин, а не EDEADLK-исключение).
                with store_b._lock():
                    entered_b.set()  # НЕ должно достигаться

        # 🔴 daemon=True обязателен: тред НАМЕРЕННО остаётся застрявшим в
        # fcntl.flock навсегда. Без daemon=True threading._shutdown() будет
        # джойнить его вечно на выходе интерпретатора — CI повиснет по
        # таймауту и унесёт весь чанк тестов, запущенных в этом процессе.
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=3.0)

        self.assertTrue(
            t.is_alive(),
            "Ожидался самозаклин (тред застрял в fcntl.flock второго "
            "экземпляра) — если тред завершился, значит между двумя "
            "StateStore на одном пути каким-то образом появилась защита; "
            "этот тест — известный документирующий кейс и должен быть "
            "УДАЛЁН (не починен), если поведение действительно изменилось",
        )
        self.assertFalse(
            entered_b.is_set(),
            "Тред не должен был попасть внутрь store_b._lock() — это "
            "означало бы, что самозаклина не произошло",
        )


if __name__ == "__main__":
    unittest.main()
