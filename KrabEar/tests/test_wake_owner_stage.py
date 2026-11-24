"""Этап owner_features и подключение признаков владельца к train_krab (без тренировочного стека)."""

from __future__ import annotations

import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WAKE_MODELS_DIR = os.path.join(PROJECT_ROOT, "wake_word_models")
for _p in (PROJECT_ROOT, WAKE_MODELS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import train_krab as tk  # noqa: E402

SRC = Path(WAKE_MODELS_DIR, "train_krab.py").read_text(encoding="utf-8")


def _func_source(name: str) -> str:
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(SRC, node) or ""
    raise AssertionError(name)


class OwnerStageWiringTests(unittest.TestCase):
    def test_owner_stage_registered_but_not_in_all(self) -> None:
        self.assertIn("owner_features", tk.OWNER_STAGES)
        self.assertNotIn("owner_features", tk.STAGES)  # --all не должен требовать личных записей
        self.assertIn("owner_features", tk.STAGE_FUNCS)
        args = tk.build_arg_parser().parse_args(["--stage", "owner_features"])
        self.assertEqual(args.stage, "owner_features")

    def test_all_flag_runs_only_public_stages(self) -> None:
        main_src = _func_source("main")
        self.assertIn("list(STAGES)", main_src)
        self.assertNotIn("OWNER_STAGES", main_src)

    def test_owner_defaults_point_outside_repo(self) -> None:
        args = tk.build_arg_parser().parse_args(["--stage", "owner_features"])
        self.assertTrue(args.owner_dir.endswith("KrabEar/wake_dataset"))
        self.assertTrue(args.golden_dir.endswith("KrabEar/golden"))
        self.assertFalse(tk.is_path_inside(args.owner_dir, PROJECT_ROOT))

    def test_stage_train_uses_owner_features_only_when_all_present(self) -> None:
        src = _func_source("stage_train")
        self.assertIn("OWNER_FEATURE_FILES", src)
        self.assertIn("all(f.exists() for f in owner_files)", src)
        # владельческие позитивы получают метку 1, прочие ключи — 0
        self.assertIn('k in ("positive", "owner_positive")', src)

    def test_stage_features_marker_not_overwritten_semantics(self) -> None:
        # owner-этап пишет features-маркер ТОЛЬКО с длиной окна из прежнего прогона
        src = _func_source("stage_owner_features")
        self.assertIn('write_marker(paths.features, "features"', src)
        self.assertIn("total_length_samples", src)

    def test_validation_clips_never_feed_backgrounds(self) -> None:
        src = _func_source("stage_owner_features")
        self.assertIn("for c in neg_train if c.block in OWNER_BACKGROUND_BLOCKS", src)
        self.assertNotIn("neg_val if c.block in OWNER_BACKGROUND_BLOCKS", src)

    def test_stage_requires_private_work_dir_before_touching_data(self) -> None:
        src = _func_source("stage_owner_features")
        self.assertLess(src.index("assert_private_work_dir"), src.index("load_owner_manifest"))
        self.assertLess(src.index("assert_private_work_dir"), src.index("_link_synthetic_inputs"))


class PoisonedArtifactGuardTests(unittest.TestCase):
    def test_features_written_to_partial_and_renamed_after_sanity_check(self) -> None:
        src = _func_source("stage_owner_features")
        self.assertIn('out_name + ".partial"', src)
        self.assertIn("output_file=str(partial)", src)
        self.assertLess(src.index("_assert_features_sane(partial)"), src.index("os.replace(partial, out_path)"))
        self.assertLess(src.index("os.replace(partial, out_path)"), src.index("write_marker(paths.features, \"owner_features\""))
        self.assertNotIn("output_file=str(out_path)", src)


class PrivateWorkDirTests(unittest.TestCase):
    def test_inside_repo_rejected_outside_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            (repo / "wake_word_models" / "x").mkdir(parents=True)
            outside = Path(td) / "outside"
            outside.mkdir()
            with self.assertRaises(RuntimeError):
                tk.assert_private_work_dir(repo / "wake_word_models" / "x", repo)
            with self.assertRaises(RuntimeError):
                tk.assert_private_work_dir(repo, repo)
            tk.assert_private_work_dir(outside, repo)  # не бросает

    def test_symlink_into_repo_is_inside(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            link = Path(td) / "link"
            os.symlink(repo, link)
            self.assertTrue(tk.is_path_inside(link / "a", repo))

    def test_similar_prefix_is_not_inside(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "repo").mkdir()
            (Path(td) / "repo2").mkdir()
            self.assertFalse(tk.is_path_inside(Path(td) / "repo2", Path(td) / "repo"))


class ComposeTests(unittest.TestCase):
    def test_parse_owner_batch_composition(self) -> None:
        self.assertEqual(tk.parse_owner_batch_composition("384,192"), (384, 192))
        for bad in ("", "1", "1,2,3", "0,5", "-1,3", "a,b"):
            with self.assertRaises(ValueError, msg=bad):
                tk.parse_owner_batch_composition(bad)


class LinkSyntheticTests(unittest.TestCase):
    def _args(self, src: str | None) -> object:
        return tk.build_arg_parser().parse_args(
            ["--stage", "owner_features"] + (["--synthetic-work-dir", src] if src else []))

    def test_requires_marker_and_files_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            work = Path(td) / "work"
            sp = tk.ProjectPaths.from_work_dir(src)
            sp.features.mkdir(parents=True)
            sp.corpora.mkdir(parents=True)
            wp = tk.ProjectPaths.from_work_dir(work)
            with self.assertRaises(RuntimeError):
                tk._link_synthetic_inputs(self._args(None), wp)          # нет --synthetic-work-dir
            with self.assertRaises(RuntimeError):
                tk._link_synthetic_inputs(self._args(str(src)), wp)       # нет маркера
            tk.write_marker(sp.features, "features", {"total_length_samples": 32000})
            with self.assertRaises(RuntimeError):
                tk._link_synthetic_inputs(self._args(str(src)), wp)       # нет файлов
            for f in ("positive_features_train.npy", "negative_features_train.npy"):
                (sp.features / f).write_bytes(b"x")
            (sp.corpora / "validation_set_features.npy").write_bytes(b"x")
            self.assertEqual(tk._link_synthetic_inputs(self._args(str(src)), wp), 32000)
            self.assertTrue((wp.features / "positive_features_train.npy").is_symlink())
            self.assertEqual(tk._link_synthetic_inputs(self._args(str(src)), wp), 32000)  # повтор ок
            # чужой файл на месте ссылки — отказ, не перезапись
            (wp.features / "negative_features_train.npy").unlink()
            (wp.features / "negative_features_train.npy").write_bytes(b"other")
            with self.assertRaises(RuntimeError):
                tk._link_synthetic_inputs(self._args(str(src)), wp)


if __name__ == "__main__":
    unittest.main()
