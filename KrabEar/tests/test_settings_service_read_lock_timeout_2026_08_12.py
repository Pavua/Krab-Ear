"""Спека 2026-08-12 settings-read-nonblocking — SettingsService side.

Живой инцидент: `StateStore.load_settings()` берёт эксклюзивный flock, общий
со всей историей. Долгая операция с историей в другом потоке (например
`_load_active_items_with_lock` под зависшим MLX) подвешивала privacy-гейт
КАЖДОГО IPC-хендлера на десятки секунд, пока Swift-агент не форс-рестартнёт
бэкенд. Фикс ограничивает ОЖИДАНИЕ именно read-path настроек коротким
бюджетом (`settings_read_lock_timeout_sec`, дефолт 0.5с) вместо блокировки.

Покрывает:
- выбор бюджета ожидания (кэш → дефолт модуля; 0 = прежнее поведение);
- fail-closed направление отказа (последнее известное значение, иначе
  дефолты с ПРИНУДИТЕЛЬНЫМ privacy_mode_enabled=True);
- WARNING один раз на эпизод контенции, не на каждый промах;
- живая проверка с РЕАЛЬНО захваченным flock из другого потока.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.models import DEFAULT_SETTINGS  # noqa: E402
from backend.settings_service import SettingsService  # noqa: E402
from backend.state_store import StateStore, StateStoreLockTimeout  # noqa: E402


def _make_mock_store(settings: dict | None = None) -> MagicMock:
    store = MagicMock()
    current: dict = dict(settings or {
        "quality_profile": "balanced",
        "privacy_mode_enabled": False,
    })
    store.load_settings.return_value = dict(current)
    store._current = current
    return store


# ---------------------------------------------------------------------------
# Budget selection (mocked store — контракт вызова StateStore.load_settings)
# ---------------------------------------------------------------------------

class TestReadLockBudgetSelection(unittest.TestCase):
    """Какой бюджет и как именно cached_settings() передаёт в load_settings()."""

    def test_cold_start_uses_module_default_budget(self):
        """Ни разу не читали настройки → бюджет берётся из DEFAULT_SETTINGS (0.5с)."""
        store = _make_mock_store()
        svc = SettingsService(store=store)

        svc.cached_settings()

        expected = float(DEFAULT_SETTINGS["settings_read_lock_timeout_sec"])
        store.load_settings.assert_called_once_with(lock_timeout_sec=expected)

    def test_subsequent_miss_uses_budget_from_last_known_cache(self):
        """После успешного чтения бюджет на СЛЕДУЮЩИЙ промах берётся из кэша, не дефолта."""
        store = _make_mock_store({
            "quality_profile": "balanced",
            "settings_read_lock_timeout_sec": 1.5,
        })
        svc = SettingsService(store=store)
        svc.cached_settings()  # populates cache with settings_read_lock_timeout_sec=1.5

        svc._cache_ts = 0.0  # форсируем промах TTL
        svc.cached_settings()

        self.assertEqual(store.load_settings.call_count, 2)
        store.load_settings.assert_called_with(lock_timeout_sec=1.5)

    def test_zero_budget_calls_without_override_prev_behavior(self):
        """settings_read_lock_timeout_sec=0 → вызов БЕЗ override (прежнее поведение)."""
        store = _make_mock_store({
            "quality_profile": "balanced",
            "settings_read_lock_timeout_sec": 0,
        })
        svc = SettingsService(store=store)
        svc.cached_settings()  # первый вызов ещё на дефолтном бюджете (кэша не было)

        svc._cache_ts = 0.0
        svc.cached_settings()

        store.load_settings.assert_called_with()  # без аргументов вовсе

    def test_fresh_uncontended_path_unchanged_result(self):
        """Свободный лок: результат идентичен старому поведению (без override)."""
        store = _make_mock_store({"quality_profile": "max", "privacy_mode_enabled": True})
        svc = SettingsService(store=store)

        result = svc.cached_settings()

        self.assertEqual(result["quality_profile"], "max")
        self.assertTrue(result["privacy_mode_enabled"])
        store.load_settings.assert_called_once()


# ---------------------------------------------------------------------------
# Fail-closed fallback (mocked store — StateStoreLockTimeout simulated)
# ---------------------------------------------------------------------------

class TestFailClosedFallback(unittest.TestCase):
    """Направление отказа: известное значение → оно; неизвестность → privacy=True."""

    def test_cold_start_contention_returns_defaults_with_privacy_forced_true(self):
        """Кэша ещё не было (холодный старт) И лок занят → дефолты, privacy_mode_enabled=True
        ПРИНУДИТЕЛЬНО, даже если сам дефолт в DEFAULT_SETTINGS — False."""
        assumption_msg = (
            "тест предполагает, что дефолт privacy_mode_enabled=False — "
            "иначе принудительное forcing ничего не доказывает"
        )
        self.assertFalse(DEFAULT_SETTINGS.get("privacy_mode_enabled", False), assumption_msg)
        store = _make_mock_store()
        store.load_settings.side_effect = StateStoreLockTimeout("simulated contention")
        svc = SettingsService(store=store)

        result = svc.cached_settings()

        forced_msg = "неизвестность обязана трактоваться как приватность включена"
        self.assertTrue(result["privacy_mode_enabled"], forced_msg)
        self.assertEqual(result["quality_profile"], DEFAULT_SETTINGS["quality_profile"])

    def test_known_stale_value_returned_as_is_not_forced(self):
        """Есть последнее известное значение (даже протухшее) → отдаём ЕГО как есть,
        даже если реальный privacy_mode_enabled в нём False — НЕ подменяем на True."""
        store = _make_mock_store({
            "quality_profile": "balanced",
            "privacy_mode_enabled": False,
        })
        svc = SettingsService(store=store)
        first = svc.cached_settings()
        self.assertFalse(first["privacy_mode_enabled"])

        svc._cache_ts = 0.0  # форсируем промах TTL
        store.load_settings.side_effect = StateStoreLockTimeout("simulated contention")
        second = svc.cached_settings()

        known_msg = "известное значение обязано вернуться КАК ЕСТЬ, не переписываться"
        self.assertFalse(second["privacy_mode_enabled"], known_msg)
        self.assertEqual(second["quality_profile"], "balanced")

    def test_known_stale_privacy_true_stays_true(self):
        """Симметричный случай: последнее известное privacy_mode_enabled=True тоже
        обязано пережить контенцию без изменений (fail-closed не путается с fail-open)."""
        store = _make_mock_store({
            "quality_profile": "balanced",
            "privacy_mode_enabled": True,
        })
        svc = SettingsService(store=store)
        svc.cached_settings()

        svc._cache_ts = 0.0
        store.load_settings.side_effect = StateStoreLockTimeout("simulated contention")
        result = svc.cached_settings()

        self.assertTrue(result["privacy_mode_enabled"])

    def test_recovery_after_contention_reads_fresh_value(self):
        """После того, как лок освобождается, следующий промах TTL снова читает
        РЕАЛЬНОЕ (не fail-closed) значение."""
        store = _make_mock_store({
            "quality_profile": "balanced",
            "privacy_mode_enabled": False,
        })
        svc = SettingsService(store=store)
        svc.cached_settings()

        svc._cache_ts = 0.0
        store.load_settings.side_effect = StateStoreLockTimeout("simulated contention")
        svc.cached_settings()

        svc._cache_ts = 0.0
        store.load_settings.side_effect = None
        store.load_settings.return_value = {"quality_profile": "max", "privacy_mode_enabled": False}
        result = svc.cached_settings()

        self.assertEqual(result["quality_profile"], "max")


# ---------------------------------------------------------------------------
# WARNING один раз на эпизод, не на каждый промах (лог-шторм из живого инцидента)
# ---------------------------------------------------------------------------

class TestWarningOncePerEpisode(unittest.TestCase):

    def test_repeated_contention_logs_warning_only_once(self):
        store = _make_mock_store()
        store.load_settings.side_effect = StateStoreLockTimeout("simulated contention")
        svc = SettingsService(store=store)

        with self.assertLogs("backend.settings_service", level="WARNING") as cm:
            svc.cached_settings()
            svc.cached_settings()
            svc.cached_settings()

        once_msg = f"expected exactly one WARNING across 3 contended calls, got: {cm.output}"
        self.assertEqual(len(cm.output), 1, once_msg)

    def test_new_episode_after_recovery_warns_again(self):
        """Эпизод закрывается успешным чтением — следующая контенция снова логирует."""
        store = _make_mock_store()
        store.load_settings.side_effect = StateStoreLockTimeout("simulated contention")
        svc = SettingsService(store=store)

        with self.assertLogs("backend.settings_service", level="WARNING") as cm1:
            svc.cached_settings()
        self.assertEqual(len(cm1.output), 1)

        # Лок освобождается — успешное чтение закрывает эпизод.
        svc._cache_ts = 0.0
        store.load_settings.side_effect = None
        store.load_settings.return_value = {"quality_profile": "balanced"}
        svc.cached_settings()

        # Новая контенция — новый эпизод, новая WARNING.
        svc._cache_ts = 0.0
        store.load_settings.side_effect = StateStoreLockTimeout("simulated contention 2")
        with self.assertLogs("backend.settings_service", level="WARNING") as cm2:
            svc.cached_settings()
        self.assertEqual(len(cm2.output), 1)


# ---------------------------------------------------------------------------
# Живая проверка: реально захваченный flock из ДРУГОГО треда
# ---------------------------------------------------------------------------

class TestRealFlockContention(unittest.TestCase):
    """DoD: под удерживаемым чужим потоком локом cached_settings() возвращается
    за ~timeout, а не блокируется. Использует настоящий StateStore + настоящий
    fcntl.flock, не мок — см. test_state_store_lock_invariants.py за образцом."""

    def _make_real_store(self, tmp_dir: str) -> StateStore:
        return StateStore(Path(tmp_dir) / "data")

    def test_cached_settings_returns_near_budget_not_full_hold_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._make_real_store(tmp)
            store.save_settings({
                "quality_profile": "balanced",
                "privacy_mode_enabled": True,
                "settings_read_lock_timeout_sec": 0.3,
            })
            svc = SettingsService(store=store)
            primed = svc.cached_settings()  # лок свободен — читает нормально, кэш заполнен
            self.assertTrue(primed["privacy_mode_enabled"])

            svc._cache_ts = 0.0  # форсируем промах TTL

            acquired = threading.Event()
            release_holder = threading.Event()

            def stuck_holder():
                with store._lock():
                    acquired.set()
                    release_holder.wait(timeout=10)

            holder = threading.Thread(target=stuck_holder, name="settings-read-nonblocking-holder")
            holder.start()
            self.assertTrue(acquired.wait(timeout=5))

            start = time.monotonic()
            result = svc.cached_settings()
            elapsed = time.monotonic() - start

            release_holder.set()
            holder.join(timeout=5)

            self.assertLess(
                elapsed, 3.0,
                "cached_settings() must bound its wait to ~settings_read_lock_timeout_sec, "
                "not the holder's full 10s hold",
            )
            # Известное (пусть протухшее) значение — не блокируемся, но и не молчим.
            self.assertTrue(result["privacy_mode_enabled"])
            self.assertEqual(result["quality_profile"], "balanced")

    def test_cold_start_contention_fails_closed_without_blocking(self):
        """Backend только что стартовал (кэша SettingsService ещё нет), и ровно в
        этот момент лок занят другим потоком — новый экземпляр SettingsService
        обязан вернуться быстро с privacy_mode_enabled=True, не дожидаясь холдера."""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._make_real_store(tmp)
            # Реальные настройки на диске говорят privacy=False — но SettingsService
            # их прочитать не сможет (лок занят), поэтому обязан считать неизвестность
            # приватностью, а НЕ читерски подглядеть в файл в обход лока.
            store.save_settings({
                "quality_profile": "balanced",
                "privacy_mode_enabled": False,
            })
            svc = SettingsService(store=store)  # холодный: cached_settings() ещё не звался

            acquired = threading.Event()
            release_holder = threading.Event()

            def stuck_holder():
                with store._lock():
                    acquired.set()
                    release_holder.wait(timeout=10)

            holder = threading.Thread(target=stuck_holder, name="settings-cold-start-holder")
            holder.start()
            self.assertTrue(acquired.wait(timeout=5))

            start = time.monotonic()
            result = svc.cached_settings()
            elapsed = time.monotonic() - start

            release_holder.set()
            holder.join(timeout=5)

            self.assertLess(elapsed, 3.0)
            cold_start_msg = "холодный старт + контенция обязаны трактоваться как privacy=True"
            self.assertTrue(result["privacy_mode_enabled"], cold_start_msg)

    def test_recovers_real_value_once_holder_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._make_real_store(tmp)
            store.save_settings({"quality_profile": "balanced", "privacy_mode_enabled": True})
            svc = SettingsService(store=store)
            svc.cached_settings()
            svc._cache_ts = 0.0

            release_holder = threading.Event()
            acquired = threading.Event()

            def stuck_holder():
                with store._lock():
                    acquired.set()
                    release_holder.wait(timeout=10)

            holder = threading.Thread(target=stuck_holder)
            holder.start()
            self.assertTrue(acquired.wait(timeout=5))

            svc.cached_settings()  # контендится, отдаёт stale — не проверяем здесь

            release_holder.set()
            holder.join(timeout=5)

            store.save_settings({"quality_profile": "max", "privacy_mode_enabled": True})
            svc._cache_ts = 0.0
            result = svc.cached_settings()
            self.assertEqual(result["quality_profile"], "max")


if __name__ == "__main__":
    unittest.main()
