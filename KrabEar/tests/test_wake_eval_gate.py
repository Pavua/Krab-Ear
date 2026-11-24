"""Чистая часть гейта приёмки wake word (wake_word_models/eval_gate.py): пороги и отчёт."""

from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WAKE_MODELS_DIR = os.path.join(PROJECT_ROOT, "wake_word_models")
for _p in (PROJECT_ROOT, WAKE_MODELS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import eval_gate as eg  # noqa: E402

GOOD = {
    "silence_noise_frames": 7500, "silence_noise_over": 0,
    "speech_frames": 2000, "speech_over": 0,
    "owner_neg_clips": 50, "owner_neg_clip_hits": 0,
    "pos_clips": 17, "pos_hits": 17,
}
THR = eg.GateThresholds()


class EvaluateGateTests(unittest.TestCase):
    def test_all_good_passes(self) -> None:
        ok, why = eg.evaluate_gate(GOOD, THR)
        self.assertTrue(ok, why)
        self.assertEqual(why, [])

    def test_each_threshold_blocks_alone(self) -> None:
        cases = {
            "silence": dict(GOOD, silence_noise_over=76),          # 1.01% > 1%
            "speech": dict(GOOD, speech_over=11),                  # 0.55% > 0.5%
            "neg_clip": dict(GOOD, owner_neg_clip_hits=1),
            "recall": dict(GOOD, pos_hits=15),                     # 88.2% < 90%
        }
        for name, metrics in cases.items():
            ok, why = eg.evaluate_gate(metrics, THR)
            self.assertFalse(ok, name)
            self.assertEqual(len(why), 1, (name, why))

    def test_boundaries_inclusive(self) -> None:
        edge = dict(GOOD, silence_noise_over=75, speech_over=10, pos_clips=20, pos_hits=18)  # ровно 1%, 0.5%, 90%
        ok, why = eg.evaluate_gate(edge, THR)
        self.assertTrue(ok, why)

    def test_missing_metric_or_empty_sets_fail_closed(self) -> None:
        for key in GOOD:
            partial = {k: v for k, v in GOOD.items() if k != key}
            ok, why = eg.evaluate_gate(partial, THR)
            self.assertFalse(ok, key)
            self.assertTrue(any(key in w for w in why), (key, why))
        for key in ("silence_noise_frames", "speech_frames", "owner_neg_clips"):
            ok, _ = eg.evaluate_gate(dict(GOOD, **{key: 0}), THR)
            self.assertFalse(ok, key)

    def test_too_few_positives_is_not_informative(self) -> None:
        ok, why = eg.evaluate_gate(dict(GOOD, pos_clips=9, pos_hits=9), THR)
        self.assertFalse(ok)
        self.assertIn("не информативна", why[0])

    def test_report_has_only_numbers_and_verdict(self) -> None:
        ok, why = eg.evaluate_gate(dict(GOOD, owner_neg_clip_hits=2), THR)
        text = eg.format_report(dict(GOOD, owner_neg_clip_hits=2, stream_hours=0.2, stream_triggers=1),
                                THR, ok, why, model_label="krab_ru_owner_v1")
        self.assertIn("НЕ ПРИНЯТА", text)
        self.assertIn("2 из 50", text)
        self.assertIn("stream_triggers=1", text)
        for forbidden in (".wav", "/Users/", "Library/Application"):
            self.assertNotIn(forbidden, text)
        good_text = eg.format_report(GOOD, THR, True, [], model_label="m")
        self.assertIn("ПРИНЯТА", good_text)
        self.assertNotIn("НЕ ПРИНЯТА", good_text)


class DebounceTests(unittest.TestCase):
    def test_consecutive_frames_count_once_and_refractory_expires(self) -> None:
        self.assertEqual(eg._debounced_triggers([0.1] * 5 + [0.9] * 6 + [0.1] * 5), 1)
        gap = eg.REFRACTORY_FRAMES + 1
        self.assertEqual(eg._debounced_triggers([0.9] + [0.0] * gap + [0.9]), 2)
        self.assertEqual(eg._debounced_triggers([]), 0)


class HygieneTests(unittest.TestCase):
    def test_no_heavy_imports_at_module_level(self) -> None:
        tree = ast.parse((Path(WAKE_MODELS_DIR) / "eval_gate.py").read_text(encoding="utf-8"))
        heavy = {"numpy", "torch", "scipy", "openwakeword"}
        for node in tree.body:  # только верхний уровень модуля
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            self.assertFalse(names & heavy, names & heavy)

    def test_gate_is_not_installing_anything(self) -> None:
        src = (Path(WAKE_MODELS_DIR) / "eval_gate.py").read_text(encoding="utf-8")
        for word in ("shutil.copy", "install", "set_settings", "subprocess"):
            self.assertNotIn(word + "(", src.replace('"""', ""), word)


if __name__ == "__main__":
    unittest.main()
