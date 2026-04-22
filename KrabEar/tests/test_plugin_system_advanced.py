"""Расширенные тесты PluginSystem — load_all_plugins, invalid manifests, dependencies, hooks."""

from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.plugin_system import (
    HOOK_ON_PASTE,
    HOOK_ON_TRANSCRIBE,
    PluginInfo,
    PluginManager,
)


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------

def _make_plugin(
    base_dir: Path,
    name: str = "test_plugin",
    version: str = "1.0.0",
    description: str = "Test plugin",
    author: str = "Test",
    entry_point: str = "plugin.py",
    extra_manifest: dict | None = None,
    plugin_code: str | None = None,
) -> Path:
    plugin_dir = base_dir / name
    plugin_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict = {
        "name": name,
        "version": version,
        "description": description,
        "author": author,
        "entry_point": entry_point,
    }
    if extra_manifest:
        manifest.update(extra_manifest)

    (plugin_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")

    if plugin_code is None:
        plugin_code = textwrap.dedent(f"""\
            class Plugin:
                name = "{name}"
                version = "{version}"
                def initialize(self, service): pass
                def get_ipc_methods(self): return {{"ping_{name}": lambda p: {{"pong": True}}}}
        """)

    (plugin_dir / entry_point).write_text(plugin_code, encoding="utf-8")
    return plugin_dir


def _make_hook_plugin(base_dir: Path, name: str, hooks: list[str]) -> Path:
    hook_methods = "\n".join(
        f"    def {h}(self, payload):\n        return {{'hook': '{h}', 'name': '{name}', 'payload': payload}}"
        for h in hooks
    )
    code = (
        f"class Plugin:\n"
        f"    name = \"{name}\"\n"
        f"    version = \"1.0\"\n"
        f"    def initialize(self, service): pass\n"
        f"    def get_ipc_methods(self): return {{}}\n"
        f"{hook_methods}\n"
    )
    return _make_plugin(base_dir, name=name, plugin_code=code)


# ---------------------------------------------------------------------------
# C1. load_all_plugins — discover + load all at once
# ---------------------------------------------------------------------------

class TestLoadAllPlugins(unittest.TestCase):
    """discover_plugins + load всех плагинов за один проход."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        self._plugins_dir = self._base / "plugins"
        self._plugins_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _discover_and_load_all(self, mgr: PluginManager) -> int:
        """Обнаруживает и загружает все плагины, возвращает количество загруженных."""
        infos = mgr.discover_plugins(self._plugins_dir)
        loaded = 0
        for info in infos:
            try:
                mgr.load_plugin(info.name)
                loaded += 1
            except Exception:
                pass
        return loaded

    def test_load_all_single_plugin(self):
        _make_plugin(self._plugins_dir, name="solo")
        mgr = PluginManager()
        loaded = self._discover_and_load_all(mgr)
        self.assertEqual(loaded, 1)
        plugins = mgr.list_plugins()
        self.assertEqual(plugins[0]["status"], "loaded")

    def test_load_all_multiple_plugins(self):
        for name in ("alpha", "beta", "gamma", "delta"):
            _make_plugin(self._plugins_dir, name=name)
        mgr = PluginManager()
        loaded = self._discover_and_load_all(mgr)
        self.assertEqual(loaded, 4)

    def test_load_all_skips_broken_plugins(self):
        """Сломанный плагин пропускается, остальные загружаются."""
        _make_plugin(self._plugins_dir, name="good_a")
        _make_plugin(self._plugins_dir, name="bad_one", plugin_code="syntax error @@@ not python")
        _make_plugin(self._plugins_dir, name="good_b")

        mgr = PluginManager()
        loaded = self._discover_and_load_all(mgr)
        self.assertEqual(loaded, 2)

        plugins = {p["name"]: p for p in mgr.list_plugins()}
        self.assertEqual(plugins["good_a"]["status"], "loaded")
        self.assertEqual(plugins["good_b"]["status"], "loaded")
        self.assertEqual(plugins["bad_one"]["status"], "error")

    def test_load_all_returns_ipc_methods(self):
        _make_plugin(self._plugins_dir, name="method_rich")
        mgr = PluginManager()
        self._discover_and_load_all(mgr)
        plugins = mgr.list_plugins()
        entry = next(p for p in plugins if p["name"] == "method_rich")
        self.assertIn("ping_method_rich", entry["methods"])

    def test_discover_empty_dir_load_all_noop(self):
        mgr = PluginManager()
        loaded = self._discover_and_load_all(mgr)
        self.assertEqual(loaded, 0)
        self.assertEqual(mgr.list_plugins(), [])


# ---------------------------------------------------------------------------
# C2. Invalid manifest.json → skip + log (разные сценарии)
# ---------------------------------------------------------------------------

class TestInvalidManifestHandling(unittest.TestCase):
    """Различные виды невалидного манифеста — пропуск без краша."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        self._plugins_dir = self._base / "plugins"
        self._plugins_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _make_bad_manifest(self, subdir: str, content: str) -> Path:
        d = self._plugins_dir / subdir
        d.mkdir(parents=True)
        (d / "plugin.json").write_text(content, encoding="utf-8")
        return d

    def test_empty_json_object_skipped(self):
        self._make_bad_manifest("empty_obj", "{}")
        mgr = PluginManager()
        result = mgr.discover_plugins(self._plugins_dir)
        self.assertEqual(result, [])

    def test_json_array_skipped(self):
        self._make_bad_manifest("array_json", "[1, 2, 3]")
        mgr = PluginManager()
        result = mgr.discover_plugins(self._plugins_dir)
        self.assertEqual(result, [])

    def test_null_json_skipped(self):
        self._make_bad_manifest("null_json", "null")
        mgr = PluginManager()
        result = mgr.discover_plugins(self._plugins_dir)
        self.assertEqual(result, [])

    def test_missing_name_field_skipped(self):
        self._make_bad_manifest(
            "no_name",
            json.dumps({"version": "1.0", "entry_point": "plugin.py"}),
        )
        mgr = PluginManager()
        result = mgr.discover_plugins(self._plugins_dir)
        self.assertEqual(result, [])

    def test_missing_entry_point_skipped(self):
        self._make_bad_manifest(
            "no_ep",
            json.dumps({"name": "noep", "version": "1.0"}),
        )
        mgr = PluginManager()
        result = mgr.discover_plugins(self._plugins_dir)
        self.assertEqual(result, [])

    def test_missing_version_skipped(self):
        self._make_bad_manifest(
            "no_ver",
            json.dumps({"name": "nover", "entry_point": "plugin.py"}),
        )
        mgr = PluginManager()
        result = mgr.discover_plugins(self._plugins_dir)
        self.assertEqual(result, [])

    def test_empty_name_string_skipped(self):
        self._make_bad_manifest(
            "empty_name",
            json.dumps({"name": "", "version": "1.0", "entry_point": "plugin.py"}),
        )
        mgr = PluginManager()
        result = mgr.discover_plugins(self._plugins_dir)
        self.assertEqual(result, [])

    def test_invalid_json_skipped(self):
        self._make_bad_manifest("bad_json", "{not valid json")
        mgr = PluginManager()
        result = mgr.discover_plugins(self._plugins_dir)
        self.assertEqual(result, [])

    def test_extra_unknown_fields_in_manifest_accepted(self):
        """Лишние поля в манифесте НЕ должны блокировать плагин."""
        d = self._plugins_dir / "extra_fields"
        d.mkdir()
        (d / "plugin.json").write_text(
            json.dumps({
                "name": "extra_fields",
                "version": "1.0",
                "entry_point": "plugin.py",
                "unknown_field": "value",
                "another_field": 42,
            }),
            encoding="utf-8",
        )
        _make_plugin(d.parent, name="extra_fields")  # перезапишет plugin.py
        mgr = PluginManager()
        result = mgr.discover_plugins(self._plugins_dir)
        found = [p for p in result if p.name == "extra_fields"]
        self.assertEqual(len(found), 1)

    def test_valid_plugin_alongside_broken_manifests(self):
        """Сломанные манифесты не мешают обнаружению валидных плагинов."""
        self._make_bad_manifest("bad1", "NOT JSON")
        self._make_bad_manifest("bad2", "{}")
        _make_plugin(self._plugins_dir, name="valid_one")

        mgr = PluginManager()
        result = mgr.discover_plugins(self._plugins_dir)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "valid_one")


