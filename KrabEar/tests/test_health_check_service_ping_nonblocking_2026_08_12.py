"""Спека 2026-08-12 ping-nonblocking — HealthCheckService side (сиблинг
спеки settings-read-nonblocking, `test_settings_service_read_lock_timeout_2026_08_12.py`).

Живой замер на задеплоенном коде (2026-08-12): захват `history.lock` из
отдельного процесса на 12с показал, что `settings_get`/`wake_word_status`/
`list_recent_errors` уже уложились в короткий read-path бюджет предыдущей
волны, но `ping` по-прежнему блокировался на все 8+ секунд — потому что
`HealthCheckService.handle_ping` вызывал `StateStore.count_active_items()`,
который брал ТОТ ЖЕ эксклюзивный flock, что и вся история, с общим 30с
инстанс-таймаутом `StateStore._lock()`, минуя read-path бюджет настроек.

`ping` — это 3-секундный heartbeat `HealthMonitor.swift` (2 подряд неответа
→ `forceRestartBackend`/`launchctl kickstart -k`): медленная операция с
историей в другом потоке заставляла Swift-агент рестартовать ЗДОРОВЫЙ
бэкенд посреди активной диктовки (живой инцидент 2026-08-12 14:07:48, две
диктовки потеряны). Фикс ограничивает ОЖИДАНИЕ именно `history_count`
внутри `handle_ping` коротким бюджетом (`ping_count_lock_timeout_sec`,
дефолт 0.3с) — не уложились, отдаём последнее известное значение вместо
блокировки хендлера.

Покрывает:
- выбор бюджета ожидания (settings → дефолт модуля; 0 = прежнее поведение);
- фоллбэк на последнее известное значение при `StateStoreLockTimeout` (0 при
  холодном старте) — НЕ `-1`, тот путь остаётся только для прочих исключений;
- WARNING один раз на эпизод контенции, не на каждый 3с тик;
- контракт `handle_ping` остаётся bit-exact (все 6 ключей, `history_count`
  всегда int) под контенцией;
- живая проверка с РЕАЛЬНО захваченным flock из другого потока.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.health_check_service import HealthCheckService  # noqa: E402
from backend.models import DEFAULT_SETTINGS  # noqa: E402
from backend.settings_service import SettingsService  # noqa: E402
from backend.state_store import StateStore, StateStoreLockTimeout  # noqa: E402


def _make_mock_store(count: int = 3) -> MagicMock:
    store = MagicMock()
    store.count_active_items.return_value = count
    return store


def _make_service(store, settings: dict | None = None, **overrides) -> tuple[HealthCheckService, MagicMock]:
    settings_svc = MagicMock()
    settings_svc.cached_settings.return_value = dict(settings or {})
    defaults: dict = dict(
        store=store,
        health_checker=MagicMock(),
        startup_diagnostics=MagicMock(),
        integrity_checker=MagicMock(),
        settings_svc=settings_svc,
        start_time=time.monotonic() - 5.0,
        app_version="test-ping-nonblocking",
        recorder=SimpleNamespace(is_recording=False),
    )
    defaults.update(overrides)
    return HealthCheckService(**defaults), settings_svc


# ---------------------------------------------------------------------------
# Budget selection (mocked store — контракт вызова StateStore.count_active_items)
# ---------------------------------------------------------------------------

class TestPingCountBudgetSelection(unittest.TestCase):
    """Какой бюджет и как именно handle_ping() передаёт в count_active_items()."""

    def test_default_budget_used_when_no_settings_svc(self):
        """settings_svc=None (не все конструкторы его передают) → дефолт модуля 0.3с."""
        store = _make_mock_store()
        svc, _ = _make_service(store, settings_svc=None)

        svc.handle_ping({})

        expected = float(DEFAULT_SETTINGS["ping_count_lock_timeout_sec"])
        store.count_active_items.assert_called_once_with(lock_timeout_sec=expected)

    def test_budget_read_from_settings(self):
        store = _make_mock_store()
        svc, _ = _make_service(store, settings={"ping_count_lock_timeout_sec": 1.5})

        svc.handle_ping({})

        store.count_active_items.assert_called_once_with(lock_timeout_sec=1.5)

    def test_zero_budget_calls_without_override_prev_behavior(self):
        """ping_count_lock_timeout_sec=0 → вызов БЕЗ override (прежнее поведение)."""
        store = _make_mock_store()
        svc, _ = _make_service(store, settings={"ping_count_lock_timeout_sec": 0})

        svc.handle_ping({})

        store.count_active_items.assert_called_once_with()  # без аргументов вовсе

    def test_invalid_budget_falls_back_to_default(self):
        store = _make_mock_store()
        svc, _ = _make_service(store, settings={"ping_count_lock_timeout_sec": "garbage"})

        svc.handle_ping({})

        expected = float(DEFAULT_SETTINGS["ping_count_lock_timeout_sec"])
        store.count_active_items.assert_called_once_with(lock_timeout_sec=expected)

    def test_fresh_uncontended_path_unchanged_result(self):
        """Свободный лок: результат идентичен старому поведению."""
        store = _make_mock_store(count=7)
        svc, _ = _make_service(store)

        result = svc.handle_ping({})

        self.assertEqual(result["history_count"], 7)
        store.count_active_items.assert_called_once()


# ---------------------------------------------------------------------------
# Fail-closed fallback to LAST KNOWN value (mocked store — StateStoreLockTimeout simulated)
# ---------------------------------------------------------------------------

class TestLastKnownFallback(unittest.TestCase):
    """StateStoreLockTimeout → последнее известное значение, НЕ -1."""

    def test_cold_start_contention_returns_zero_not_minus1(self):
        """Ни разу не читали (холодный старт) И лок занят → 0, а не -1 —
        -1 зарезервирован под прочие (не lock-timeout) ошибки."""
        store = _make_mock_store()
        store.count_active_items.side_effect = StateStoreLockTimeout("simulated contention")
        svc, _ = _make_service(store)

        result = svc.handle_ping({})

        self.assertEqual(result["history_count"], 0)

    def test_known_value_survives_contention(self):
        store = _make_mock_store(count=7)
        svc, _ = _make_service(store)
        first = svc.handle_ping({})
        self.assertEqual(first["history_count"], 7)

        store.count_active_items.side_effect = StateStoreLockTimeout("simulated contention")
        second = svc.handle_ping({})

        self.assertEqual(second["history_count"], 7, "known value must survive contention as-is")

    def test_generic_exception_still_returns_minus1(self):
        """Не lock-contention (повреждённый файл истории и т.п.) — старое
        поведение (-1) сохраняется, отличается от StateStoreLockTimeout-ветки."""
        store = _make_mock_store()
        store.count_active_items.side_effect = RuntimeError("history file corrupted")
        svc, _ = _make_service(store)

        result = svc.handle_ping({})

        self.assertEqual(result["history_count"], -1)

    def test_recovery_after_contention_reads_fresh_value(self):
        store = _make_mock_store(count=2)
        svc, _ = _make_service(store)
        svc.handle_ping({})

        store.count_active_items.side_effect = StateStoreLockTimeout("simulated contention")
        svc.handle_ping({})  # contended, returns stale — не проверяем здесь

        store.count_active_items.side_effect = None
        store.count_active_items.return_value = 9
        result = svc.handle_ping({})

        self.assertEqual(result["history_count"], 9)

    def test_privacy_mode_short_circuits_before_store_call(self):
        """privacy_mode_enabled=True → 0, count_active_items() не вызывается вовсе
        (существующий wave-1770 гейт, не должен ломаться новым кодом)."""
        store = _make_mock_store(count=5)
        settings_svc = MagicMock()
        settings_svc.cached_settings.return_value = {"privacy_mode_enabled": True}
        svc, _ = _make_service(store, settings_svc=settings_svc)

        result = svc.handle_ping({})

        self.assertEqual(result["history_count"], 0)
        store.count_active_items.assert_not_called()


# ---------------------------------------------------------------------------
# WARNING один раз на эпизод, не на каждый 3с тик (лог-шторм из живого инцидента)
# ---------------------------------------------------------------------------

class TestWarningOncePerEpisode(unittest.TestCase):

    def test_repeated_contention_logs_warning_only_once(self):
        store = _make_mock_store()
        store.count_active_items.side_effect = StateStoreLockTimeout("simulated contention")
        svc, _ = _make_service(store)

        with self.assertLogs("KrabEar.Backend.HealthCheckService", level="WARNING") as cm:
            svc.handle_ping({})
            svc.handle_ping({})
            svc.handle_ping({})

        once_msg = f"expected exactly one WARNING across 3 contended pings, got: {cm.output}"
        self.assertEqual(len(cm.output), 1, once_msg)

    def test_new_episode_after_recovery_warns_again(self):
        store = _make_mock_store()
        store.count_active_items.side_effect = StateStoreLockTimeout("simulated contention")
        svc, _ = _make_service(store)

        with self.assertLogs("KrabEar.Backend.HealthCheckService", level="WARNING") as cm1:
            svc.handle_ping({})
        self.assertEqual(len(cm1.output), 1)

        # Лок освобождается — успешное чтение закрывает эпизод.
        store.count_active_items.side_effect = None
        store.count_active_items.return_value = 4
        svc.handle_ping({})

        # Новая контенция — новый эпизод, новая WARNING.
        store.count_active_items.side_effect = StateStoreLockTimeout("simulated contention 2")
        with self.assertLogs("KrabEar.Backend.HealthCheckService", level="WARNING") as cm2:
            svc.handle_ping({})
        self.assertEqual(len(cm2.output), 1)


# ---------------------------------------------------------------------------
# Контракт handle_ping остаётся bit-exact под контенцией
# ---------------------------------------------------------------------------

class TestPingContractUnderContention(unittest.TestCase):

    def test_all_keys_present_and_history_count_is_int_under_contention(self):
        store = _make_mock_store()
        store.count_active_items.side_effect = StateStoreLockTimeout("simulated contention")
        svc, _ = _make_service(store)

        result = svc.handle_ping({})

        required = {"status", "service", "version", "uptime_sec", "is_recording", "history_count"}
        self.assertEqual(required, set(result.keys()))
        self.assertEqual(result["status"], "ok")
        self.assertIsInstance(result["history_count"], int)
        self.assertNotIsInstance(result["history_count"], bool)


# ---------------------------------------------------------------------------
# Живая проверка: реально захваченный flock из ДРУГОГО треда
# ---------------------------------------------------------------------------

class TestRealFlockContention(unittest.TestCase):
    """DoD: под удерживаемым чужим потоком локом handle_ping() возвращается
    за ~бюджет, а не блокируется. Использует настоящий StateStore + настоящий
    fcntl.flock, не мок — см. test_settings_service_read_lock_timeout_2026_08_12.py
    за образцом того же приёма на SettingsService."""

    def _make_real_store(self, tmp_dir: str) -> StateStore:
        return StateStore(Path(tmp_dir) / "data")

    def _make_real_service(self, store: StateStore) -> HealthCheckService:
        return HealthCheckService(
            store=store,
            health_checker=MagicMock(),
            startup_diagnostics=MagicMock(),
            integrity_checker=MagicMock(),
            settings_svc=SettingsService(store=store),
            start_time=time.monotonic() - 1.0,
            app_version="test-ping-live",
            recorder=SimpleNamespace(is_recording=False),
        )

    def test_handle_ping_returns_near_budget_not_full_hold_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._make_real_store(tmp)
            store.save_settings({
                "quality_profile": "balanced",
                "ping_count_lock_timeout_sec": 0.3,
            })
            store.add_history_item(text="one")
            store.add_history_item(text="two")
            svc = self._make_real_service(store)

            primed = svc.handle_ping({})  # лок свободен — читает нормально, кэш заполнен
            self.assertEqual(primed["history_count"], 2)

            acquired = threading.Event()
            release_holder = threading.Event()

            def stuck_holder():
                with store._lock():
                    acquired.set()
                    release_holder.wait(timeout=10)

            holder = threading.Thread(target=stuck_holder, name="ping-nonblocking-holder")
            holder.start()
            self.assertTrue(acquired.wait(timeout=5))

            start = time.monotonic()
            result = svc.handle_ping({})
            elapsed = time.monotonic() - start

            release_holder.set()
            holder.join(timeout=5)

            self.assertLess(
                elapsed, 3.0,
                "handle_ping() must bound its wait to ~ping_count_lock_timeout_sec, "
                "not the holder's full 10s hold",
            )
            # Известное (последнее успешно прочитанное) значение — не блокируемся, но и не -1.
            self.assertEqual(result["history_count"], 2)
            self.assertEqual(result["status"], "ok")

    def test_cold_start_contention_returns_zero_without_blocking(self):
        """Backend только что стартовал (HealthCheckService ещё ни разу не читал
        history_count), и ровно в этот момент лок занят другим потоком —
        handle_ping() обязан вернуться быстро с history_count=0, не дожидаясь
        холдера и не возвращая -1 (это НЕ ошибка чтения истории, это contention)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._make_real_store(tmp)
            store.save_settings({"ping_count_lock_timeout_sec": 0.3})
            store.add_history_item(text="one")
            svc = self._make_real_service(store)  # холодный: handle_ping() ещё не звался

            acquired = threading.Event()
            release_holder = threading.Event()

            def stuck_holder():
                with store._lock():
                    acquired.set()
                    release_holder.wait(timeout=10)

            holder = threading.Thread(target=stuck_holder, name="ping-cold-start-holder")
            holder.start()
            self.assertTrue(acquired.wait(timeout=5))

            start = time.monotonic()
            result = svc.handle_ping({})
            elapsed = time.monotonic() - start

            release_holder.set()
            holder.join(timeout=5)

            self.assertLess(elapsed, 3.0)
            self.assertEqual(result["history_count"], 0)

    def test_recovers_real_value_once_holder_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._make_real_store(tmp)
            store.save_settings({"ping_count_lock_timeout_sec": 0.3})
            store.add_history_item(text="one")
            svc = self._make_real_service(store)
            svc.handle_ping({})

            release_holder = threading.Event()
            acquired = threading.Event()

            def stuck_holder():
                with store._lock():
                    acquired.set()
                    release_holder.wait(timeout=10)

            holder = threading.Thread(target=stuck_holder)
            holder.start()
            self.assertTrue(acquired.wait(timeout=5))

            svc.handle_ping({})  # контендится, отдаёт stale — не проверяем здесь

            release_holder.set()
            holder.join(timeout=5)

            store.add_history_item(text="two")
            result = svc.handle_ping({})
            self.assertEqual(result["history_count"], 2)


if __name__ == "__main__":
    unittest.main()
