# Sentry warning provenance Implementation Plan

Use superpowers:executing-plans. Coordinator-owned source-only task.

Goal: distinguish original warning times from delayed batch delivery.
Base: origin/codex/krab-ear-v2 @ 994cb4f8cbb61932c637f624782a93adcd60cab7.
Architecture: add reserved `warn_batch` Sentry extras with UTC
`first_error_at` / `last_error_at` (chronological extrema) and `flushed_at`.
Treat naive timestamps as UTC, consistent with KrabError serialization.
Keep event timestamp, batching, caps, dedupe, UI/IPC and shutdown unchanged.
Latest error context stays present; reserved provenance wins collisions.
No new dependencies. No live Sentry traffic, model loads, backend restart,
agent launch, production config, Main/Gateway edits, or real calls.
Only explicit paths may be staged; preserve every other worktree/WIP.

## Task 1: synthetic RED, minimal source fix, regression and review

Files: `KrabEar/backend/error_bus.py`,
`KrabEar/tests/test_warn_batch_provenance.py`.

1. Write tests: delayed shutdown flush keeps original time, automatic size
   flush sorts mixed naive/aware timestamps, context cannot spoof metadata,
   each code/batch retains its own range and empty flush sends nothing.
2. Run new file with existing /tmp/py312 Python, PYTHONPATH=KrabEar,
   `-q -p no:cacheprovider -p no:xdist --timeout=60`.
   Expected RED: missing `warn_batch` extras, not import/environment errors.
3. Add UTC-normalized min/max original timestamps and delivery timestamp to
   `_flush_locked`; no scheduling or lifecycle edits.
4. Run same new file plus test_error_bus.py, test_error_bus_extras.py and
   test_error_bus_sentry_cap_2026_08_18.py. Expected all PASS.
5. Run `scripts/pre_merge_py312_check.sh` on the new file using existing
   /tmp/py312 only after checking it is usable and lacks MLX; no rebuild.
6. Independent whole-branch source review. Address confirmed findings and
   rerun affected tests. Commit explicit paths, publish PR, inspect exact
   head checks, merge on green, inspect exact merge checks. No deploy.

Ruling: no full test-tree execution; targeted source-dependent regression
avoids the repository's documented live/ML resource incident risks.
Swift search finds only IPC consumers; no IPC/schema change, so no Swift
build is required for Sentry-only extras.

## Review focus

Datetime offsets, out-of-order arrivals, naive UTC, collision provenance,
batch isolation, bounded metadata, unchanged outbound caps and UI schema.

## Verification record

The canonical Python3.14 environment aborted during the shared conftest's
MLX/Metal import before tests. Do not repeat that path for this source-only
task. The first Python3.12 fixture run also stalled on an import during a
host connection outage and was stopped; it was not a causal RED.
Direct execution of the four synthetic test functions established RED4 on
the pristine source and GREEN4 after the change; this is supplemental unit
evidence, not a substitute for the fixture-based gates.

After host recovery the unmodified project fixtures passed all 52 tests in
the four named files on Python3.12.11. The standard pre_merge_py312_check.sh
passed the new file with MLX absent, backend imports OK and no new workers.
Independent source review: GO, including a separate causal4RED→4GREEN
driver and window-flush edge cases; no P1/P2. Runtime remains unqualified.
