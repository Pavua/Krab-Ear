# Wave 979 — FeatureFlags audit

**File:** `KrabEar/backend/feature_flags.py`  
**Date:** 2026-05-26  
**Auditor:** Sub-agent W979 (read-only)

---

## Summary

`FeatureFlags` is a small, focused module. It is generally well-designed but has
five concrete findings: one medium bug already documented by tests, two low
issues, one dead-wire finding, and one design-level note about dual sources of
truth.

---

## Finding 1 — Non-atomic write (MEDIUM)

**Location:** `_save()` lines 111–119

```python
self._flags_path.write_text(
    json.dumps(self._flags, ...),
    encoding="utf-8",
)
```

`Path.write_text` truncates the file then writes. A crash mid-write leaves a
zero-byte or partial JSON file. On next startup `safe_json_loads` returns `{}`,
so all builtin flags silently revert to their coded defaults. For
`llm_rewrite=False` or `auto_backup=False` this is a silent regression.

**Fix:** tmp-file + `fsync` + `os.replace` pattern (same as `StateStore`):

```python
import os, tempfile
tmp = self._flags_path.with_suffix(".tmp")
tmp.write_text(json.dumps(self._flags, ...), encoding="utf-8")
tmp_fd = os.open(str(tmp), os.O_WRONLY)
os.fsync(tmp_fd)
os.close(tmp_fd)
os.replace(tmp, self._flags_path)
```

---

## Finding 2 — Whitespace-only flag names accepted via `set_flag` (LOW / BUG)

**Location:** `set_flag()` lines 133–151; documented in test line 452–459

The guard is:
```python
if not flag_name or not isinstance(flag_name, str) or not flag_name.strip() or flag_name != flag_name.strip():
```

This correctly rejects names with *leading/trailing* whitespace. However the
Wave 98 test suite (lines 452–459) documents — as a known BUG — that the guard
*was not present* at Wave 98 time, so whitespace-only names like `"   "` were
accepted. The guard was later added (Wave 159 test class confirms the fix), but
the Wave 98 test `test_invalid_flag_name_whitespace_only_accepted_as_custom`
still *asserts the bug behaviour* (`self.ff.set_flag("   ", True)` does NOT
raise). This creates a contradiction: the Wave 159 class expects `ValueError`,
but the Wave 98 test expects success.

**Action:** Remove or invert the stale Wave 98 test to avoid misleading future
readers. The current guard in `set_flag` is correct; only the old test is wrong.

---

## Finding 3 — `get_flag_info` releases lock before `_BUILTIN_FLAGS` lookup (LOW)

**Location:** `get_flag_info()` lines 173–191

```python
with self._lock:
    if flag_name not in self._flags: raise KeyError(...)
    enabled = self._flags[flag_name]
# lock released here
if flag_name in _BUILTIN_FLAGS:   # races with concurrent set_flag
    ...
```

Between reading `enabled` and constructing the return dict another thread could
call `set_flag`, changing the in-memory value. Because `_BUILTIN_FLAGS` is
module-level and immutable this is benign for description/version fields. The
`enabled` snapshot is already taken under the lock, so the returned dict is
internally consistent. No fix needed for correctness, but the comment "lock
released here" should be added for clarity.

---

## Finding 4 — Flags are effectively dead (MEDIUM / WIRE)

**Location:** `KrabEar/backend/service.py` grep for `_feature_flags.is_enabled`

```
$ grep -rn "_feature_flags.is_enabled\|feature_flags.is_enabled" KrabEar/ --include="*.py"
(no output)
```

The six built-in flags (`pipeline_v2`, `auto_backup`, `llm_rewrite`,
`confidence_calibration`, `search_index`, `webhook_notifications`) are **never
queried** at runtime. `_feature_flags` in `BackendService` is only used for
`get_feature_flags` / `set_feature_flag` IPC dispatch. The actual backend
components that these flags are supposed to gate (`LLMRewriter`,
`AutoBackupManager`, `WebhookManager`, `SearchIndex`, `ConfidenceCalibrator`)
run unconditionally — they never call `is_enabled()`.

Flags can be toggled and persisted via IPC, but toggling them has zero effect on
system behaviour. This makes the subsystem decorative.

**Action:** Wire `is_enabled` checks into the corresponding services. At minimum
`llm_rewrite` and `webhook_notifications` (both have prod impact) should gate
their respective subsystems.

---

## Finding 5 — Dual source of truth with `settings_service` (DESIGN / LOW)

**`core/config.py` line 814:**
```python
"llm_rewrite_enabled": False,
```

`SettingsService` has a `llm_rewrite_enabled` settings key. `FeatureFlags` has
a `llm_rewrite` flag. Neither gates the actual `LLMRewriter` (see Finding 4),
but if they were wired, two independent runtime knobs would control the same
subsystem. A future developer wiring the flag might miss the settings key, or
vice versa.

**Action:** Decide ownership. Settings is the right long-term home for
user-visible toggles; `FeatureFlags` is appropriate for operator-controlled
experimental gates. Document the distinction in `feature_flags.py` module
docstring.

---

## Non-findings

| Question | Result |
|---|---|
| Persistence survives restart? | YES — `feature_flags.json` in `data_dir`, loaded on `__init__` |
| `set_flag` thread-safe? | YES — `threading.Lock` wraps both in-memory mutation and `_save()` call |
| Validation of `enabled` values? | YES — only `bool` accepted; non-bool stored values on load are ignored |
| Default fallback for missing flag? | YES — `False` for unknown flags; `_BUILTIN_FLAGS` defaults for known |
| Auth for flag mutation? | Local-process only (Unix socket IPC, no token gate) — same as all other IPC methods; acceptable for local assistant |
| Schema versioning? | Not present — flat JSON dict; forward-compatible (new flags ignored if not in `_BUILTIN_FLAGS`, old flags silently accepted) |

---

## Test coverage

`KrabEar/tests/test_feature_flags.py` — 7 test classes, ~55 test methods.
Covers defaults, set/get, persistence, IPC handlers, whitespace validation,
concurrent writes (Wave 98 `test_atomic_set_concurrent_writes`).

Gap: no test verifies that `_save()` produces a valid file after a concurrent
write (the concurrent test only checks errors are absent, not file integrity).
No test exercises behaviour when the file is truncated mid-write (the
non-atomic write scenario in Finding 1).

---

## Recommendations (priority order)

1. **F4 (MEDIUM-WIRE):** Wire `_feature_flags.is_enabled(...)` into at minimum
   `llm_rewrite` and `webhook_notifications` subsystems. Without this the whole
   module is a no-op.
2. **F1 (MEDIUM):** Replace `write_text` with tmp+fsync+rename in `_save()`.
3. **F2 (LOW):** Remove stale Wave 98 whitespace test that documents the old
   broken behaviour (current guard is correct).
4. **F5 (DESIGN):** Document the `settings_service` / `feature_flags` boundary
   in code comments; consider consolidating `llm_rewrite_enabled` into one place.
