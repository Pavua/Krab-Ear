# Wave 809 — `backend/rest_server.py` Security & Quality Audit

**Date**: 2026-05-26  
**File**: `KrabEar/backend/rest_server.py`  
**LOC**: 1106  
**Auditor**: automated (Wave 809)

---

## Scope

Full read-only audit of `rest_server.py` covering:

1. Authentication model and token handling
2. Input validation and sanitisation
3. Error response safety (stack-trace leakage)
4. CORS configuration
5. Rate limiting
6. Logging (structured / PII safety)
7. Miscellaneous code quality issues

Supporting files consulted:
- `backend/rest_auth.py` — token store
- `core/config.py` — `Settings` class (all relevant env-var defaults)

---

## Route inventory

| Method | Path | Auth guard | Rate limit |
|--------|------|-----------|-----------|
| GET | `/info` | none | default (60/min) |
| GET | `/health` | none | 120/min explicit |
| GET | `/metrics` | `require_api_key` | default (60/min) |
| GET | `/metrics/prometheus` | `require_api_key` | default (60/min) |
| GET | `/health/dashboard` | none | default (60/min) |
| GET | `/v1/readiness` | `require_api_key` | default (60/min) |
| GET | `/v1/vocabulary` | none | default (60/min) |
| POST | `/v1/vocabulary` | none | default (60/min) |
| POST | `/v1/stt/transcribe` | none | 10/min explicit |
| GET | `/v1/events` (SSE) | none | default (60/min) |
| WS | `/ws/events` | none | none |

---

## Findings

### CRITICAL

None.

---

### HIGH

#### H-1 — Auth opt-in default: `/v1/stt/transcribe` and vocabulary endpoints require no token

**Lines**: 862–964 (`transcribe_audio`), 834–859 (`get_vocabulary`, `add_vocabulary`)

Both the primary transcription endpoint and vocabulary write endpoint have no `require_api_key` decorator. Any local process (or, if the bind address were ever changed, any network peer) can:
- Upload audio and receive transcriptions (latency: 10/min rate limit is the only guard)
- Read and append to the user vocabulary word list

The startup warning at line 1096–1103 acknowledges the auth-disabled posture for the server overall, but it does not mention that even when auth *is* enabled, `POST /v1/vocabulary` and `POST /v1/stt/transcribe` remain open.

**Risk**: On a shared machine (multi-user macOS), any local user can call these endpoints without credentials.

**Recommendation**: Add `@require_api_key` to `transcribe_audio`, `add_vocabulary`, and `get_vocabulary`. The existing `require_api_key` decorator is a no-op when neither auth mode is enabled (mode 3 pass-through), so existing zero-auth deployments would be unaffected unless the operator explicitly enables one of the auth modes. Alternatively, document explicitly in the route docstrings that these endpoints are intentionally public.

---

#### H-2 — `/health/dashboard` leaks internal system information without auth

**Lines**: 788–802 (`health_dashboard`), 486–768 (`_build_dashboard_html`)

The dashboard HTML page exposes:
- Python version and platform string (`platform.platform()`)
- CPU %, RAM used/total/%, disk free/total
- STT engine quality profile
- All health-check subsystem status strings (disk, LLM, IPC socket, STT model)
- Latency percentiles, error rates

There is no `@require_api_key` on this route. On the default 127.0.0.1 bind this is a low-severity local-only information disclosure, but it is worth noting for auditors and future network-exposure scenarios.

**Recommendation**: Add `@require_api_key` to `health_dashboard`, or at minimum add a comment documenting that it is intentionally public and why.

---

### MEDIUM

#### M-1 — CORS default is `"*"` with `supports_credentials=True`

**Lines**: 71–78, `core/config.py` line 188

The default `CORS_ORIGINS = "*"` combined with `supports_credentials=True` is technically an invalid combination (browsers refuse credentialed requests to wildcard origins per CORS spec), but it signals that the default configuration was not hardened for production. Any deploy where `CORS_ORIGINS` is left at `"*"` and the server is exposed on a network interface will accept cross-origin requests from any origin without restriction.

**Recommendation**: Change the default `CORS_ORIGINS` to `""` (empty = no CORS) or `"http://localhost:*"` for local dev. Document that `"*"` is a developer convenience value that must not be used in production.

---

#### M-2 — Rate limiting uses in-memory storage; resets on server restart

**Lines**: 99–106

`storage_uri="memory://"` means rate-limit state is lost on every process restart. An attacker who triggers a crash or a scheduled restart resets their window immediately. Additionally, the `key_func=get_remote_address` will always return `127.0.0.1` for all callers on a localhost-only deployment, meaning the 10/min transcription limit is shared across *all* callers, not per-client.