# ---------------------------------------------------------------------------
# C3. Plugin dependencies (через manifest "dependencies" поле)
# ---------------------------------------------------------------------------

class TestPluginDependencies(unittest.TestCase):
    """Манифест с полем dependencies — загрузка работает корректно."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        self._plugins_dir = self._base / "plugins"
        self._plugins_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_manifest_with_dependencies_field_loaded(self):
        """Плагин с полем dependencies в манифесте обнаруживается и загружается."""
        _make_plugin(
            self._plugins_dir,
            name="dep_plugin",
            extra_manifest={"dependencies": ["other_plugin"]},
        )
        mgr = PluginManager()
        result = mgr.discover_plugins(self._plugins_dir)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "dep_plugin")

        # Загружаем — должно работать (no built-in dep resolution)
        plugin = mgr.load_plugin("dep_plugin")
        self.assertEqual(plugin.name, "dep_plugin")

    def test_manifest_with_empty_dependencies_list(self):
        _make_plugin(
            self._plugins_dir,
            name="nodep_plugin",
            extra_manifest={"dependencies": []},
        )
        mgr = PluginManager()
        result = mgr.discover_plugins(self._plugins_dir)
        self.assertEqual(len(result), 1)

    def test_get_plugin_info_preserves_discovery(self):
        """Плагин с dependencies попадает в get_plugin_info корректно."""
        _make_plugin(
            self._plugins_dir,
            name="info_dep",
            extra_manifest={"dependencies": ["some_dep"], "license": "MIT"},
        )
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        info = mgr.get_plugin_info("info_dep")
        self.assertEqual(info["name"], "info_dep")
        self.assertIn("status", info)


# ---------------------------------------------------------------------------
# C4. Hook execution — concurrent and edge cases
# ---------------------------------------------------------------------------

class TestHookEdgeCases(unittest.TestCase):
    """Расширенные edge-cases вызова хуков."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        self._plugins_dir = self._base / "plugins"
        self._plugins_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_hook_with_none_payload(self):
        """Хук принимает None как payload."""
        code = textwrap.dedent("""\
            class Plugin:
                name = "none_payload"
                version = "1.0"
                def initialize(self, service): pass
                def get_ipc_methods(self): return {}
                def on_transcribe(self, payload):
                    return {"received": payload is None}
        """)
        _make_plugin(self._plugins_dir, name="none_payload", plugin_code=code)
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        mgr.load_plugin("none_payload")
        results = mgr.call_hook(HOOK_ON_TRANSCRIBE, None)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["received"])

    def test_hook_with_complex_payload(self):
        """Хук получает сложный payload без изменений."""
        code = textwrap.dedent("""\
            class Plugin:
                name = "complex_payload"
                version = "1.0"
                def initialize(self, service): pass
                def get_ipc_methods(self): return {}
                def on_paste(self, payload):
                    return payload
        """)
        _make_plugin(self._plugins_dir, name="complex_payload", plugin_code=code)
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        mgr.load_plugin("complex_payload")
        payload = {"text": "hello", "lang": "ru", "nested": {"key": [1, 2, 3]}}
        results = mgr.call_hook(HOOK_ON_PASTE, payload)
        self.assertEqual(results[0], payload)

    def test_hook_unknown_name_returns_empty(self):
        """Неизвестный хук — все плагины не имеют этого метода → пустой список."""
        _make_plugin(self._plugins_dir, name="no_custom_hook")
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        mgr.load_plugin("no_custom_hook")
        results = mgr.call_hook("on_unknown_event", {"data": 42})
        self.assertEqual(results, [])

    def test_hook_returns_none_included_in_results(self):
        """Хук, возвращающий None, включается в results."""
        code = textwrap.dedent("""\
            class Plugin:
                name = "returns_none"
                version = "1.0"
                def initialize(self, service): pass
                def get_ipc_methods(self): return {}
                def on_transcribe(self, payload):
                    return None
        """)
        _make_plugin(self._plugins_dir, name="returns_none", plugin_code=code)
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        mgr.load_plugin("returns_none")
        results = mgr.call_hook(HOOK_ON_TRANSCRIBE, {})
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0])

    def test_concurrent_hook_calls_no_errors(self):
        """Concurrent call_hook из N потоков не вызывает исключений."""
        for name in ("conc_a", "conc_b", "conc_c"):
            _make_hook_plugin(self._plugins_dir, name, [HOOK_ON_TRANSCRIBE])

        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        for name in ("conc_a", "conc_b", "conc_c"):
            mgr.load_plugin(name)

        errors = []
        all_results = []
        lock = threading.Lock()

        def worker():
            try:
                r = mgr.call_hook(HOOK_ON_TRANSCRIBE, {"text": "concurrent"})
                with lock:
                    all_results.extend(r)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent hook errors: {errors}")
        # 10 workers * 3 plugins = 30 results
        self.assertEqual(len(all_results), 30)

    def test_hook_exception_propagates_to_error_log_not_caller(self):
        """Исключение в хуке не прокидывается вызывающему — возвращается результат."""
        bad_code = textwrap.dedent("""\
            class Plugin:
                name = "exception_hook"
                version = "1.0"
                def initialize(self, service): pass
                def get_ipc_methods(self): return {}
                def on_transcribe(self, payload):
                    raise ValueError("intentional hook error")
        """)
        _make_plugin(self._plugins_dir, name="exception_hook", plugin_code=bad_code)
        _make_hook_plugin(self._plugins_dir, "safe_hook", [HOOK_ON_TRANSCRIBE])

        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        mgr.load_plugin("exception_hook")
        mgr.load_plugin("safe_hook")

        # Не должно бросать исключений
        results = mgr.call_hook(HOOK_ON_TRANSCRIBE, {"text": "test"})
        # Только safe_hook должен вернуть результат
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "safe_hook")


