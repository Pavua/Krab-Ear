# Wave 1264 Re-audit: `core/normalization_profiles.py` — Residual Issues

**Date:** 2026-05-26
**Auditor:** W1264
**File:** `KrabEar/core/normalization_profiles.py` (283 lines)
**Prior audits:** W1050 (6 findings, 2M/3L/1I), W1055 (fix F1+F2), W1058 (fix F4 LOW)
**Scope:** Re-audit after W1055/W1050 — find NEW residual issues not caught previously.

---

## W1055/W1058 Merge State

Both PRs are **NOT merged** into `codex/krab-ear-v2` as of 2026-05-26:

- PR #977 (`wire-normalization-profiles-W1055`) — **OPEN** (F1+F2 fixes: CRUD IPC handlers + atomic save)
- PR #975 (`fix-normalization-es-capitalize-W1058`) — **OPEN** (F4 fix: Spanish accented chars)
- PR #971 (`audit-normalization-profiles-W1050`) — **OPEN** (original W1050 audit doc)

Confirmed by `git log` on `codex/krab-ear-v2`: none of the W1055/W1058 commits are present.
The W1050 findings F3 (rule validation), F5 (singleton thread-safety), F6 (schema version)
were designated LOW/INFO and have no dedicated fix PRs — they remain **open**.

**Effect on this re-audit:** All W1050 findings (F1–F6) remain present in `codex/krab-ear-v2`.
This re-audit focuses on NEW issues not identified by W1050.

---

## Summary

5 NEW findings (1 medium, 3 low, 1 informational).

The most significant new issue is that the custom profiles JSON file loaded at startup can silently
override builtin profiles if it contains a builtin name — there is no protection against this in
`_load_custom()`. Additionally, the `add_normalization_profile` IPC handler (once W1055 merges) will
have no test coverage in the dispatch invariant suite, and duplicate rules in a profile are silently
accepted and applied multiple times.

---

## New Findings

### N1 — MEDIUM: `_load_custom()` allows disk file to silently override builtin profiles

**Location:** `core/normalization_profiles.py:225-241`

The `__init__` method loads builtins first, then calls `_load_custom()`:

```python
for raw in _BUILTIN_PROFILES:
    self._profiles[p.name] = p        # builtins loaded first

if data_dir:
    self._load_custom(data_dir)       # disk loaded second — overwrites builtins by name
```

`_load_custom()` iterates the disk JSON and does `self._profiles[p.name] = p` with
`builtin=False` hardcoded. If the custom profiles JSON contains an entry with `name: "clean"`,
it replaces the builtin `clean` profile with the disk version, silently:

```
verbatim after inject: builtin=False, rules=['cleanup_strict'], description='injected'
```

**Confirmed reproducible:** Manually writing `[{"name": "verbatim", ...}]` to
`normalization_profiles.json` completely replaces the builtin `verbatim` profile in the
running registry for that session.

**Attack vector:** If an external process (or a bug in another feature) writes to
`normalization_profiles.json`, a user's builtin profiles can be silently replaced with
incorrect rule sets. The `fix_punctuation` and `capitalize_sentences` steps could be
removed from `formal`, or `cleanup_strict` injected into `verbatim`, degrading STT output.

**Impact:** Medium — requires filesystem access to `data_dir`, so remote exploitation is
not relevant. But a bug in any other code that writes `normalization_profiles.json` (e.g.,
a partial write failure leaving stale JSON from an older schema version) could permanently
corrupt the builtin registry for all affected sessions.

**Fix:** In `_load_custom()`, skip any profile whose name is a known builtin:

```python
BUILTIN_NAMES = frozenset(p["name"] for p in _BUILTIN_PROFILES)

for raw in raw_list:
    if raw.get("name") in BUILTIN_NAMES:
        logger.warning("Disk profile %r conflicts with builtin — skipped", raw.get("name"))
        continue
    p = NormalizationProfile(...)
    self._profiles[p.name] = p
```

---

### N2 — LOW: Duplicate rule names in a profile are silently accepted and applied multiple times

**Location:** `core/normalization_profiles.py:181-201` (`add_profile()`), `NormalizationProfile.apply()` line 133

`add_profile()` calls `rules=list(rules)` — a plain copy with no deduplication. A profile with
`rules=["cleanup_soft", "cleanup_soft", "strip_hallucinations"]` stores and applies both
`cleanup_soft` steps. For idempotent rules this is harmless; for stateful ones it wastes CPU;
for rules like `strip_trailing_period` repeated application is still idempotent. But it
complicates debugging and violates the principle that a profile's rule list is a *set* of
processing steps.

The IPC handler added by W1055 (`add_normalization_profile`) inherits this gap — a Swift caller
that sends duplicate rules will persist them to disk:

```python
# After add_profile('dupe_test', ['cleanup_soft', 'cleanup_soft', 'strip_hallucinations'])
p.rules == ['cleanup_soft', 'cleanup_soft', 'strip_hallucinations']  # confirmed
```

**Fix:** Deduplicate rules while preserving order:

```python
seen = set()
deduped = [r for r in rules if not (r in seen or seen.add(r))]
```

Or add it to the validation step proposed in W1050 F3.

---

### N3 — LOW: No dispatch test coverage for `add_normalization_profile` / `remove_normalization_profile` / `apply_normalization_profile` (pre-W1055 and missing from dispatch invariants)

**Location:** `KrabEar/tests/test_dispatch_complete.py:807-808`, `line 1272`

