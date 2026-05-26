# Wave 931 Audit — `sharing_manager.py`: Token Security, PII, TTL, Revocation

**Date:** 2026-05-26  
**Auditor:** W931 (sub-agent)  
**Files examined:**
- `KrabEar/backend/sharing_manager.py` (372 lines)
- `KrabEar/tests/test_sharing_manager.py` (897 lines)
- `KrabEar/tests/test_sharing_manager_ttl.py` (364 lines)

---

## Summary

`SharingManager` packages one or more history items into text/markdown/JSON files stored
under `{data_dir}/shares/`. Shares are identified by an 8-character token and listed in a
JSON index file. Wave 158 added TTL (`expires_at`) and `revoke_share()`.
Overall posture is **medium risk** — the most critical gap (no TTL, no revocation) was
closed in Wave 158, but five meaningful issues remain.

---

## Findings

### F1 — MEDIUM: Token generated with `random.choices`, not `secrets`

**Location:** `sharing_manager.py:106`

```python
def generate_share_id(self) -> str:
    return "".join(random.choices(_BASE62_CHARS, k=_SHARE_ID_LEN))
```

`random` is the Mersenne Twister PRNG, which is not cryptographically secure.
An attacker who can observe a sequence of share IDs can predict future (and sometimes past)
IDs within the same process lifetime.
With only 8 base-62 characters (~47.6 bits of entropy), the brute-force bar is already low;
using a non-CSPRNG reduces it further.

**Fix:** replace with `secrets.token_urlsafe()` or `secrets.choice()`:

```python
import secrets
def generate_share_id(self) -> str:
    return "".join(secrets.choice(_BASE62_CHARS) for _ in range(_SHARE_ID_LEN))
```

---

### F2 — MEDIUM: Privacy mode not checked before including transcript content

**Location:** `sharing_manager.py:108–164` (`prepare_share`)

`prepare_share` calls `_fetch_items → _render` without consulting any privacy-mode flag.
If the backend has privacy mode enabled (no transcript retention / redaction), a caller can
still create a shareable package of any history item by ID. The parameter
`include_translation=False` strips translation fields but leaves the raw transcript text
intact in every format.

There is no `BackendService`-level guard visible in this file; whether the IPC handler in
`service.py` enforces privacy mode before delegating to `handle_prepare_share` was not
audited here, but the `SharingManager` itself has zero awareness of it.

**Risk:** privacy-mode transcripts can be silently packaged and shared.

**Fix:** accept an optional `privacy_mode: bool` flag in the constructor or in
`prepare_share()`; if privacy mode is active, raise `PermissionError` or strip PII fields
before packaging.

---

### F3 — LOW-MEDIUM: Disk fill — no per-share size cap, no aggregate quota

**Location:** `sharing_manager.py:351–362` (`_persist_package`)

Each call to `prepare_share` writes a full copy of transcript content to disk (no
deduplication). The edge-case test in `test_sharing_manager.py:499–503` deliberately
creates a 10 MB file and asserts it succeeds — effectively documenting that there is no size
cap. With many large audio imports, a single user can fill the data directory.
The `DiskSpaceMonitor` (backend module) will eventually warn at < 2 GB free, but it does
not prevent share creation.

**Fix:** enforce a `max_share_size_bytes` cap (e.g., 50 MB) in `prepare_share()`, and/or a
maximum total `shares/` directory quota.

---

### F4 — LOW: Package file is not written atomically; index save races with file write

**Location:** `sharing_manager.py:351–362` (`_persist_package`)

```python
def _persist_package(self, package: SharePackage) -> None:
    file_path = self._shares_dir / package.filename
    try:
        file_path.write_text(package.content, encoding="utf-8")   # <-- not atomic
    except Exception as exc:
        logger.error(...)

    with self._lock:
        self._index[package.share_id] = package.to_dict()
        self._save_index()   # atomic (tmp+replace)
```

The index save (`_save_index`) uses the correct tmp+replace pattern, but the package file
itself is written with a plain `write_text` call. A crash between the two steps leaves a
registered share_id in the index pointing to a partially-written or empty file.

The index save also happens **after** the (unprotected) file write; there is no lock held
around the file write itself, so a concurrent `prepare_share` call with the same share_id
(unlikely but non-zero with `random`) could interleave writes.

**Fix:** write the content file via a temp path then `rename()` to the final path, identical
to how `_save_index` works. Acquire `self._lock` before both the file write and the index
update.

---

### F5 — LOW: Stale-test divergence — Wave 98 test asserts revoke does NOT exist

**Location:** `test_sharing_manager.py:751–759` (`Wave98RequiredTestCase`)

```python
def test_revoke_share_link_not_supported(self) -> None:
    """SharingManager не предоставляет revoke API — нет метода revoke_share."""
    ...
    self.assertFalse(hasattr(self._mgr, "revoke_share"))
```

Wave 158 added `revoke_share()` and `handle_revoke_share_link()`, but this Wave 98 test
was **not updated** and now asserts the opposite of reality. The test class is named
`Wave98RequiredTestCase` and is imported in the same file that the new Wave 158 test file
correctly validates revocation. The stale test **passes** only because `revoke_share` exists
on the object — `hasattr()` returns `True`, `assertFalse(True)` would fail — unless the
test is currently **broken/skipped** or was left unrun. This is a dangling false-assertion
that will silently pass if the import is isolated, creating misleading audit evidence that
revocation was never implemented.

**Fix:** update or remove `test_revoke_share_link_not_supported` in
`test_sharing_manager.py:751–759`; add an explicit positive assertion that
`revoke_share` is callable.

---

### F6 (INFORMATIONAL) — No audit trail for share creation or access

`SharingManager` does not log who created a share, when it was accessed via `get_shared`,
or when it was revoked beyond the standard `logger.error` on disk failures.
The `AuditLogger` module (`backend/audit_logger.py`) is not used here.
For compliance use cases (e.g., sharing medical transcripts), access logging per token
would be expected.

No action required for current scope, but consider wiring `AuditLogger` if PII sensitivity
escalates.

---

## What Is Working Well

| Area | Status |
|---|---|
| TTL (7-day default) | Implemented in Wave 158; tested in `test_sharing_manager_ttl.py` |
| Revocation (`revoke_share`) | Implemented; persisted across reload; concurrency-safe |
| Index persistence | Atomic tmp+replace for the index file |
| Thread safety on index reads/writes | `threading.Lock` throughout |
| TTL check on `get_shared` | Correctly returns `None` for expired or revoked shares |
| Test coverage | 897 + 364 lines of tests; good branch coverage of TTL and revoke paths |

---

## Risk Summary

| # | Severity | Finding |
|---|---|---|
| F1 | MEDIUM | `random.choices` instead of `secrets` for token generation |
| F2 | MEDIUM | Privacy mode not checked before packaging transcript content |
| F3 | LOW-MEDIUM | No per-share size cap or aggregate quota; disk fill possible |
| F4 | LOW | Package file write is not atomic; possible partial file on crash |
| F5 | LOW | Wave 98 stale test asserts revoke API does not exist (contradicts Wave 158) |
| F6 | INFO | No audit trail for share creation / access |
