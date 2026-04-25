"""Unit tests for benchmark history tracking (conftest plugin + compare script)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Project root on sys.path
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

REPO_ROOT = os.path.dirname(PROJECT_ROOT)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# ---------------------------------------------------------------------------
# Helpers imported from production scripts
# ---------------------------------------------------------------------------
# Import compare_benchmarks as a module without executing main()
import importlib.util as _ilu

_SCRIPT_PATH = Path(REPO_ROOT) / "scripts" / "compare_benchmarks.py"
_spec = _ilu.spec_from_file_location("compare_benchmarks", _SCRIPT_PATH)
_cmp_mod = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_cmp_mod)  # type: ignore[union-attr]

_load_history = _cmp_mod._load_history
_compute_stats = _cmp_mod._compute_stats
_check_regressions = _cmp_mod._check_regressions
_format_table = _cmp_mod._format_table
_percentile = _cmp_mod._percentile
_compare_main = _cmp_mod.main


def _write_entries(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def _make_entry(
    bench_name: str,
    elapsed_sec: float,
    ts: str = "2026-04-25T10:00:00Z",
    commit: str = "abc1234",
) -> dict:
    return {
        "ts": ts,
        "commit": commit,
        "bench_name": bench_name,
        "elapsed_sec": elapsed_sec,
        "test_node_id": f"tests/test_benchmarks.py::{bench_name}",
        "os": "darwin",
        "python": "3.11",
    }


class TestLoadHistory(unittest.TestCase):
    def test_empty_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            path.write_text("")
            result = _load_history(path)
        self.assertEqual(result, {})

    def test_missing_file_returns_empty(self) -> None:
        path = Path("/nonexistent/path/history.jsonl")
        self.assertEqual(_load_history(path), {})

    def test_groups_by_bench_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            _write_entries(
                path,
                [
                    _make_entry("bench_a", 1.0, ts="2026-04-25T10:00:00Z"),
                    _make_entry("bench_b", 2.0, ts="2026-04-25T10:01:00Z"),
                    _make_entry("bench_a", 1.1, ts="2026-04-25T10:02:00Z"),
                ],
            )
            groups = _load_history(path)
        self.assertIn("bench_a", groups)
        self.assertIn("bench_b", groups)
        self.assertEqual(len(groups["bench_a"]), 2)
        self.assertEqual(len(groups["bench_b"]), 1)

    def test_sorted_by_ts_ascending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            _write_entries(
                path,
                [
                    _make_entry("bench_x", 3.0, ts="2026-04-25T12:00:00Z"),
                    _make_entry("bench_x", 1.0, ts="2026-04-25T08:00:00Z"),
                    _make_entry("bench_x", 2.0, ts="2026-04-25T10:00:00Z"),
                ],
            )
            groups = _load_history(path)
        times = [e["elapsed_sec"] for e in groups["bench_x"]]
        self.assertEqual(times, [1.0, 2.0, 3.0])

    def test_ignores_malformed_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            path.write_text(
                json.dumps(_make_entry("bench_ok", 1.0)) + "\n"
                "NOT_JSON\n"
                "{broken\n"
            )
            groups = _load_history(path)
        self.assertIn("bench_ok", groups)
        self.assertEqual(len(groups), 1)


class TestComputeStats(unittest.TestCase):
    def test_single_entry_no_regression(self) -> None:
        entries = [_make_entry("bench_a", 1.0)]
        stats = _compute_stats("bench_a", entries)
        self.assertEqual(stats.last_elapsed, 1.0)
        self.assertIsNone(stats.prev_elapsed)
        self.assertIsNone(stats.regression_pct)

    def test_two_entries_regression_computed(self) -> None:
        entries = [
            _make_entry("bench_a", 1.0, ts="2026-04-25T10:00:00Z"),
            _make_entry("bench_a", 1.3, ts="2026-04-25T11:00:00Z"),
        ]
        stats = _compute_stats("bench_a", entries)
        self.assertAlmostEqual(stats.regression_pct or 0, 0.30, places=5)

    def test_window_limited_to_10(self) -> None:
        entries = [_make_entry("bench_a", float(i), ts=f"2026-04-25T{i:02d}:00:00Z") for i in range(15)]
        stats = _compute_stats("bench_a", entries)
        self.assertEqual(stats.count, 15)
        # last 10 are indices 5..14
        expected_last = 14.0
        self.assertEqual(stats.last_elapsed, expected_last)

    def test_no_regression_when_faster(self) -> None:
        entries = [
            _make_entry("bench_a", 2.0, ts="2026-04-25T10:00:00Z"),
            _make_entry("bench_a", 1.5, ts="2026-04-25T11:00:00Z"),
        ]
        stats = _compute_stats("bench_a", entries)
        self.assertLess(stats.regression_pct or 0, 0.0)


class TestCheckRegressions(unittest.TestCase):
    def _stats(self, regression_pct: float | None) -> _cmp_mod.BenchStats:
        return _cmp_mod.BenchStats(
            name="bench",
            last_elapsed=1.0,
            prev_elapsed=0.8 if regression_pct is not None else None,
            mean=0.9,
            p50=0.9,
            p90=1.0,
            count=2,
            regression_pct=regression_pct,
        )

    def test_regression_above_threshold_flagged(self) -> None:
        s = self._stats(0.25)
        regressions = _check_regressions([s], threshold=0.20)
        self.assertEqual(len(regressions), 1)

    def test_regression_below_threshold_not_flagged(self) -> None:
        s = self._stats(0.10)
        regressions = _check_regressions([s], threshold=0.20)
        self.assertEqual(regressions, [])

    def test_no_previous_not_flagged(self) -> None:
        s = self._stats(None)
        regressions = _check_regressions([s], threshold=0.20)
        self.assertEqual(regressions, [])

    def test_improvement_not_flagged(self) -> None:
        s = self._stats(-0.15)
        regressions = _check_regressions([s], threshold=0.20)
        self.assertEqual(regressions, [])


class TestFormatTable(unittest.TestCase):
    def test_empty_history_message(self) -> None:
        out = _format_table([])
        self.assertIn("No benchmark data", out)

    def test_contains_bench_name(self) -> None:
        s = _cmp_mod.BenchStats(
            name="History write 1000 items",
            last_elapsed=1.5,
            prev_elapsed=1.4,
            mean=1.45,
            p50=1.45,
            p90=1.5,
            count=3,
            regression_pct=0.07,
        )
        out = _format_table([s])
        self.assertIn("History write 1000 items", out)
        self.assertIn("1.500", out)


class TestPercentile(unittest.TestCase):
    def test_p50_of_sorted_list(self) -> None:
        data = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertAlmostEqual(_percentile(data, 0.5), 3.0, places=5)

    def test_p90(self) -> None:
        data = list(range(1, 11))
        result = _percentile([float(x) for x in data], 0.90)
        self.assertGreaterEqual(result, 9.0)

    def test_empty_returns_zero(self) -> None:
        self.assertEqual(_percentile([], 0.5), 0.0)


class TestMainCLI(unittest.TestCase):
    def test_main_exit0_no_regression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            _write_entries(
                path,
                [
                    _make_entry("bench_a", 1.0, ts="2026-04-25T10:00:00Z"),
                    _make_entry("bench_a", 1.05, ts="2026-04-25T11:00:00Z"),
                ],
            )
            rc = _compare_main(["--history", str(path)])
        self.assertEqual(rc, 0)

    def test_main_exit1_on_regression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            _write_entries(
                path,
                [
                    _make_entry("bench_a", 1.0, ts="2026-04-25T10:00:00Z"),
                    _make_entry("bench_a", 2.0, ts="2026-04-25T11:00:00Z"),
                ],
            )
            rc = _compare_main(["--history", str(path), "--threshold", "0.20"])
        self.assertEqual(rc, 1)

    def test_main_exit0_missing_history(self) -> None:
        rc = _compare_main(["--history", "/nonexistent/missing.jsonl"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
