# Wave 888 — ObsidianSyncManager Audit

**Date:** 2026-05-26  
**File audited:** `KrabEar/backend/obsidian_sync.py` (434 lines)  
**Scope:** incremental sync correctness, YAML frontmatter escaping, conflict resolution, state file safety  

---

## Summary

4 findings (2 HIGH, 1 MEDIUM, 1 LOW). No critical data-loss bugs, but two HIGH issues can silently corrupt Obsidian vaults or produce invalid YAML that breaks frontmatter parsers.

---

## Findings

### F1 — HIGH: YAML frontmatter values are not escaped

**Location:** `_build_md_content()`, lines 336–349

Scalar values are interpolated directly into YAML lines without quoting or escaping:

```python
lines.append(f"title: Транскрипция {date_str}")
lines.append(f"date: {datetime_str}")
lines.append(f"id: {item_id}")
lines.append(f"source_lang: {source_lang}")
lines.append(f"target_lang: {target_lang}")
```

**Risk:** Any value containing a colon, leading/trailing space, `#`, `[`, `{`, `>`, `|`, or a newline will produce syntactically invalid YAML. A `source_lang` value of `ru: auto` becomes `source_lang: ru: auto`, which most YAML parsers (including Obsidian's) reject. Confidence floats are safe; ISO lang codes are low-risk today, but `item_id` (UUID) is fine while arbitrary user tags are not. The tag normalisation on lines 330–333 (`re.sub(r"[#\s]+"…)`) does sanitise tags but does not protect the scalar fields above.

**Fix:** Wrap scalar values in double quotes and escape internal double quotes and backslashes:

```python
def _yaml_scalar(v: str) -> str:
    escaped = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'

lines.append(f"title: {_yaml_scalar('Транскрипция ' + date_str)}")
lines.append(f"id: {_yaml_scalar(item_id)}")
```

Alternatively, use `yaml.dump({...}, allow_unicode=True)` for the entire frontmatter block (PyYAML is already an indirect dependency via pyannote).

---

### F2 — HIGH: Incremental timestamp comparison is purely lexicographic string comparison

**Location:** `sync()`, lines 151–153

```python
if not force and last_sync_ts is not None:
    if item_ts <= last_sync_ts:
        result.skipped_count += 1
        continue
```

`_get_item_ts()` returns `str(ts)` as-is. `last_sync_ts` is an ISO-8601 string produced by `datetime.now(timezone.utc).isoformat()` which includes the `+00:00` suffix (e.g. `"2026-05-26T14:01:00.123456+00:00"`).

History items whose `ts` field is stored without timezone (`"2026-05-26T14:01:00"`) or with `Z` suffix (`"2026-05-26T14:01:00Z"`) will **always compare less-than** a `+00:00` string because `Z` < `+` in ASCII. This causes every item from a naive-timestamp store to be **permanently skipped** after the first sync, silently producing `synced_count=0` for all subsequent runs even when `force=False` is intended to be incremental.

Observed symptom: user adds new recordings; sync reports 0 synced / N skipped; vault is never updated again without `force=True`.

**Fix:** Parse both timestamps before comparing:

```python
from backend.models import _parse_ts  # or inline datetime.fromisoformat fallback

def _ts_comparable(ts_str: str) -> datetime:
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)
```

Replace the string comparison with `_ts_comparable(item_ts) <= _ts_comparable(last_sync_ts)`.

---

### F3 — MEDIUM: No conflict resolution — silent overwrite on concurrent sync calls

**Location:** `sync()`, lines 165–175

```python
existed = md_path.exists()
content = self._build_md_content(item)
md_path.write_text(content, encoding="utf-8")
```

The outer `self._lock` is released between the `with self._lock:` block (lines 120–127) and the file write loop. Two concurrent `sync()` calls (e.g. from the IPC handler and a scheduled task) will race: both read the same `last_sync_ts`, both decide to write the same files, and the second write silently overwrites any manual Obsidian edits the user made to that file.

There is no content-hash check, no merge, and no backup before overwrite.

**Risk level:** MEDIUM (requires concurrent callers; in practice, the IPC server is single-threaded per-method but an explicit scheduler calling `sync()` in a background thread is realistic).

**Fix options (in order of preference):**
1. Hold `self._lock` across the entire item loop (acceptable given typical item counts and I/O latency).
2. Before overwriting, check if the file's mtime is newer than `last_sync_ts` and skip with a warning.
3. Write to a temp file then `rename()` (atomic) — already done for state file, should be extended to .md writes.

---

### F4 — LOW: `_make_filename` collision on same-second items with identical id prefix

**Location:** `_make_filename()`, lines 288–303

The filename template is `transcript_{YYYY-MM-DD_HH-MM-SS}_{id[:8]}.md`. Two items recorded within the same second whose UUIDs share the first 8 characters (extremely unlikely but possible with custom or imported IDs) will map to the same filename. The second write silently overwrites the first with no warning in `SyncResult`.

**Fix:** Append a monotonic counter or use the full `id` (truncated to 16–32 chars) as the suffix.

---

## What Works Well

- **Atomic state file writes** (lines 429–431): write to `.tmp` then `Path.replace()` — correct, avoids partial-write corruption.
- **Vault existence check** in `configure()` (lines 79–83): raises `ValueError` before mutating state.
- **`_load_state` defensive recovery** (lines 401–415): catches all exceptions, logs a warning, and leaves the manager in a safe unconfigured state.
- **Tag sanitisation** (lines 330–333): strips `#` and whitespace from user tags before YAML emission.
- **`target_dir.mkdir(parents=True, exist_ok=True)`** called both in `configure()` and at the start of `sync()`: resilient to vault folder deletion between calls.
- **Progress events via EventBus**: correct — emitted for every item including skipped ones.

---

## Test Coverage Notes

Six existing test files cover ObsidianSync (`test_obsidian_sync.py`, `test_obsidian_sync_coverage.py`, `test_obsidian_sync_errors_wave603.py`, `test_obsidian_sync_wave623.py`, `test_obsidian_sync_wave642.py`, `test_obsidian_sync_wave659.py`). None of the existing tests:

- Inject a `ts` value without timezone to exercise the F2 lexicographic comparison bug.
- Produce YAML values containing colons or newlines to catch F1.
- Call `sync()` concurrently to test the F3 race.

Adding regression tests for F1 and F2 is straightforward and recommended alongside any fix.
