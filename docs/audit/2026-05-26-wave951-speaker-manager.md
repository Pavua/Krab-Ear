# Wave 951 — SpeakerManager Audit

**File:** `KrabEar/backend/speaker_manager.py` (328 lines)  
**Tests:** `KrabEar/tests/test_speaker_manager.py` (667 lines, ~50 test cases)  
**Date:** 2026-05-26  
**Auditor:** W951 sub-agent

---

## Summary

5 confirmed findings of varying severity. No critical blockers for current production usage (voice fingerprint is disabled by default), but two issues become active risks when `VOICE_FINGERPRINT_ENABLED=True` is turned on.

---

## Finding 1 — Non-atomic disk writes (MEDIUM)

**Location:** `_save()` lines 81–91, `_save_fingerprints()` lines 115–125

Both persist methods use `Path.write_text()` directly, with no tmp-file+fsync+rename pattern:

```python
self._path.write_text(
    json.dumps(self._aliases, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
```

**Risk:** If the process is killed mid-write (SIGKILL from `BackendSupervisor` during circuit-breaker cooldown, system crash, disk full), the JSON file is left truncated or empty. On the next startup `_load()` will fail silently (exception caught, `_aliases` stays empty) — all speaker name mappings are lost.

**Pattern used everywhere else in the project:** `StateStore` uses `fcntl.flock` + tmp file + `os.replace` (atomic on POSIX). `SettingsBackup` uses similar rolling-backup pattern.

**Fix:** Write to `<path>.tmp`, `fsync`, then `os.replace()` to the target path. Optionally keep one `.bak` copy.

---

## Finding 2 — Voice embeddings stored in plaintext JSON (HIGH — privacy)

**Location:** `_save_fingerprints()` writes to `{data_dir}/speaker_fingerprints.json`

`register_speaker()` serialises a full 512-dimensional pyannote voice embedding as a list of floats and writes it to disk unconditionally, regardless of `privacy_mode_enabled` setting.

**Why this matters:** Voice embeddings are biometric data (unique voice fingerprint). Under GDPR/CCPA they require explicit consent and protection. The file sits unencrypted next to `history.ndjson`.

**Specific gap:** `SpeakerManager.__init__` always calls `_load_fingerprints()` on startup. There is no check of `privacy_mode_enabled` (defined in `DEFAULT_SETTINGS` at `core/config.py:987` and used by `translation_service.py` and `observability.py`). By contrast, Sentry init (`observability.py:122`) and translation (`translation_service.py:96, 201`) both gate on `privacy_mode_enabled`.

**Fix:**
1. Gate `_save_fingerprints()` / `_load_fingerprints()` on `privacy_mode_enabled`.
2. When privacy mode is enabled at runtime, delete the fingerprints file and clear `self._fingerprints` in memory.
3. Add a note to the user-facing docs that voice fingerprint data is biometric.

Note: `VOICE_FINGERPRINT_ENABLED` defaults to `False` (config line 384), so this is dormant in standard installs but active for any user who enables fingerprinting.

---

## Finding 3 — Merge does not update history references (MEDIUM — data integrity)

**Location:** `remove_alias()` / no merge method exists

There is no dedicated `merge_speakers(from_id, into_id)` method. The test `test_merge_speakers` (line 398) and `test_merge_two_profiles` (line 236) simulate merge by calling `remove_alias("SPEAKER_01")` + `delete_fingerprint("SPEAKER_01")`.

After this "merge", history items stored in `StateStore`/`history.ndjson` still contain raw `[SPEAKER_01]` tags. `apply_aliases()` will now return `[SPEAKER_01]` verbatim (no alias) rather than the merged speaker's name.

The test at line 307 explicitly documents this as a design decision:
```python
# в тексте теги SPEAKER_01 остаются нетронутыми
# (merge = переименование сегментов в тексте остаётся задачей верхнего уровня)
```

**Risk:** Every existing transcript that contained `SPEAKER_01` segments will silently lose the human name after a merge. Users renaming duplicate speakers will see old transcripts regress to raw tags.

**Fix:** Add a `merge_speakers(from_id: str, into_id: str)` IPC method that also issues a `StateStore` scan to rewrite `[SPEAKER_01]` → `[SPEAKER_00]` in stored transcript texts, or at minimum documents the limitation clearly in the IPC API reference.

