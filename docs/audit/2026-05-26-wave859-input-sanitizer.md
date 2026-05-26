# Wave 859 — Input Sanitizer Audit: IPC Param Validation Coverage

**Date:** 2026-05-26  
**Scope:** `KrabEar/backend/input_sanitizer.py` + all IPC handlers in `service.py` and extracted service files  
**Findings:** 7

---

## 1. Overview

`InputSanitizer` (`backend/input_sanitizer.py`, 172 lines) provides three public methods:

| Method | What it does |
|---|---|
| `sanitize_params(method, params)` | Iterates dict, dispatches each value by key |
| `sanitize_string(s, max_length)` | Strip, remove `\x00-\x08\x0b\x0c\x0e-\x1f\x7f`, truncate at 10 000 chars |
| `sanitize_path(p, allowed_dirs)` | Reject relative paths, resolve symlinks, enforce `is_relative_to` against allowed dirs |

**Critical gap: `sanitize_params` is never called from any handler.** Searching the entire `KrabEar/` tree for `InputSanitizer`, `sanitize_params`, and `input_sanitizer` returns only the class definition and test files — zero production call sites. The sanitizer exists as a standalone utility with no wiring into `handle_request` or any extracted service.

---

## 2. What the Sanitizer Covers (when called directly)

### 2.1 Path fields (`_PATH_FIELDS`)

Nine field names are declared as path fields and would be validated via `sanitize_path`:

```
path, file_path, audio_path, import_path, export_path,
backup_path, output_path, transcript_path, ndjson_path
```

`sanitize_path` uses `Path.is_relative_to()` (Python 3.9+) — correct.

### 2.2 Numeric fields (`_NUMERIC_FIELDS`)

Ten fields with `(min, max, coerce_type)` tuples:
`page`, `page_size`, `limit`, `offset`, `days`, `confidence_threshold`, `duration_seconds`, `max_items`, `min_confidence`, `max_confidence`.

### 2.3 Short string fields (`_SHORT_STRING_FIELDS`)

Nine fields with reduced length limits (64–512 chars instead of the default 10 000):
`method`, `id`, `item_id`, `speaker`, `lang`, `source_lang`, `target_lang`, `profile`, `preset`, `format`.

### 2.4 List truncation

Lists longer than 1 000 elements are truncated; items are recursively sanitized.

### 2.5 Nested dict recursion

Dict values are recursively processed by `_sanitize_value`.

---

## 3. Findings

### FINDING-1 (HIGH): Sanitizer not wired — zero IPC coverage

**File:** `backend/service.py`, `handle_request` (line 888)

`handle_request` extracts `params = payload.get("params", {})` and passes it **raw** to every handler. There is no call to `sanitize_params` before or after dispatch. All 307 registered IPC methods receive unvalidated params.

**Impact:** All protections described in Section 2 are effectively dead in production.

---

### FINDING-2 (HIGH): Path traversal in `audio_analytics_service.py` — no bounds check

**File:** `backend/audio_analytics_service.py`, handlers `handle_analyze_quality`, `handle_analyze_silence`, `handle_profile_noise`, `handle_get_audio_info`, `handle_check_audio_fingerprint`

These handlers do:
```python
file_path = params.get("file_path", "")
path = Path(file_path).expanduser()
```
No allowed-dirs check, no `is_relative_to` guard. An attacker can pass `file_path="/etc/passwd"` or any path on the filesystem. The file is then opened and read via `soundfile.read()` or `analyze_file()`.

---

### FINDING-3 (HIGH): Path traversal in `transcription_queue.py` — no bounds check

**File:** `backend/transcription_queue.py`, `handle_enqueue` (line 319)

```python
file_path = str(params.get("file_path", "")).strip()
job_id = self.enqueue(file_path=file_path, ...)
```
No path validation. The file is subsequently opened for audio transcription, allowing arbitrary filesystem reads.

---

### FINDING-4 (MEDIUM): Inconsistent path check pattern in `recording_core_service.py` and `history_service.py`

**Files:** `backend/recording_core_service.py` (lines 301–308, 490–497, 1268–1272), `backend/history_service.py` (lines 265–267)