Currently `test_dispatch_complete.py` tests only `list_normalization_profiles` in the dispatch
invariant. Once W1055 merges, three new handlers are added to `service.py`:
`add_normalization_profile`, `remove_normalization_profile`, `apply_normalization_profile`.

The W1055 commit adds these to a *new* test file (`test_normalization_profiles_ipc_W1055.py`)
but does NOT add them to the dispatch completeness invariant list in `test_dispatch_complete.py`.
Confirmed: `grep add_normalization_profile KrabEar/tests/test_dispatch_complete.py` returns empty.

This means the dispatch invariant suite — which guards that all registered methods are tested —
will not cover the three new handlers. A future wave that accidentally deletes one of these
entries from the dispatch table would pass the invariant check undetected.

**Fix:** After W1055 merges, add to the exhaustive list in `test_dispatch_complete.py`:

```python
"add_normalization_profile",
"remove_normalization_profile",
"apply_normalization_profile",
```

---

### N4 — LOW: `_save_custom()` does not log saved profile count — silent on disk errors that skip the write

**Location:** `core/normalization_profiles.py:243-252`

The `_save_custom()` method catches `Exception` broadly and logs a warning with the error message.
But the **success path** logs nothing at all. Other persistence modules in the codebase
(e.g., `StateStore.compact()`, `VocabularyStore._save()`) log at `DEBUG` level after successful
writes (e.g., `"Saved N items to disk"`).

More critically, the early-return path (`if path is None: return`) is also silent — any caller
using a registry instance created without `data_dir` (e.g., the module-level `add_profile()`
function when called before `BackendService.__init__` sets a `data_dir`) will silently discard
the write with no feedback. The user gets no indication that their custom profile was not persisted.

**Confirmed:** `add_profile()` called on a no-`data_dir` registry succeeds in memory, returns
the profile, but writes nothing — the caller has no way to detect this from the return value.

**Fix:** Log at `DEBUG` on success:
```python
logger.debug("Saved %d custom profiles to %s", len(custom), path)
```
And consider returning a warning in `add_profile()` response when `data_dir is None`:
```python
if self._data_dir is None:
    logger.warning("Registry has no data_dir — profile %r not persisted to disk", name)
```

---

### N5 — INFORMATIONAL: `fix_punctuation` in `formal` profile swallows exceptions silently with no logging

**Location:** `core/normalization_profiles.py:94-98`

```python
if rule == "fix_punctuation":
    try:
        return TextUtils.fix_punctuation(text)
    except Exception:
        return text
```

This is the only rule in `_apply_rule()` with a bare `except Exception: return text`. The intent
is to make `fix_punctuation` fail-safe, but the exception is swallowed without any logging. A
crash in `PunctuationFixer` (e.g., a regex timeout on adversarial input, an import error after
refactor) will silently degrade the `formal` profile to `fix_punctuation`-less output.

This differs from the other rule calls (e.g., `cleanup_soft`, `capitalize_sentences`) which let
exceptions propagate to `apply()` and then to the IPC caller, who can report the error.

Contrast with `_apply_rule()` line 116: unknown rules use `logger.warning(...)` before returning.
The `fix_punctuation` exception handler is the only path with no observability.

**Impact:** Informational — `fix_punctuation` is not called at runtime frequently enough to matter
in most deployments. But if `PunctuationFixer` is refactored and breaks, the `formal` profile will
silently produce incorrectly punctuated output with no error in Sentry or logs.

**Fix:** Add a `logger.warning` inside the except clause:

```python
if rule == "fix_punctuation":
    try:
        return TextUtils.fix_punctuation(text)
    except Exception as exc:
        logger.warning("fix_punctuation failed (returning input unchanged): %s", exc)
        return text
```

---

## Open W1050 Findings (Not Yet Fixed, Confirmed Still Present)

| ID | Severity | Status | Summary |
|----|----------|--------|---------|
| F1 | MEDIUM | PR #977 OPEN | add/remove/apply not wired to IPC |
| F2 | MEDIUM | PR #977 OPEN | `_save_custom()` non-atomic (no tmp+rename) |
| F3 | LOW | No PR | No rule name validation at `add_profile()` time |
| F4 | LOW | PR #975 OPEN | `capitalize_sentences` misses Spanish accented chars |
| F5 | LOW | No PR | `get_registry()` singleton not thread-safe |
| F6 | INFO | No PR | No schema version in persisted JSON |

---

## Positive Observations

- **Builtin recovery on restart:** After `overwrite=True` + `remove_profile()` cycle, a fresh
  registry correctly re-loads the builtin from code (builtins are always seeded from `_BUILTIN_PROFILES`
  at `__init__` time). The overwritten+removed builtin is fully recoverable by creating a new registry.
- **`fix_punctuation` fail-safe is intentional:** The bare except is a deliberate degradation path,
  not an oversight. N5 is about adding observability, not changing the behavior.
- **Rule engine is complete:** All 8 rule names in `_BUILTIN_PROFILES` resolve to valid
  `_apply_rule()` branches. No dangling references.
- **Dispatch table entry exists:** `list_normalization_profiles` is wired and tested in
  `test_dispatch_complete.py`. W1055 adds the remaining 3 handlers correctly.
- **`formal` profile rule order is correct:** `cleanup_soft` → `cleanup_strict` → `fix_punctuation`
  → `capitalize_sentences` is the correct ordering for these transformations.
