"""Спека 2026-08-12 ping-zero-wait — SettingsService side.

Сиблинг `test_state_store_nowait_2026_08_12.py` (StateStore-примитив) и
`test_settings_service_read_lock_timeout_2026_08_12.py` (существующий
бюджетный read-path). `HealthCheckService.handle_ping` внутри себя читает
`privacy_mode_enabled` через `SettingsService.cached_settings()` — на
промахе 5с TTL-кэша это ТОЖЕ попытка захвата `history.lock` (тот же
эксклюзивный flock, что и вся история), с собственным бюджетом
(`settings_read_lock_timeout_sec`, дефолт 0.5с, до 60с по валидатору).
Живой замер показал: сумма ЭТОЙ попытки плюс попытки чтения history_count
внутри одного ping может доходить до ~2с под контенцией — уже сравнимо со
Swift-таймаутом ping (2с). `cached_settings(nowait=True)` даёт ping способ
прочитать privacy_mode БЕЗ единой попытки подождать: свежий TTL-кэш
возвращается как обычно (без похода к StateStore вовсе); промах TTL под
контенцией — РОВНО ОДНА неблокирующая попытка вместо ожидания бюджета,
с ТЕМ ЖЕ fail-closed направлением отказа (последнее известное значение,
иначе дефолты с privacy_mode_enabled=True принудительно), что и обычный
`cached_settings()`.

Покрывает:
- TTL-fresh cache: nowait=True не трогает StateStore вовсе (тот же путь,
  что и обычный cached_settings());
- TTL-промах + свободный лок: nowait=True читает и обновляет кэш нормально;
- TTL-промах + контенция: nowait=True немедленно (не через бюджет) падает
  в тот же fail-closed фоллбэк, что budgeted-путь;
- `settings_read_lock_timeout_sec` НЕ влияет на nowait=True (независимость
  от значения бюджета — тот же принцип, что и у `_lock(nowait=True)`);
- WARNING once-per-episode шарится с обычным (non-nowait) путём — не спамит
  отдельно.
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
    return store


class TestNowaitDispatch(unittest.TestCase):
    """Какой именно вызов делает cached_settings(nowait=True) в store.load_settings()."""

    def test_fresh_ttl_cache_never_touches_store(self):
        """Тёплый TTL-кэш — nowait=True не трогает store вовсе (как и обычный путь)."""
        store = _make_mock_store()
        svc = SettingsService(store=store)
        svc.cached_settings()  # прогреваем кэш
        store.load_settings.reset_mock()

        result = svc.cached_settings(nowait=True)

        store.load_settings.assert_not_called()
        self.assertEqual(result["quality_profile"], "balanced")

    def test_ttl_miss_free_lock_calls_load_settings_nowait_true(self):
        store = _make_mock_store()
        svc = SettingsService(store=store)

        svc.cached_settings(nowait=True)

        store.load_settings.assert_called_once_with(nowait=True)

    def test_ttl_miss_ignores_configured_budget_entirely(self):
        """settings_read_lock_timeout_sec выставлен в нечто огромное — nowait=True
        игнорирует его целиком (независимость от значения бюджета)."""
        store = _make_mock_store({
            "quality_profile": "balanced",
            "settings_read_lock_timeout_sec": 45.0,
        })
        svc = SettingsService(store=store)
        svc.cached_settings()  # прогреваем кэш с большим бюджетом внутри
        svc._cache_ts = 0.0  # форсируем промах TTL
        store.load_settings.reset_mock()

        svc.cached_settings(nowait=True)

        store.load_settings.assert_called_once_with(nowait=True)


class TestNowaitFailClosedFallback(unittest.TestCase):
    """Тот же fail-closed фоллбэк, что и budgeted-путь, просто без ожидания."""

    def test_cold_start_contention_returns_defaults_with_privacy_forced_true(self):
        store = _make_mock_store()
        store.load_settings.side_effect = StateStoreLockTimeout("simulated contention")
        svc = SettingsService(store=store)

        result = svc.cached_settings(nowait=True)

        self.assertTrue(result["privacy_mode_enabled"])
        self.assertEqual(result["quality_profile"], DEFAULT_SETTINGS["quality_profile"])

    def test_known_stale_value_returned_as_is(self):
        store = _make_mock_store({"quality_profile": "balanced", "privacy_mode_enabled": False})
        svc = SettingsService(store=store)
        svc.cached_settings()  # normal warm-up (non-nowait)
        svc._cache_ts = 0.0

        store.load_settings.side_effect = StateStoreLockTimeout("simulated contention")
        result = svc.cached_settings(nowait=True)

        self.assertFalse(result["privacy_mode_enabled"])
        self.assertEqual(result["quality_profile"], "balanced")

    def test_recovers_real_value_once_contention_clears(self):
        store = _make_mock_store()
        store.load_settings.side_effect = StateStoreLockTimeout("simulated contention")
        svc = SettingsService(store=store)
        svc.cached_settings(nowait=True)

        store.load_settings.side_effect = None
        store.load_settings.return_value = {"quality_profile": "max", "privacy_mode_enabled": False}
        svc._cache_ts = 0.0
        result = svc.cached_settings(nowait=True)

        self.assertEqual(result["quality_profile"], "max")


class TestNowaitWarningSharedEpisode(unittest.TestCase):
    """WARNING once-per-episode shared between nowait and budgeted paths — no
    separate spam channel just because the caller used nowait=True."""

    def test_nowait_contention_warns_once_and_suppresses_budgeted_path_too(self):
        store = _make_mock_store()
        store.load_settings.side_effect = StateStoreLockTimeout("simulated contention")
        svc = SettingsService(store=store)

        with self.assertLogs("backend.settings_service", level="WARNING") as cm:
            svc.cached_settings(nowait=True)
            svc.cached_settings()  # budgeted path, same episode
            svc.cached_settings(nowait=True)

        self.assertEqual(len(cm.output), 1, f"expected exactly one WARNING, got: {cm.output}")


class TestRealFlockContentionNowait(unittest.TestCase):
    """DoD: под удерживаемым чужим потоком локом cached_settings(nowait=True)
    возвращается ПОЧТИ МГНОВЕННО (не через бюджет) — настоящий StateStore +
    настоящий fcntl.flock."""

    def _make_real_store(self, tmp_dir: str) -> StateStore:
        return StateStore(Path(tmp_dir) / "data")

    def test_cached_settings_nowait_returns_instantly_regardless_of_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._make_real_store(tmp)
            store.save_settings({
                "quality_profile": "balanced",
                "privacy_mode_enabled": True,
                # Намеренно ОГРОМНЫЙ бюджет — если бы nowait деградировал до
                # budgeted-пути, тест завис бы на секунды.
                "settings_read_lock_timeout_sec": 30.0,
            })
            svc = SettingsService(store=store)
            primed = svc.cached_settings()
            self.assertTrue(primed["privacy_mode_enabled"])
            svc._cache_ts = 0.0  # форсируем промах TTL

            acquired = threading.Event()
            release_holder = threading.Event()

            def stuck_holder():
                with store._lock():
                    acquired.set()
                    release_holder.wait(timeout=10)

            holder = threading.Thread(target=stuck_holder, name="nowait-holder")
            holder.start()
            self.assertTrue(acquired.wait(timeout=5))

            start = time.monotonic()
            result = svc.cached_settings(nowait=True)
            elapsed = time.monotonic() - start

            release_holder.set()
            holder.join(timeout=5)

            self.assertLess(
                elapsed, 0.2,
                "cached_settings(nowait=True) must never wait on the configured budget",
            )
            self.assertTrue(result["privacy_mode_enabled"])
            self.assertEqual(result["quality_profile"], "balanced")

    def test_cold_start_contention_fails_closed_instantly(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._make_real_store(tmp)
            store.save_settings({"quality_profile": "balanced", "privacy_mode_enabled": False})
            svc = SettingsService(store=store)  # cold: no priming call

            acquired = threading.Event()
            release_holder = threading.Event()

            def stuck_holder():
                with store._lock():
                    acquired.set()
                    release_holder.wait(timeout=10)

            holder = threading.Thread(target=stuck_holder, name="nowait-cold-holder")
            holder.start()
            self.assertTrue(acquired.wait(timeout=5))

            start = time.monotonic()
            result = svc.cached_settings(nowait=True)
            elapsed = time.monotonic() - start

            release_holder.set()
            holder.join(timeout=5)

            self.assertLess(elapsed, 0.2)
            self.assertTrue(result["privacy_mode_enabled"])


if __name__ == "__main__":
    unittest.main()
