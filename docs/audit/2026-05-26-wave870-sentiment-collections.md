# Wave 870 — Audit: sentiment_trends / collection_manager / daily_digest

**Date:** 2026-05-26
**Branch:** feature/audit-sentiment-W870
**Files audited:**
- `KrabEar/backend/sentiment_trends.py` (253 lines)
- `KrabEar/backend/collection_manager.py` (333 lines)
- `KrabEar/backend/daily_digest.py` (316 lines)

**Total findings: 9**
(2 bugs, 3 design gaps, 4 minor/informational)

---

## 1. `sentiment_trends.py` — SentimentTrendAnalyzer

### 1-A BUG: `_EMOTION_SCORE` missing "questioning" key → silent 0.0 fallback

`EmotionDetector` can return `primary_emotion = "questioning"`, but `_EMOTION_SCORE` only maps:
`positive / excited / neutral / questioning / negative / frustrated`.

Wait — `questioning` **is** present in the dict with score `−0.1`. However `EmotionDetector`
produces exactly these six emotions and all are mapped.  No bug here — confirmed clean.

### 1-B DESIGN GAP: `days` parameter does NOT align cut-off to UTC midnight

```python
cutoff = datetime.now(timezone.utc) - timedelta(days=days)
```

`days=30` means "30×24 h ago", not "start of the 30th calendar day".  Items timestamped
at 23:59 UTC on the boundary day may be included or excluded depending on wall-clock at
call time.  For a `days=1` call at 00:05 UTC, only 5 minutes of yesterday are included.

**Recommendation:** align to UTC midnight of `(today − days)`:
```python
today_utc = datetime.now(timezone.utc).date()
cutoff = datetime(*(today_utc - timedelta(days=days)).timetuple()[:3], tzinfo=timezone.utc)
```

### 1-C MINOR: `most_positive_day` and `most_negative_day` are the same dict reference when there is only one day

When `daily_sentiment` has exactly one entry, `max()` and `min()` both return the same
dict object (not a copy).  Callers that mutate the returned `SentimentTrendReport` fields
in-place would observe aliased mutation.  Low risk since the report is effectively
read-only after construction, but worth noting.

### 1-D INFORMATIONAL: `_EMOTION_SCORE` uses a module-level dict (mutable at runtime)

The dict is module-level and not frozen.  Any caller that imports `_EMOTION_SCORE` and
mutates it changes scoring globally.  Should be `types.MappingProxyType` or a constant
named with a leading underscore documented as private.  Current naming already signals
private; low priority.

### 1-E INFORMATIONAL: `overall_sentiment` duplicates what `sum(all_scores)/len` already computed

After the early-return guard `if not daily_sentiment` the code recomputes `overall`
from `all_scores` with `if all_scores` guard.  The `all_scores` list is non-empty
whenever `daily_sentiment` is non-empty (they are populated in the same loop), so the
inner guard is always true at that point.  No bug, but the guard is misleading dead code.

---

## 2. `collection_manager.py` — CollectionManager

### 2-A BUG: `rename_collection` IPC handler is NOT registered in the dispatch table

`CollectionManager` exposes `handle_rename_collection` and the method is tested, but
`KrabEar/backend/ipc_dispatch.py` (lines 171-177) does **not** include `"rename_collection"`
in the dispatch table.  Clients calling `rename_collection` over IPC receive
`{"ok": false, "error": "Unknown method: rename_collection"}`.

```
# ipc_dispatch.py lines 171-177 — missing entry:
"create_collection":      svc._collections.handle_create_collection,
"delete_collection":      svc._collections.handle_delete_collection,
"list_collections":       svc._collections.handle_list_collections,
"add_to_collection":      svc._collections.handle_add_to_collection,
"remove_from_collection": svc._collections.handle_remove_from_collection,
# ← "rename_collection" absent
"get_collection_items":   svc._collections.handle_get_collection_items,
```

**Fix:** add one line between `remove_from_collection` and `get_collection_items`:
```python
"rename_collection": svc._collections.handle_rename_collection,
```

No Swift callers found (`grep -r "rename_collection" native/` returns nothing), so this
feature is unreachable from the GUI today.  The IPC wire-up gap means the feature was
never exercised end-to-end despite full unit-test coverage.

### 2-B DESIGN GAP: `_save()` is called inside the lock, doing synchronous file I/O under `threading.Lock`

Every mutating operation holds `self._lock` for the entire duration of the JSON
serialisation and `write_text()` call.  On a slow disk (or iCloud-sync path) this blocks
all concurrent IPC threads for the whole write.  Low risk at the current scale (small
JSON), but the pattern does not match the project's standard of non-blocking writes.

**Recommendation (long-term):** copy the data snapshot inside the lock, release, then
write outside — same pattern used by `StateStore`.

### 2-C MINOR: No input length limit on collection `name` or `description`

