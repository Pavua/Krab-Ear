"""KE05C-01 / KE05C-20: evict() не выходит за пределы кэша и не врёт об удалении.

``_model_folder_name`` возвращает имена, начинающиеся с ``models--``, как есть, поэтому
``models--/../../outside`` давало ``cache_dir / "models--/../../outside"`` и ``rmtree``
вне кэша. ``enforce_size_limit`` добавлял модель в «удалённые» независимо от результата evict().
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.model_cache_manager import ModelCacheManager  # noqa: E402


def _fixture(tmp: str):
    root = Path(tmp)
    cache = root / "hub"
    cache.mkdir()
    inside = cache / "models--owner--repo"
    inside.mkdir()
    (inside / "w.bin").write_bytes(b"x")
    outside = root / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep")
    return cache, inside, outside


class EvictContainment(unittest.TestCase):
    def test_traversal_names_do_not_delete_outside(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache, _inside, outside = _fixture(tmp)
            mgr = ModelCacheManager(cache_dir=cache)
            for name in (
                "models--owner--repo/../../outside",
                "models--owner--repo/../../outside/",
                "models--/../../outside",
                "models--x/../../outside",
                "models--..",
                "models--/../..",
                "../outside",
                "/" + str(outside),
                "models--" + "/.." * 3,
            ):
                self.assertFalse(mgr.evict(name), name)
            self.assertTrue((outside / "precious.txt").exists())
            self.assertTrue(cache.exists())

    def test_symlink_entry_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache, _inside, outside = _fixture(tmp)
            (cache / "models--evil--link").symlink_to(outside, target_is_directory=True)
            mgr = ModelCacheManager(cache_dir=cache)
            self.assertFalse(mgr.evict("models--evil--link"))
            self.assertTrue((outside / "precious.txt").exists())

    def test_normal_eviction_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache, inside, _outside = _fixture(tmp)
            mgr = ModelCacheManager(cache_dir=cache)
            self.assertTrue(mgr.evict("owner/repo"))
            self.assertFalse(inside.exists())
            self.assertFalse(mgr.evict("owner/repo"))

    def test_exact_folder_name_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache, inside, _outside = _fixture(tmp)
            self.assertTrue(ModelCacheManager(cache_dir=cache).evict("models--owner--repo"))
            self.assertFalse(inside.exists())


class EnforceSizeLimitReportsTruth(unittest.TestCase):
    def test_failed_evict_is_not_reported_as_evicted(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache, _inside, _outside = _fixture(tmp)
            mgr = ModelCacheManager(cache_dir=cache, size_limit_mb=0.0)
            with patch.object(mgr, "is_over_size_limit", return_value=True), patch.object(
                mgr, "evict", return_value=False
            ):
                self.assertEqual(mgr.enforce_size_limit(), [])


if __name__ == "__main__":
    unittest.main()
