# Audit W1674 — REST Server Internals (`backend/rest_server.py`)

**Date:** 2026-05-30
**File:** `KrabEar/backend/rest_server.py` (1396 LOC)
**Auditor:** Sub-agent W1674 (read-only, no code changes)
**Scope:** Full REST surface beyond W1207 (auth/CORS/rate-limiting) and W1213
(file-upload pipeline) — routes, v2 catch-all, error handling, lifecycle,
WebSocket, Swagger, startup.
**Prior waves:** W809 (initial survey), W1207 (security hardening), W1213 (file-upload).

---

## Route inventory (current state after W1207/W1213 fixes)

| Method | Path | Auth guard | Rate limit |
|--------|------|-----------|-----------|
| GET | `/info` | none | default 60/min |
| GET | `/health` | none | 120/min explicit |
| GET | `/metrics` | `@require_api_key` | default 60/min |
| GET | `/metrics/prometheus` | `@require_api_key` | default 60/min |
| GET | `/health/dashboard` | `@require_api_key` | default 60/min |
| GET | `/v1/readiness` | `@require_api_key` | default 60/min |
| GET | `/v1/vocabulary` | `@require_api_key` | default 60/min |
| POST | `/v1/vocabulary` | `@require_api_key` | default 60/min |
| POST | `/v1/stt/transcribe` | `@require_api_key` | 10/min explicit |
| GET | `/v1/events` (SSE) | `@require_api_key` | default 60/min |
| WS | `/ws/events` | `_ws_check_auth()` | **none** |
| ANY | `/v2/*` | **none** | **none** |

Fixes from W1207 are reflected: `/health/dashboard`, `/v1/vocabulary`,
`/v1/stt/transcribe`, and `/v1/events` now carry `@require_api_key`.

---

## Findings

### F1 — MED: `/v2/*` catch-all has no auth guard and no rate limiter

**Location:** `rest_server.py:1217–1227`

```python
@app.route("/v2/", defaults={"p": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@app.route("/v2/<path:p>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def v2_not_implemented(p):
    response = jsonify({"error": "API v2 not yet implemented", "supported_versions": ["v1"]})
    response.status_code = 501
    ...
```

The `/v2/*` catch-all route is registered directly on `app` (not on a Blueprint),
bypassing both `@require_api_key` and `flask-limiter`. Two consequences:

1. **Enumeration without credentials:** an unauthenticated caller can send arbitrary
   `POST /v2/stt/transcribe`, `GET /v2/vocabulary`, etc. and always receive 501.
   This confirms the server is present and its version without any token, which
   can be used for reconnaissance before credential brute-force.

2. **Rate-limit bypass:** because the v2 handler is on the bare `app` object (not
   on a Blueprint covered by `limiter`), the default 60/min limit is not applied.
   An attacker can send unlimited requests to `/v2/*` with no throttling at all —
   useful as a keep-alive or probe loop.

The 501 body also lists `supported_versions: ["v1"]`, confirming the exact API
surface to attack.

**Recommendation:** Add `@require_api_key` and an explicit `@limiter.limit("60 per
minute")` to `v2_not_implemented`, or register it on a Blueprint that inherits both.

---

### F2 — MED: No custom 413 error handler — Flask returns HTML on oversized upload

**Location:** `rest_server.py:43, 152`

```python
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB max
...
app.register_error_handler(429, _rate_limit_exceeded_handler)  # only 429 is registered
```

When a request body exceeds `MAX_CONTENT_LENGTH`, Flask raises
`werkzeug.exceptions.RequestEntityTooLarge` and returns an HTML error page with
status 413. Only the 429 handler has a custom JSON response; 413 is not registered.

The REST API contract is entirely JSON. A client that sends a 501 MB file (just over
the limit) receives:

```html
<!DOCTYPE HTML PUBLIC ...>
<title>413 Request Entity Too Large</title>
<h1>Request Entity Too Large</h1>
<p>The data value transmitted exceeds the capacity limit.</p>
```

This breaks any JSON-only client (e.g. the Swift `HistoryPanelController+Import.swift`
flow which `JSONSerialization.data(from:)` the response). The existing test
(`test_transcribe_endpoint_rejects_oversized_file`) confirms 413 is returned but does
not verify the response body is JSON.

**Recommendation:** Register a JSON 413 handler alongside the existing 429 handler:

```python
@app.errorhandler(413)
def request_entity_too_large(e):
    return jsonify({"error": "Request too large", "max_bytes": app.config.get("MAX_CONTENT_LENGTH")}), 413
```

---

### F3 — MED: `app.run()` has no port-conflict guard — EADDRINUSE crashes silently

**Location:** `rest_server.py:1395`

```python
app.run(host="127.0.0.1", port=5005)
```

If port 5005 is already bound (duplicate launchd agent, stale process, other app),
`app.run()` raises `OSError: [Errno 48] Address already in use` with a Python
traceback printed to stderr and the process exits. There is no `try/except`, no
user-readable error message, and no attempt to log the failure via `logger`.

The `start_rest_service.command` script does not check for existing processes before
launching. The symptom is a silent exit with no toast or Sentry event — the operator
must inspect the launchd log to discover the failure.

**Recommendation:** Wrap `app.run()` in a port-conflict handler:

```python
try:
    app.run(host="127.0.0.1", port=5005)
except OSError as exc:
    if exc.errno == 48:  # EADDRINUSE
        logger.error("REST server port 5005 already in use — is another instance running?")
    else:
        logger.exception("REST server failed to start: %s", exc)
    raise SystemExit(1)
```