# ---------------------------------------------------------------------------
# C5. Plugin manifest with all optional fields
# ---------------------------------------------------------------------------

class TestManifestOptionalFields(unittest.TestCase):
    """Манифест с минимальными обязательными полями работает корректно."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        self._plugins_dir = self._base / "plugins"
        self._plugins_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_manifest_without_optional_description(self):
        """Манифест без description → description пустая строка."""
        d = self._plugins_dir / "nodesc"
        d.mkdir()
        (d / "plugin.json").write_text(
            json.dumps({"name": "nodesc", "version": "1.0", "entry_point": "plugin.py"}),
            encoding="utf-8",
        )
        code = textwrap.dedent("""\
            class Plugin:
                name = "nodesc"
                version = "1.0"
                def initialize(self, service): pass
                def get_ipc_methods(self): return {}
        """)
        (d / "plugin.py").write_text(code, encoding="utf-8")

        mgr = PluginManager()
        result = mgr.discover_plugins(self._plugins_dir)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].description, "")

    def test_manifest_without_optional_author(self):
        """Манифест без author → author пустая строка."""
        d = self._plugins_dir / "noauthor"
        d.mkdir()
        (d / "plugin.json").write_text(
            json.dumps({"name": "noauthor", "version": "1.0", "entry_point": "plugin.py"}),
            encoding="utf-8",
        )
        code = textwrap.dedent("""\
            class Plugin:
                name = "noauthor"
                version = "1.0"
                def initialize(self, service): pass
                def get_ipc_methods(self): return {}
        """)
        (d / "plugin.py").write_text(code, encoding="utf-8")

        mgr = PluginManager()
        result = mgr.discover_plugins(self._plugins_dir)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].author, "")

    def test_plugin_info_dataclass_defaults(self):
        """PluginInfo с минимальными полями — plugin_dir по умолчанию."""
        info = PluginInfo(
            name="minimal",
            version="0.1",
            description="",
            author="",
            entry_point="main.py",
        )
        self.assertEqual(info.description, "")
        self.assertEqual(info.author, "")
        self.assertIsInstance(info.plugin_dir, Path)


# ---------------------------------------------------------------------------
# C6. PluginManager re-discover — повторный вызов не дублирует плагины
# ---------------------------------------------------------------------------

class TestRediscovery(unittest.TestCase):
    """Повторный вызов discover_plugins не дублирует плагины в списке."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        self._plugins_dir = self._base / "plugins"
        self._plugins_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_rediscover_same_dir_no_duplicates(self):
        _make_plugin(self._plugins_dir, name="stable")
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        mgr.discover_plugins(self._plugins_dir)
        # dict-based storage → нет дубликатов
        self.assertEqual(len(mgr.list_plugins()), 1)

    def test_rediscover_adds_new_plugin(self):
        _make_plugin(self._plugins_dir, name="first")
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        self.assertEqual(len(mgr.list_plugins()), 1)

        # Добавляем новый плагин и повторно сканируем
        _make_plugin(self._plugins_dir, name="second")
        mgr.discover_plugins(self._plugins_dir)
        self.assertEqual(len(mgr.list_plugins()), 2)

    def test_loaded_status_preserved_on_rediscover(self):
        """Загруженный плагин остаётся loaded после повторного discover."""
        _make_plugin(self._plugins_dir, name="persistent")
        mgr = PluginManager()
        mgr.discover_plugins(self._plugins_dir)
        mgr.load_plugin("persistent")
        mgr.discover_plugins(self._plugins_dir)
        plugins = {p["name"]: p for p in mgr.list_plugins()}
        self.assertEqual(plugins["persistent"]["status"], "loaded")


if __name__ == "__main__":
    unittest.main()
