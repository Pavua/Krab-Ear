# Wave 1242 Re-Audit — `sharing_manager.py`: Residual Findings After W931/W939

**Date:** 2026-05-26  
**Auditor:** W1242 (sub-agent, re-audit)  
**Branch audited:** `codex/krab-ear-v2` (commit `6c900317`)  
**Files examined:**
- `KrabEar/backend/sharing_manager.py` (371 lines)
- `KrabEar/backend/service.py` (lines 394, 1072–1075)
- `docs/audit/2026-05-26-wave931-sharing-manager.md` (W931 original audit)
- Branch `fix/sharing-manager-secrets-W939` (W939 fix commit `ea6e434a`)
- Branch `docs/audit-sharing-manager-W931` (W931 audit commit `41fc1aa9`)

---

## W931 / W939 Merge State

| Wave | Branch | Commit | Merged into `codex/krab-ear-v2`? |
|------|--------|--------|----------------------------------|
| W931 | `docs/audit-sharing-manager-W931` | `41fc1aa9` | **NOT MERGED** |
| W939 | `fix/sharing-manager-secrets-W939` | `ea6e434a` | **NOT MERGED** |

Both branches exist as remote branches and are ahead of `codex/krab-ear-v2`. The `random`
module is still imported and used in `generate_share_id()` on the main branch — the
`secrets` fix from W939 has not landed.

---

## IPC Handler Coverage

Four handlers are wired in `service.py:1072–1075`:

| IPC method | Handler |
|---|---|
| `prepare_share` | `SharingManager.handle_prepare_share` |
| `list_shared` | `SharingManager.handle_list_shared` |
| `get_shared` | `SharingManager.handle_get_shared` |
| `revoke_share_link` | `SharingManager.handle_revoke_share_link` |

Coverage is complete — all public methods have a corresponding IPC handler. No missing
wiring identified.

---

## New Residual Findings (W931/W939 NOT applied)

The following 5 findings are NEW — they are not covered by W931 F1–F6 and represent
additional residual risk in the current `codex/krab-ear-v2` state.

---

### F1 — MEDIUM: W939 fix not merged — `random.choices` still used for token generation

**Location:** `sharing_manager.py:14` (import), `sharing_manager.py:106`

```python
import random
...
def generate_share_id(self) -> str:
    return "".join(random.choices(_BASE62_CHARS, k=_SHARE_ID_LEN))
```

W939 (commit `ea6e434a`) replaced this with `secrets.choice()` on branch
`fix/sharing-manager-secrets-W939`, but that branch has never been merged into
`codex/krab-ear-v2`. The Mersenne Twister PRNG is not cryptographically secure.
With only 47.6 bits of entropy (62^8) and a non-CSPRNG, the token is predictable from
a sequence of observed IDs within the same process.

**Fix:** merge branch `fix/sharing-manager-secrets-W939` into `codex/krab-ear-v2`.

---

### F2 — MEDIUM: Negative or non-finite `ttl_hours` accepted without validation

**Location:** `sharing_manager.py:231`, `sharing_manager.py:142`

```python
# handle_prepare_share:
ttl_hours: Optional[float] = float(ttl_hours_raw) if ttl_hours_raw is not None else None

# prepare_share:
expires_at = time.time() + effective_ttl * 3600.0
```

`_resolve_ttl` passes any caller-supplied float through unchanged (line 277: `return ttl_hours`).
No guard prevents:

- **Negative values** (e.g., `ttl_hours=-1`): `expires_at` is set in the past, so the
  package is immediately considered expired on the first `get_shared` call. The share file
  is still written to disk and the index entry persists, wasting disk space. The error is
  silent from the IPC caller's perspective (a successful response is returned, then every
  subsequent `get_shared` returns `None`).
- **`float('inf')`**: `expires_at = inf`, which JSON-encodes without error in CPython.
  `inf > time.time()` is always `True`, so the share never expires regardless of the 7-day
  default. This bypasses the TTL safety net added in Wave 158.
- **`float('nan')`**: `expires_at = nan`. The comparison `nan > time.time()` is `False`, so
  the share is immediately treated as expired (same symptom as negative TTL).

**Fix:** validate `ttl_hours` in `handle_prepare_share`:
```python
if ttl_hours is not None:
    if not math.isfinite(ttl_hours) or ttl_hours < 0:
        raise RuntimeError("ttl_hours must be a non-negative finite number")
    # Optionally cap: ttl_hours = min(ttl_hours, MAX_TTL_HOURS)
```

---

### F3 — MEDIUM: No `item_ids` length cap — CPU/IO DoS via oversized batch

**Location:** `sharing_manager.py:225–236` (`handle_prepare_share`)

