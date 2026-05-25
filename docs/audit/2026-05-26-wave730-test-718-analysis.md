# Wave 730 — test_full_workflow.py Wave 718 Blocker Analysis

**Date**: 2026-05-26
**Severity**: P0 — CI gate failure, ~62 PRs blocked
**Status**: Fix exists in worktree; needs merge to origin/codex/krab-ear-v2

## Failing Tests

```
test_13_search_by_tag       — AssertionError: 0 not greater than 0
test_15_get_favorites       — AssertionError: 0 not greater than 0
test_24_get_collection_items — AssertionError: 0 not greater than 0
test_26_analytics_overview  — AssertionError: 0 not greater than 0
test_39_integrity_check     — RuntimeError: ids должен быть непустым списком строк
```

## Root Cause

**xdist worker-split breaks sequential class state.**

CI runs `pytest ... -n auto --dist=loadgroup` (added in PR #236, commit `3f0a7fbf`).
`--dist=loadgroup` groups tests by `@pytest.mark.xdist_group(name)`. Tests WITHOUT
an explicit group use `load` distribution — individual test methods can be dispatched
to different workers.

`FullWorkflowTestCase` is a 49-step sequential simulation: `setUpClass` creates a
single shared `BackendService` + `StateStore` instance in `cls.service`, and
`cls.item_ids` accumulates IDs across tests 01–50. When xdist splits the class
across workers, each worker gets its own `cls` — so items added on worker-A are
invisible to worker-B. Tests 13/15/24/26 read back state written by tests 10/11/14/21
on a different worker, finding empty results. Test 39 calls `export_obsidian` with
`cls.item_ids[:2]` which is empty, triggering the `ids` validation guard.

Note: `--dist=loadscope` would keep unittest classes together by scope, but the CI
uses `loadgroup`, which does NOT guarantee class cohesion without an explicit mark.

## Why origin/codex/krab-ear-v2 Is Broken

The file at `origin/codex/krab-ear-v2:KrabEar/tests/test_full_workflow.py` has NO
`pytestmark` declaration — confirmed via `git show origin/codex/krab-ear-v2:KrabEar/tests/test_full_workflow.py | grep xdist_group` (empty output).

## Fix (Already Present in This Worktree)

Line 41 of `KrabEar/tests/test_full_workflow.py`:

```python
pytestmark = pytest.mark.xdist_group("full_workflow")
```

This forces all 49 `FullWorkflowTestCase` methods onto the same xdist worker,
preserving class-level state and sequential execution order.

## Suggested Action

1. Merge the worktree version of `test_full_workflow.py` (which already has the
   `pytestmark` fix) to `origin/codex/krab-ear-v2`.
2. No code changes required to `history_service.py`, `state_store.py`, or any
   service extraction — the persistence logic is correct. The bug is purely in the
   test distribution configuration.
3. Optionally add `--dist=loadscope` as a safer default or document the reliance
   on `xdist_group` marks for sequential test classes.

## Commits of Interest

| Commit | Role |
|--------|------|
| `3f0a7fbf` | Added `pytest-xdist` + `-n auto --dist=loadgroup` to CI (PR #236) |
| `1dc7d549` | Wave 718 blocker audit doc added (PR #658) |
| `bd19890e` | Last change to `test_full_workflow.py` before this analysis (wave65) |
