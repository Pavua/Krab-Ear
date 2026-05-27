# Audit: UsageTracker — Wave 927

**File:** `KrabEar/backend/usage_tracker.py` (221 lines)
**Tests:** `KrabEar/tests/test_usage_tracker.py` (896 lines, ~70 test methods)
**Date:** 2026-05-26
**Auditor:** W927 (read-only)

---

## Summary

`UsageTracker` is a straightforward daily-statistics tracker (recordings count, duration, word count) with persistence to `usage_stats.json`. The implementation is clean and mostly correct. Five findings of note were identified, ranked by severity.

---

## Findings

### F1 — MEDIUM: "Day" is system-local time, not UTC; DST transitions can double-count or skip

**Location:** `usage_tracker.py:44`, `58`, `139`, `160`, `183`

`date.today()` returns the local calendar date using the OS timezone. This is consistent within a single process, but creates two risks:

1. **Day rollover race** — a recording that starts at 23:59:59 local and finishes after midnight gets attributed to the new day. This is the expected user-facing behaviour, so it is not a bug per se, but it is undocumented.
2. **DST spring-forward** — on the night clocks skip from 01:59 to 03:00, `date.today()` still returns the correct calendar date, so no data is lost.
3. **DST fall-back** — clocks repeat the hour 01:00–01:59 twice. `date.today()` during both repetitions returns the same ISO date, so the day's counter is incremented twice for what the user experiences as the same hour. Again not a meaningful bug (the recordings genuinely happened that calendar day), but worth noting for analytics accuracy.

The `_prune_old_days` cutoff (`date.today() - timedelta(days=30)`) uses local time as well, which is consistent.

**Recommendation:** document that the timezone is system-local; no code change required unless cross-timezone consistency becomes a requirement.

---

### F2 — MEDIUM: Non-atomic write to disk; crash after in-memory update loses data

**Location:** `usage_tracker.py:42–54`, `188–205`

`record_usage` increments counters inside the lock, then calls `_persist()` **outside** the lock (line 54). `_persist` itself re-acquires the lock to snapshot the data, then writes via `Path.write_text`. This creates two gaps:

1. **Crash window** — if the process crashes between the in-memory update (line 52) and the `write_text` call completing (line 203), the increment is lost on the next load. This is an inherent limitation of the design (no WAL, no temp-file-then-rename).
2. **Non-atomic file write** — `write_text` is not atomic. A crash mid-write yields a truncated JSON file. On reload, `_load` catches the `JSONDecodeError` and silently resets all stats to zero (the `except Exception` at line 219). The test `test_persist_across_reload_corrupted_file` confirms this total-loss behaviour is intentional, but it is a data-loss event for all previously accumulated `all_time` counters.

**Recommendation:** use a write-to-temp-file + `os.replace` pattern for atomic writes. This limits worst-case data loss to the last unflushed increment rather than all historical data.

---

### F3 — LOW: `_persist` re-acquires `self._lock` after `record_usage` already releases it; concurrent `record_usage` can interleave between unlock and persist

**Location:** `usage_tracker.py:54` (call outside lock), `192` (re-acquire inside `_persist`)

The lock discipline is: increment under lock → release → call `_persist()` → `_persist` re-acquires lock to snapshot. Between the first release and the `_persist` snapshot another thread's `record_usage` can increment counters. The persisted snapshot therefore reflects the later thread's data, which is fine. However, the first thread's `_persist` call silently writes data that already includes the second thread's increment, causing the second thread's subsequent `_persist` call to write the same data again. This is harmless (idempotent write) but wastes one file write per concurrent pair. More importantly, under 50-thread concurrency (as tested in `test_concurrent_record_thread_safe_50_threads`) this produces 50 sequential file writes where 1 would suffice.

**Recommendation:** batch persistence (e.g., write once per second via a background thread) to reduce I/O under concurrent load.

---

### F4 — LOW: No privacy-mode guard in `record_usage`

**Location:** `usage_tracker.py:42–54`

`UsageTracker` has no awareness of privacy mode. When the backend's privacy mode is enabled, `record_usage` is still called unconditionally (the caller in `BackendService` does not gate on privacy mode before invoking the tracker). Usage counts (recordings, duration, words) are metadata — arguably not sensitive — but the word count is directly derived from the transcript content, which is sensitive. Under strict privacy mode, word counts should not be accumulated.

No privacy-mode parameter is accepted by `record_usage`, and there is no test that verifies privacy-mode suppression.

**Recommendation:** accept an optional `privacy_mode: bool = False` parameter and skip the increment (or zero-out `word_count`) when true.

---

### F5 — LOW: `_prune_old_days` is called on every `record_usage` but not on `_load`; stale entries from a gap in uptime survive in `_daily` until the next recording

**Location:** `usage_tracker.py:36` (`_load` calls `_prune_old_days`), `53` (also called in `record_usage`)

Actually `_load` **does** call `_prune_old_days` (line 218), so stale entries are pruned at startup. However, the `all_time` counters and the `_daily` dict are pruned independently: pruning removes daily entries older than 30 days from `_daily`, but the `all_time` counters are never reconciled with the remaining daily data. This means `all_time.recordings` can be larger than `sum(_daily[d]["recordings"] for d in _daily)` — which is correct by design (all_time is the lifetime total), but could confuse callers who compare the two.

There is also a minor inconsistency: `_prune_old_days` uses `date.today() - timedelta(days=30)` as the cutoff (i.e., entries with key `<` 30 days ago are removed), while `get_usage_stats` queries `range(30)` which covers days `0..29` (today minus 0 to 29). The boundary is consistent but the "month" window is 30 days inclusive (0-indexed), so day 30 ago is excluded from both the query and the prune cutoff — correct.

**Recommendation:** add a comment clarifying that `all_time` is a lifetime accumulator independent of the rolling 30-day window.

---

## Checklist Summary

| Dimension | Status | Finding |
|---|---|---|
| Timezone handling | WARN | System-local; DST fall-back can double-count same hour | F1 |
| Persistence atomicity | WARN | Non-atomic write; crash = total loss of `all_time` counters | F2 |
| Day rollover race | OK | `date.today()` at increment time is correct; documented under F1 |
| Bounded growth | OK | 30-day rolling window; `_prune_old_days` called on load + every write |
| Concurrent increments | OK | `threading.Lock` wraps all counter mutations |
| Data validation on load | OK (with caveat) | Corrupt file → full reset (intentional); see F2 |
| Privacy mode | WARN | No gate; word counts still recorded when privacy mode active | F4 |
| Test coverage | GOOD | ~70 test methods across 13 test classes; concurrency, persistence, edge cases covered |
| Integer overflow | OK | Python arbitrary-precision int; no overflow risk |
| Read/write consistency | OK (with note) | Lock released before `_persist`; see F3 for concurrent-persist detail |

---

## Test Coverage Assessment

Coverage is strong. Key scenarios covered:
- Zero stats at init, single and multi-recording accumulation
- Weekly/monthly period boundaries (day 7 excluded from `get_weekly`, day 30 excluded from `get_monthly`)
- Streak calculation with and without gaps
- Persistence round-trip (save + reload)
- Corrupted file → reset to zero
- 50-thread concurrent `record_usage` — exact count verified
- In-memory mode (no data_dir)
- Float rounding to 2 decimal places

**Not tested:**
- Behaviour when `data_dir` is on a read-only filesystem (OSError in `_persist`)
- Privacy-mode suppression (feature not implemented)
- Atomic write failure (temp-file-then-rename pattern)
- Day-boundary at exactly midnight (mock `date.today`)