```python
item_ids = params.get("item_ids")
if not isinstance(item_ids, list) or not item_ids:
    raise RuntimeError(...)
# No upper-bound check
...
items = self._fetch_items(item_ids)   # one store lookup per id
```

`_fetch_items` issues one `store.get_history_item_by_id()` call per entry. A caller can
pass `item_ids` with thousands of entries. Combined with `_render`, which serialises the
full content of every matched item, this can produce multi-megabyte payloads, exhaust
memory, and stall the backend IPC loop. The existing F3 finding in W931 addressed the
_per-share disk size cap_; this finding covers the _upstream input list size cap_ that
prevents the content from ever being rendered in the first place.

**Fix:**
```python
MAX_ITEMS_PER_SHARE = 100
if len(item_ids) > MAX_ITEMS_PER_SHARE:
    raise RuntimeError(f"item_ids превышает лимит {MAX_ITEMS_PER_SHARE}")
```

---

### F4 — LOW: `get_shared` / `revoke_share` use plain dict lookup — timing oracle

**Location:** `sharing_manager.py:188`, `sharing_manager.py:210`

```python
entry = self._index.get(share_id)   # in get_shared
if token not in self._index:         # in revoke_share
```

CPython `dict.__contains__` and `dict.get` for string keys use `PyObject_RichCompareBool`
with early-exit on first unequal character. For 8-character base-62 tokens, this leaks
timing information: a request for a token that shares a longer prefix with an existing
token takes measurably longer than one with no prefix match. Combined with the non-CSPRNG
generation (F1), this allows a timing-assisted enumeration attack to narrow the token
search space.

The `rest_server.py` received a constant-time compare fix in `fix(wave187)` (PR #535) for
its Bearer token check — the same pattern is needed here.

**Fix:** use `hmac.compare_digest` for token validation:
```python
import hmac
for stored_id in self._index:
    if hmac.compare_digest(stored_id, share_id):
        entry = self._index[stored_id]
        break
else:
    return None
```

---

### F5 — LOW: `prepare_share` succeeds when all `item_ids` resolve to empty — silent data loss

**Location:** `sharing_manager.py:144–161`

```python
items = self._fetch_items(item_ids)   # may return [] if none found
content = self._render(items, fmt, include_translation)  # renders empty list
# ...
package = SharePackage(share_id=share_id, content=content, ...)
self._persist_package(package)  # writes empty file, creates index entry
return package  # ok=True returned to caller
```

`_fetch_items` silently skips any item that is not found in the store (logs a warning only).
If the caller passes a list of item IDs that are all invalid or already deleted, `items` is
an empty list. `_render` then produces valid but empty content (empty JSON array, or a bare
markdown header), and `prepare_share` returns an `ok: True` response with a real `share_id`.
The caller has no way to distinguish "share created with content" from "share created with
empty content" without inspecting `size_bytes`.

This can happen silently after a `cleanup_old_history` purge: a UI that pre-selects history
IDs and then calls `prepare_share` may produce ghost shares.

**Fix:** raise `ValueError` when `items` is empty after fetch:
```python
items = self._fetch_items(item_ids)
if not items:
    raise ValueError("Ни одна из указанных записей не найдена в истории")
```

---

## Privacy Mode Interaction (W931 F2 — still open)

W931 F2 (privacy mode not checked before packaging) remains unresolved. The `SharingManager`
constructor accepts no privacy-mode flag, and `service.py:394` instantiates it with only
`store=self.store`. No guard in either `service.py` or `sharing_manager.py` prevents
`prepare_share` from packaging transcripts when backend privacy mode is active.

This is a carry-over finding from W931 and is not counted in the 5 new findings above.

---

## Summary

| # | Severity | Finding | New? |
|---|---|---|---|
| F1 | MEDIUM | W939 not merged — `random.choices` still used | Yes (merge state) |
| F2 | MEDIUM | Negative / non-finite `ttl_hours` accepted without validation | Yes |
| F3 | MEDIUM | No `item_ids` length cap — CPU/IO DoS | Yes |
| F4 | LOW | Plain dict lookup for token — timing oracle | Yes |
| F5 | LOW | All-missing item IDs produce empty share silently | Yes |
| (W931-F2) | MEDIUM | Privacy mode not enforced before packaging | Carry-over, not new |

**Recommended action order:**
1. Merge `fix/sharing-manager-secrets-W939` (trivial, already done in branch).
2. Add `ttl_hours` validation guard (F2) and `item_ids` length cap (F3) in
   `handle_prepare_share`.
3. Raise on empty items after fetch (F5) — prevents silent ghost shares.
4. Timing-safe token lookup (F4) is a hardening item; lower priority than F1–F3.
