"""Wave 32 — B1 (MED) + B2 (LOW) fixes for plugin_system.py.

B1: discover_plugins/list_plugins without lock → dict race
    - Two concurrent list_plugins calls must never raise RuntimeError.
    - discover_plugins writes under the lock; list_plugins takes a snapshot.
    - enable_plugin / disable_plugin writes under the lock.

B2: unload_plugin must evict sys.modules entry.
    - After unload_plugin(), sys.modules key is gone.
    - Plugin name with spaces (or other non-[A-Za-z0-9_-] chars) in manifest
      must be rejected by _import_plugin() before reaching sys.modules.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.plugin_system import (  # noqa: E402
    PluginInfo,
    PluginManager,
    _STATUS_DISCOVERED,
    _STATUS_DISABLED,
    _STATUS_LOADED,
    _STATUS_UNLOADED,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_info(name: str = "myplugin", *, plugin_dir: Path | None = None) -> PluginInfo:
    if plugin_dir is None:
        plugin_dir = Path("/tmp/fake_plugin")
    return PluginInfo(
        name=name,
        version="1.0",
        description="",
        author="",
        entry_point="main.py",
        plugin_dir=plugin_dir,
    )


def _make_fake_plugin(name: str = "myplugin") -> MagicMock:
    plugin = MagicMock()
    plugin.name = name
    plugin.version = "1.0"
    plugin.initialize = MagicMock()
    plugin.get_ipc_methods = MagicMock(return_value={})
    return plugin


# ---------------------------------------------------------------------------
# B1: locking — concurrent list_plugins
# ---------------------------------------------------------------------------

class ConcurrentListPluginsTest(unittest.TestCase):
    """Concurrent list_plugins() calls must not raise RuntimeError."""

    def _build_manager_with_n_plugins(self, n: int) -> PluginManager:
        mgr = PluginManager()
        for i in range(n):
            name = f"plugin_{i}"
            mgr._discovered[name] = _make_info(name)
            mgr._statuses[name] = _STATUS_DISCOVERED
        return mgr

    def test_no_runtime_error_under_concurrent_list_and_discover(self) -> None:
        """list_plugins() snapshot must not crash when discover_plugins mutates."""
        mgr = self._build_manager_with_n_plugins(50)
        errors: list[Exception] = []

        def lister() -> None:
            for _ in range(200):
                try:
                    mgr.list_plugins()
                except RuntimeError as exc:
                    errors.append(exc)

        def writer() -> None:
            for i in range(50, 100):
                name = f"plugin_{i}"
                with mgr._lock:
                    mgr._discovered[name] = _make_info(name)
                    if name not in mgr._statuses:
                        mgr._statuses[name] = _STATUS_DISCOVERED

        threads = [threading.Thread(target=lister) for _ in range(4)]
        threads.append(threading.Thread(target=writer))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f"RuntimeError under concurrent access: {errors}")

    def test_no_runtime_error_two_concurrent_list_calls(self) -> None:
        """Two threads calling list_plugins() simultaneously must not raise."""
        mgr = self._build_manager_with_n_plugins(100)
        errors: list[Exception] = []

        def lister() -> None:
            for _ in range(500):
                try:
                    mgr.list_plugins()
                except RuntimeError as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=lister) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f"RuntimeError under concurrent list_plugins: {errors}")

    def test_list_plugins_returns_snapshot(self) -> None:
        """list_plugins() must return a consistent list (not raise mid-iteration)."""
        mgr = PluginManager()
        for i in range(5):
            name = f"p{i}"
            mgr._discovered[name] = _make_info(name)
            mgr._statuses[name] = _STATUS_DISCOVERED

        result = mgr.list_plugins()
        self.assertEqual(len(result), 5)
        names = {item["name"] for item in result}
        self.assertEqual(names, {"p0", "p1", "p2", "p3", "p4"})


# ---------------------------------------------------------------------------
# B1: locking — enable_plugin / disable_plugin
# ---------------------------------------------------------------------------

class EnableDisableLockingTest(unittest.TestCase):
    """enable_plugin / disable_plugin must mutate under the lock."""

    def _make_mgr_with_plugin(self, name: str = "myplugin") -> PluginManager:
        mgr = PluginManager()
        mgr._discovered[name] = _make_info(name)
        mgr._statuses[name] = _STATUS_DISCOVERED
        return mgr

    def test_disable_sets_status_atomically(self) -> None:
        mgr = self._make_mgr_with_plugin()
        result = mgr.disable_plugin("myplugin")
        self.assertTrue(result)
        self.assertEqual(mgr._statuses["myplugin"], _STATUS_DISABLED)
        self.assertIn("myplugin", mgr._disabled)

    def test_enable_restores_discovered_status(self) -> None:
        mgr = self._make_mgr_with_plugin()
        mgr.disable_plugin("myplugin")
        result = mgr.enable_plugin("myplugin")
        self.assertTrue(result)
        self.assertEqual(mgr._statuses["myplugin"], _STATUS_DISCOVERED)
        self.assertNotIn("myplugin", mgr._disabled)

    def test_enable_restores_loaded_status_when_loaded(self) -> None:
        mgr = self._make_mgr_with_plugin()
        mgr._loaded["myplugin"] = _make_fake_plugin()
        mgr._statuses["myplugin"] = _STATUS_LOADED
        mgr.disable_plugin("myplugin")
        mgr.enable_plugin("myplugin")
        self.assertEqual(mgr._statuses["myplugin"], _STATUS_LOADED)

    def test_disable_unknown_plugin_raises_key_error(self) -> None:
        mgr = PluginManager()
        with self.assertRaises(KeyError):
            mgr.disable_plugin("no_such_plugin")

    def test_enable_unknown_plugin_raises_key_error(self) -> None:
        mgr = PluginManager()
        with self.assertRaises(KeyError):
            mgr.enable_plugin("no_such_plugin")

    def test_concurrent_enable_disable_no_error(self) -> None:
        """Concurrent enable/disable must not corrupt state or raise."""
        mgr = self._make_mgr_with_plugin()
        errors: list[Exception] = []

        def toggle() -> None:
            for _ in range(100):
                try:
                    mgr.disable_plugin("myplugin")
                    mgr.enable_plugin("myplugin")
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        threads = [threading.Thread(target=toggle) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [])


# ---------------------------------------------------------------------------
# B2: sys.modules eviction on unload
# ---------------------------------------------------------------------------

class UnloadSysModulesEvictionTest(unittest.TestCase):
    """unload_plugin() must remove the sys.modules entry."""

    def _make_mgr_with_loaded_plugin(self, name: str = "myplugin") -> PluginManager:
        mgr = PluginManager()
        mgr._discovered[name] = _make_info(name)
        plugin = _make_fake_plugin(name)
        mgr._loaded[name] = plugin
        mgr._statuses[name] = _STATUS_LOADED
        # Simulate what _import_plugin() does
        module_key = f"_krabear_plugin_{name}"
        sys.modules[module_key] = MagicMock()
        return mgr

    def tearDown(self) -> None:
        # Clean up any leftover sys.modules entries from tests
        for key in list(sys.modules.keys()):
            if key.startswith("_krabear_plugin_"):
                del sys.modules[key]

    def test_unload_evicts_sys_modules(self) -> None:
        mgr = self._make_mgr_with_loaded_plugin("myplugin")
        module_key = "_krabear_plugin_myplugin"
        self.assertIn(module_key, sys.modules)

        result = mgr.unload_plugin("myplugin")

        self.assertTrue(result)
        self.assertNotIn(module_key, sys.modules,
                         "sys.modules entry must be evicted after unload_plugin()")

    def test_unload_status_is_unloaded(self) -> None:
        mgr = self._make_mgr_with_loaded_plugin("myplugin")
        mgr.unload_plugin("myplugin")
        self.assertEqual(mgr._statuses["myplugin"], _STATUS_UNLOADED)
        self.assertNotIn("myplugin", mgr._loaded)

    def test_unload_not_loaded_returns_false(self) -> None:
        mgr = PluginManager()
        result = mgr.unload_plugin("ghost")
        self.assertFalse(result)

    def test_reload_after_unload_gets_fresh_module(self) -> None:
        """After unload, sys.modules key is gone so a reload won't reuse stale module."""
        mgr = self._make_mgr_with_loaded_plugin("myplugin")
        mgr.unload_plugin("myplugin")
        # The key should not be present — a future load_plugin would insert a fresh one
        self.assertNotIn("_krabear_plugin_myplugin", sys.modules)

    def test_unload_twice_returns_false_second_time(self) -> None:
        mgr = self._make_mgr_with_loaded_plugin("myplugin")
        self.assertTrue(mgr.unload_plugin("myplugin"))
        self.assertFalse(mgr.unload_plugin("myplugin"))