**Recommendation**: If multi-user or abuse-resistance is a concern, switch to `storage_uri="redis://..."` or persist state to disk. Document the shared-IP caveat in the configuration comments.

---

#### M-3 — Vocabulary endpoint: no per-word sanitisation beyond length truncation

**Lines**: 852–853

```python
new_words = [str(w).strip()[:MAX_WORD_LENGTH] for w in new_words if str(w).strip()]
```

Words are length-truncated but otherwise untouched. STT vocabulary words are later injected into Whisper's `initial_prompt` (via `transcript_context.py`). If a malicious word contains special Unicode control characters, Whisper-specific injection patterns, or null bytes, they could influence transcription output unexpectedly.

**Recommendation**: Add a vocabulary word character-class allowlist (letters, digits, hyphens, apostrophes, spaces) consistent with what Whisper accepts in `initial_prompt`. Reject or strip control characters and non-printable codepoints.

---

#### M-4 — SSE `/v1/events` and WebSocket `/ws/events` have no auth or rate limit

**Lines**: 966–992 (`events_stream`), 1054–1072 (`ws_events`)

Both streaming endpoints are entirely unauthenticated and have no rate-limit decorator. Each connection subscribes to the global `EventBus` queue. An attacker or misbehaving client could open many concurrent SSE/WS connections, holding `queue.Queue` objects in memory and accumulating events. There is no connection count cap.

**Recommendation**:
1. Add `@require_api_key` to both streaming routes.
2. Add a server-side concurrent connection counter with a configurable max (e.g., 20) to prevent resource exhaustion.

---

### LOW

#### L-1 — `transcribe_audio` exception handler swallows the exception traceback silently in non-debug mode

**Lines**: 953–956

```python
except Exception:
    logger.exception("Ошибка при обработке аудио-запроса")
    metrics.record(0, 0, is_error=True)
    return jsonify({"error": "Internal processing error"}), 500
```

`logger.exception()` logs the full traceback to the server log — good. The response correctly returns only a generic message — no stack trace leaks to the client. This is correct behaviour; documenting as low only because the Russian-language log message may be missed by non-Russian operators scanning logs for errors.

**Recommendation**: Consider emitting an additional English-language structured log field (`"event": "transcription_error"`) alongside the Russian message for interoperability with log aggregators and Sentry breadcrumbs.

---

#### L-2 — Temporary upload file uses `uuid4().hex[:12]` — 12 hex chars = 48 bits of entropy

**Lines**: 895

```python
temp_path = TEMP_DIR / f"{uuid.uuid4().hex[:12]}_{filename}"
```

48 bits is adequate for collision avoidance but below the recommended 128-bit entropy floor for security-sensitive path components. A predictable temp file location is not exploitable here (the directory is inside the data dir, not `/tmp`), but it is worth flagging as a habit issue.

**Recommendation**: Use the full UUID4 hex string (32 chars = 128 bits) or `secrets.token_hex(16)`.

---

#### L-3 — `_build_dashboard_html` uses f-string interpolation into HTML without escaping

**Lines**: 536–768

Values from health-check subsystems (`details_parts`, status strings, `platform_str`, `version`) are interpolated directly into the HTML response via f-strings. If any subsystem returns a string containing `<`, `>`, `"`, or `&` (e.g., a file path or error message), the output could be malformed HTML. In the most extreme case (a health-check detail value containing `<script>`), this would be a self-XSS — only exploitable by someone who can already control what the health checker returns, which in the current codebase means already-privileged code.

**Recommendation**: HTML-escape all dynamically interpolated values using `html.escape()` before insertion. This is a defensive hygiene fix.

---

#### L-4 — `logging.basicConfig(level=logging.INFO)` called at module scope

**Lines**: 38

Calling `basicConfig` at module import time configures the root logger globally. If this module is imported into a process that has already configured logging (e.g., when the REST server is imported as a WSGI module alongside other components), the `basicConfig` call silently becomes a no-op (first call wins). The net effect depends on import order, which may differ between production and test.

**Recommendation**: Remove the `basicConfig` call and rely on the caller (`__main__` block or WSGI entry point) to configure root-logger handlers. The module-level `logger = logging.getLogger("KrabEar.REST")` is sufficient.

---

#### L-5 — `/v1/readiness` calls a static class method on `BackendService`

**Lines**: 825

```python
report = BackendService._build_readiness_report_static()
```

This imports and calls `BackendService` from the REST server, tightly coupling the REST server to the IPC backend service implementation. If `BackendService` changes its internal structure (e.g., during future extraction waves), this coupling could break silently.

**Recommendation**: Extract `_build_readiness_report_static` into a standalone function in `backend/health_checker.py` or `backend/startup_diagnostics.py`, which the REST server can import directly without pulling in all of `BackendService`.

---

#### L-6 — `require_api_key` decorator: mode detection is done on every request