---

## Finding 4 — Sequential predictable speaker IDs (LOW — privacy)

**Location:** `register_speaker()` line 226: `speaker_id = f"Speaker_{self._auto_speaker_counter}"`

IDs are `Speaker_0`, `Speaker_1`, etc. A third party observing IPC calls could infer recording volume and speaker-registration ordering from the counter.

This is low severity because IPC access requires local Unix socket access (already privileged). However, if speaker IDs are ever exposed in shareable exports, sequential IDs leak session metadata.

**Fix:** Use `uuid.uuid4().hex[:8]` or similar for new fingerprint-registered speakers (the `SPEAKER_XX` diarization IDs from pyannote are externally generated and are fine as-is).

---

## Finding 5 — No rate limiting on rename operations (LOW — disk I/O)

**Location:** `set_alias()` → `_save()` on every call

Each `set_alias` call triggers a full JSON serialisation + file write. There is no debounce or write-coalescing. A rapid sequence of renames (e.g. an IPC client calling `set_speaker_alias` 1000× per second) causes 1000 disk writes per second.

`IPCThrottle` (`backend/ipc_throttle.py`) provides per-method token-bucket rate limiting, but `set_speaker_alias` is not wired into it (only the speaker-manager handlers `set_speaker_alias`, `get_speaker_aliases`, `remove_speaker_alias` are in the dispatch table — none appear in the throttle config).

**Fix:** Apply `IPCThrottle` to `set_speaker_alias` and `remove_speaker_alias`, or add a dirty-flag + background-flush timer (write at most once per second).

---

## Non-issues / Items Verified OK

| Check | Status |
|-------|--------|
| Thread safety in-memory | OK — `threading.Lock` wraps all `_aliases` and `_fingerprints` mutations |
| Profile deletion | OK — `remove_alias()` and `delete_fingerprint()` exist and are tested |
| Concurrent rename corruption | OK — `_lock` serialises writes; concurrent rename test passes (line 447) |
| Schema migration | N/A — no version field; format is simple flat dict; risk is negligible at current schema |
| IPC handler completeness (alias) | OK — `set_speaker_alias`, `get_speaker_aliases`, `remove_speaker_alias` wired |
| IPC handler completeness (fingerprint) | PARTIAL — `handle_register_speaker`, `handle_delete_speaker_fingerprint`, `handle_list_speaker_fingerprints` exist in the class but are NOT wired in `service.py` dispatch table (only 3 alias handlers registered at lines 1155–1157). Fingerprint management is unreachable via IPC. |
| Unbounded profile growth | PARTIAL — aliases are deletable; fingerprints are deletable; but `resolve_speaker_for_segment(auto_register=True)` auto-registers every new speaker segment with no cleanup policy |

---

## Test Coverage

**File:** `KrabEar/tests/test_speaker_manager.py` — 667 lines, ~50 test cases across 5 test classes.

Covered well:
- CRUD for aliases (create, read, update, delete, persistence, unicode)
- `apply_aliases` with single/multiple/unknown/repeated speakers
- IPC handlers for alias operations
- Thread safety (concurrent rename)
- Fingerprint register/find/update/delete roundtrip
- `find_matching_speaker` above/below threshold, zero embedding
- Persistence/reload of fingerprints and counter

**Not covered by tests:**
- Atomic write failure (simulate disk-full during `_save`)
- Privacy mode gating of fingerprint storage (no test for `privacy_mode_enabled=True` blocking fingerprint write)
- Merge with subsequent `apply_aliases` on old transcripts (Finding 3 — noted in test as known limitation)
- `resolve_speaker_for_segment` with `auto_register=True` over 1000 calls (unbounded growth)

---

## Priority Recommendations

| # | Finding | Severity | Effort |
|---|---------|----------|--------|
| 2 | Voice embeddings ignore privacy_mode | HIGH | Medium |
| 1 | Non-atomic JSON writes | MEDIUM | Low |
| 3 | Merge orphans history references | MEDIUM | High |
| 4 | Fingerprint IPC handlers unwired | MEDIUM | Low (1-liner in service.py) |
| 5 | No rename rate limiting | LOW | Low |
