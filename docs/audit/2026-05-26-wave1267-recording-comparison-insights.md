# Audit W1267 — `recording_comparison.py` + `recording_insights.py`

**Date:** 2026-05-26  
**Branch:** `audit/recording-comparison-insights-W1267`  
**Files audited:**
- `KrabEar/backend/recording_comparison.py` (229 lines)
- `KrabEar/backend/recording_insights.py` (637 lines)

---

## Summary

5 findings across both files. No critical correctness bugs; one medium input-validation gap, one medium privacy gap, three low-severity issues. All 110 existing tests pass.

---

## Findings

### F1 — MEDIUM · `recording_comparison.py` · Missing minimum-2-items guard

**Location:** `RecordingComparison.compare()`, line 132–140  
**Description:** The docstring states the method accepts "от 2 до MAX_ITEMS" items, but the validation only enforces an upper bound (`len > MAX_ITEMS`) and non-empty (`not item_ids`). Passing a single ID is silently accepted. For n=1 the similarity matrix is a 1×1 `[[1.0]]` (correct mechanically), but `common_words` returns the item's own tokens (the n=1 dead-branch on line 199–200), which is misleading — no meaningful comparison has occurred. Callers receive a `ComparisonView` that looks like a comparison result but isn't one.

**Risk:** UI or Swift caller displays a "comparison" for a single recording without any indication that a minimum of 2 items is required.

**Fix:**
```python
if len(item_ids) < 2:
    raise ValueError("Для сравнения необходимо минимум 2 записи")
```
Add this check after the empty check on line 133. The dead branch on line 199–200 can then be removed.

---

### F2 — MEDIUM · `recording_insights.py` · Privacy-mode bypass in text-based insights

**Location:** `_handle_get_recording_insights` in `service.py` line 2855; `_compute_most_discussed_topic` and `_compute_speaking_pace_change` in `recording_insights.py`

**Description:** Both `_compute_most_discussed_topic` (reads full transcript text) and `_compute_speaking_pace_change` (reads `text` for word count via `len(text.split())`) process transcript content. The IPC handler at `service.py:2855` loads all active items unconditionally and passes them to `generate_insights()` without checking `privacy_mode_enabled`. Compare: `TranslationService` returns early with an error when `privacy_mode_enabled` is set (lines 96, 201 in `translation_service.py`).

**Risk:** When the user enables privacy mode expecting no transcript analysis, `get_recording_insights` still scans and aggregates transcript text to produce topic and pace insights. This contradicts the privacy guarantee applied consistently elsewhere.

**Fix:** In `_handle_get_recording_insights`, check `self._get_runtime_setting("privacy_mode_enabled", False)` before calling `generate_insights`. Either return `{"insights": [], "count": 0, "days": days}` or call a variant that skips text-dependent methods.

---

### F3 — LOW · `recording_insights.py` · `get_daily_insight` has no IPC handler

**Location:** `RecordingInsightsGenerator.get_daily_insight()`, line 231; `service.py` handler table

**Description:** `get_daily_insight()` is a public method that returns the single highest-confidence insight for today. It is fully implemented (10+ tests cover it), but there is no corresponding IPC method — only `get_recording_insights` is wired. Swift callers cannot access the convenient "daily insight" shortcut without parsing the full list and picking the max-confidence entry themselves.

**Risk:** Feature is complete but unreachable from the native agent. Any Swift panel wanting to display a single daily insight must re-implement the max-confidence selection.

**Fix:** Add `"get_daily_insight": self._handle_get_daily_insight` to the handler table, implemented as:
```python
def _handle_get_daily_insight(self, params: dict[str, Any]) -> dict[str, Any]:
    try:
        with self.store._lock():
            items = self.store._load_active_items_unlocked()
    except Exception:
        items = []
    insight = self._recording_insights.get_daily_insight(items)
    return {"insight": insight.to_dict() if insight else None}
```

---

### F4 — LOW · `recording_insights.py` · Unbounded `all_tokens` list in topic analysis

**Location:** `_compute_most_discussed_topic()`, line 498–507

**Description:** The method concatenates `all_tokens.extend(tokens)` for every item in `recent` (the 7-day window). There is no cap on how many items or tokens are processed. For a heavily active user with thousands of transcriptions in the window, `all_tokens` can grow to millions of string objects before `Counter()` is called. The `Counter` construction itself is O(n), but peak RAM for the intermediate list is proportional to total word count across all recent transcriptions.

**Risk:** On a system with tens of thousands of items in 7 days (e.g., bulk reprocess results stored in history), this could allocate hundreds of MB before `Counter` completes. The `_compute_most_discussed_topic` does not share the `MAX_ITEMS=10` bound that `RecordingComparison` has.

**Fix:** Add a constant cap, e.g., `MAX_TOPIC_ITEMS = 500`, and slice `recent[:MAX_TOPIC_ITEMS]` before the loop, or stream into `Counter` directly without materialising `all_tokens`:
```python
token_counter: Counter[str] = Counter()
for item in recent:
    text = _get_text(item)
    if text:
        token_counter.update(
            w for w in _tokenize(text)
            if w not in _STOP_WORDS and len(w) > 3
        )
```

---

### F5 — LOW · `recording_comparison.py` · TF formula is binary-set, not true TF

**Location:** `_build_tf()`, line 68–78; `_cosine_sim()`, line 81–93

**Description:** `_tokenize()` returns a `set` (line 61–65), so duplicate words within a single transcript are de-duplicated before TF is computed. The TF weight is `1/n` per unique type, not a count-weighted frequency. Two items with the same vocabulary but very different word frequencies will score `1.0` similarity. Example: item A with text "server server server database" and item B with "server database database database" both produce `tokens = {"server", "database"}` → identical TF vectors → similarity 1.0.

This is not a correctness bug per se — it is a documented simplification — but it can over-report similarity for short texts where a single repeated word differs significantly from one item to another.

**Risk:** Low. Misleading similarity scores for short, repetitive recordings (e.g., a 2-second "да да да" vs "да нет"). No crash or data loss.

**Note:** The formula is mathematically correct as implemented (verified: identical sets → 1.0, disjoint sets → 0.0, diagonal → 1.0, symmetry holds). The limitation is the set-collapse step before TF computation.

**Fix (optional):** Replace `re.findall(…)` result conversion to `set` with a `Counter` and compute proper TF weights. Not blocking; acceptable for a heuristic comparison tool.

---

## Non-findings (confirmed OK)

- **O(n²) performance:** The similarity matrix is bounded by `MAX_ITEMS=10`, so the worst case is 45 pairwise `_cosine_sim` calls. Each `_cosine_sim` iterates at most over a small vocabulary set. Total is O(1) in practice.
- **Memory (comparison):** All data structures in `RecordingComparison.compare()` are bounded by `MAX_ITEMS × vocab_size`. For 10 items with up to 10k unique tokens each: ~100k dict entries total — well within limits.
- **Symmetry exploit:** The `i > j → sim_matrix[j][i]` shortcut (line 164) correctly halves cosine computations with no correctness risk.
- **IPC handler wiring:** Both `compare_recordings` (line 1116) and `get_recording_insights` (line 1055) are wired and confirmed reachable.
- **Test coverage:** 110 tests across both test files, all passing. IPC dispatch is covered for `compare_recordings`. Unit coverage for all 6 insight generators is present.

---

## Test coverage gaps (informational, not findings)

- No test for `compare_recordings` with `item_ids` of length 1 going through the IPC handler (only the raw service method is tested).
- No privacy-mode test for `get_recording_insights`.
- No test for `get_daily_insight` via IPC (method has no handler — see F3).
