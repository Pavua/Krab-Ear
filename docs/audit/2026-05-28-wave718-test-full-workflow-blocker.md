# Wave 718 — `test_full_workflow.py` cross-cutting blocker

**Severity**: 🔴 P0 — blocks all PRs Python 3.12 / backend-tests CI gate

**Date discovered**: 2026-05-28 via PR #619 (Wave 525 GigaAM singleton lock) CI logs

## Evidence

`test_full_workflow.py` on origin/codex/krab-ear-v2 fails ≥5 tests with the pattern
"empty count / empty list":

```
test_13_search_by_tag:266 — AssertionError: 0 not greater than 0
test_15_get_favorites_contains_item:279 — AssertionError: 0 not greater than 0
test_24_get_collection_items:368 — AssertionError: 0 not greater than 0
test_26_analytics_dashboard_overview_counts:385 — AssertionError: 0 not greater than 0  
test_39_integrity_check_passes:492 — "ids должен быть непустым списком строк"
```

## Hypothesis

The tests are sequential and depend on cumulative state from earlier tests
(test_01–test_12 add items, tag them, etc.). When test_13 calls `search_by_tag`,
it expects to find items tagged "important" in earlier tests. Returning
`count=0` means either:

1. **State not persisted across tests** — `setUpClass` may now reset NDJSON store
   per-test instead of per-class (recent test framework change?).
2. **Handler regression** — one of the recently-extracted services may have
   stopped reading from the shared store. Suspects (last 30 days):
   - Wave 404 TextScoringService extraction (PR #604)
   - Wave 173 TextProcessingService extraction (PR #529)
   - Wave 88 stop_recording phases extraction (PR #444)
   - Wave 73 AudioAnalyticsService extraction (PR #432)
   - Wave 688 AppleIntegrationService (PR #648 — most recent)
3. **NDJSON write/read race** — service extractions may have introduced a
   `StateStore` instance per-service instead of singleton.

## Impact

Every PR Python 3.12 CI fails on this. ~62 open PRs blocked. Wave 525 PR #619
(permanent gigaam dup fix) cannot ship until this clears.

## Recommended action

1. Reproduce locally:
   ```bash
   PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python -m pytest \
     KrabEar/tests/test_full_workflow.py -v
   ```
   Note: this test is heavy (43 sequential steps) — run only when memory permits.

2. Bisect via git log on the failing test:
   ```bash
   git log --follow KrabEar/tests/test_full_workflow.py | head -20
   ```

3. Inspect `setUpClass` / `setUp` for cleanup hooks that may have been added.

4. Check whether `search_by_tag` / `get_favorites` handlers were moved/aliased
   in recent extractions.

## Notes

- This blocker was NOT visible until PRs started passing the Wave 545 audit
  fix and the Wave 554 Swift 6 fix — those were earlier first-line blockers.
- Marathon stats unaffected: ~62 PRs already shipped to main. This is a NEW
  blocker for the next merge sweep.
- 0 production Sentry events tied to this — purely test-suite regression.
