# Wave 891 Audit: AutoBackupManager + SettingsBackup

**Date:** 2026-05-26  
**Auditor:** Claude (wave891/conflict-triage)  
**Files:** `KrabEar/backend/auto_backup.py`, `KrabEar/backend/settings_backup.py`  
**Focus:** Scheduling correctness · Copy limit / rotation · Sensitive-field redaction

---

## Executive Summary

Both modules are well-implemented and cover their stated scope. Five findings are noted — two medium-severity and three low — none are regressions or security breaches. The most actionable gap is a **sensitive-field divergence** between `SettingsBackup._SENSITIVE` and `SettingsService._SENSITIVE_FIELDS`, which means `auto_backup` copies raw `settings.json` (containing any persisted key) while `SettingsBackup` redacts the same keys correctly.

---

## 1. AutoBackupManager (`backend/auto_backup.py`)

### 1.1 Scheduling correctness — PASS

- `check_and_backup()` uses `datetime.now(timezone.utc)` on both sides of the elapsed-hours comparison.
- Naive timestamps (missing tzinfo) loaded from meta are normalised to UTC via `replace(tzinfo=timezone.utc)` before comparison (line 185).
- Corrupted or missing `last_backup_ts` falls through to perform a backup — safe fail-open behaviour.
- Called once at `BackendService.__init__` (line 513 of `service.py`) — opportunistic, not periodic. Works as documented.

**No issues.**

### 1.2 Copy limit / rotation — PASS with one caveat

- `_prune_old_backups()` sorts by directory name (`auto_backup_YYYYMMDD_HHMMSS`) alphabetically ascending, which is equivalent to chronological order — correct.
- Slice `backups[:max(0, len(backups) - max_copies)]` is correct; keeps newest N directories.
- Default `AUTO_BACKUP_MAX_COPIES = 7` is applied at `BackendService` construction (line 354).

**FINDING AB-1 (Low) — Prune runs after `_do_backup` but before `_save_meta`.**  
If `_save_meta` throws (e.g. disk full), the backup directory exists and the copy count could temporarily exceed `max_copies` by one on the next call, because `last_backup_ts` was not persisted and the backup will be retried. The retry will create another directory before pruning. This is a very narrow race; the next successful `_save_meta` restores invariant.

### 1.3 Sensitive-field redaction — MEDIUM FINDING

`_do_backup()` copies `self.store.settings_path` (i.e. `settings.json`) verbatim via `shutil.copy2` (line 127). This file is written by `StateStore.save_settings()` and may contain *any* key that was passed to `set_settings`, including:

- `telnyx_api_key`, `twilio_account_sid`, `twilio_auth_token` — telephony credentials  
- `sentry_dsn` — error-reporting DSN  
- `hf_token`, `voice_gateway_api_key`, `lm_studio_api_key`, `rest_api_key`  
- `stt_gigaam_hf_token`  

**FINDING AB-2 (Medium) — `auto_backup` copies raw `settings.json` without redaction.**  
The backup directory produced by `AutoBackupManager` is stored in `<data_dir>/backups/auto_backup_*`, which is under the user's home directory but is readable by any process running as the same user. A secret stored in `settings.json` (e.g. a Telnyx API key set via IPC) will appear in plaintext in every auto-backup.

**Contrast:** `SettingsBackup.create_backup()` correctly redacts all 9 sensitive fields (lines 27–41 of `settings_backup.py`) before writing its rolling snapshots.

**Recommendation:** Before copying `settings_path`, load and redact it the same way `SettingsBackup` does, or reuse `SettingsBackup.create_backup()` for the settings file portion of the auto-backup.

### 1.4 Atomicity

Backup directories are created with `mkdir(parents=True, exist_ok=True)` and files copied individually. There is no atomic rename of the entire directory — a crash mid-copy leaves a partial directory. However:
- `_list_auto_backups()` includes these partial directories.
- `_prune_old_backups()` will eventually delete them when over limit.
- Tests in `test_auto_backup_advanced.py` explicitly cover this scenario (TestAutoBackupPartialFileCleanup).

This is an acceptable trade-off; no restore path uses `AutoBackupManager` directories directly (restore is in `HistoryService.handle_restore_history`).

### 1.5 Thread safety — PASS

Single `threading.Lock()` wraps all state-mutating paths in `check_and_backup()` and `get_auto_backup_status()`. `_load_meta()` itself is not locked but is called only inside the lock in the public API.

---

## 2. SettingsBackup (`backend/settings_backup.py`)

### 2.1 Scheduling / trigger correctness — PASS

- `create_backup()` is called in `SettingsService.handle_set_settings()` (line 119) *before* applying the new settings — correct pre-write snapshot.
- Manual backup via `handle_create_manual_settings_backup` is caller-triggered; no scheduling needed.