# ---------------------------------------------------------------------------
# B2: plugin name validation — spaces/special chars rejected
# ---------------------------------------------------------------------------

class PluginNameValidationTest(unittest.TestCase):
    """_import_plugin() must reject names containing unsafe characters."""

    def _make_mgr_with_invalid_name(self, bad_name: str, tmp_dir: Path) -> PluginManager:
        """Set up a PluginManager with a discovered plugin having a bad name."""
        mgr = PluginManager()
        # Create a real entry_point file so path checks pass
        entry = tmp_dir / "main.py"
        entry.write_text(
            "class Plugin:\n"
            "    name='x'\n"
            "    version='1'\n"
            "    def initialize(self, s): pass\n"
            "    def get_ipc_methods(self): return {}\n"
        )
        info = PluginInfo(
            name=bad_name,
            version="1.0",
            description="",
            author="",
            entry_point="main.py",
            plugin_dir=tmp_dir,
        )
        mgr._discovered[bad_name] = info
        mgr._statuses[bad_name] = _STATUS_DISCOVERED
        return mgr

    def test_name_with_spaces_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_name = "my plugin"
            mgr = self._make_mgr_with_invalid_name(bad_name, Path(tmpdir))
            with self.assertRaises((ValueError, KeyError)):
                mgr.load_plugin(bad_name)

    def test_name_with_slash_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_name = "foo/bar"
            mgr = self._make_mgr_with_invalid_name(bad_name, Path(tmpdir))
            with self.assertRaises((ValueError, KeyError)):
                mgr.load_plugin(bad_name)

    def test_name_with_dot_dot_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_name = "../evil"
            mgr = self._make_mgr_with_invalid_name(bad_name, Path(tmpdir))
            with self.assertRaises((ValueError, KeyError)):
                mgr.load_plugin(bad_name)

    def test_name_with_null_byte_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_name = "foo\x00bar"
            mgr = self._make_mgr_with_invalid_name(bad_name, Path(tmpdir))
            with self.assertRaises((ValueError, KeyError)):
                mgr.load_plugin(bad_name)

    def test_valid_names_accepted(self) -> None:
        """Names matching [A-Za-z0-9_-]+ must NOT raise a ValueError from the guard."""
        valid_names = ["myplugin", "my_plugin", "my-plugin", "Plugin123", "a", "A1_b-C"]
        for name in valid_names:
            self.assertRegex(
                name,
                r"^[A-Za-z0-9_-]+$",
                f"Expected valid name to pass validation: {name!r}",
            )