**Lines**: 144–177

The decorator re-reads `settings.REST_API_AUTH_ENABLED` and `settings.REST_API_KEY` on every request. For `REST_API_AUTH_ENABLED=True`, it also calls `_get_rest_auth()` which is a singleton lazy-init (fine). For `REST_API_KEY` (mode 2), `hmac.compare_digest` is called on every request with the key read from `settings`. If `settings` is a cached Pydantic object this is fine; if it re-reads a file it could be expensive. Current implementation reads from a Pydantic-Settings singleton so this is not a performance issue, but the pattern should be noted for future maintainers.

No action required beyond a code comment clarifying this.

---

## Summary table

| ID | Severity | Area | Description |
|----|----------|------|-------------|
| H-1 | HIGH | Auth | `/v1/stt/transcribe` and `/v1/vocabulary` have no auth guard |
| H-2 | HIGH | Info-Disclosure | `/health/dashboard` exposes system internals without auth |
| M-1 | MEDIUM | CORS | Default `CORS_ORIGINS="*"` with `supports_credentials=True` |
| M-2 | MEDIUM | Rate-limit | In-memory rate-limit resets on restart; shared `127.0.0.1` key |
| M-3 | MEDIUM | Input-validation | Vocabulary words not character-class validated before Whisper injection |
| M-4 | MEDIUM | Auth / DoS | SSE and WebSocket endpoints unauthenticated, no connection cap |
| L-1 | LOW | Logging | Russian-only exception message reduces log interoperability |
| L-2 | LOW | Entropy | Temp file prefix uses 48-bit UUID fragment instead of full 128-bit |
| L-3 | LOW | XSS hygiene | Dashboard HTML uses unescaped f-string interpolation |
| L-4 | LOW | Logging | `basicConfig` at module scope may silently no-op |
| L-5 | LOW | Coupling | Readiness endpoint imports `BackendService` directly |
| L-6 | LOW | Code quality | Auth mode detected on every request (minor perf note) |

**Total: 12 findings (0 critical, 2 high, 4 medium, 6 low)**

---

## Positives (what is done well)

- **Generic 500 response**: the `transcribe_audio` exception handler correctly returns `{"error": "Internal processing error"}` — no traceback leaks to clients.
- **HMAC constant-time comparison**: `hmac.compare_digest` is used for the legacy single-key check, preventing timing oracle attacks on token comparison.
- **SHA-256 hashed token storage**: `RestAuth` stores only `SHA-256(token)` in `api_tokens.json`; raw tokens are returned once and never persisted.
- **0600 permissions on token file**: `os.chmod(tmp, S_IRUSR | S_IWUSR)` is applied atomically before rename, so other OS users cannot read the token store.
- **Secure filename sanitisation**: `werkzeug.utils.secure_filename` is applied to uploaded filenames before constructing the temp path.
- **Extension allowlist**: audio uploads are validated against a fixed `ALLOWED_EXTENSIONS` set before saving.
- **Bound enum validation**: `quality_profile`, `cleanup_profile`, and `domain` parameters are validated against explicit `VALID_*` sets before use.
- **`@require_api_key` on sensitive endpoints**: `/metrics`, `/metrics/prometheus`, and `/v1/readiness` are correctly guarded.
- **Localhost-only bind in `__main__`**: `app.run(host="127.0.0.1")` prevents remote exposure in dev mode.
- **`X-Request-ID` tracing header**: every response carries a correlation ID for log correlation.
- **Marshmallow schemas**: request/response schemas are defined for all endpoints, providing a validation layer for structured data.
- **Rate-limit JSON error**: the 429 handler returns `{"error": "rate_limit_exceeded", "retry_after": N}` rather than a Flask HTML error page.

---

## Prioritised recommendations

1. **(H-1)** Add `@require_api_key` to `POST /v1/stt/transcribe`, `GET /v1/vocabulary`, and `POST /v1/vocabulary`. Keep mode-3 pass-through so zero-auth deployments are unaffected.
2. **(H-2)** Add `@require_api_key` to `GET /health/dashboard`.
3. **(M-4)** Add `@require_api_key` to `/v1/events` (SSE) and `/ws/events`. Add a concurrent-connection cap.
4. **(M-3)** Add a character-class allowlist for vocabulary words (strip control characters, restrict to `[\w\s'\-]`).
5. **(M-1)** Change default `CORS_ORIGINS` to empty string (no CORS) or `"http://localhost:*"`; document `"*"` as dev-only.
6. **(L-3)** HTML-escape all dynamic values in `_build_dashboard_html` via `html.escape()`.
7. **(L-4)** Remove `logging.basicConfig(level=logging.INFO)` from module scope.
8. **(L-5)** Extract `_build_readiness_report_static` to `health_checker.py` or `startup_diagnostics.py`.
