# Performance Budget

Documents the regression gate that runs on every CI push/PR.

## What is measured

The gate reads `.benchmarks/history.jsonl` — an append-only NDJSON file written by
the benchmark test suite (`KrabEar/tests/test_performance_benchmarks.py`).  Each line
has the shape:

```json
{
  "ts": "2026-04-27T12:11:25Z",
  "commit": "495ee96",
  "bench_name": "History write 1000 items",
  "elapsed_sec": 0.312,
  "test_node_id": "tests/test_performance_benchmarks.py::...",
  "os": "darwin",
  "python": "3.11"
}
```

18 benchmarks are currently tracked (as of 2026-04-27 baseline):

| Benchmark | Unit |
|-----------|------|
| CSV export 1000 items | elapsed_sec |
| FuzzySearcher 1000 texts | elapsed_sec |
| History page load (first page, 10000 items) | elapsed_sec |
| History search 10000 items | elapsed_sec |
| History write 1000 items | elapsed_sec |
| PipelineContext creation 10000 | elapsed_sec |
| SearchIndex build 1000 items | elapsed_sec |
| SearchIndex full rebuild 1001 items | elapsed_sec |
| SearchIndex.search 10000 items | elapsed_sec |
| TextUtils.cleanup_transcript 10000 texts | elapsed_sec |
| audio_normalization_throughput 100x | elapsed_sec |
| html_report_generation 500 items | elapsed_sec |
| record_to_transcript_e2e_long | elapsed_sec |
| settings_set_get_round_trip 1000x | elapsed_sec |
| translation_round_trip 100x | elapsed_sec |
| ... (3 more) | elapsed_sec |

## Regression threshold

`scripts/compare_benchmarks.py` compares the **last run vs the previous run** for each
benchmark using a sliding window of the 10 most recent entries.

Default threshold: **20%** (`--threshold 0.20`).

The CI step uses the default, so a benchmark that regresses by more than 20% relative
to its immediately preceding run will fail the build.

> Note: `CLAUDE.md` mentions "15% p95 latency regression gate" — this referred to an
> earlier design intention. The actual gate in `scripts/compare_benchmarks.py` uses 20%
> on last-vs-prev, not p95. Align threshold to 15% in a future wave if tighter gating
> is desired (pass `--threshold 0.15` in the CI step).

## Baseline location

`.benchmarks/history.jsonl` — committed to the repository.  The file grows with every
benchmark run.  CI uploads it as an artifact (`benchmark-history`, retained 90 days).

**Staleness notice**: as of 2026-05-26, the youngest entry in the file is from
2026-04-27 (28 days old).  The gate will report "No regressions detected" without
actually comparing fresh runs until the benchmark suite is re-run and the results
appended to the file.  To refresh:

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_performance_benchmarks.py -v
# results are auto-appended to .benchmarks/history.jsonl by the test suite
git add .benchmarks/history.jsonl
git commit -m "chore: refresh benchmark baseline"
```

## How to update the baseline

1. Run the benchmark suite locally (see above).
2. Inspect the output of `python scripts/compare_benchmarks.py` — confirm no spurious
   regressions from environment differences (CI is Ubuntu, dev is macOS).
3. Commit the updated `history.jsonl`.

To intentionally accept a regression (e.g., after a deliberate trade-off):

```bash
# Remove the oldest entries for the affected benchmark so the new slower value
# becomes the "previous" baseline, then commit.
python scripts/compare_benchmarks.py --threshold 0.30   # temporarily widen gate
```

## CI integration

Defined in `.github/workflows/krabear-ci.yml` under `backend-tests`:

```yaml
- name: Compare benchmark history (regression gate)
  env:
    PYTHONPATH: ${{ github.workspace }}/KrabEar
  run: |
    python scripts/compare_benchmarks.py || (echo '::error::Benchmark regression detected' && exit 1)
```

The step **fails the build** on regression — there is no `|| true` soft mode.

## JSON output mode

Pass `--json` to get machine-readable output (useful for future dashboard tooling):

```bash
python scripts/compare_benchmarks.py --json
# exits 0/1 as usual; stdout is JSON instead of the human table
```

Output schema:

```json
{
  "threshold": 0.20,
  "regressions": [],
  "benchmarks": [
    {
      "name": "History write 1000 items",
      "last_elapsed": 0.312,
      "prev_elapsed": 0.298,
      "mean": 0.305,
      "p50": 0.303,
      "p90": 0.318,
      "count": 10,
      "regression_pct": 0.047
    }
  ]
}
```
