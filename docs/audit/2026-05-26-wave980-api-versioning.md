# Wave 980 — API Versioning Audit

**Date:** 2026-05-26  
**File audited:** `KrabEar/backend/api_versioning.py` (130 lines)  
**Auditor:** W980 sub-agent

---

## Summary

`api_versioning.py` is a clean, self-contained module. Version negotiation is
well-designed; the main gaps are on the **wire integration** side — the v2
blueprint is missing, `deprecation_warning()` is never called automatically,
and deprecated-version logging does not exist.

---

## Findings (5)

### F1 — V2 blueprint declared but never registered (MEDIUM)

`APIVersion.V2` exists in `SUPPORTED_VERSIONS` and `get_api_version()` can
resolve it, but `rest_server.py` only defines a `v1_blp` Blueprint with
`url_prefix="/v1"`. No `/v2/*` routes are registered.

**Impact:** Any client that sends `Accept: application/vnd.krabear.v2+json` or
hits `/v2/*` receives a Flask 404 with `X-API-Version: v2` — giving the
impression the server understood the request but found no resource, rather than
the correct signal that v2 is not yet live.  
`test_rest_api_versioning.py:TestV2PathBehaviour` explicitly documents this as
"acceptable for now" but does not block the confusing 404 response.

**Recommendation:** Either remove V2 from `SUPPORTED_VERSIONS` until routes are
registered, or add an explicit `/v2/*` catch-all that returns 501 Not
Implemented with a `Link` header pointing to v1.

---

### F2 — `DEPRECATED_VERSIONS` is always empty; deprecation headers are never injected automatically (MEDIUM)

`DEPRECATED_VERSIONS: dict[APIVersion, str] = {}` is defined but remains empty
at runtime. `deprecation_warning()` exists and injects `Sunset` + `Deprecation`
headers correctly, but it is never called by the `api_version_header()`
after-request handler or any route.

**Impact:** If/when v1 is officially deprecated, operators must remember to:
1. populate `DEPRECATED_VERSIONS`, AND
2. wire a call to `deprecation_warning()` in the after-request handler or per-route.

Neither step is automated. A client using v1 after the sunset date will receive
no warning headers.

**Recommendation:** Extend `api_version_header()` to automatically call
`deprecation_warning()` when the resolved version appears in
`DEPRECATED_VERSIONS`:

```python
def handler(response: Response) -> Response:
    version = get_api_version()
    response.headers["X-API-Version"] = version.value
    if version in DEPRECATED_VERSIONS:
        response = deprecation_warning(response, version.value, DEPRECATED_VERSIONS[version])
    return response
```

---

### F3 — No logging of deprecated version usage (LOW)

`get_api_version()` resolves the client-requested version but never logs it. If
a client is calling a deprecated version heavily in production, there is no
server-side signal (log line, metric, Sentry breadcrumb) to detect it.

**Recommendation:** Add a `logger.warning()` inside the auto-injection logic in
F2's recommended fix, following the structured-logging pattern (`extra={"method": ...,
"version": ..., "sunset": ...}`). This matches the breadcrumb pattern already
established in `backend/observability.py`.

---

### F4 — Unknown version silently falls back to DEFAULT instead of returning 400 (LOW)

When a client sends `?api_version=v99` or `Accept: application/vnd.krabear.v99+json`,
`get_api_version()` silently returns `DEFAULT_VERSION` (v1) with no indication
of the error. The client gets a `200 X-API-Version: v1` response and may never
know their version hint was ignored.

**Impact:** Low in practice since the only callers are `api_version_header()`
(response decoration, no routing effect) and `get_api_info()`. However it makes
debugging harder for integrators.

**Recommendation:** For query-param and Accept-header paths, if the value is
explicitly set to an unrecognised string, return a 400 Bad Request with a body
like `{"error": "unsupported_version", "supported": ["v1","v2"]}` instead of
silently falling back. URL-prefix detection should remain silent (Flask already
returns 404 for unregistered routes).

---

### F5 — No auto-block / sunset enforcement after sunset date (LOW)

`DEPRECATED_VERSIONS` stores a sunset ISO-8601 date, but neither the negotiation
logic nor any middleware checks whether today's date is past that sunset. A client
can continue to call a "sunset" version indefinitely.

**Recommendation:** Add an optional `ENFORCE_SUNSET = False` flag. When `True`,
`api_version_header()` (or a separate middleware) compares `datetime.date.today()`
against the sunset date and returns `410 Gone` with a migration hint in the
response body. Default `False` keeps current safe-fallback behaviour.

---

## Wire Status

| Integration point | Status |
|---|---|
| `rest_server.py` imports `api_version_header`, `get_api_info` | Wired |
| `app.after_request(api_version_header())` | Wired |
| `GET /info` returns `get_api_info()` | Wired |
| `deprecation_warning()` called automatically | **Not wired** |
| `get_api_version()` called per-route for routing decisions | Not used (header-decoration only) |
| V2 blueprint routes | **None registered** |

The module is effectively used only for: (a) setting `X-API-Version` on every
response, and (b) exposing version metadata at `/info`.

---

## Test Coverage

Three test files cover `api_versioning.py`:

| File | Tests | Notes |
|---|---|---|
| `test_api_versioning.py` | 35 | Unit tests: enum values, negotiation priority, headers, deprecation_warning, get_api_info, concurrency |
| `test_rest_api_versioning.py` | ~50 | Integration tests via Flask test client: v1 routes, v2 404, unsupported versions, concurrent load, ISO-8601 date format, Accept-header priority |
| `test_rest_e2e.py` | ~6 | E2E header presence checks |

Coverage is **thorough for the code that exists**. There are no tests for:
- auto-injection of deprecation headers (F2 — feature does not exist yet)
- sunset enforcement / 410 Gone (F5 — feature does not exist yet)
- logging of deprecated-version usage (F3)

---

## Backward Compatibility

No v1 fields are silently removed in v2 — because v2 has no routes yet. When v2
routes are eventually added, there is no migration-hint mechanism in the current
error responses to guide v1 clients.

`get_api_info()` does expose `deprecated_versions` with `sunset_date` which
is the correct discovery mechanism for well-behaved clients, once `DEPRECATED_VERSIONS`
is populated.

---

## Client Error Messages

`get_api_version()` never returns an error to the client — it always returns a
valid `APIVersion`. Migration hints (pointing a deprecated-version caller to v2)
are entirely absent. There is no `Link: </v2/readiness>; rel="successor-version"`
header pattern, and no body field like `"migration": "upgrade to v2"`.

---

## Documentation

`docs/IPC_API_REFERENCE.md` covers IPC (Unix socket) methods only and does not
reference REST API versioning. The REST `/info` endpoint exposes
`get_api_info()` which is the canonical runtime source of version metadata.
No Markdown documentation links the REST versioning scheme to client guidelines.
