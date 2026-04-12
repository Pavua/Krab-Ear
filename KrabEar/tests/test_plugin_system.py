"""Unit-тесты для PluginManager (система плагинов Krab Ear).

Покрывает:
- discover_plugins: обнаружение плагинов, пустая директория, сломанный манифест
- load_plugin: успешная загрузка, повторная загрузка (кеш), несуществующий плагин
- list_plugins: статус, методы
- get_plugin_info: детали, отсутствующий плагин
- IPC-обработчики handle_list_plugins / handle_get_plugin_info
"""

from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.plugin_system import PluginInfo, PluginManager


# ---------------------------------------------------------------------------
# Вспомогательные функции для создания мок-плагинов в tmp-директории
# ---------------------------------------------------------------------------

def _make_plugin_dir(
    base_dir: Path,
    name: str = "test_plugin",
    version: str = "1.0.0",
    description: str = "Тестовый плагин",
    author: str = "Test Author",
    entry_point: str = "plugin.py",
    extra_manifest: dict | None = None,
    plugin_code: str | None = None,
) -> Path:
    """Создаёт директорию плагина с plugin.json и опциональным plugin.py."""
    plugin_dir = base_dir / name
    plugin_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "name": name,
        "version": version,
        "description": description,
        "author": author,
        "entry_point": entry_point,
    }
    if extra_manifest:
        manifest.update(extra_manifest)

    (plugin_dir / "plugin.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    if plugin_code is None:
        plugin_code = textwrap.dedent(f"""\
            class Plugin:
                name = "{name}"
                version = "{version}"

                def initialize(self, service):
                    pass

                def get_ipc_methods(self):
                    return {{"hello_{name}": lambda p: {{"ok": True}}}}
        """)

    (plugin_dir / entry_point).write_text(plugin_code, encoding="utf-8")
    return plugin_dir


# ---------------------------------------------------------------------------
# 1. discover_plugins
# ---------------------------------------------------------------------------

class TestDiscoverPlugins(unittest.TestCase):
    """Тесты обнаружения плагинов."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_discover_returns_empty_list_when_dir_missing(self) -> None:
        mgr = PluginManager()
        result = mgr.discover_plugins(self._base / "nonexistent")
        self.assertEqual(result, [])

    def test_discover_returns_empty_list_when_dir_is_empty(self) -> None:
        plugins_dir = self._base / "plugins"
        plugins_dir.mkdir()
        mgr = PluginManager()
        result = mgr.discover_plugins(plugins_dir)
        self.assertEqual(result, [])

    def test_discover_finds_valid_plugin(self) -> None:
        plugins_dir = self._base / "plugins"
        plugins_dir.mkdir()
        _make_plugin_dir(plugins_dir, name="alpha", version="0.1.0")
        mgr = PluginManager()
        result = mgr.discover_plugins(plugins_dir)
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], PluginInfo)
        self.assertEqual(result[0].name, "alpha")
        self.assertEqual(result[0].version, "0.1.0")

    def test_discover_finds_multiple_plugins(self) -> None:
        plugins_dir = self._base / "plugins"
        plugins_dir.mkdir()
        for name in ("alpha", "beta", "gamma"):
            _make_plugin_dir(plugins_dir, name=name)
        mgr = PluginManager()
        result = mgr.discover_plugins(plugins_dir)
        names = {p.name for p in result}
        self.assertEqual(names, {"alpha", "beta", "gamma"})

    def test_discover_ignores_dir_without_manifest(self) -> None:
        plugins_dir = self._base / "plugins"
        plugins_dir.mkdir()
        # Директория без plugin.json
        (plugins_dir / "orphan").mkdir()
        mgr = PluginManager()
        result = mgr.discover_plugins(plugins_dir)
        self.assertEqual(result, [])

    def test_discover_ignores_broken_manifest(self) -> None:
        plugins_dir = self._base / "plugins"
        plugins_dir.mkdir()
        broken_dir = plugins_dir / "broken"
        broken_dir.mkdir()
        (broken_dir / "plugin.json").write_text("NOT_JSON", encoding="utf-8")
        mgr = PluginManager()
        result = mgr.discover_plugins(plugins_dir)
        self.assertEqual(result, [])

    def test_discover_ignores_manifest_missing_required_fields(self) -> None:
        plugins_dir = self._base / "plugins"
        plugins_dir.mkdir()
        bad_dir = plugins_dir / "incomplete"
        bad_dir.mkdir()
        # Нет обязательных полей name и entry_point.
        (bad_dir / "plugin.json").write_text(
            json.dumps({"description": "Неполный манифест"}),
            encoding="utf-8",
        )
        mgr = PluginManager()
        result = mgr.discover_plugins(plugins_dir)
        self.assertEqual(result, [])

    def test_discover_uses_data_dir_when_no_argument(self) -> None:
        plugins_dir = self._base / "plugins"
        plugins_dir.mkdir()
        _make_plugin_dir(plugins_dir, name="auto_discovered")
        mgr = PluginManager(data_dir=self._base)
        result = mgr.discover_plugins()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "auto_discovered")


# ---------------------------------------------------------------------------
# 2. load_plugin
# ---------------------------------------------------------------------------

class TestLoadPlugin(unittest.TestCase):
    """Тесты загрузки плагина."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        self._plugins_dir = self._base / "plugins"
        self._plugins_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_load_plugin_success(self) -> None:
        _make_plugin_dir(self._plugins_dir, name="loader_test")
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        plugin = mgr.load_plugin("loader_test")
        self.assertEqual(plugin.name, "loader_test")
        self.assertEqual(plugin.version, "1.0.0")

    def test_load_plugin_raises_if_not_discovered(self) -> None:
        mgr = PluginManager()
        with self.assertRaises(KeyError):
            mgr.load_plugin("unknown_plugin")

    def test_load_plugin_cached_on_second_call(self) -> None:
        _make_plugin_dir(self._plugins_dir, name="cached_plugin")
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        plugin1 = mgr.load_plugin("cached_plugin")
        plugin2 = mgr.load_plugin("cached_plugin")
        self.assertIs(plugin1, plugin2)

    def test_load_plugin_ipc_methods_callable(self) -> None:
        _make_plugin_dir(self._plugins_dir, name="method_plugin")
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        plugin = mgr.load_plugin("method_plugin")
        methods = plugin.get_ipc_methods()
        self.assertIsInstance(methods, dict)
        self.assertTrue(len(methods) > 0)

    def test_load_plugin_via_create_plugin_factory(self) -> None:
        code = textwrap.dedent("""\
            class _Impl:
                name = "factory_plugin"
                version = "2.0.0"
                def initialize(self, service): pass
                def get_ipc_methods(self): return {}

            def create_plugin():
                return _Impl()
        """)
        _make_plugin_dir(self._plugins_dir, name="factory_plugin", plugin_code=code)
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        plugin = mgr.load_plugin("factory_plugin")
        self.assertEqual(plugin.name, "factory_plugin")
        self.assertEqual(plugin.version, "2.0.0")

    def test_load_plugin_error_sets_status(self) -> None:
        # Плагин с синтаксической ошибкой в коде.
        bad_code = "this is not valid python @@@"
        _make_plugin_dir(self._plugins_dir, name="bad_plugin", plugin_code=bad_code)
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        with self.assertRaises(Exception):
            mgr.load_plugin("bad_plugin")
        info = mgr.list_plugins()
        bad_entry = next(p for p in info if p["name"] == "bad_plugin")
        self.assertEqual(bad_entry["status"], "error")
        self.assertIn("error", bad_entry)

    def test_load_plugin_missing_entry_file_raises(self) -> None:
        # Манифест указывает на несуществующий файл.
        plugin_dir = self._plugins_dir / "ghost_plugin"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.json").write_text(
            json.dumps({"name": "ghost_plugin", "version": "1.0", "entry_point": "missing.py"}),
            encoding="utf-8",
        )
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        with self.assertRaises(FileNotFoundError):
            mgr.load_plugin("ghost_plugin")


