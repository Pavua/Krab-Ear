# Audit: settings_backup.py — W929
**Date:** 2026-05-26  
**File:** `KrabEar/backend/settings_backup.py`  
**Auditor:** W929 (read-only, 5-7 findings cap)

---

## Summary

`SettingsBackup` is a clean, focused module (217 LOC). Five issues found: one security gap (path traversal), one data-integrity gap (non-atomic write), one correctness gap (restore bypasses validation and reintroduces redacted-field gaps), one divergence between two `SENSITIVE` sets, and one test assertion mismatch (test claims atomicity the implementation does not provide).

---

## Findings

### F1 — Path Traversal in `restore_backup` (MEDIUM)
**Location:** `settings_backup.py:137–141`

`restore_backup(backup_id)` constructs the path as:
```python
backup_path = self._dir / f"{backup_id}.json"
```
There is no check that `backup_path` remains under `self._dir`. A caller supplying `backup_id = "../../etc/passwd"` resolves to `/etc/passwd.json`. The method then opens and JSON-parses it. While the IPC handler does `str(params.get("backup_id", "")).strip()`, there is no `is_relative_to` or `resolve()` guard.

**Fix:** Add `backup_path.resolve().is_relative_to(self._dir.resolve())` check before opening, raising `ValueError` on escape.

---

### F2 — Non-Atomic Backup Write (LOW-MEDIUM)
**Location:** `settings_backup.py:94–95`

`create_backup` writes directly to the final path:
```python
with out_path.open("w", encoding="utf-8") as fh:
    json.dump(safe, fh, ensure_ascii=False, indent=2)
```
No `fsync`, no tmp+rename. A crash mid-write leaves a corrupt `.json` file that will silently surface as `list_backups` metadata (key count = 0) or a `ValueError` on restore.

The test `TestSettingsBackupAtomicWrite.test_atomic_write_no_partial_file_left` passes only because it checks for `.tmp` residue — but the implementation never creates `.tmp` files, so the assertion is trivially true while the underlying risk (partial write) is untested and unaddressed.

**Fix:** Write to `out_path.with_suffix(".tmp")`, call `fh.flush(); os.fsync(fh.fileno())`, then `os.replace(tmp_path, out_path)`.

---

### F3 — Restore Bypasses Settings Validation; Redacted Fields Return as Absent (MEDIUM)
**Location:** `settings_service.py:530–532`

`handle_restore_settings_backup` calls:
```python
restored = self._backup.restore_backup(backup_id)
self.store.save_settings(restored)
self.invalidate_cache()
```

Two sub-issues:

**3a — No schema validation before save.** `handle_set_settings` runs 30+ normalization and coercion steps (enum checks, range clamps, bool coercion). `handle_restore_settings_backup` skips all of them, writing whatever is in the JSON file directly to `settings.json`. A backup from a schema-v1.x file or a hand-edited backup could persist invalid enum values.

**3b — Sensitive fields become absent, not empty-string.** Backups strip sensitive fields (e.g. `voice_gateway_api_key`, `sentry_dsn`) entirely. On restore, those keys are missing from `restored`. `store.save_settings` writes a settings.json without them, so the next read returns `DEFAULT_SETTINGS` fallback values (empty strings). This is safe for secrets (no plaintext exposure) but means restoring from backup silently drops all configured API keys, giving no user warning.

**Fix for 3a:** Run `restored` through `SettingsValidator.validate()` before `save_settings`. **Fix for 3b:** Log a warning listing how many sensitive fields were absent from the restored backup.

---

### F4 — `_SENSITIVE` in `settings_backup.py` and `_SENSITIVE_FIELDS` in `settings_service.py` Are Divergent Sets (LOW)
**Location:** `settings_backup.py:27–41`, `settings_service.py:386–391`

`settings_backup._SENSITIVE` (9 fields):
```
voice_gateway_api_key, hf_token, rest_api_key, lm_studio_api_key,
telnyx_api_key, twilio_account_sid, twilio_auth_token, sentry_dsn,
stt_gigaam_hf_token
```

`settings_service._SENSITIVE_FIELDS` (4 fields):
```
voice_gateway_api_key, hf_token, rest_api_key, lm_studio_api_key
```

`handle_export_settings` uses `_SENSITIVE_FIELDS` — so exported settings files include `telnyx_api_key`, `twilio_account_sid`, `twilio_auth_token`, `sentry_dsn`, and `stt_gigaam_hf_token` in plaintext. These are the Telnyx/Twilio credentials and the Sentry DSN with embedded credentials. The backup is more conservative than the export path.

Note: W897 unified the constant within `settings_backup.py` itself; this finding is about the `settings_service._SENSITIVE_FIELDS` not being updated to match.

**Fix:** Extend `settings_service._SENSITIVE_FIELDS` to include the five additional fields present in `settings_backup._SENSITIVE`, or import `_SENSITIVE` from `settings_backup` and reuse it in both places.

---

### F5 — Timestamp Collision Overwrites Same-Second Backups (LOW)
**Location:** `settings_backup.py:89–91`

Backup filename format is `%Y%m%dT%H%M%SZ_{reason}` — second-level resolution. Two concurrent `handle_set_settings` IPC calls arriving within the same second and with the same `reason` produce the same `backup_id`, causing `out_path.open("w")` to silently overwrite the first backup. The `test_concurrent_backup_safe` test uses `threading` but does not assert that all N files are present, only that no exception was raised and `len(files) <= MAX_BACKUPS`.

**Fix:** Append a microsecond component (`%f`) or a monotonic counter to the timestamp string, making collisions practically impossible.

---

## Non-findings (checked, clear)

- **Rotation count (MAX_BACKUPS=10):** bounded, `_prune()` runs after every `create_backup`. No unbounded growth.
- **Lock interaction:** `handle_set_settings` calls `create_backup` before acquiring any store lock; the backup itself holds no lock, so no deadlock risk. Concurrency is handled at the OS level (separate file per backup).
- **Privacy mode interaction:** no privacy-mode flag in `SettingsService` or `SettingsBackup`; backups are always created and always redact `_SENSITIVE` — acceptable given the backup dir is under the user's `~/Library` and is not synced by default.
- **Test coverage:** `test_settings_backup.py` covers create, list, prune, restore round-trip, auto-prune, concurrent safety, and Unicode. Coverage is good for the happy path; gaps are in the areas identified in F1 (path traversal), F2 (partial write atomicity), and F5 (collision).
