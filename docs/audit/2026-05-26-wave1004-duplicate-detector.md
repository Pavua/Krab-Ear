# Audit: DuplicateDetector — Wave 1004

**Date:** 2026-05-26  
**File:** `KrabEar/core/duplicate_detector.py`  
**Tests:** `KrabEar/tests/test_duplicate_detector.py`  
**Auditor:** Sub-agent W1004 (read-only)

---

## Summary

`DuplicateDetector` is a 120-line module that detects near-duplicate transcription history entries using Python's `difflib.SequenceMatcher`. It is wired into two consumers: `HistoryService.handle_find_duplicates` (manual IPC) and `AutoDeduplicator.check_duplicate` (automatic on-save path). Overall design is sound for a small dataset, but five issues merit attention.

---

## Findings

### 1. Similarity Metric: SequenceMatcher (Ratcliff/Obershelp), not Cosine or Jaccard

**Metric:** `difflib.SequenceMatcher(None, text_i, text_j).ratio()` — character-level longest-common-subsequence ratio.  
**Threshold default:** `0.9` (both `is_duplicate` and `find_duplicates`).

SequenceMatcher is byte-order-sensitive and character-level. This works correctly for Unicode (Cyrillic, Spanish with accents, emoji) because Python's `str` is already Unicode code-points; no special tokenization is required. However it is sensitive to word-order changes and punctuation padding, which can produce false negatives when two transcriptions convey the same speech but differ in punctuation normalization.

**Verdict:** Acceptable for the use-case. The 0.9 threshold is reasonably conservative.

---

### 2. Performance: O(N²) Within a Time Window — Adequate up to ~500 Items

`find_duplicates` runs a nested loop over all item pairs: O(N²) comparisons. The 60-second time window prunes most comparisons in practice (only items within a 60 s span are compared), but the outer loop is still O(N) over the full list.

`HistoryService.handle_find_duplicates` caps `limit` at **500** items by default, which bounds the worst-case to 500×500/2 = 125 000 SequenceMatcher calls. Each call on typical short transcriptions (~50–200 chars) takes ~5–50 µs, giving a worst-case of ~6 s — noticeable but not catastrophic for a manual IPC request.

`AutoDeduplicator.check_duplicate` fetches only the last 20 items from the store (guarded by the time window), making the auto-path O(20) = effectively O(1).

**Gap:** There is no caching of computed similarity scores or text fingerprints. Every call to `handle_find_duplicates` with the same history re-runs all comparisons from scratch.

**Recommendation:** Low priority given the 500-item cap, but a Bloom-filter or MinHash fingerprint cache keyed on item ID would eliminate redundant comparisons on repeated calls.

---

### 3. Locale / Normalization: No Case or Whitespace Normalization Before Comparison

`is_duplicate` calls `.strip()` on both sides — trailing/leading whitespace is removed. However **no lowercase normalization** is applied. Two otherwise-identical transcriptions that differ only in capitalization (e.g., after LLM post-processing applies sentence casing) will not reach the 0.9 threshold reliably:

```
"привет мир" vs "Привет мир"
```

SequenceMatcher ratio for these is `0.9565` — they would still be flagged as duplicates at the 0.9 threshold. But after more significant casing differences (e.g., all-caps STT output vs. normalized output), ratio can fall below threshold.

Similarly, no punctuation stripping is applied. A transcript ending with `"..."` vs `"."` will lose ~2–5% of similarity depending on text length.

**Verdict:** Minor risk. For the primary use-case (detecting repeated STT output within 60 s), texts are typically produced by the same engine pipeline and will be nearly identical. Explicit `.casefold()` normalization before comparison would be a 1-line hardening.

---

### 4. Grouping Bug: Third-Item Transitive Membership

The grouping algorithm assigns all matching `j` indices to the group opened by item `i`, but then adds all of them to the `assigned` set at once. This means a third item `k` that is similar to item `j` (but not to `i`) will be **missed** if `i` was already processed and `k` appears later in the list.

