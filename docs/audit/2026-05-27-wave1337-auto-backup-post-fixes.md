# Wave 1337 Re-Audit: AutoBackupManager post-W897 residual

**Date:** 2026-05-27
**Auditor:** Claude (W1337 sub-agent)
**Branch:** audit/auto-backup-post-W1337 (off codex/krab-ear-v2 @ 62df2ec9)
**Files examined:**
- `KrabEar/backend/auto_backup.py`
- `KrabEar/backend/settings_backup.py`
- `KrabEar/backend/settings_service.py`
- `KrabEar/backend/history_service.py`
- `KrabEar/backend/recording_core_service.py`
- `KrabEar/backend/ipc_dispatch.py`
- `KrabEar/tests/test_auto_backup.py`
- `KrabEar/tests/test_auto_backup_advanced.py`

---

## W897 Merge State

**PR #821 — MERGED** (2026-05-26T03:30:28Z).

W897 fix applied correctly:
- `auto_backup.py` now imports `SENSITIVE_FIELDS` from `settings_backup` and uses it to
  redact `settings.json` before writing it into the backup directory (lines 18, 140).
- `settings_service.py` now imports `SENSITIVE_FIELDS` from `settings_backup` as its own
  `_SENSITIVE_FIELDS` class attribute (line 411) — unified source of truth across all three modules.
- `settings_backup.py` exports the canonical 9-field `SENSITIVE_FIELDS` frozenset (lines 29–43).

**W891 AB-2 (Medium) — FIXED.** Settings are now redacted before copy.
**W891 SB-2 (Medium) — FIXED.** `SettingsService._SENSITIVE_FIELDS` now equals the 9-field canonical set.

---

## Residual Findings (NEW, post-W897)

### F1 — MED — No test asserts sensitive-field redaction in auto_backup (AB-2 test gap persists)

**File:** `KrabEar/tests/test_auto_backup.py`, `KrabEar/tests/test_auto_backup_advanced.py`

The W891 audit explicitly flagged that no test verifies the auto_backup `settings.json` copy does
NOT contain sensitive values. After W897's code fix, this test gap remains open.

`test_settings_backup.py::TestSettingsBackupCreate::test_sensitive_fields_excluded` covers
`SettingsBackup`, but there is no equivalent assertion in the `AutoBackupManager` test suite. A
regression in the `_do_backup` settings-redaction path (e.g., a rebase dropping the `_SENSITIVE_FIELDS`
import) would be invisible to CI.

**Recommendation:** Add a test that seeds `settings.json` with a known sensitive key (e.g.
`telnyx_api_key`) and asserts that the auto_backup copy of `settings.json` does not contain it.

---

### F2 — MED — `restore_history` re-injects redacted-out settings when `restore_settings=True`

**File:** `KrabEar/backend/history_service.py` lines 2453–2456

`handle_restore_history` with `restore_settings=True` copies `backup_dir/settings.json` verbatim
back to `store.settings_path`. After W897, the auto_backup copy of `settings.json` is *redacted* —
it does not contain any of the 9 sensitive fields. Restoring from this backup therefore **silently
drops all credentials** that were set before the backup (API keys, tokens, DSN).

The user believes they are restoring their settings; in fact they get back a settings file stripped
of all 9 credential fields, with no warning. Any running service (Telnyx, VG, HF, LM Studio, etc.)
will lose its credentials silently until the user re-enters them.

**Recommendation:** Either (a) add a `WARNING: credentials not included in backup` notice to the
`restore_history` response when `restore_settings=True` is requested from an auto-backup path, or
(b) document the redaction policy in the backup manifest (`backup_meta.json`'s `files` list) with a
`settings_redacted: true` flag so callers can surface this to the user.

---

### F3 — LOW — `SB-3` path traversal in `restore_backup` (settings_backup) not fixed

**File:** `KrabEar/backend/settings_backup.py` line 142

W891 finding SB-3 recommended adding `"/" in backup_id or "\\" in backup_id` guard to
`restore_backup`. The current code still has no path-separator validation:

```python
backup_path = self._dir / f"{backup_id}.json"
if not backup_path.exists():
    raise FileNotFoundError(...)
```

A `backup_id` value like `../../etc/passwd` (minus extension) passed via IPC would resolve to a path
outside `_dir`. The `handle_restore_settings_backup` IPC handler performs only `.strip()` on the
raw string (line 547 of `settings_service.py`). Risk is low — the Unix socket requires local user
access — but hardening was explicitly recommended in W891 and remains unimplemented.

**Recommendation:** In `restore_backup`, add before the `backup_path` construction:
```python
if "/" in backup_id or "\\" in backup_id or ".." in backup_id:
    raise ValueError(f"backup_id содержит недопустимые символы: {backup_id!r}")
```

---

### F4 — LOW — Missing `trigger_auto_backup` IPC handler (dead-zone: startup + every 100 recordings)

**File:** `KrabEar/backend/ipc_dispatch.py`, `KrabEar/backend/auto_backup.py`

`check_and_backup()` is called in exactly two places:
1. `BackendService.__init__` at startup (line 562 of `service.py`).
2. Every 100th transcription in `recording_core_service.py` line 1238.

There is no IPC method that lets a client (Swift UI, test harness, admin script) trigger an
immediate auto-backup. `backup_history` (dispatched via `svc._history.handle_backup_history`)
creates a manual one-off copy but does not go through `AutoBackupManager` — it bypasses the 24h
interval, rotation logic, and redacted-settings handling entirely.

Operators cannot force an auto-backup without waiting for startup or 100 transcriptions, limiting
disaster-recovery usability. A simple `"trigger_auto_backup": lambda p: svc._auto_backup.check_and_backup()` entry in `ipc_dispatch.py` would close this gap.

---

### F5 — LOW — `privacy_mode_enabled` not checked before auto-backup execution

**File:** `KrabEar/backend/auto_backup.py` (class `AutoBackupManager`),
`KrabEar/backend/recording_core_service.py` line 1238

When `privacy_mode_enabled=True` the backend suppresses Sentry, skips translation, and avoids
external network calls. Auto-backup is not treated as a privacy-sensitive action and runs
regardless: history, tombstones, and status files are copied to `<data_dir>/backups/`.

If a user enables privacy mode to prevent transcript persistence (e.g. before a confidential
recording), the auto-backup silently preserves a copy of `history.ndjson` in the backups
subdirectory. The `history.ndjson` file contains plain-text transcripts; the backup copy is not
subject to any purge when privacy mode is toggled off.

There is no existing test for the privacy_mode + auto_backup interaction.

**Recommendation:** In `check_and_backup()`, accept an optional `settings_getter` callback (or
check `self.store.get_settings().get("privacy_mode_enabled")` if available) and skip when privacy
mode is active. At minimum document the behaviour in the module docstring.

---

## Summary Table

| ID | Sev | Module | Description | W891 Origin |
|----|-----|--------|-------------|-------------|
| F1 | MED | tests | No test asserts sensitive-field exclusion in auto_backup settings copy | AB-2 test gap (new) |
| F2 | MED | history_service | `restore_settings=True` re-applies redacted settings → silent credential loss | New (W897 side-effect) |
| F3 | LOW | settings_backup | `restore_backup` path traversal (SB-3) not fixed post-W897 | SB-3 carry-forward |
| F4 | LOW | ipc_dispatch | No IPC method to trigger auto-backup on demand | New |
| F5 | LOW | auto_backup | `privacy_mode_enabled=True` does not suppress auto-backup | New |

**W897 status: MERGED, fixes verified. AB-2 and SB-2 are closed.**  
5 new residual findings: 2 medium, 3 low. No critical or high findings.
