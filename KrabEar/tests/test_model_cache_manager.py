"""Тесты ModelCacheManager — менеджера кэша ML-моделей HuggingFace Hub."""

from __future__ import annotations
from backend.model_cache_manager import ModelCacheManager, ModelInfo

import sys
import tempfile
import unittest
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _make_model_dir(cache_dir: Path, model_name: str, size_bytes: int = 1024) -> Path:
    """Создаёт искусственную папку модели с одним файлом заданного размера."""
    folder_name = "models--" + model_name.replace("/", "--")
    model_dir = cache_dir / folder_name
    model_dir.mkdir(parents=True, exist_ok=True)
    # Создаём файл-заглушку нужного размера
    dummy = model_dir / "model.bin"
    dummy.write_bytes(b"\x00" * size_bytes)
    return model_dir


class TestModelCacheManagerGetCachePath(unittest.TestCase):
    def test_returns_configured_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = ModelCacheManager(cache_dir=Path(tmp))
            self.assertEqual(mgr.get_cache_path(), tmp)

    def test_default_path_contains_huggingface(self):
        mgr = ModelCacheManager()
        self.assertIn("huggingface", mgr.get_cache_path())


class TestModelCacheManagerIsModelCached(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache_dir = Path(self.tmp)
        self.mgr = ModelCacheManager(cache_dir=self.cache_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_returns_true_when_model_exists(self):
        _make_model_dir(self.cache_dir, "openai/whisper-small")
        self.assertTrue(self.mgr.is_model_cached("openai/whisper-small"))

    def test_returns_false_when_model_absent(self):
        self.assertFalse(self.mgr.is_model_cached("nonexistent/model"))

    def test_accepts_folder_name_format(self):
        _make_model_dir(self.cache_dir, "Helsinki-NLP/opus-mt-ru-es")
        self.assertTrue(self.mgr.is_model_cached("models--Helsinki-NLP--opus-mt-ru-es"))


class TestModelCacheManagerListCachedModels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache_dir = Path(self.tmp)
        self.mgr = ModelCacheManager(cache_dir=self.cache_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_cache_returns_empty_list(self):
        result = self.mgr.list_cached_models()
        self.assertEqual(result, [])

    def test_nonexistent_cache_returns_empty_list(self):
        mgr = ModelCacheManager(cache_dir=Path(self.tmp) / "no_such_dir")
        self.assertEqual(mgr.list_cached_models(), [])

    def test_lists_all_model_dirs(self):
        _make_model_dir(self.cache_dir, "openai/whisper-small")
        _make_model_dir(self.cache_dir, "Helsinki-NLP/opus-mt-ru-es")
        models = self.mgr.list_cached_models()
        names = {m.name for m in models}
        self.assertIn("openai/whisper-small", names)
        self.assertIn("Helsinki-NLP/opus-mt-ru-es", names)

    def test_ignores_non_model_dirs(self):
        # Папки без префикса models-- должны игнорироваться
        (self.cache_dir / "datasets--foo").mkdir()
        (self.cache_dir / "some_other_dir").mkdir()
        result = self.mgr.list_cached_models()
        self.assertEqual(result, [])

    def test_model_info_has_correct_fields(self):
        _make_model_dir(self.cache_dir, "openai/whisper-base", size_bytes=1024 * 1024)
        models = self.mgr.list_cached_models()
        self.assertEqual(len(models), 1)
        m = models[0]
        self.assertIsInstance(m, ModelInfo)
        self.assertEqual(m.name, "openai/whisper-base")
        self.assertGreater(m.size_mb, 0)
        self.assertIsNotNone(m.last_accessed)
        self.assertIn("openai", m.cache_path)

    def test_model_info_size_reflects_file_size(self):
        _make_model_dir(self.cache_dir, "test/model", size_bytes=1024 * 1024)  # 1 MB
        models = self.mgr.list_cached_models()
        self.assertEqual(len(models), 1)
        # Размер должен быть около 1 МБ (с небольшой погрешностью для метаданных ФС)
        self.assertAlmostEqual(models[0].size_mb, 1.0, delta=0.01)


class TestModelCacheManagerGetCacheSizeTotal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache_dir = Path(self.tmp)
        self.mgr = ModelCacheManager(cache_dir=self.cache_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_returns_zero_for_empty_cache(self):
        self.assertEqual(self.mgr.get_cache_size_total(), 0.0)

    def test_sums_multiple_models(self):
        _make_model_dir(self.cache_dir, "model/a", size_bytes=512 * 1024)  # 0.5 MB
        _make_model_dir(self.cache_dir, "model/b", size_bytes=512 * 1024)  # 0.5 MB
        total = self.mgr.get_cache_size_total()
        self.assertAlmostEqual(total, 1.0, delta=0.01)


class TestModelCacheManagerGetCacheInfo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache_dir = Path(self.tmp)
        self.mgr = ModelCacheManager(cache_dir=self.cache_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cache_info_structure(self):
        _make_model_dir(self.cache_dir, "openai/whisper-tiny")
        info = self.mgr.get_cache_info()
        self.assertIn("cache_path", info)
        self.assertIn("model_count", info)
        self.assertIn("total_size_mb", info)
        self.assertIn("models", info)
        self.assertEqual(info["model_count"], 1)
        self.assertIsInstance(info["models"], list)
        self.assertEqual(len(info["models"]), 1)


class TestModelCacheManagerIPCHandlers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache_dir = Path(self.tmp)
        self.mgr = ModelCacheManager(cache_dir=self.cache_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_handle_list_cached_models_empty(self):
        result = self.mgr.handle_list_cached_models({})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["models"], [])

    def test_handle_list_cached_models_with_data(self):
        _make_model_dir(self.cache_dir, "test/model-v1")
        result = self.mgr.handle_list_cached_models({})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["models"][0]["name"], "test/model-v1")

    def test_handle_get_model_cache_info_keys(self):
        result = self.mgr.handle_get_model_cache_info({})
        for key in ("cache_path", "model_count", "total_size_mb", "models"):
            self.assertIn(key, result)

    def test_model_info_to_dict(self):
        info = ModelInfo(
            name="test/model",
            size_mb=42.5,
            last_accessed="2026-04-12T00:00:00+00:00",
            cache_path="/tmp/models--test--model",
        )
        d = info.to_dict()
        self.assertEqual(d["name"], "test/model")
        self.assertEqual(d["size_mb"], 42.5)
        self.assertEqual(d["cache_path"], "/tmp/models--test--model")


class TestModelCacheManagerFolderToModelName(unittest.TestCase):
    def test_simple_org_repo(self):
        result = ModelCacheManager._folder_to_model_name("models--openai--whisper-small")
        self.assertEqual(result, "openai/whisper-small")

    def test_hyphenated_repo_name(self):
        result = ModelCacheManager._folder_to_model_name("models--Helsinki-NLP--opus-mt-ru-es")
        self.assertEqual(result, "Helsinki-NLP/opus-mt-ru-es")

    def test_model_folder_name_round_trip(self):
        original = "pyannote/speaker-diarization"
        folder = ModelCacheManager._model_folder_name(original)
        restored = ModelCacheManager._folder_to_model_name(folder)
        self.assertEqual(restored, original)

    def test_already_folder_format_unchanged(self):
        folder = "models--foo--bar"
        result = ModelCacheManager._model_folder_name(folder)
        self.assertEqual(result, folder)


if __name__ == "__main__":
    unittest.main()