# ---------------------------------------------------------------------------
# 3. list_plugins
# ---------------------------------------------------------------------------

class TestListPlugins(unittest.TestCase):
    """Тесты перечисления плагинов."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        self._plugins_dir = self._base / "plugins"
        self._plugins_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_list_plugins_empty(self) -> None:
        mgr = PluginManager()
        result = mgr.list_plugins()
        self.assertEqual(result, [])

    def test_list_plugins_after_discover(self) -> None:
        _make_plugin_dir(self._plugins_dir, name="plugin_a")
        _make_plugin_dir(self._plugins_dir, name="plugin_b")
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        result = mgr.list_plugins()
        names = {p["name"] for p in result}
        self.assertIn("plugin_a", names)
        self.assertIn("plugin_b", names)

    def test_list_plugins_status_discovered_before_load(self) -> None:
        _make_plugin_dir(self._plugins_dir, name="status_test")
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        result = mgr.list_plugins()
        self.assertEqual(result[0]["status"], "discovered")

    def test_list_plugins_status_loaded_after_load(self) -> None:
        _make_plugin_dir(self._plugins_dir, name="loadme")
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        mgr.load_plugin("loadme")
        result = mgr.list_plugins()
        self.assertEqual(result[0]["status"], "loaded")
        self.assertIn("hello_loadme", result[0]["methods"])

    def test_list_plugins_contains_expected_fields(self) -> None:
        _make_plugin_dir(
            self._plugins_dir,
            name="full_info",
            version="3.2.1",
            description="Описание",
            author="Автор",
        )
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        entry = mgr.list_plugins()[0]
        for field in ("name", "version", "description", "author", "status", "methods"):
            self.assertIn(field, entry, f"Отсутствует поле '{field}'")


# ---------------------------------------------------------------------------
# 4. get_plugin_info
# ---------------------------------------------------------------------------

class TestGetPluginInfo(unittest.TestCase):
    """Тесты получения деталей плагина."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        self._plugins_dir = self._base / "plugins"
        self._plugins_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_get_plugin_info_raises_for_unknown(self) -> None:
        mgr = PluginManager()
        with self.assertRaises(KeyError):
            mgr.get_plugin_info("nonexistent")

    def test_get_plugin_info_returns_correct_data(self) -> None:
        _make_plugin_dir(
            self._plugins_dir,
            name="detail_plugin",
            version="1.2.3",
            description="Детальный плагин",
            author="Автор Плагина",
        )
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        info = mgr.get_plugin_info("detail_plugin")
        self.assertEqual(info["name"], "detail_plugin")
        self.assertEqual(info["version"], "1.2.3")
        self.assertEqual(info["description"], "Детальный плагин")
        self.assertEqual(info["author"], "Автор Плагина")
        self.assertIn("plugin_dir", info)
        self.assertIn("status", info)

    def test_get_plugin_info_includes_methods_after_load(self) -> None:
        _make_plugin_dir(self._plugins_dir, name="methods_plugin")
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        mgr.load_plugin("methods_plugin")
        info = mgr.get_plugin_info("methods_plugin")
        self.assertIn("methods", info)
        self.assertIn("hello_methods_plugin", info["methods"])