Example: items [A, B, C] where A≈B and B≈C but A≉C.  
- Iteration i=0: A matches B → group [A, B], both assigned.  
- Iteration i=2 (C): C is not assigned, inner loop finds nothing (A and B are assigned) → C is a singleton → not reported.

This is a **false negative**, not a false positive: legitimate duplicates can be missed when similarity is non-transitive within a group. The tests do not cover this case.

**Severity:** Medium. Rarely triggered in practice (most STT duplicates are nearly identical to all members), but correctness gap worth noting.

---

### 5. Privacy Mode: Not Checked — Text Is Always Compared

Neither `DuplicateDetector` nor `AutoDeduplicator` checks for a privacy-mode flag before processing transcript text. In privacy mode, history items should not have their text exposed to non-privacy-safe code paths.

`AutoDeduplicator.check_duplicate` is called from the `BackendService` recording pipeline. If `privacy_mode=True` is set in settings, the text passed to the detector is still the raw transcript content.

**Risk:** Low-to-medium. Text never leaves the process and is only compared in-memory, but it is passed through a non-privacy-aware module. A guard at the `BackendService` call-site (skip auto-dedup when `privacy_mode=True`) is the correct fix.

---

### 6. Wire Status: Two Consumers — Manual IPC + Auto-path

| Consumer | Path | Privacy guard? | Caching? |
|---|---|---|---|
| `HistoryService.handle_find_duplicates` | Manual IPC `find_duplicates` | No | No |
| `AutoDeduplicator.check_duplicate` | Called on recording save (auto) | No | N/A (per-item) |

`AutoDeduplicator` is instantiated fresh per `BackendService` init; `DuplicateDetector()` is also instantiated fresh inside `handle_find_duplicates` on every call. No shared state or singleton.

**Output schema of `find_duplicates` IPC:**
```json
{
  "groups": [{"items": [...], "similarity": 0.9876}],
  "total_duplicates": 3
}
```
Schema is stable and consistent with `DuplicateGroup` dataclass. No drift observed.

---

## Test Coverage

`test_duplicate_detector.py` — **437 lines**, 6 test classes, ~35 test methods:

- `IsDuplicateTestCase`: exact match, high similarity, low similarity, empty strings, custom thresholds.
- `FindDuplicatesTestCase`: empty list, no duplicates, identical pairs, 3-item groups, time window (include/exclude), two separate groups, similarity value range, items without timestamps.
- `HistoryServiceFindDuplicatesTestCase`: empty history, finds duplicates, no duplicates, result structure.
- `GetTextFieldsTestCase`: `text` vs `transcript` field priority.
- `GetTimestampTestCase`: float, int, ISO string, missing, invalid, `timestamp` alias.
- `FindDuplicatesExtraTestCase`: empty-text skip, low threshold, boundary at exactly 60 s.
- `TestWave117DuplicateDetector`: Cyrillic/Spanish unicode, concurrent thread-safety.

**Gaps:**
- Non-transitive grouping (A≈B, B≈C, A≉C) — not tested.
- Privacy mode bypass — not tested.
- Performance/scaling test for N=500 — not tested.

**Overall coverage:** Good for happy-path and most edge cases. The transitive grouping gap and privacy bypass are untested.

---

## Recommendations (Priority Order)

| # | Finding | Severity | Fix |
|---|---|---|---|
| 1 | Transitive grouping bug — third item can be missed | Medium | Implement union-find or iterative expansion in `find_duplicates` |
| 2 | No privacy-mode guard in auto-dedup path | Medium | Skip `AutoDeduplicator.check_duplicate` when `privacy_mode=True` |
| 3 | No casefold normalization before comparison | Low | `.casefold()` in `is_duplicate` before `.strip()` |
| 4 | No fingerprint cache — repeated IPC calls recompute all | Low | Optional: cache SequenceMatcher results keyed by item ID pair |
| 5 | `DuplicateDetector()` instantiated fresh on every `handle_find_duplicates` call | Negligible | Reuse instance as `HistoryService` attribute |
