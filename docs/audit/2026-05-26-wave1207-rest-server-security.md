# Audit W1207 — REST Server Security (`backend/rest_server.py`)

**Date:** 2026-05-26  
**Branch:** `audit/rest-server-security-W1207`  
**Auditor:** Sub-agent W1207 (read-only, no code changes)  
**Scope:** `KrabEar/backend/rest_server.py` + `KrabEar/backend/rest_auth.py` + `KrabEar/core/config.py` (REST-relevant fields)

---

## Executive Summary

The REST server (port 5005) correctly binds to `127.0.0.1` only, uses `hmac.compare_digest` for constant-time key comparison, has rate limiting and upload size limits, and sanitises file names with `secure_filename`. However, **6 findings** remain — ranging from a MEDIUM auth gap to LOW information-disclosure issues.

---

## Findings

### F1 — MEDIUM: Auth disabled by default; `/v1/vocabulary` and `/v1/events` unprotected

**Location:** `rest_server.py:180, 834–858, 966–992` | `core/config.py:176–180`

Both `REST_API_KEY` and `REST_API_AUTH_ENABLED` default to off (`""` / `False`). In that state:

- `GET /v1/vocabulary` — returns the user's STT vocabulary list with no auth check.
- `POST /v1/vocabulary` — lets any local process append arbitrary vocabulary words.
- `GET /v1/events` — streams the live SSE event bus (all STT results, live-subs output, etc.) indefinitely.
- `GET /info` — exposes API version metadata.

Only `/v1/readiness`, `/metrics`, and `/metrics/prometheus` are unconditionally decorated with `@require_api_key`; the three endpoints above are not decorated at all. A malicious local process (or an app sandboxed under the same user account) can read transcription events or poison the vocabulary without credentials.

**Why it matters here:** macOS sandboxed apps can reach `127.0.0.1:5005` if they have the `com.apple.security.network.client` entitlement. The transcript stream (`/v1/events`) is the highest-sensitivity leak.

**Recommendation:** Add `@require_api_key` to `/v1/vocabulary` (GET + POST) and `/v1/events`. Alternatively, gate the three endpoints behind a new `REST_API_REQUIRE_AUTH_FOR_VOCAB_EVENTS` flag (default `True`) so existing users can opt out but new installs are secure by default.

---

### F2 — MEDIUM: CORS wildcard `*` with `supports_credentials=True`

**Location:** `rest_server.py:71–78` | `core/config.py:188`

```python
CORS(
    app,
    origins=_parse_cors_origins(settings.CORS_ORIGINS),  # default "*"
    supports_credentials=True,
    ...
)
```

The combination of `origins="*"` and `supports_credentials=True` is explicitly banned by the CORS specification — browsers will refuse to include cookies/credentials when `*` is the allowed origin *and* credentials are requested. However, the server **still sends the header pair** which confuses some non-browser HTTP clients and creates a false sense of security. More importantly, the `CORS_ORIGINS` default of `"*"` means any website visited by the user (in a browser on the same machine) can make unauthenticated cross-origin requests to `http://127.0.0.1:5005` and read responses, because the browser does not enforce the credentials restriction on requests that don't send credentials.

A drive-by page can `fetch("http://127.0.0.1:5005/v1/events")` and stream live transcriptions.

**Recommendation:** Change the default `CORS_ORIGINS` to `"http://localhost:5005,http://127.0.0.1:5005"` (or an explicit list). Remove `supports_credentials=True` unless cookie-based auth is intentionally implemented.

---

### F3 — LOW: `/health/dashboard` unauthenticated — leaks platform and Python version

**Location:** `rest_server.py:788–802, 532–533, 719–730`

`GET /health/dashboard` has no `@require_api_key` decorator and is not listed under any rate-limiting rule beyond the global 60 req/min default. The rendered HTML includes:

```html
<div>v{version} · Python {py_version}</div>
<tr><td>Platform</td><td>{platform_str}</td></tr>  <!-- e.g. macOS-15.4-arm64 -->
```

`platform.platform()` returns the full OS build string (e.g. `macOS-15.4-arm64-arm-64bit`). This is reconnaissance data for a local attacker enumerating services.

**Recommendation:** Add `@require_api_key` to `/health/dashboard`. If an unauthenticated status page is desired, strip Python version and platform string from the unauthenticated view, leaving only `status: ok/degraded`.

---

### F4 — LOW: `/v1/stt/transcribe` lacks auth decorator

**Location:** `rest_server.py:862–864`

