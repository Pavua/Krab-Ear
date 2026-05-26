# Audit W1339 — ConfigPresetsLibrary

**Date:** 2026-05-27
**Branch:** audit/config-presets-library-W1339
**File audited:** `KrabEar/backend/config_presets_library.py` (374 lines)
**Test file:** `KrabEar/tests/test_config_presets_library.py` (364 lines)

---

## Summary

ConfigPresetsLibrary manages 5 built-in config presets (interview, meeting, voice_memo,
language_practice, podcast) plus user-created custom presets persisted in
`{data_dir}/config_presets.json`. The module is overall well-structured: builtin-override
prevention is correct, concurrency guard covers critical mutations, and test coverage is
thorough. Five findings below, two of which are significant gaps.

---

## Findings

### F1 — MEDIUM: `apply_config_preset` returns patch only; caller must separately call `set_settings` (no atomicity guarantee)

**Location:** `config_presets_library.py:343-353`, `service.py:1090`

`handle_apply_config_preset` returns `{"name": name, "settings_patch": patch}` — it does
**not** write the patch to settings. The IPC caller (Swift UI or external client) receives
the patch and must issue a second `set_settings` IPC call to actually apply it. This creates
a two-step non-atomic apply: the preset is returned but settings are not changed atomically
in a single IPC round-trip.

By contrast, `apply_profile_preset` in `SettingsService.handle_apply_profile_preset`
(line 312–344) performs a full read-modify-write-invalidate-emit cycle in a single IPC call:
reads cached settings, merges the preset, saves, invalidates cache, and emits
`preset.changed` via EventBus.

No Swift caller for `apply_config_preset` was found in any `.swift` file — suggesting the
feature is either unused or the client does its own two-step dance. If a process crash or
network interruption occurs between the two IPC calls, the preset appears "applied" to the
caller but settings remain unchanged.

**Recommendation:** Add a `handle_apply_config_preset_and_save` variant (or update the
existing handler) that mirrors `handle_apply_profile_preset`: read cached settings, merge
the `settings_patch`, call `store.save_settings`, invalidate settings cache, emit
`preset.applied` event, and return the merged settings. The current no-write variant can be
kept as `get_config_preset_patch` for dry-run/preview use.

---

### F2 — MEDIUM: Three public methods (`delete_preset`, `export_preset`, `import_preset`) have no IPC handlers wired in `service.py`

**Location:** `config_presets_library.py:246,270,314`; `service.py:1089-1091`

The dispatch table in `service.py` wires only three IPC methods:

```
"list_config_presets"   → handle_list_config_presets
"apply_config_preset"   → handle_apply_config_preset
"create_config_preset"  → handle_create_config_preset
```

The class provides fully implemented methods `delete_preset`, `export_preset`, and
`import_preset` — complete with validation and tests — but has no corresponding
`handle_*` IPC wrappers on the class **and** no wiring in `service.py`. A client cannot
delete, export, or import presets via IPC at all.

The test file `ConfigPresetsLibraryIPCTestCase` covers only the three wired handlers,
confirming the gap is not just a wiring omission but the entire `handle_delete/export/import`
tier is absent.

**Recommendation:** Add `handle_delete_config_preset`, `handle_export_config_preset`, and
`handle_import_config_preset` methods to the class (mirroring the pattern of
`handle_create_config_preset`), and wire them in `service.py`.

---

### F3 — LOW: `_save()` writes directly (non-atomic); partial write on crash corrupts preset file

**Location:** `config_presets_library.py:151-161`

`_save()` calls `self._presets_path.write_text(...)` directly. If the process crashes
mid-write, the file is left truncated/corrupt. On next startup, `_load()` will catch the
`json.JSONDecodeError` and log a warning, silently dropping **all custom presets**.

`StateStore` (the canonical persistence layer in this project) uses write-to-tmp then
`tmp_path.replace(final_path)` for atomic writes (`state_store.py:144-149, 849-856`).

**Recommendation:** Follow the established pattern:
```python
tmp = self._presets_path.with_suffix(".json.tmp")
tmp.write_text(json.dumps(payload, ...), encoding="utf-8")
tmp.replace(self._presets_path)
```

---

### F4 — LOW: `settings_patch` keys are not validated against known settings keys

**Location:** `config_presets_library.py:228-229`, `create_preset`, `import_preset`

`create_preset` validates only that `settings_patch` is a `dict`. It does not check that
the keys are valid setting names recognised by `SettingsService` / `SettingsValidator`. A
custom preset can store arbitrary unknown keys (e.g. `{"typo_qualiy_profile": "max"}`).
When the caller applies the patch via `set_settings`, `SettingsValidator` will accept it
(it does not reject unknown keys — it only validates known enum/range fields), so the typo
silently has no effect.

`SettingsValidator` in `backend/settings_validator.py` operates on known fields only
(`_ENUM_FIELDS`, `_RANGE_FIELDS`) and passes unknown keys through unchanged — it cannot
catch the silent mis-key.

**Recommendation:** Either (a) emit a warning log for keys not present in
`DEFAULT_SETTINGS` at create/import time, or (b) document this as intentional forward-
compatibility. Not a blocking issue but can cause silent mis-configuration in production.

---

### F5 — LOW: `delete_preset` checks `_BUILTIN_PRESETS` outside the lock (minor TOCTOU; cosmetic in practice)

**Location:** `config_presets_library.py:323-324`

```python
def delete_preset(self, name: str) -> bool:
    if name in _BUILTIN_PRESETS:          # ← outside self._lock
        raise ValueError(...)
    with self._lock:
        ...
```

The builtin check at line 323 reads the module-level `_BUILTIN_PRESETS` dict before
acquiring `self._lock`. While `_BUILTIN_PRESETS` is effectively immutable (no code path
modifies it at runtime), the inconsistency is worth noting: `create_preset` (line 224)
performs the same builtin guard **before** acquiring the lock too. `delete_preset` would be
more consistent if the guard were inside the lock block, matching the lock scope used for
the actual mutation. In the unlikely event a future refactor makes builtins mutable, the
current pattern would introduce a genuine TOCTOU race.

**Recommendation:** Move the builtin guard inside `with self._lock:` for consistency.
Low-priority cosmetic fix.

---

## Not-a-finding: Builtin-override prevention

`create_preset` (line 224) correctly rejects names matching any `_BUILTIN_PRESETS` key with
`ValueError`. `import_preset` delegates to `create_preset`, so the check applies to the
import path too. The `get_built_in_presets` static method returns shallow copies, preventing
mutation of the module-level dict. Builtin protection is solid — no equivalent of the W1272
normalization_profiles gap found here.

## Not-a-finding: W1308 settings hook interaction

`ConfigPresetsLibrary` does not register any `register_after_save_hook`. The module is
purely a preset store; it does not subscribe to live settings changes. This is correct —
presets are applied on demand, not reactively. No integration issue with W1308 hooks.

## Not-a-finding: Test coverage quality

The test suite (364 lines, 6 test classes, ~35 test methods) is comprehensive: builtin
queries, custom CRUD, persistence round-trip, export/import with invalid inputs, and all
three wired IPC handlers. The gap is structural (F2 missing handlers), not a test
quality issue.

---

## Statistics

| Metric | Value |
|--------|-------|
| Source lines | 374 |
| Test lines | 364 |
| Builtin presets | 5 |
| IPC handlers wired | 3 of 6 |
| Test classes | 6 |
| Findings | 5 (2 MEDIUM, 3 LOW) |