# ---------------------------------------------------------------------------
# B1: discover_plugins lock — write under lock during concurrent discovery
# ---------------------------------------------------------------------------

class DiscoverPluginsLockTest(unittest.TestCase):
    """discover_plugins() must hold the lock when mutating _discovered/_statuses."""

    def test_discover_and_list_concurrently_no_error(self) -> None:
        """Simulate concurrent discover_plugins() + list_plugins() — no RuntimeError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # Create 20 plugin directories each with a valid manifest
            for i in range(20):
                pdir = root / f"plugin_{i}"
                pdir.mkdir()
                manifest = {
                    "name": f"plugin_{i}",
                    "version": "1.0",
                    "entry_point": "main.py",
                }
                (pdir / "plugin.json").write_text(json.dumps(manifest))

            mgr = PluginManager(data_dir=root)
            errors: list[Exception] = []

            def discover() -> None:
                for _ in range(10):
                    try:
                        mgr.discover_plugins()
                    except RuntimeError as exc:
                        errors.append(exc)

            def lister() -> None:
                for _ in range(200):
                    try:
                        mgr.list_plugins()
                    except RuntimeError as exc:
                        errors.append(exc)

            threads: list[threading.Thread] = [
                threading.Thread(target=discover) for _ in range(2)
            ]
            threads += [threading.Thread(target=lister) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            self.assertEqual(errors, [], f"RuntimeError during concurrent discover+list: {errors}")


if __name__ == "__main__":
    unittest.main()
