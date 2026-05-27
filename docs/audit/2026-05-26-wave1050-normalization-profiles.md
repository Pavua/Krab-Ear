# Wave 1050 Audit: `core/normalization_profiles.py` — NormalizationProfileRegistry

**Date:** 2026-05-26  
**Auditor:** W1050  
**File:** `KrabEar/core/normalization_profiles.py` (283 lines)  
**Scope:** profile composition correctness, ordering stability, user-defined profile validation, persistence atomicity, wire status, test coverage, schema versioning, default profiles correctness.

---

## Summary

6 findings (2 medium, 3 low, 1 informational). No critical bugs. Core logic — rule dispatch, builtin loading, custom persistence — is sound. The module is functionally correct for its primary use case (RU text normalization). Issues centre on missing IPC exposure (add/remove/apply are unwired), non-atomic disk writes, absent rule validation at profile creation time, a stale singleton pattern, and accented-character blind spots in `capitalize_sentences`.

---

## Findings

### F1 — MEDIUM: `add_profile` / `remove_profile` / `apply_profile` are not wired to IPC

**Location:** `KrabEar/backend/ipc_dispatch.py:177`, `KrabEar/backend/service.py:1140-1142`

Only `list_normalization_profiles` is exposed via IPC. The three mutation methods (`add_profile`, `remove_profile`, `apply_profile`) exist on the registry but have no corresponding IPC handlers. Swift callers cannot create, delete, or apply normalization profiles at all — only list them.

The module-level `apply_profile()` / `add_profile()` convenience functions are also unreachable from the Swift UI layer, making them effectively dead code in production.

**Impact:** The normalization profiles system is read-only from the Swift agent. Any UI that would let users create custom profiles or apply a profile to a transcript text is blocked at the IPC layer.

**Fix:** Wire `add_normalization_profile`, `remove_normalization_profile`, and `apply_normalization_profile` IPC handlers in `ipc_dispatch.py`, delegating to `self._norm_profiles`.

---

### F2 — MEDIUM: `_save_custom()` is non-atomic — power loss or crash corrupts the file

**Location:** `core/normalization_profiles.py:243-252`

```python
path.write_text(json.dumps(custom, ...), encoding="utf-8")
```

`Path.write_text()` is a direct truncate-then-write. If the process crashes or loses power mid-write, the file is left truncated/empty. On next startup, `_load_custom()` will silently catch the `json.JSONDecodeError` and log a warning — all user-defined profiles are lost.

The `StateStore` (history.ndjson) and `settings.json` use the same non-atomic pattern, so this is consistent across the codebase. However, `normalization_profiles.json` is the only file where full data loss means user-defined workflow configuration is silently dropped.

**Fix:** Write to a `.tmp` sibling file, then `os.replace()` (POSIX rename — atomic on same filesystem):
```python
tmp_path = path.with_suffix(".tmp")
tmp_path.write_text(json.dumps(custom, ...), encoding="utf-8")
tmp_path.replace(path)
```

---

### F3 — LOW: No rule name validation at `add_profile()` time — unknown rules log warnings at apply time only

**Location:** `core/normalization_profiles.py:181-201`, `core/normalization_profiles.py:116`

`add_profile()` accepts any list of rule name strings without checking them against the known rule set. Unknown rules produce a `logger.warning(...)` at `apply()` time — silently ignored at profile creation. A user who typos `"cleanup_sof"` instead of `"cleanup_soft"` gets a profile that does nothing to the text, with no feedback at save time.

Known valid rules: `strip_hallucinations`, `cleanup_soft`, `cleanup_strict`, `normalize_entities`, `fix_punctuation`, `capitalize_sentences`, `strip_trailing_period`, `wrap_lines_42`.

**Fix:** Add a `_VALID_RULES` frozenset and validate in `add_profile()`:
```python
_VALID_RULES: frozenset[str] = frozenset({
    "strip_hallucinations", "cleanup_soft", "cleanup_strict",
    "normalize_entities", "fix_punctuation", "capitalize_sentences",
    "strip_trailing_period", "wrap_lines_42",
})

def add_profile(self, name, rules, description="", *, overwrite=False):
    unknown = [r for r in rules if r not in _VALID_RULES]
    if unknown:
        raise ValueError(f"Неизвестные правила нормализации: {unknown}")
    ...
```

---

### F4 — LOW: `capitalize_sentences` regex misses Spanish/accented sentence-initial characters

