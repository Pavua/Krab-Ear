# Krab Ear Memory Measurements

This directory captures baseline + soak test results for memory leak
investigation (Phase C C.1).

## Workflow

1. Run baseline at known fresh state:
   ```
   python3 scripts/memory_baseline.py --once --output docs/measurements/baseline-fresh-2026-05-04.csv
   ```

2. After 1h of normal use:
   ```
   python3 scripts/memory_baseline.py --once --output docs/measurements/baseline-1h-use-2026-05-04.csv
   ```

3. Run soak test (100 cycles):
   ```
   CYCLES=100 OUTPUT=docs/measurements/soak-2026-05-04.csv ./scripts/memory_soak_test.command
   ```

4. Compare CSV deltas — large growth in `backend_rss_mb` over baseline =
   leak suspect.

## Captured measurements
(populated as runs accumulate)
