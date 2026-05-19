"""Wave 160 — тесты для PluginManager.unload_plugin() и on_unload хука."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Path bootstrap — позволяет запускать тест напрямую из корня репозитория
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.plugin_system import (  # noqa: E402
    PluginManager,
    PluginInfo,
    _STATUS_DISABLED,
    _STATUS_LOADED,
    _STATUS_UNLOADED,
)


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------

def _make_plugin(name: str = "test_plugin", version: str = "1.0.0") -> MagicMock:
    """Создаёт mock-объект, реализующий Protocol Plugin."""
    plugin = MagicMock()
    plugin.name = name
    plugin.version = version
    plugin.initialize = MagicMock()
    plugin.get_ipc_methods = MagicMock(return_value={})
    return plugin


def _make_info(name: str = "test_plugin") -> PluginInfo:
    return PluginInfo(
        name=name,
        version="1.0.0",
        description="Test plugin",
        author="test",
        entry_point="plugin.py",
        plugin_dir=Path("/tmp/fake"),
    )


def _register_loaded(manager: PluginManager, name: str = "test_plugin") -> MagicMock:
    """Регистрирует плагин как discovered + loaded в PluginManager, минуя файловую систему."""
    info = _make_info(name)
    plugin = _make_plugin(name)
    manager._discovered[name] = info
    manager._loaded[name] = plugin
    manager._statuses[name] = _STATUS_LOADED
    return plugin


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------

class TestUnloadPlugin(unittest.TestCase):

    def setUp(self) -> None:
        self.manager = PluginManager()

    # ------------------------------------------------------------------
    # test_unload_removes_from_registry
    # ------------------------------------------------------------------
    def test_unload_removes_from_registry(self) -> None:
        """unload_plugin() должен убирать плагин из _loaded."""
        _register_loaded(self.manager, "alpha")

        result = self.manager.unload_plugin("alpha")

        self.assertTrue(result)
        self.assertNotIn("alpha", self.manager._loaded)

    # ------------------------------------------------------------------
    # test_unload_calls_on_unload_hook
    # ------------------------------------------------------------------
    def test_unload_calls_on_unload_hook(self) -> None:
        """unload_plugin() вызывает on_unload() хук ровно один раз."""
        plugin = _register_loaded(self.manager, "beta")
        on_unload_mock = MagicMock()
        plugin.on_unload = on_unload_mock

        self.manager.unload_plugin("beta")

        on_unload_mock.assert_called_once_with()

    # ------------------------------------------------------------------
    # test_unload_nonexistent_returns_false
    # ------------------------------------------------------------------
    def test_unload_nonexistent_returns_false(self) -> None:
        """unload_plugin() возвращает False для незагруженного/несуществующего плагина."""
        result = self.manager.unload_plugin("ghost")

        self.assertFalse(result)

    # ------------------------------------------------------------------
    # test_unload_sets_status_unloaded
    # ------------------------------------------------------------------
    def test_unload_sets_status_unloaded(self) -> None:
        """После выгрузки статус плагина = 'unloaded' (а не 'loaded')."""
        _register_loaded(self.manager, "gamma")

        self.manager.unload_plugin("gamma")

        self.assertEqual(self.manager._statuses.get("gamma"), _STATUS_UNLOADED)

    # ------------------------------------------------------------------
    # test_disable_keeps_in_registry  (existing behaviour — не сломали)
    # ------------------------------------------------------------------
    def test_disable_keeps_in_registry(self) -> None:
        """disable_plugin() НЕ удаляет плагин из _loaded — только скрывает."""
        _register_loaded(self.manager, "delta")

        self.manager.disable_plugin("delta")

        # Плагин по-прежнему в памяти.
        self.assertIn("delta", self.manager._loaded)
        self.assertEqual(self.manager._statuses.get("delta"), _STATUS_DISABLED)

    # ------------------------------------------------------------------
    # test_unload_then_load_works
    # ------------------------------------------------------------------
    def test_unload_then_load_works(self) -> None:
        """После unload плагин можно снова зарегистрировать как loaded."""
        _register_loaded(self.manager, "epsilon")
        self.manager.unload_plugin("epsilon")

        # Симулируем повторную загрузку (как это делает load_plugin).
        plugin2 = _make_plugin("epsilon")
        self.manager._loaded["epsilon"] = plugin2
        self.manager._statuses["epsilon"] = _STATUS_LOADED

        self.assertIn("epsilon", self.manager._loaded)
        self.assertIs(self.manager._loaded["epsilon"], plugin2)
        self.assertEqual(self.manager._statuses["epsilon"], _STATUS_LOADED)

    # ------------------------------------------------------------------
    # test_concurrent_unload_thread_safe
    # ------------------------------------------------------------------
    def test_concurrent_unload_thread_safe(self) -> None:
        """Параллельные вызовы unload_plugin() на разных плагинах не вызывают гонок."""
        names = [f"plugin_{i}" for i in range(10)]
        for n in names:
            _register_loaded(self.manager, n)

        errors: list[Exception] = []

        def do_unload(name: str) -> None:
            try:
                self.manager.unload_plugin(name)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=do_unload, args=(n,)) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f"Ошибки при конкурентном unload: {errors}")
        for n in names:
            self.assertNotIn(n, self.manager._loaded)

    # ------------------------------------------------------------------
    # test_on_unload_exception_caught_gracefully
    # ------------------------------------------------------------------
    def test_on_unload_exception_caught_gracefully(self) -> None:
        """Исключение в on_unload() не должно мешать выгрузке плагина."""
        plugin = _register_loaded(self.manager, "zeta")

        def bad_on_unload() -> None:
            raise RuntimeError("cleanup failed")

        plugin.on_unload = bad_on_unload

        # Не должно бросать исключение.
        result = self.manager.unload_plugin("zeta")

        self.assertTrue(result)
        self.assertNotIn("zeta", self.manager._loaded)
        self.assertEqual(self.manager._statuses.get("zeta"), _STATUS_UNLOADED)


# ---------------------------------------------------------------------------
# IPC handler тесты
# ---------------------------------------------------------------------------

class TestHandleUnloadPlugin(unittest.TestCase):

    def setUp(self) -> None:
        self.manager = PluginManager()

    def test_handle_unload_plugin_success(self) -> None:
        """handle_unload_plugin возвращает unloaded=True при успехе."""
        _register_loaded(self.manager, "eta")

        resp = self.manager.handle_unload_plugin({"name": "eta"})

        self.assertTrue(resp["unloaded"])
        self.assertEqual(resp["name"], "eta")
        self.assertNotIn("reason", resp)

    def test_handle_unload_plugin_not_loaded(self) -> None:
        """handle_unload_plugin возвращает unloaded=False + reason=not_loaded."""
        resp = self.manager.handle_unload_plugin({"name": "does_not_exist"})

        self.assertFalse(resp["unloaded"])
        self.assertEqual(resp["reason"], "not_loaded")

    def test_handle_unload_plugin_missing_name_raises(self) -> None:
        """handle_unload_plugin бросает ValueError при отсутствии name."""
        with self.assertRaises(ValueError):
            self.manager.handle_unload_plugin({})


if __name__ == "__main__":
    unittest.main()