### 2.2 Copy limit / rotation — PASS

- `MAX_BACKUPS = 10` enforced by `_prune()` after every `create_backup()`.
- Prune sorts by filename ascending (lexicographic = chronological given `YYYYMMDDTHHMMSSz` prefix) and deletes the oldest excess files.
- `list_backups()` sorts descending (newest first) — consistent with intent; separate sort from `_prune()`.

**FINDING SB-1 (Low) — `_prune()` is not thread-safe.**  
`SettingsBackup` has no internal lock. Two concurrent `handle_set_settings` IPC calls can race in `_prune()`:
1. Both observe 11 files.
2. Both compute `excess = 1`.
3. Both try to `unlink` the same oldest file.
4. The second `unlink` catches `OSError` (already deleted) and logs a warning — harmless but noisy.

Given that IPC is single-threaded in `BackendService` (one handler at a time), this race is theoretical but could manifest if `SettingsBackup` is used elsewhere in the future.

### 2.3 Sensitive-field redaction — PASS with caveat

`_SENSITIVE` frozenset covers 9 fields (lines 27–41):
```
voice_gateway_api_key, hf_token, rest_api_key, lm_studio_api_key,
telnyx_api_key, twilio_account_sid, twilio_auth_token,
sentry_dsn, stt_gigaam_hf_token
```

This is a superset of `SettingsService._SENSITIVE_FIELDS` (4 fields only: `voice_gateway_api_key`, `hf_token`, `rest_api_key`, `lm_studio_api_key`).

**FINDING SB-2 (Medium) — `SettingsService._SENSITIVE_FIELDS` is a strict subset of `SettingsBackup._SENSITIVE`.**  
`handle_export_settings` uses `_SENSITIVE_FIELDS` (4 fields) to filter. This means `telnyx_api_key`, `twilio_account_sid`, `twilio_auth_token`, `sentry_dsn`, and `stt_gigaam_hf_token` would appear in an exported settings file even if the user explicitly chose to export. This is separate from the backup path but represents inconsistent redaction posture.

**Recommendation:** Align `SettingsService._SENSITIVE_FIELDS` with `SettingsBackup._SENSITIVE` (9 fields), or extract a single source-of-truth frozenset shared by both modules.

### 2.4 `restore_backup` path traversal — PASS

`backup_id` is used to construct `self._dir / f"{backup_id}.json"`. A malicious `backup_id` containing `../` would resolve to a path outside `_dir`. However:
- The IPC handler (`handle_restore_settings_backup`) calls `str(params.get("backup_id", "")).strip()` with no sanitization of path components.
- `Path` resolution would allow traversal if `backup_id = "../../etc/passwd"`.

**FINDING SB-3 (Low) — `restore_backup` does not validate `backup_id` for path components.**  
Exploiting this requires a malicious IPC client on the Unix socket, which itself requires local user access. The risk is low in practice but worth hardening.

**Recommendation:** Add `if "/" in backup_id or "\\" in backup_id: raise ValueError(...)` in `restore_backup`.

---

## 3. Test Coverage

| File | Tests | Areas covered |
|------|-------|---------------|
| `test_auto_backup.py` | 20 | Defaults, skip/run logic, pruning, status, concurrency |
| `test_auto_backup_advanced.py` | 12 | Thread loop, partial dirs, permission errors |
| `test_settings_backup.py` | 20 | Create, list, prune, restore, IPC handlers, concurrency |
| `test_backup_restore.py` | 15 | HistoryService backup/restore (separate path) |

**Gap:** No test asserts that `auto_backup`'s copy of `settings.json` does NOT contain sensitive values (finding AB-2). `test_settings_backup.py::TestSettingsBackupCreate::test_sensitive_fields_excluded` correctly covers `SettingsBackup` but not `AutoBackupManager`.

---

## 4. Summary of Findings

| ID | Severity | Module | Description |
|----|----------|--------|-------------|
| AB-1 | Low | auto_backup | Prune before meta-save: transient over-count on `_save_meta` failure |
| AB-2 | Medium | auto_backup | Raw `settings.json` (may contain secrets) copied verbatim — no redaction |
| SB-1 | Low | settings_backup | `_prune()` not thread-safe — concurrent calls may double-unlink oldest file |
| SB-2 | Medium | settings_backup | `_SENSITIVE_FIELDS` in `SettingsService` (4 fields) diverges from `SettingsBackup._SENSITIVE` (9 fields); `handle_export_settings` leaks 5 credential types |
| SB-3 | Low | settings_backup | `backup_id` not validated for path separators — theoretical local traversal |

No critical findings. No data loss vectors identified. The most impactful fix is AB-2 (redact settings before copying in auto-backup) and SB-2 (align export redaction with backup redaction).