**Location:** `core/normalization_profiles.py:25`, `_apply_rule()` lines 100-107`

```python
_RE_CAPITALIZE_SENT = re.compile(r"(?:^|(?<=[.!?…])\s+)([а-яa-z])")
```

The character class `[а-яa-z]` covers Cyrillic lowercase and ASCII lowercase, but misses Spanish lowercase accented letters (`á`, `é`, `í`, `ó`, `ú`, `ñ`) that appear at the start of a sentence after a period. For example:

```
"hola. énfasis al inicio"  →  "Hola. énfasis al inicio"  # 'é' not capitalized
```

Since the project is explicitly RU/ES primary (CLAUDE.md), this silently under-capitalizes Spanish text in the `formal` profile.

**Fix:** Extend the character class to include Spanish lowercase characters, or use `re.UNICODE` with `[^\W\d_]` matching approach:
```python
_RE_CAPITALIZE_SENT = re.compile(
    r"(?:^|(?<=[.!?…])\s+)([а-яa-záéíóúñüà])", re.UNICODE
)
```

---

### F5 — LOW: `get_registry()` singleton is not thread-safe and resets on any `data_dir` argument

**Location:** `core/normalization_profiles.py:260-265`

```python
def get_registry(data_dir: Path | None = None) -> NormalizationProfileRegistry:
    global _default_registry
    if _default_registry is None or data_dir is not None:
        _default_registry = NormalizationProfileRegistry(data_dir=data_dir)
    return _default_registry
```

Two problems:

1. **Thread race:** Two threads calling `get_registry()` simultaneously when `_default_registry is None` each create a new instance; the second write wins. For the module-level convenience functions (`apply_profile()`, `list_profiles()`), this means the singleton can be replaced mid-call with a fresh registry that hasn't loaded custom profiles yet.

2. **Singleton resets on any `data_dir` arg:** Every call with a non-`None` `data_dir` replaces `_default_registry`. The `BackendService.__init__` correctly uses the instance-level `self._norm_profiles` (not the singleton), so production is unaffected. But the module-level functions (`apply_profile`, `add_profile`) go through the singleton and will use whichever `data_dir` was last passed to `get_registry()`.

**Fix:** Guard with a `threading.Lock` and only create once if `data_dir` matches:
```python
_registry_lock = threading.Lock()

def get_registry(data_dir: Path | None = None) -> NormalizationProfileRegistry:
    global _default_registry
    with _registry_lock:
        if _default_registry is None or data_dir is not None:
            _default_registry = NormalizationProfileRegistry(data_dir=data_dir)
    return _default_registry
```

---

### F6 — INFORMATIONAL: No schema version in persisted `normalization_profiles.json`

**Location:** `core/normalization_profiles.py:243-252`, `_load_custom()` lines 225-241`

The custom profiles file is a plain JSON array of profile dicts. There is no schema version field. If the rule names or profile structure changes in a future wave, there is no migration path — the old JSON will silently load with whatever fields exist (missing keys fall back to defaults via `.get()`).

Other persistence files in the codebase (settings.json via `SettingsValidator`) have schema versioning and migration logic. Normalization profiles has neither.

**Impact:** Low — the current schema is simple and stable. Risk increases if rules become parameterised (e.g., `wrap_lines_42` → `wrap_lines:N`).

**Fix:** Add a `"schema_version": 1` top-level wrapper:
```json
{"schema_version": 1, "profiles": [...]}
```
And update `_load_custom()` / `_save_custom()` accordingly.

---

## Positive Findings

- **Profile composition order is correct:** `formal` profile runs `cleanup_soft` before `cleanup_strict` (indices 1 and 2), which is the required order per `TextUtils` semantics.
- **Ordering stability:** `list_profiles()` returns profiles in deterministic insertion order (builtins first, then custom by load order) — Python 3.7+ dict insertion order guarantee.
- **Builtin protection is solid:** `builtin=True` flag in disk JSON is ignored at load time — `_load_custom()` hardcodes `builtin=False` for all disk-loaded profiles. No injection path.
- **`overwrite=True` semantics:** When a builtin is overwritten, the original builtin is replaced in memory and the overridden version (as non-builtin) is saved to disk. On restart, disk custom is loaded after builtins, correctly taking precedence. Logically consistent, though no "reset to builtin" path exists.
- **Test coverage is good:** `KrabEar/tests/test_normalization_profiles.py` covers all 5 builtins, custom CRUD, persistence round-trip, concurrency (5 threads), unknown profile ValueError, empty name ValueError, and Wave 130 spec cases. Dispatch invariant test also present in `test_dispatch_invariants_wave790_full.py`.
- **All rule implementations resolve correctly:** All 8 rule names in `_apply_rule()` map to existing `TextUtils` methods. No dangling references.
