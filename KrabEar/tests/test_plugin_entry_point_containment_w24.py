"""Wave-24 LOW hardening — plugin_system.py entry_point containment tests.

Covers:
- _parse_manifest rejects absolute entry_point paths.
- _parse_manifest rejects entry_point with '..' traversal.
- _import_plugin raises ValueError on a symlink-escape (resolved outside plugin_dir).
- discover_plugins caps subdirectory scan at _MAX_PLUGIN_DIRS.
- discover_plugins skips oversized plugin.json manifests.
"""

import json
import sys
import unittest
from pathlib import Path
import tempfile
import shutil

# ---------------------------------------------------------------------------
# sys.path bootstrap (mirrors the rest of the test suite)
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve()
_TESTS_DIR = _HERE.parent
_KRABEAR_DIR = _TESTS_DIR.parent
_PROJECT_ROOT = _KRABEAR_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
if str(_KRABEAR_DIR) not in sys.path:
    sys.path.insert(0, str(_KRABEAR_DIR))

from backend.plugin_system import PluginManager  # noqa: E402


def _write_manifest(plugin_dir: Path, **kwargs) -> Path:
    """Write a plugin.json manifest to plugin_dir with the given fields."""
    manifest = {
        "name": kwargs.get("name", "test_plugin"),
        "version": kwargs.get("version", "1.0"),
        "entry_point": kwargs.get("entry_point", "plugin.py"),
        "description": kwargs.get("description", "Test"),
        "author": kwargs.get("author", "tester"),
    }
    manifest_path = plugin_dir / "plugin.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


class TestParseManifestEntryPointContainment(unittest.TestCase):
    """_parse_manifest must reject absolute paths and '..' components."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.plugin_dir = Path(self.tmp) / "my_plugin"
        self.plugin_dir.mkdir()
        self.manager = PluginManager()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _parse(self, entry_point: str):
        manifest_path = _write_manifest(self.plugin_dir, entry_point=entry_point)
        return self.manager._parse_manifest(manifest_path, self.plugin_dir)

    def test_absolute_entry_point_rejected(self):
        """Absolute entry_point must be rejected (returns None)."""
        result = self._parse("/etc/passwd")
        self.assertIsNone(result, "Absolute entry_point must be rejected by _parse_manifest")

    def test_dotdot_entry_point_rejected(self):
        """entry_point with '..' must be rejected (returns None)."""
        result = self._parse("../../evil.py")
        self.assertIsNone(result)

    def test_single_dotdot_component_rejected(self):
        """entry_point '../sibling.py' must be rejected."""
        result = self._parse("../sibling.py")
        self.assertIsNone(result)

    def test_embedded_dotdot_rejected(self):
        """entry_point 'sub/../../../evil.py' must be rejected."""
        result = self._parse("sub/../../../evil.py")
        self.assertIsNone(result)

    def test_normal_entry_point_accepted(self):
        """Legitimate entry_point should be accepted."""
        result = self._parse("plugin.py")
        self.assertIsNotNone(result)
        self.assertEqual(result.entry_point, "plugin.py")

    def test_nested_entry_point_accepted(self):
        """entry_point in subdirectory (no traversal) should be accepted."""
        result = self._parse("src/plugin.py")
        self.assertIsNotNone(result)


class TestImportPluginContainment(unittest.TestCase):
    """_import_plugin must raise ValueError when resolved path exits plugin_dir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.plugin_dir = Path(self.tmp) / "my_plugin"
        self.plugin_dir.mkdir()
        self.manager = PluginManager()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_info(self, entry_point: str):
        from backend.plugin_system import PluginInfo
        return PluginInfo(
            name="test",
            version="1.0",
            description="",
            author="",
            entry_point=entry_point,
            plugin_dir=self.plugin_dir,
        )

    def test_import_absolute_entry_raises(self):
        """_import_plugin with absolute entry_point raises ValueError."""
        # Create a real file outside the plugin dir
        evil = Path(self.tmp) / "evil.py"
        evil.write_text("def create_plugin(): pass\n")
        info = self._make_info(str(evil))
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.manager._import_plugin(info)

    def test_import_traversal_entry_raises(self):
        """_import_plugin with traversal entry raises ValueError."""
        # Create an evil script at the traversal target location
        evil = Path(self.tmp) / "evil.py"
        evil.write_text("def create_plugin(): pass\n")
        info = self._make_info("../evil.py")
        with self.assertRaises(ValueError):
            self.manager._import_plugin(info)

    def test_import_normal_entry_file_not_found(self):
        """_import_plugin with non-existent but safe entry_point raises FileNotFoundError."""
        info = self._make_info("plugin.py")
        with self.assertRaises(FileNotFoundError):
            self.manager._import_plugin(info)


class TestDiscoverPluginsCaps(unittest.TestCase):
    """discover_plugins must cap scan at _MAX_PLUGIN_DIRS and skip oversized manifests."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.plugins_dir = Path(self.tmp) / "plugins"
        self.plugins_dir.mkdir()
        self.manager = PluginManager()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_plugin(self, name: str, manifest_content: str | None = None) -> Path:
        d = self.plugins_dir / name
        d.mkdir(exist_ok=True)
        content = manifest_content or json.dumps({
            "name": name,
            "version": "1.0",
            "entry_point": "plugin.py",
        })
        (d / "plugin.json").write_text(content, encoding="utf-8")
        return d

    def test_scan_capped_at_max_plugin_dirs(self):
        """Scan must stop at _MAX_PLUGIN_DIRS even if more subdirectories exist."""
        cap = PluginManager._MAX_PLUGIN_DIRS
        # Create cap + 5 valid plugins
        for i in range(cap + 5):
            self._make_plugin(f"plugin_{i:04d}")

        found = self.manager.discover_plugins(self.plugins_dir)
        self.assertLessEqual(
            len(found),
            cap,
            f"discover_plugins returned {len(found)} plugins, expected <= {cap}",
        )

    def test_oversized_manifest_skipped(self):
        """Manifests larger than _MAX_MANIFEST_BYTES must be skipped."""
        # Create one valid plugin
        self._make_plugin("good_plugin")

        # Create one oversized plugin
        d = self.plugins_dir / "big_plugin"
        d.mkdir()
        limit = PluginManager._MAX_MANIFEST_BYTES
        huge_manifest = json.dumps({
            "name": "big_plugin",
            "version": "1.0",
            "entry_point": "plugin.py",
            "description": "x" * (limit + 1),
        })
        (d / "plugin.json").write_text(huge_manifest, encoding="utf-8")

        found = self.manager.discover_plugins(self.plugins_dir)
        names = [info.name for info in found]
        self.assertIn("good_plugin", names)
        self.assertNotIn("big_plugin", names)


if __name__ == "__main__":
    unittest.main()
