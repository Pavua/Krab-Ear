"""Unit-тесты для PluginManager (система плагинов Krab Ear).

Покрывает:
- discover_plugins: обнаружение плагинов, пустая директория, сломанный манифест
- load_plugin: успешная загрузка, повторная загрузка (кеш), несуществующий плагин
- list_plugins: статус, методы
- get_plugin_info: детали, отсутствующий плагин
- IPC-обработчики handle_list_plugins / handle_get_plugin_info
"""

from __future__ import annotations
from backend.plugin_system import PluginInfo, PluginManager

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


from backend.plugin_system import HOOK_ON_TRANSCRIBE, HOOK_ON_PASTE


# ---------------------------------------------------------------------------
# 7. enable_plugin / disable_plugin
# ---------------------------------------------------------------------------

class TestEnableDisablePlugin(unittest.TestCase):
    """Тесты включения и отключения плагинов."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        self._plugins_dir = self._base / "plugins"
        self._plugins_dir.mkdir()
        _make_plugin_dir(self._plugins_dir, name="toggle_plugin")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _loaded_mgr(self) -> "PluginManager":
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        mgr.load_plugin("toggle_plugin")
        return mgr

    def test_disable_plugin_returns_true(self) -> None:
        mgr = self._loaded_mgr()
        result = mgr.disable_plugin("toggle_plugin")
        self.assertTrue(result)

    def test_disable_plugin_sets_status_disabled(self) -> None:
        mgr = self._loaded_mgr()
        mgr.disable_plugin("toggle_plugin")
        plugins = mgr.list_plugins()
        entry = next(p for p in plugins if p["name"] == "toggle_plugin")
        self.assertEqual(entry["status"], "disabled")
        self.assertFalse(entry["enabled"])

    def test_disable_already_disabled_returns_false(self) -> None:
        mgr = self._loaded_mgr()
        mgr.disable_plugin("toggle_plugin")
        result = mgr.disable_plugin("toggle_plugin")
        self.assertFalse(result)

    def test_enable_plugin_restores_loaded_status(self) -> None:
        mgr = self._loaded_mgr()
        mgr.disable_plugin("toggle_plugin")
        result = mgr.enable_plugin("toggle_plugin")
        self.assertTrue(result)
        plugins = mgr.list_plugins()
        entry = next(p for p in plugins if p["name"] == "toggle_plugin")
        self.assertEqual(entry["status"], "loaded")
        self.assertTrue(entry["enabled"])

    def test_enable_not_disabled_returns_false(self) -> None:
        mgr = self._loaded_mgr()
        result = mgr.enable_plugin("toggle_plugin")
        self.assertFalse(result)

    def test_disable_unknown_plugin_raises_key_error(self) -> None:
        mgr = PluginManager()
        with self.assertRaises(KeyError):
            mgr.disable_plugin("nonexistent")

    def test_enable_unknown_plugin_raises_key_error(self) -> None:
        mgr = PluginManager()
        with self.assertRaises(KeyError):
            mgr.enable_plugin("nonexistent")

    def test_enable_discovered_not_loaded(self) -> None:
        """enable_plugin на discovered (не loaded) восстанавливает статус discovered."""
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        mgr.disable_plugin("toggle_plugin")
        mgr.enable_plugin("toggle_plugin")
        plugins = mgr.list_plugins()
        entry = next(p for p in plugins if p["name"] == "toggle_plugin")
        self.assertEqual(entry["status"], "discovered")


# ---------------------------------------------------------------------------
# 8. call_hook (on_transcribe / on_paste)
# ---------------------------------------------------------------------------

def _make_hook_plugin(
    plugins_dir: Path,
    name: str,
    hooks: list[str],
) -> Path:
    """Создаёт плагин с реализованными хуками."""
    hook_methods = "\n".join(
        f"    def {hook}(self, payload):\n        return {{'hook': '{hook}', 'name': '{name}', 'payload': payload}}"
        for hook in hooks
    )
    code = (
        f'class Plugin:\n'
        f'    name = "{name}"\n'
        f'    version = "1.0"\n'
        f'    def initialize(self, service): pass\n'
        f'    def get_ipc_methods(self): return {{}}\n'
        f'{hook_methods}\n'
    )
    return _make_plugin_dir(plugins_dir, name=name, plugin_code=code)


class TestCallHook(unittest.TestCase):
    """Тесты вызова хуков плагинов (on_transcribe, on_paste)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        self._plugins_dir = self._base / "plugins"
        self._plugins_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_on_transcribe_hook_called(self) -> None:
        _make_hook_plugin(self._plugins_dir, "hook_a", [HOOK_ON_TRANSCRIBE])
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        mgr.load_plugin("hook_a")
        results = mgr.call_hook(HOOK_ON_TRANSCRIBE, {"text": "hello"})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["hook"], HOOK_ON_TRANSCRIBE)
        self.assertEqual(results[0]["payload"], {"text": "hello"})

    def test_on_paste_hook_called(self) -> None:
        _make_hook_plugin(self._plugins_dir, "hook_b", [HOOK_ON_PASTE])
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        mgr.load_plugin("hook_b")
        results = mgr.call_hook(HOOK_ON_PASTE, {"text": "pasted"})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["hook"], HOOK_ON_PASTE)

    def test_hook_not_called_for_plugin_without_hook(self) -> None:
        """Плагин без метода on_transcribe — хук не вызывается."""
        _make_plugin_dir(self._plugins_dir, name="no_hook")
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        mgr.load_plugin("no_hook")
        results = mgr.call_hook(HOOK_ON_TRANSCRIBE, {"text": "x"})
        self.assertEqual(results, [])

    def test_hook_not_called_for_unloaded_plugin(self) -> None:
        """Хук не вызывается для обнаруженного, но не загруженного плагина."""
        _make_hook_plugin(self._plugins_dir, "unloaded_hook", [HOOK_ON_TRANSCRIBE])
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        # Намеренно не вызываем load_plugin
        results = mgr.call_hook(HOOK_ON_TRANSCRIBE, {})
        self.assertEqual(results, [])

    def test_hook_not_called_for_disabled_plugin(self) -> None:
        """Хук не вызывается у отключённого плагина."""
        _make_hook_plugin(self._plugins_dir, "disabled_hook", [HOOK_ON_TRANSCRIBE])
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        mgr.load_plugin("disabled_hook")
        mgr.disable_plugin("disabled_hook")
        results = mgr.call_hook(HOOK_ON_TRANSCRIBE, {"text": "x"})
        self.assertEqual(results, [])

    def test_hook_called_after_reenable(self) -> None:
        """После enable_plugin хук снова вызывается."""
        _make_hook_plugin(self._plugins_dir, "reenable_hook", [HOOK_ON_TRANSCRIBE])
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        mgr.load_plugin("reenable_hook")
        mgr.disable_plugin("reenable_hook")
        mgr.enable_plugin("reenable_hook")
        results = mgr.call_hook(HOOK_ON_TRANSCRIBE, {"text": "y"})
        self.assertEqual(len(results), 1)

    def test_hook_error_in_one_plugin_does_not_break_others(self) -> None:
        """Ошибка в хуке одного плагина не прерывает вызов остальных."""
        # Плагин с падающим on_transcribe
        bad_code = textwrap.dedent("""\
            class Plugin:
                name = "bad_hook_plugin"
                version = "1.0"
                def initialize(self, service): pass
                def get_ipc_methods(self): return {}
                def on_transcribe(self, payload):
                    raise RuntimeError("hook error!")
        """)
        _make_plugin_dir(self._plugins_dir, name="bad_hook_plugin", plugin_code=bad_code)
        _make_hook_plugin(self._plugins_dir, "good_hook_plugin", [HOOK_ON_TRANSCRIBE])

        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        mgr.load_plugin("bad_hook_plugin")
        mgr.load_plugin("good_hook_plugin")

        # Должен вернуть результат только от good_hook_plugin (bad — пропущен из-за ошибки)
        results = mgr.call_hook(HOOK_ON_TRANSCRIBE, {"text": "test"})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "good_hook_plugin")

    def test_multiple_plugins_all_receive_hook(self) -> None:
        """Несколько плагинов с on_paste — все получают вызов."""
        for name in ("paste_a", "paste_b", "paste_c"):
            _make_hook_plugin(self._plugins_dir, name, [HOOK_ON_PASTE])
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        for name in ("paste_a", "paste_b", "paste_c"):
            mgr.load_plugin(name)
        results = mgr.call_hook(HOOK_ON_PASTE, {"text": "multi"})
        self.assertEqual(len(results), 3)
        names = {r["name"] for r in results}
        self.assertEqual(names, {"paste_a", "paste_b", "paste_c"})


if __name__ == "__main__":
    unittest.main()