---

### F4 — LOW: `/info` unauthenticated, exposes application version

**Location:** `rest_server.py:439–442`, `backend/api_versioning.py:187–194`

```python
@monitoring_blp.route("/info", methods=["GET"])
def api_info():
    return jsonify(get_api_info())
```

`get_api_info()` returns `{"app_version": APP_VERSION, "current_version": "v1",
"supported_versions": ["v1", "v2"], "deprecated_versions": []}`. The `app_version`
field exposes the exact Krab Ear release string (e.g. `"2.0.5"`).

Combined with publicly available changelogs, an attacker who knows the exact running
version can target known CVEs or recently patched bugs. The route has no
`@require_api_key` and is not in the current W1207 fix set. The `/health` endpoint
similarly leaks `engine.quality_profile` but that is lower sensitivity.

The W809 H-2 finding covered `/health/dashboard` (since fixed); `/info` was not
addressed.

**Recommendation:** Add `@require_api_key` to `api_info`, or strip `app_version`
from the unauthenticated response (return only versioning metadata without the build
string).

---

### F5 — LOW: WebSocket `/ws/events` has no rate limiter

**Location:** `rest_server.py:1333–1361`

```python
@sock.route("/ws/events")
def ws_events(ws):
    if not _ws_check_auth(ws):
        return
    ...
    _handle_ws_connection(ws, event_bus, type_filter)
```

`flask-limiter` operates exclusively on HTTP requests — it does not intercept
WebSocket upgrades handled by `flask-sock`. The `_ws_check_auth` function enforces
the Bearer token when auth is enabled, but even with auth enabled there is no limit
on:
- How many simultaneous WS connections a single IP can open.
- How frequently a client can reconnect after being disconnected.

A local process can open hundreds of persistent connections to `/ws/events` and
receive a copy of every STT event on each. Each connection holds an entry in
`EventBus._subscribers` (a `queue.Queue`), causing unbounded memory growth in
the event bus ring buffer (bounded at 50 events per subscriber but unbounded
across N simultaneous subscribers).

**Recommendation:** Track the count of active WS connections per IP (a module-level
`collections.Counter` protected by a `threading.Lock`). Reject new connections
beyond a configurable cap (e.g. `MAX_WS_CONNECTIONS_PER_IP = 5`).

---

### F6 — LOW: Swagger UI loaded from external CDN — breaks offline and raises supply-chain concern

**Location:** `rest_server.py:51`

```python
app.config["OPENAPI_SWAGGER_UI_URL"] = "https://cdn.jsdelivr.net/npm/swagger-ui-dist/"
```

The OpenAPI documentation UI at `/api/docs` loads all JavaScript and CSS from
`cdn.jsdelivr.net`. Two issues:

1. **Offline / air-gapped use:** the Swagger UI is non-functional without internet
   access, which conflicts with the project's offline-first design (offline STT,
   local LLM). An operator running Krab Ear in an air-gapped environment loses
   the API documentation entirely.

2. **Supply-chain risk:** if `cdn.jsdelivr.net` were compromised or the package
   version pinned by flask-smorest were hijacked, the injected JavaScript would
   execute in the browser context of anyone visiting `/api/docs`. While low-risk
   for a localhost-only server, the pattern is worth flagging.

**Recommendation:** Pin a specific semver for `swagger-ui-dist` in the CDN URL
(e.g. `https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/`) or serve the UI
assets locally from the `static/` directory using the `flask-smorest` local-bundle
option.

---

## Lifecycle / threading notes (informational, no new finding)

- `app.run()` uses Flask's built-in Werkzeug server with `threaded=True` (the
  default since Flask 1.0). Each HTTP request is served in its own thread.
  The `engine`, `store`, `transcriber`, and `metrics` objects are module-level
  singletons shared across all threads. This was acceptable as of W1213; no new
  races were found in this pass.
- `AudioEngine(skip_gigaam_warmup=True)` at module level is correct (W69 guard).
- `atexit.register(_rest_engine_cleanup)` provides graceful GigaAM adapter teardown.
- Rate-limit storage `memory://` resets on restart (W809 M-2 acknowledged in code
  comment at line 127).

---

## Test coverage gaps

The REST test suite has ~450 tests across 17 files. Gaps identified in this pass:

| Scenario | Covered? |
|----------|---------|
| `/v2/*` unauthenticated 501 | Yes (test_rest_api_versioning.py) |
| `/v2/*` rate-limit bypass | **No** |
| 413 response is valid JSON | **No** (only status code verified) |
| Port-conflict `EADDRINUSE` log | **No** |
| `/info` returns `app_version` without auth | **No** (existence tested, auth not) |
| Multiple simultaneous WS connections | **No** |
| Swagger CDN URL pinned / accessible | **No** |

---

## Summary

| ID | Severity | Finding |
|----|----------|---------|
| F1 | MED | `/v2/*` catch-all: no auth + no rate limiter |
| F2 | MED | No custom 413 handler — HTML response breaks JSON clients |
| F3 | MED | `app.run()` EADDRINUSE unhandled — silent crash |
| F4 | LOW | `/info` unauthenticated leaks `app_version` |
| F5 | LOW | `/ws/events` has no connection-rate limiter |
| F6 | LOW | Swagger UI on external CDN — offline broken + supply-chain |