# ---------------------------------------------------------------------------
# 5. IPC-обработчики
# ---------------------------------------------------------------------------

class TestIPCHandlers(unittest.TestCase):
    """Тесты IPC-обработчиков handle_list_plugins и handle_get_plugin_info."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        plugins_dir = self._base / "plugins"
        plugins_dir.mkdir()
        _make_plugin_dir(plugins_dir, name="ipc_plugin", version="0.9")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_handle_list_plugins_returns_list(self) -> None:
        mgr = PluginManager(data_dir=self._base)
        result = mgr.handle_list_plugins({})
        self.assertIn("plugins", result)
        self.assertIsInstance(result["plugins"], list)
        self.assertEqual(result["plugins"][0]["name"], "ipc_plugin")

    def test_handle_list_plugins_lazy_discover(self) -> None:
        # Менеджер ещё не вызывал discover_plugins — должен сделать это сам.
        mgr = PluginManager(data_dir=self._base)
        result = mgr.handle_list_plugins({})
        self.assertEqual(len(result["plugins"]), 1)

    def test_handle_get_plugin_info_returns_details(self) -> None:
        mgr = PluginManager(data_dir=self._base)
        mgr.discover_plugins()
        result = mgr.handle_get_plugin_info({"name": "ipc_plugin"})
        self.assertEqual(result["name"], "ipc_plugin")
        self.assertEqual(result["version"], "0.9")

    def test_handle_get_plugin_info_raises_on_missing_name(self) -> None:
        mgr = PluginManager(data_dir=self._base)
        mgr.discover_plugins()
        with self.assertRaises(ValueError):
            mgr.handle_get_plugin_info({})

    def test_handle_get_plugin_info_raises_on_unknown_plugin(self) -> None:
        mgr = PluginManager(data_dir=self._base)
        mgr.discover_plugins()
        with self.assertRaises(KeyError):
            mgr.handle_get_plugin_info({"name": "ghost"})


# ---------------------------------------------------------------------------
# 6. PluginInfo dataclass
# ---------------------------------------------------------------------------

class TestPluginInfoDataclass(unittest.TestCase):
    """Проверяет поля PluginInfo."""

    def test_plugin_info_fields(self) -> None:
        info = PluginInfo(
            name="sample",
            version="1.0",
            description="Пример",
            author="Кто-то",
            entry_point="main.py",
        )
        self.assertEqual(info.name, "sample")
        self.assertEqual(info.version, "1.0")
        self.assertEqual(info.description, "Пример")
        self.assertEqual(info.author, "Кто-то")
        self.assertEqual(info.entry_point, "main.py")
        # plugin_dir по умолчанию — Path().
        self.assertIsInstance(info.plugin_dir, Path)


if __name__ == "__main__":
    unittest.main()