A malformed IPC call with a multi-MB `name` string is accepted and serialised to disk.
`InputSanitizer` (used elsewhere in the project) should be applied.

### 2-D INFORMATIONAL: `_load()` silently accepts a corrupt `collections.json` that has `"collections"` key but wrong value type

```python
if isinstance(loaded, dict) and "collections" in loaded:
    self._data = loaded
```

If `loaded["collections"]` is a list instead of a dict, all subsequent `.keys()` /
`__contains__` calls raise `AttributeError`.  Adding
`isinstance(loaded.get("collections"), dict)` to the guard would make recovery
robust.

---

## 3. `daily_digest.py` — DailyDigestGenerator

### 3-A BUG: Accesses private StateStore API (`_lock()` and `_load_active_items_unlocked()`)

```python
with store._lock():
    all_items = store._load_active_items_unlocked()
```

`_lock` and `_load_active_items_unlocked` are private by naming convention in
`StateStore`.  `StateStore` already exposes the public helper
`_load_active_items_with_lock()` (line 836) that does exactly the same thing under the
lock.  Using the public helper would eliminate the double-underscore coupling and is
consistent with how all other services access the store.

**Fix:**
```python
all_items = store._load_active_items_with_lock()
```

The method exists and is already used by other callers.  The `try/except` wrapper
remains valid — simply remove the `with store._lock()` context manager.

Note: tests mock both private methods explicitly, so they would need updating after
the fix.

### 3-B DESIGN GAP: `_parse_item_date` does not handle timezone-aware ISO strings

```python
return datetime.fromisoformat(ts).date()
```

`datetime.fromisoformat` on Python 3.10 handles `+00:00` offsets but the result is
a timezone-aware `datetime`; calling `.date()` on it gives a UTC date, which is
correct.  However strings ending in `Z` (`"2026-05-26T23:00:00Z"`) raise `ValueError`
on Python <3.11 because `fromisoformat` did not support `Z` until 3.11.  The project
runs on macOS with a system Python 3.9 path (see CLAUDE.md) meaning this is a latent
crash for any history item whose `ts` field ends in `Z` (common when items arrive
from the Swift agent, which uses ISO8601 with `Z`).

**Fix:** pre-process `ts` the same way `SentimentTrendAnalyzer._get_ts` does:
```python
ts_norm = ts.replace("Z", "+00:00")
return datetime.fromisoformat(ts_norm).date()
```

### 3-C MINOR: `formatted_markdown` is built twice when `total_recordings == 0`

In `_build_markdown`, the check `if total_recordings == 0` appends the "no recordings"
message.  `_empty_digest` also calls `_build_markdown` with all-zero args and gets the
message.  But `generate_digest` can also call `_build_markdown` with `total_recordings`
that is already 0 (all items filtered by date).  In that case `_build_markdown` produces
the message but the `"## Сводка"` section header and `"Записей: 0"` line are still
emitted before it — producing a non-empty markdown for an "empty" day.  No data
corruption; cosmetic inconsistency only.

---

## Summary table

| # | Module | Severity | Category | Description |
|---|--------|----------|----------|-------------|
| 2-A | collection_manager | **BUG** | API | `rename_collection` not in dispatch table → unreachable over IPC |
| 3-A | daily_digest | **BUG** | Design | Uses private `store._lock()` + `_load_active_items_unlocked()` instead of public helper |
| 1-B | sentiment_trends | Design gap | Edge case | `days` window is rolling-hours, not calendar-day aligned |
| 2-B | collection_manager | Design gap | Concurrency | Blocking file I/O held under `threading.Lock` |
| 3-B | daily_digest | Design gap | Edge case | `_parse_item_date` crashes on `Z`-suffix ISO strings on Python <3.11 |
| 1-C | sentiment_trends | Minor | Edge case | `most_positive_day` / `most_negative_day` aliased dict reference when 1 day |
| 2-C | collection_manager | Minor | Input safety | No length limit on collection name/description |
| 2-D | collection_manager | Info | Robustness | Corrupt `collections.json` (wrong type for `.collections`) not guarded |
| 1-E | sentiment_trends | Info | Clarity | Dead `if all_scores` guard after non-empty `daily_sentiment` established |

---

## Existing test coverage assessment

- `sentiment_trends.py`: well-covered (7 test classes, mock-detector tests for
  improving/declining/stable, timezone, edge cases).  Gap: no test for `days` boundary
  alignment (finding 1-B).
- `collection_manager.py`: CRUD, IPC handlers, bulk, rename — all covered.  Gap:
  `rename_collection` dispatch-table registration not exercised end-to-end (finding 2-A
  would only be caught by a dispatch-invariant test that checks the method name).
- `daily_digest.py`: broad mock-store tests covering highlights, languages, topics,
  truncation.  Gap: `Z`-suffix timestamp crash not tested (finding 3-B); private-API
  coupling not flagged (finding 3-A is a design concern, not a test gap per se).
