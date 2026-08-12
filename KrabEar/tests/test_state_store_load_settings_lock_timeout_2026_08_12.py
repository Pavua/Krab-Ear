"""Спека 2026-08-12 settings-read-nonblocking — StateStore side.

`StateStore.load_settings()` берёт `self._lock()` — тот же эксклюзивный
flock, что охраняет всю историю. `SettingsService.cached_settings()` на
промахе TTL раньше ждал этот лок СКОЛЬКО ПРИДЁТСЯ (общий инстанс-таймаут
30с) — долгая операция с историей в другом потоке подвешивала privacy-гейт
каждого IPC-хендлера. Эти тесты покрывают ТОЛЬКО механизм override на
уровне StateStore (`_lock(timeout_sec=...)` / `load_settings(lock_timeout_sec=...)`),
которым пользуется SettingsService — сам fail-closed фоллбэк покрыт
test_settings_service_read_lock_timeout_2026_08_12.py.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore, StateStoreLockTimeout  # noqa: E402


def _make_store(tmp_dir: str, **kwargs) -> StateStore:
    return StateStore(Path(tmp_dir) / "data", **kwargs)


class TestLoadSettingsLockTimeoutOverride(unittest.TestCase):
    """load_settings(lock_timeout_sec=...) обязан использовать СВОЙ бюджет,
    а не инстанс-таймаут StateStore (по умолчанию 30с в проде)."""

    def test_short_override_raises_quickly_while_instance_default_is_long(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Инстанс-таймаут намеренно ОГРОМНЫЙ — если бы override не работал,
            # тест бы завис на 30с (или на длительность удержания холдером).
            store = _make_store(tmp, lock_acquire_timeout_sec=30.0)

            acquired = threading.Event()
            release_holder = threading.Event()

            def stuck_holder():
                with store._lock():
                    acquired.set()
                    release_holder.wait(timeout=10)

            holder_thread = threading.Thread(target=stuck_holder, name="stuck-holder")
            holder_thread.start()
            self.assertTrue(acquired.wait(timeout=5))

            start = time.monotonic()
            with self.assertRaises(StateStoreLockTimeout):
                store.load_settings(lock_timeout_sec=0.2)
            elapsed = time.monotonic() - start

            release_holder.set()
            holder_thread.join(timeout=5)

            self.assertLess(
                elapsed, 2.0,
                "load_settings(lock_timeout_sec=0.2) must give up near its OWN budget, "
                "not wait for the instance-level 30s default",
            )
            self.assertGreaterEqual(elapsed, 0.15, "gave up suspiciously before its own deadline")

    def test_none_override_uses_instance_default_unaffected(self):
        """load_settings() без override — обычные ~50 call sites не видят разницы."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp, lock_acquire_timeout_sec=0.3)

            acquired = threading.Event()
            release_holder = threading.Event()

            def stuck_holder():
                with store._lock():
                    acquired.set()
                    release_holder.wait(timeout=5)

            holder_thread = threading.Thread(target=stuck_holder, name="stuck-holder")
            holder_thread.start()
            self.assertTrue(acquired.wait(timeout=5))

            start = time.monotonic()
            with self.assertRaises(StateStoreLockTimeout):
                store.load_settings()  # no override → falls back to instance default (0.3s here)
            elapsed = time.monotonic() - start

            release_holder.set()
            holder_thread.join(timeout=5)

            self.assertGreaterEqual(elapsed, 0.25, "instance default timeout was not honored")
            self.assertLess(elapsed, 2.0)

    def test_fresh_lock_reads_settings_normally_with_override(self):
        """Свободный лок: override не меняет поведение (значение, отсутствие лишних чтений)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_settings({"quality_profile": "max", "history_page_size": 77})

            plain = store.load_settings()
            overridden = store.load_settings(lock_timeout_sec=0.5)

            self.assertEqual(plain["quality_profile"], "max")
            self.assertEqual(overridden["quality_profile"], "max")
            self.assertEqual(overridden["history_page_size"], 77)
            self.assertEqual(plain, overridden)

    def test_reentrant_call_from_held_lock_ignores_short_override(self):
        """Поток, уже держащий лок, не должен спотыкаться о короткий override
        на вложенном вызове (реентерабельность — no-op поверх depth-counter,
        см. StateStore._lock docstring)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_settings({"quality_profile": "balanced"})

            with store._lock():
                # Вложенный вызов той же нитью с крошечным бюджетом — обязан
                # быть мгновенным no-op, а не пытаться захватить flock заново.
                result = store.load_settings(lock_timeout_sec=0.001)
                self.assertEqual(result["quality_profile"], "balanced")


if __name__ == "__main__":
    unittest.main()