```python
@v1_blp.route("/stt/transcribe", methods=["POST"])
@v1_blp.response(200, TranscribeResponseSchema)
@limiter.limit("10 per minute")
def transcribe_audio():
```

`@require_api_key` is absent. The rate limit (10/min) provides denial-of-service mitigation but does not prevent unauthorised use of the STT pipeline. Any local process can trigger full MLX Whisper transcription (heavy CPU/GPU load, writes to history store) without credentials.

The file size cap (500 MB) and extension whitelist are correctly applied; the gap is the missing auth check.

**Recommendation:** Add `@require_api_key` above `@limiter.limit(...)`.

---

### F5 — LOW: Upload size (500 MB) inconsistent with IPC limit (1000 MB) and not rate-limited by bytes

**Location:** `rest_server.py:42` | `core/config.py:119`

```python
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB max
```

`MAX_AUDIO_MB = 1000` in config (IPC path), but the REST server hard-codes 500 MB. This is a minor inconsistency. More importantly, the rate limiter counts **requests** (10/min), not bytes transferred. An attacker sending 10 × 500 MB requests per minute can saturate disk I/O and the TEMP_DIR with 5 GB of data. Werkzeug raises a `413` once `MAX_CONTENT_LENGTH` is exceeded, but the check happens after the stream is partially buffered — Flask buffers the full body before the view function sees it when using `request.files`.

**Recommendation:** (a) Tie `MAX_CONTENT_LENGTH` to `settings.MAX_AUDIO_MB * 1024 * 1024` for consistency. (b) Add a per-IP byte-budget rate limit (flask-limiter supports `moving-window` with custom cost functions) or document the known disk DoS window.

---

### F6 — LOW: No privacy-mode gate on REST endpoints

**Location:** `rest_server.py` (entire file) | `core/config.py:987 (DEFAULT_SETTINGS)`

`privacy_mode_enabled` is a runtime setting managed by `SettingsService` and toggled via IPC. When privacy mode is active, the IPC server disables history writes and clears clipboard. The REST server has no equivalent gate: `POST /v1/stt/transcribe` will still transcribe audio, write to `StateStore` (via `store.add_history_item`), and write idempotency keys even when privacy mode is on.

**Recommendation:** Add a privacy-mode check at the top of `transcribe_audio()`:

```python
if store.get_setting("privacy_mode_enabled", False):
    return jsonify({"error": "privacy_mode_active"}), 403
```

Mirror the IPC behaviour: refuse transcription and history writes in privacy mode.

---

## Non-findings (confirmed OK)

| Topic | Finding |
|---|---|
| Bind interface | Hardcoded `127.0.0.1` in `__main__` and `gunicorn_config.py` — remote access blocked. |
| Auth implementation | `hmac.compare_digest` used; SHA-256 hashes stored (never raw token) — correct. |
| TLS / HTTPS | Not provided; localhost-only bind makes TLS optional for localhost. No cert leakage. |
| File traversal | `secure_filename()` + `TEMP_DIR / uuid_prefix_filename` — safe. |
| Error message leak | `except Exception: ... return jsonify({"error": "Internal processing error"}), 500` — stack traces suppressed. |
| Rate limiting (DoS) | `flask-limiter` active by default (`RATE_LIMIT_ENABLED=True`); 60 req/min global, 120/min on `/health`, 10/min on `/stt/transcribe`. |
| Input validation | `VALID_QUALITY`, `VALID_DOMAIN`, `VALID_CLEANUP` enum sets validated; extension whitelist applied before file save. |
| CSRF | REST API is stateless Bearer-token auth; no session cookies → CSRF N/A (with caveat in F2). |
| Swagger UI CDN | `https://cdn.jsdelivr.net/npm/swagger-ui-dist/` — external CDN; acceptable for a localhost dev tool. |

---

## Summary Table

| ID | Severity | Endpoint(s) | Issue |
|----|----------|-------------|-------|
| F1 | MEDIUM | `/v1/vocabulary`, `/v1/events` | No auth decorator — unauthenticated read/write |
| F2 | MEDIUM | All origins | CORS `*` + `supports_credentials=True` — drive-by read from browser |
| F3 | LOW | `/health/dashboard` | No auth — exposes OS version, Python version, platform string |
| F4 | LOW | `/v1/stt/transcribe` | No auth decorator — free STT triggering by any local process |
| F5 | LOW | `/v1/stt/transcribe` | 500 MB cap inconsistent with IPC 1000 MB; byte-budget DoS window |
| F6 | LOW | `/v1/stt/transcribe` | Privacy mode not checked — transcription proceeds even when privacy active |

**Total findings:** 6 (2 MEDIUM, 4 LOW)
