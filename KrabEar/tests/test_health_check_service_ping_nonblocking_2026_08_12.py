"""Спека 2026-08-12 ping-nonblocking → ping-zero-wait (сиблинг спеки
settings-read-nonblocking, `test_settings_service_read_lock_timeout_2026_08_12.py`,
и её nowait-продолжения `test_settings_service_cached_settings_nowait_2026_08_12.py`).

ИСТОРИЯ волны в этом файле (обе части живут в одном файле — один и тот же
день, один и тот же метод `handle_ping`):

1. Живой замер на задеплоенном коде (2026-08-12, коммит `2f27c547`): захват
   `history.lock` из отдельного процесса на 12с показал, что
   `settings_get`/`wake_word_status`/`list_recent_errors` уже уложились в
   короткий read-path бюджет settings-read-nonblocking, но `ping`
   по-прежнему блокировался на 8+ секунд — `HealthCheckService.handle_ping`
   вызывал `StateStore.count_active_items()` без override, общий 30с
   инстанс-таймаут `StateStore._lock()`. Фикс (`0c10efbf`): бюджет
   `ping_count_lock_timeout_sec` (дефолт 0.3с) — ЛУЧШЕ, но НЕ ЗАКРЫЛО:
2. Повторный живой замер ПОСЛЕ фикса #1 (тот же владелец, тот же прод,
   store ~12 600 записей, захват `history.lock` из отдельного процесса)
   показал: с дефолтными бюджетами суммарное ожидание ВНУТРИ ОДНОГО
   `handle_ping` (privacy-чтение через `cached_settings()` + count-чтение
   через `count_active_items(lock_timeout_sec=budget)`, у каждого свой
   бюджет ожидания) доходило до **2.04с** — Swift
   `main+HealthMonitor.swift:217` шлёт `ping` с таймаутом РОВНО 2с, два
   подряд промаха → `forceRestartBackend`/`launchctl kickstart -k` — живой
   инцидент 2026-08-12 14:07:48 (две диктовки потеряны). Бюджетный подход
   принципиально не даёт жёсткой гарантии: сумма НЕСКОЛЬКИХ попыток растёт
   вместе с их числом, даже если каждая по отдельности мала.

Финальный фикс: `handle_ping` больше НЕ ЧИТАЕТ никакой бюджет вовсе —
`count_active_items(nowait=True)` и privacy-чтение через
`cached_settings(nowait=True)` делают РОВНО ОДНУ неблокирующую попытку
каждое (см. `StateStore._lock(nowait=True)`), независимо от значений
`ping_count_lock_timeout_sec`/`settings_read_lock_timeout_sec` — эти две
настройки остаются в схеме (обратная совместимость settings.json), но
`handle_ping` их больше не консультирует; `TestPingCountBudgetSelection`
ниже (первая часть волны) заменена на `TestPingCountNowaitDispatch`.

Покрывает:
- РОВНО ОДНА неблокирующая попытка для count (nowait=True), независимо от
  `ping_count_lock_timeout_sec`;
- фоллбэк на последнее известное значение при `StateStoreLockTimeout` (0 при
  холодном старте) — НЕ `-1`, тот путь остаётся только для прочих исключений;
- WARNING один раз на эпизод контенции, не на каждый 3с тик;
- контракт `handle_ping` остаётся bit-exact (все 6 ключей, `history_count`
  всегда int) под контенцией;
- живая проверка с РЕАЛЬНО захваченным flock из другого потока — DoD:
  handle_ping() возвращается за < 0.3с независимо от значений бюджетов.
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
# Nowait dispatch (mocked store — контракт вызова StateStore.count_active_items)
# ---------------------------------------------------------------------------

class TestPingCountNowaitDispatch(unittest.TestCase):
    """handle_ping() всегда вызывает count_active_items(nowait=True) — НЕ бюджет
    (заменяет TestPingCountBudgetSelection первой части волны, см. модульный
    докстринг: бюджетный подход не давал жёсткой гарантии)."""

    def test_calls_count_active_items_with_nowait_true(self):
        store = _make_mock_store()
        svc, _ = _make_service(store)

        svc.handle_ping({})

        store.count_active_items.assert_called_once_with(nowait=True)

    def test_ping_count_lock_timeout_sec_setting_is_ignored_entirely(self):
        """Значение ping_count_lock_timeout_sec (даже большое) НЕ влияет на
        вызов — handle_ping больше не консультирует эту настройку вовсе."""
        store = _make_mock_store()
        svc, _ = _make_service(store, settings={"ping_count_lock_timeout_sec": 45.0})

        svc.handle_ping({})

        store.count_active_items.assert_called_once_with(nowait=True)

    def test_no_settings_svc_still_uses_nowait(self):
        """settings_svc=None (не все конструкторы его передают) — count-путь
        не зависит от settings_svc вовсе (в отличие от привязанного к нему
        privacy-чтения)."""
        store = _make_mock_store()
        svc, _ = _make_service(store, settings_svc=None)

        svc.handle_ping({})

        store.count_active_items.assert_called_once_with(nowait=True)

    def test_fresh_uncontended_path_unchanged_result(self):
        """Свободный лок: результат идентичен старому поведению."""
        store = _make_mock_store(count=7)
        svc, _ = _make_service(store)

        result = svc.handle_ping({})

        self.assertEqual(result["history_count"], 7)
        store.count_active_items.assert_called_once_with(nowait=True)


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
    """DoD (спека 2026-08-12 ping-zero-wait): под удерживаемым чужим потоком
    локом handle_ping() возвращается за < 0.3с — НЕЗАВИСИМО от значений
    ping_count_lock_timeout_sec/settings_read_lock_timeout_sec (обе намеренно
    выставлены в тестах ниже в НЕЧТО ОГРОМНОЕ — если бы nowait-путь
    деградировал до budgeted-пути, тесты зависали бы на секунды). Использует
    настоящий StateStore + настоящий fcntl.flock, не мок — см.
    test_settings_service_read_lock_timeout_2026_08_12.py за образцом того
    же приёма на SettingsService."""

    # DoD-порог из живого замера (2.04с суммарного ожидания под бюджетным
    # подходом, Swift-таймаут ping — 2с) — берём с большим запасом.
    DOD_MAX_ELAPSED_SEC = 0.3

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

    def test_handle_ping_returns_under_300ms_regardless_of_budget_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._make_real_store(tmp)
            store.save_settings({
                "quality_profile": "balanced",
                # Оба бюджета намеренно ОГРОМНЫЕ — DoD требует независимости
                # от их значений, не просто "маленького" бюджета по умолчанию.
                "ping_count_lock_timeout_sec": 45.0,
                "settings_read_lock_timeout_sec": 45.0,
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

            holder = threading.Thread(target=stuck_holder, name="ping-zero-wait-holder")
            holder.start()
            self.assertTrue(acquired.wait(timeout=5))

            start = time.monotonic()
            result = svc.handle_ping({})
            elapsed = time.monotonic() - start

            release_holder.set()
            holder.join(timeout=5)

            self.assertLess(
                elapsed, self.DOD_MAX_ELAPSED_SEC,
                "handle_ping() must never wait on the configured budgets — "
                "zero-wait means constant time regardless of contention",
            )
            # Известное (последнее успешно прочитанное) значение — не блокируемся, но и не -1.
            self.assertEqual(result["history_count"], 2)
            self.assertEqual(result["status"], "ok")

    def test_cold_start_contention_returns_zero_under_300ms(self):
        """Backend только что стартовал (ни HealthCheckService, ни SettingsService
        ещё ни разу не читали), и ровно в этот момент лок занят другим
        потоком — handle_ping() обязан вернуться за < 0.3с с history_count=0
        (fail-closed через privacy — неизвестность трактуется как приватность
        включена, см. _is_privacy_mode_nowait), не дожидаясь холдера и не
        возвращая -1 (это НЕ ошибка чтения истории, это contention)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._make_real_store(tmp)
            store.save_settings({
                "ping_count_lock_timeout_sec": 45.0,
                "settings_read_lock_timeout_sec": 45.0,
            })
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

            self.assertLess(elapsed, self.DOD_MAX_ELAPSED_SEC)
            self.assertEqual(result["history_count"], 0)

    def test_warmed_caches_contended_count_still_under_300ms(self):
        """И privacy-кэш, И history_count УЖЕ прогреты (не холодный старт) —
        под контенцией единственная работа handle_ping() — одна nowait-
        попытка count_active_items(), которая должна упасть в last-known
        мгновенно, а не через привязанный к настройке бюджет."""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._make_real_store(tmp)
            store.save_settings({
                "privacy_mode_enabled": False,
                "ping_count_lock_timeout_sec": 45.0,
                "settings_read_lock_timeout_sec": 45.0,
            })
            store.add_history_item(text="one")
            store.add_history_item(text="two")
            store.add_history_item(text="three")
            svc = self._make_real_service(store)
            primed = svc.handle_ping({})
            self.assertEqual(primed["history_count"], 3)

            acquired = threading.Event()
            release_holder = threading.Event()

            def stuck_holder():
                with store._lock():
                    acquired.set()
                    release_holder.wait(timeout=10)

            holder = threading.Thread(target=stuck_holder, name="ping-warmed-holder")
            holder.start()
            self.assertTrue(acquired.wait(timeout=5))

            start = time.monotonic()
            result = svc.handle_ping({})
            elapsed = time.monotonic() - start

            release_holder.set()
            holder.join(timeout=5)

            self.assertLess(elapsed, self.DOD_MAX_ELAPSED_SEC)
            self.assertEqual(result["history_count"], 3, "must fall back to last-known, not -1/0")

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
