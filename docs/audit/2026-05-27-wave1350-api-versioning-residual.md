# Wave 1350 — api_versioning.py Re-audit (Residual)

**Date:** 2026-05-27
**File audited:** `KrabEar/backend/api_versioning.py` (130 lines on `codex/krab-ear-v2`)
**Auditor:** W1350 sub-agent
**Prior waves:** W980 (initial audit, PR #899 OPEN), W986 (deprecation-header fix, PR #908 OPEN)

---

## W980 / W986 Merge State

| PR | Branch | Status |
|---|---|---|
| #899 | `docs/audit-api-versioning-W980` | **OPEN** (not merged) |
| #908 | `fix/api-versioning-deprecation-W986` | **OPEN** (not merged) |

Neither fix is on `codex/krab-ear-v2`. All five findings below are NEW residuals
not addressed by W980 or W986.

---

## Summary

`api_versioning.py` is structurally sound but has accumulated five residual issues
that span three layers: a docstring misleads callers, `SUPPORTED_VERSIONS` is a
mutable list, `V2` is advertised but entirely unimplemented, W986's unreleased
patch has a style defect, and test coverage for the auto-injection path (once W986
lands) remains split across files with a gap in `test_rest_api_versioning.py`.

---

## Findings (5)

### F1 — `api_version_header()` docstring shows wrong calling convention (LOW)

**File:** `KrabEar/backend/api_versioning.py`, lines 74-79

The docstring code example reads:

```python
app.after_request(api_version_header)
```

`api_version_header` is a *factory* — it returns a handler closure. Passing the
factory itself to `after_request` means Flask will call
`api_version_header(response)` at request time, which fails immediately with:

```
TypeError: api_version_header() takes 0 positional arguments but 1 was given
```

`rest_server.py:56` correctly calls `app.after_request(api_version_header())`
(note the parentheses), so the bug is documentation-only — but a developer
copying the docstring example would break every endpoint silently.

The W986 branch fixes this in the docstring as a side-effect of its other
changes (the branch docstring reads `api_version_header()`), but W986 is not
merged.

**Recommendation:** Change the docstring example to `app.after_request(api_version_header())`.

---

### F2 — `SUPPORTED_VERSIONS` is a mutable list; accidental mutation silently poisons version detection (LOW)

**File:** `KrabEar/backend/api_versioning.py`, line 29

```python
SUPPORTED_VERSIONS = [APIVersion.V1, APIVersion.V2]
```

`get_api_version()` iterates `SUPPORTED_VERSIONS` at request time. Because it is
a plain `list`, any code (including tests that forget to restore state) can do:

```python
SUPPORTED_VERSIONS.append(APIVersion.V3)
SUPPORTED_VERSIONS.clear()
```

without any guard. `DEPRECATED_VERSIONS` is intentionally mutable (it is the
config surface for deprecation), but `SUPPORTED_VERSIONS` should not be.
`test_api_versioning.py` imports it directly and asserts membership but never
restores it in `tearDown`, so future tests that temporarily extend the list could
leave state behind.

**Recommendation:** Change to a `tuple`:

```python
SUPPORTED_VERSIONS: tuple[APIVersion, ...] = (APIVersion.V1, APIVersion.V2)
```

The three iteration sites (`get_api_version()`, `get_api_info()`, and W986's
`_extract_raw_version_hint()`) all use `for v in SUPPORTED_VERSIONS` which works
identically on tuples.

---

### F3 — `V2` is in `SUPPORTED_VERSIONS` but has zero registered routes; clients receive confusing 404 (MEDIUM)

**File:** `KrabEar/backend/rest_server.py`, lines 806-1002

`APIVersion.V2` is declared supported, `get_api_version()` resolves it, and
`X-API-Version: v2` is set on responses — but there is no `v2_blp` Blueprint and
no `/v2/*` routes are registered with `api.register_blueprint()`.

A client that:
- GETs `/v2/readiness`
- Sends `Accept: application/vnd.krabear.v2+json`

receives `HTTP 404 NOT FOUND` with `X-API-Version: v2` in the response headers.
This tells the client the server understood the version but could not find the
resource, rather than the correct signal that V2 does not yet exist.

`test_rest_api_versioning.py::TestV2PathBehaviour` documents this as "acceptable
for now" but does not block the misleading response. W980 F1 raises this; W986
does not address it.

**Recommendation (pick one):**
1. Remove `APIVersion.V2` from `SUPPORTED_VERSIONS` until v2 routes are
   registered (`SUPPORTED_VERSIONS = (APIVersion.V1,)`).
2. Register a v2 catch-all returning `501 Not Implemented` with a
   `Link: </v1/>; rel="successor-version"` header.

---

### F4 — W986's `_extract_raw_version_hint()` uses an inline `import re` inside the function body (LOW)

**File:** `fix/api-versioning-deprecation-W986` branch,
`KrabEar/backend/api_versioning.py` (not yet on `codex/krab-ear-v2`)

```python
def _extract_raw_version_hint(req=None) -> str | None:
    ...
    import re as _re          # line 95 in W986 branch
    m = _re.match(r"^/(v\d+)", path)
```

`import re as _re` appears **inside the function body**, not at module level.
Python caches the module in `sys.modules` so there is no performance hit, but:

1. It violates PEP 8 (all imports at top of file unless conditional).
2. It makes the function harder to read: a reviewer scanning imports at the top
   of the file will not see `re` and may think the regex dependency is absent.
3. It duplicates the import pattern that already exists in the rest of the
   module (no module-level `import re`).

This will become a live issue the moment W986 merges.

**Recommendation:** Move `import re` to the module-level import block alongside
`from enum import Enum` before W986 merges.

---

### F5 — `test_rest_api_versioning.py` (merged wave215) has no route-level test for auto-injection of deprecation headers (LOW)

**File:** `KrabEar/tests/test_rest_api_versioning.py`

`TestDeprecatedVersionHeaders` (lines 227-277) tests `deprecation_warning()`
as a standalone function — it builds a bare Flask `Response`, calls the
function directly, and asserts headers. It does **not** make a test-client
request to an actual `/v1/*` route with `DEPRECATED_VERSIONS` populated.

This means if the auto-injection wiring inside `api_version_header()` (added by
W986, not yet merged) is removed or broken, the wave215 test suite passes
silently. W986 adds `TestDeprecationAutoInjection` in
`test_api_versioning.py` which does test via `test_client.get("/v1/ping")`,
but once W986 merges there will still be no equivalent route-level test in
`test_rest_api_versioning.py` that exercises the actual REST server routes
(`/v1/readiness`, `/v1/vocabulary`, `/v1/stt/transcribe`).

**Recommendation:** Add one test to `TestDeprecatedVersionHeaders` in
`test_rest_api_versioning.py` that:
1. Sets `DEPRECATED_VERSIONS[APIVersion.V1] = "2027-01-01"`.
2. Calls `client.get("/v1/readiness")` (or any registered v1 route).
3. Asserts `Sunset` and `Deprecation` headers are present on the response.
4. Restores `DEPRECATED_VERSIONS` in `tearDown`.

This complements W986's unit-level test and guards against regressions in the
REST server's wiring.

---

## Test Coverage Summary

| File | Status | Gap |
|---|---|---|
| `test_api_versioning.py` (wave147, merged) | Good for current code; W986 adds 9 new tests | W986 not merged yet |
| `test_rest_api_versioning.py` (wave215, merged) | Tests `deprecation_warning()` standalone only | No route-level auto-injection test (F5) |
| `test_rest_e2e.py` | Header presence smoke tests | Does not cover deprecation or unknown-version warning |

---

## Backward Compatibility

- F1 (docstring) — no runtime impact, pure documentation error.
- F2 (mutable list) — no current mutation observed; change is safe.
- F3 (V2 routes) — existing behavior (404) is unchanged; fixing to tuple (F2)
  does not affect V2 detection.
- F4 (inline import) — style only, no behavior change needed.
- F5 (test gap) — adding a test is additive; no production impact.
