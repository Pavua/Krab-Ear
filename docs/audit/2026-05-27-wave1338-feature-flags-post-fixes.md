# W1338 — Feature Flags Re-audit (post W988 + W998)

**Date:** 2026-05-27  
**Auditor:** W1338 sub-agent  
**Branch audited:** `codex/krab-ear-v2` (HEAD `6c900317`)  
**Files examined:** `KrabEar/backend/feature_flags.py`, `KrabEar/tests/test_feature_flags.py`, `KrabEar/backend/llm_rewriter.py`, `KrabEar/backend/service.py`

---

## W988 / W998 Merge State

| Branch | Commits ahead of main | Status |
|--------|----------------------|--------|
| `fix/feature-flags-atomic-W988` | 1 commit (`3a3ca5d4`) | **NOT MERGED** |
| `feat/wire-feature-flags-llm-W998` | 1 commit (`10535fd9`) | **NOT MERGED** |

Both branches exist as open PRs on the remote but have not been merged into `codex/krab-ear-v2`.

**Critical observation:** The whitespace validation in `set_flag()` (line 144 of `feature_flags.py`) was introduced by Wave 159 (commit `f2ff3415`, PR #510) and IS already on main. W988 adds only the atomic save (`_save` tmp+fsync+rename) and drops the stale Wave 98 test. W998 adds `feature_flags=None` param to `LLMRewriter.__init__` and a guard in `rewrite()`, plus wires `_feature_flags` into the rewriter in `service.py`.

---

## Findings (5 NEW residual issues)

### F1 — FAIL: Stale Wave-98 test is broken on main (CI red)

**Severity:** HIGH  
**File:** `KrabEar/tests/test_feature_flags.py:452–460`  
**Class:** `TestFeatureFlagsWave98`  
**Test:** `test_invalid_flag_name_whitespace_only_accepted_as_custom`

The test was written when whitespace-only names were accepted (pre-Wave-159). It documents the old bug by calling `self.ff.set_flag("   ", True)` and asserting it succeeds. Wave 159 (PR #510, already on main) added the strict whitespace guard in `set_flag()`. W988 was supposed to drop this test before merging, but since W988 is not yet merged, the stale test remains on main and **causes a CI failure**:

```
FAILED KrabEar/tests/test_feature_flags.py::TestFeatureFlagsWave98::test_invalid_flag_name_whitespace_only_accepted_as_custom
ValueError: Имя флага должно быть непустой строкой без ведущих/завершающих пробельных символов
```

Confirmed by running the full test suite — 1 failed, 54 passed.

**Fix:** Drop `test_invalid_flag_name_whitespace_only_accepted_as_custom` from `TestFeatureFlagsWave98`. The correct behavior (whitespace rejected) is already covered by `TestFeatureFlagsWhitespaceValidation.test_set_flag_whitespace_only_rejected` (line 261).

---

### F2 — UNMERGED: Atomic save not on main (_save still uses non-atomic write_text)

**Severity:** MEDIUM  
**File:** `KrabEar/backend/feature_flags.py:111–119`

The current `_save()` on main uses `Path.write_text()` — a non-atomic single call. If the process is killed mid-write, `feature_flags.json` can end up truncated or empty, silently resetting all flags to defaults on next startup.

W988 fixes this with `tmp_path → fh.flush() → os.fsync() → os.replace()` pattern but is NOT merged. Production is exposed to the data-loss window.

**Current main `_save()`:**
```python
def _save(self) -> None:
    try:
        self._flags_path.write_text(
            json.dumps(self._flags, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.error(...)
```

**Risk:** Concurrent termination during set_flag() (e.g., SIGTERM from BackendSupervisor restart) corrupts the only persistent state for all flags.

---

### F3 — UNMERGED: `llm_rewrite` flag has no runtime effect (W998 not merged)

**Severity:** MEDIUM  
**Files:** `KrabEar/backend/llm_rewriter.py`, `KrabEar/backend/service.py`

The IPC method `set_feature_flag {"flag_name": "llm_rewrite", "enabled": false}` persists the flag to disk and returns success, but does NOT disable LLM post-processing at runtime. The `LLMRewriter.rewrite()` method on main has no awareness of `FeatureFlags`.

W998 adds:
1. `feature_flags=None` parameter to `LLMRewriter.__init__()`.
2. A short-circuit guard at the top of `rewrite()` that returns `LLMRewriteResult(ok=False, fallback_reason="feature_flag_disabled")` when the flag is off.
3. Injection in `service.py`: `self._llm_rewriter._feature_flags = self._feature_flags` (after `_error_bus` wire, line 257).

Until W998 is merged, users who disable `llm_rewrite` via IPC see a misleading success response but LLM rewrites continue unaffected.

---

### F4 — Dead flags: 4 of 6 builtin flags are never read in production code

**Severity:** LOW  
**File:** `KrabEar/backend/feature_flags.py:32–63`

Only `llm_rewrite` (via W998, unmerged) has any production read path. The other 5 builtin flags are declared in `_BUILTIN_FLAGS` and persisted to disk, but no production code calls `feature_flags.is_enabled("<name>")` for them:

| Flag | Read in production? | Expected consumer |
|------|---------------------|-------------------|
| `pipeline_v2` | No | `core/pipeline/executor.py` — only uses the string as a label, not as a gate |
| `auto_backup` | No | `AutoBackupManager` runs unconditionally; flag has no effect |
| `llm_rewrite` | No (pending W998) | `LLMRewriter.rewrite()` |
| `confidence_calibration` | No | `ConfidenceCalibrator` — instantiated unconditionally in `engine.py:362` |
| `search_index` | No | `StateStore` — always builds `SearchIndex`, no flag check |
| `webhook_notifications` | No | `WebhookManager` — fires webhooks unconditionally; no flag guard |

Setting any of these flags (other than `llm_rewrite` post-W998) produces a stored value that has zero runtime effect. This silently misleads operators.

---

### F5 — IPC inconsistency: `handle_set_feature_flag` normalizes names that `set_flag` rejects

**Severity:** LOW  
**File:** `KrabEar/backend/feature_flags.py:213–226`

`handle_set_feature_flag` calls `str(params.get("flag_name", "")).strip()` before passing to `set_flag()`. This means:

- Direct call `set_flag("  pipeline_v2  ", True)` → **raises ValueError** (leading/trailing spaces).
- IPC call `set_feature_flag {"flag_name": " pipeline_v2 ", "enabled": true}` → **succeeds silently** after stripping.

The IPC layer is more permissive than the public API, creating two different contracts for the same operation. An IPC client sending `" pipeline_v2 "` gets success; a direct Python caller with the same string gets ValueError.

Additionally, no `reset_feature_flags` IPC method exists. There is no way to reset all flags to defaults via IPC without manually calling `set_feature_flag` for each builtin. No `reload_feature_flags` method exists either, meaning a flags file edited on disk while the backend is running is not reloaded without a full restart.

**Recommended fix:** Remove the `.strip()` from `handle_set_feature_flag`, forcing callers to send clean names (consistent with `set_flag` contract). Add a `reset_feature_flags` IPC method that re-runs `_load()` while holding the lock.

---

## No-broadcast finding (confirmed non-issue)

There is no EventBus emission on flag change. This is acceptable for the current architecture: `FeatureFlags` is a stateless read-at-callsite mechanism. The only consumer (`LLMRewriter`, post-W998) reads `is_enabled()` synchronously on each call. No async subscribers exist or are planned. No finding raised.

---

## Summary

| ID | Severity | Status | Action |
|----|----------|--------|--------|
| F1 | HIGH | Active CI failure | Drop stale test from main immediately |
| F2 | MEDIUM | Unmerged W988 | Merge `fix/feature-flags-atomic-W988` |
| F3 | MEDIUM | Unmerged W998 | Merge `feat/wire-feature-flags-llm-W998` |
| F4 | LOW | Design gap | Wire remaining 5 flags to their named consumers |
| F5 | LOW | API inconsistency | Remove `.strip()` from IPC handler; add `reset_feature_flags` |

**Immediate action required:** F1 causes CI failure on main today. The stale test must be dropped with or without merging W988 — it documents behavior that no longer exists.