These files do implement an allowed-dirs check, but use `str(resolved).startswith(str(root))` instead of `resolved.is_relative_to(root)`. The `startswith` approach is vulnerable to a prefix collision bypass:

```python
# Example:
root = Path("/Users/pablito")
evil = Path("/Users/pablitoevil/secret.txt")
str(evil).startswith(str(root))  # → True  (WRONG)
evil.is_relative_to(root)        # → False (correct)
```

The sanitizer's own `sanitize_path` correctly uses `is_relative_to`, but the handlers bypass the sanitizer and implement their own weaker check.

---

### FINDING-5 (MEDIUM): Missing path fields in `_PATH_FIELDS`

Three file-path param names used in handlers are absent from `_PATH_FIELDS`:

| Missing field | Used in |
|---|---|
| `file` | `settings_service.py` `handle_export_settings` / `handle_import_settings` |
| `vault_path` | `obsidian_sync.py` `handle_configure` |
| `file_path` | `transcription_queue.py`, `audio_analytics_service.py` |

`file` and `file_path` are the two most common generic path param names in the broader IPC surface. Even if the sanitizer were wired in, these fields would not receive path traversal protection.

---

### FINDING-6 (LOW): `settings_service.py` `handle_export_settings` — no allowed-dirs guard

**File:** `backend/settings_service.py`, line 406

```python
out_path = Path(str(params["file"])).expanduser().resolve()
out_path.parent.mkdir(parents=True, exist_ok=True)
```

The `mkdir(parents=True, exist_ok=True)` call will create any directory tree the caller names. While the `handle_export_settings` method writes only non-sensitive settings fields, the directory creation side-effect is unguarded.

---

### FINDING-7 (LOW): `live_subs_ingest` — no audio chunk size cap

**File:** `backend/live_subs_service.py`, `handle_ingest`

The socket-level cap is `IPC_MAX_MESSAGE_BYTES = 1 MB`. The `audio_chunk` field is a base64 string. A 1 MB base64 payload decodes to ~750 KB of PCM data. No additional check is performed before `np.frombuffer()`. While the 1 MB socket limit provides a hard cap, there is no explicit per-field size guard; a malformed very-long base64 string would silently succeed. The `sanitize_string` 10 000-char default would block this, but it is not called.

---

## 4. What Is Validated Today (without the sanitizer)

| Mechanism | Where | Quality |
|---|---|---|
| Allowed-dirs path check | `recording_core_service.py`, `history_service.py` | Present but uses `startswith` (FINDING-4) |
| Non-empty path required | Most path-taking handlers | Good |
| SSRF guard for webhook URLs | `webhook_manager.py` `_is_safe_webhook_url` | Correct |
| Phone number: non-empty check only | `call_session_service.py` | No format validation |
| Limit inline clamp | `service.py`: `limit`, `days`, `heatmap_days`, `scan_limit`, `top_k` | Ad-hoc, inconsistent |
| 1 MB IPC socket cap | `ipc_constants.py` + `IPCServer._handle_client` | Correct, but coarse |

---

## 5. Recommendations

| Priority | Action |
|---|---|
| HIGH | Wire `sanitize_params` into `handle_request` before the dispatch table lookup (one line: `params = self._sanitizer.sanitize_params(method, params)`) |
| HIGH | Add `file`, `file_path`, `vault_path` to `_PATH_FIELDS` |
| HIGH | Replace `startswith(str(root))` checks in `recording_core_service.py` and `history_service.py` with `resolved.is_relative_to(root)` |
| MEDIUM | Add allowed-dirs check to `audio_analytics_service.py` and `transcription_queue.py` path handlers |
| MEDIUM | Add `mkdir` guard in `settings_service.handle_export_settings` |
| LOW | Add `audio_chunk_max_bytes` field or explicit base64 length cap in `live_subs_service.handle_ingest` |

---

## 6. Test Coverage

Three test files cover `InputSanitizer` in isolation:
- `KrabEar/tests/test_input_sanitizer.py` — unit tests for string/path/params
- `KrabEar/tests/test_input_sanitizer_extras.py` — HTML, SQL injection, concurrency
- `KrabEar/tests/test_sanitize_path_traversal.py` — path traversal edge cases

No integration test verifies that a live IPC call to `handle_request` applies sanitization. The gap in FINDING-1 is not covered by any test.
