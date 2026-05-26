# Krab Ear REST API Reference

Base URL: `http://127.0.0.1:5005`  
Default rate limit: 60 requests/minute (disable via `KRAB_EAR_RATE_LIMIT_ENABLED=false`).  
Auth: optional Bearer token via `KRAB_EAR_REST_API_KEY`. If not set, auth is skipped.  
OpenAPI/Swagger UI: `http://127.0.0.1:5005/api/docs`

---

## Monitoring

### `GET /health`
Liveness check. No auth required. Rate limit: 120/min.  
Returns `200`: `{status: "ok", service: "krab-ear", profile: "<quality_profile>"}`

---

### `GET /metrics`
Aggregated performance and quality metrics.  
Auth required if `REST_API_KEY` is set.

Returns `200`:
```json
{
  "latency_p50_ms": 450.2,
  "latency_p95_ms": 1200.0,
  "latency_p99_ms": 2100.0,
  "confidence_avg": 0.87,
  "request_count": 42,
  "error_count": 1,
  "total_requests": 43,
  "error_rate": 0.023,
  "status": "ok"
}
```

---

### `GET /metrics/prometheus`
Metrics in Prometheus text exposition format 0.0.4.  
Auth required if `REST_API_KEY` is set.  
Content-Type: `text/plain; version=0.0.4; charset=utf-8`

Exposed metrics:
- `krab_ear_transcriptions_total` (counter)
- `krab_ear_errors_total` (counter)
- `krab_ear_confidence_avg` (gauge)
- `krab_ear_uptime_seconds` (gauge)
- `krab_ear_stt_latency_seconds` (histogram, buckets: 0.1–10s)

---

## V1 API (`/v1/`)

### `GET /v1/readiness`
Readiness probe — filesystem check of HuggingFace model cache.  
Auth required if `REST_API_KEY` is set.

Returns `200` when ready, `503` when any required component is missing:
```json
{
  "overall_ready": true,
  "components": {
    "stt": true,
    "diarization": false,
    "translation": true
  }
}
```

---

### `GET /v1/vocabulary`
Returns the current persistent user vocabulary.

Returns `200`: `{words: ["word1", "word2", ...]}`

---

### `POST /v1/vocabulary`
Add words to the persistent vocabulary. Duplicates silently ignored.  
Max vocabulary size: 500 words. Max word length: 100 chars.

Request body (JSON): `{words: ["word1", "word2"]}`

Returns `200`: `{status: "ok", count: <new_total>}`  
Returns `400` if vocabulary limit exceeded.

---

### `POST /v1/stt/transcribe`
Transcribe an audio file to text. Rate limit: 10/min.

Request: `multipart/form-data`

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | file | yes | Audio file (.wav .mp3 .ogg .m4a .flac .opus .webm .mp4 .aac) |
| `quality_profile` | string | no | `fast`, `balanced` (default), `accurate` |
| `cleanup_profile` | string | no | `off`, `soft` (default), `strict` |
| `domain` | string | no | `casual` (default), `finance`, `code`, `conversational`, `medical` |
| `lang_hint` | string | no | ISO 639-1 language code (auto-detected if omitted) |
| `vocabulary` | string | no | Comma-separated hint words |
| `chat_id` + `message_id` | string | no | Idempotency key pair — skips if already processed |

Returns `200`:
```json
{
  "status": "ok",
  "text": "Transcribed text",
  "confidence": 0.91,
  "duration_ms": 1240,
  "engine": "mlx-whisper",
  "model": "mlx-community/whisper-large-v3-turbo",
  "language": "ru",
  "segments": [...],
  "diarization": {...},
  "history_id": "abc123"
}
```

Returns `200` with `{status: "skipped", reason: "duplicate"}` for duplicate idempotency key.  
Returns `400` for missing/unsupported file or invalid params.  
Returns `500` for internal processing error.

---

### `GET /v1/events`
Server-Sent Events (SSE) stream for real-time STT pipeline events.  
Long-lived connection. Keepalive comment (`: ping`) emitted every ~15 seconds.

Event types:
- `event: stt.final` — data: `{history_id, text, confidence, duration_sec, language}`
- `event: stt.failed` — data: `{reason, duration_sec}`

Example:
```
event: stt.final
data: {"history_id": "abc123", "text": "Hello world", "confidence": 0.93, "duration_sec": 2.1, "language": "en"}
```

---

## WebSocket

### `WS /ws/events`
WebSocket endpoint for real-time event streaming.

Query params:
- `types` (optional): comma-separated filter, e.g. `?types=stt.final,translation`

Protocol:
- **Server → Client:** `{type, ts, data}` JSON string
- **Server → Client:** `{"type": "ping"}` heartbeat every 30 seconds
- **Client → Server:** any incoming data is ignored

Example connect: `ws://127.0.0.1:5005/ws/events?types=stt.final`

---

## Error Responses

All endpoints return JSON errors in the format:
```json
{"error": "description"}
```

| Status | Meaning |
|---|---|
| `400` | Bad request (missing field, unsupported format, invalid param) |
| `401` | Missing or invalid API key |
| `429` | Rate limit exceeded — check `Retry-After` header |
| `500` | Internal processing error |
| `503` | Service not ready (readiness check) |

Response headers:
- `X-Request-ID`: UUID for request tracing
- `Retry-After`: seconds until rate limit resets (on 429)

---

## Auth Example

```bash
curl -H "Authorization: Bearer your-api-key" \
     http://127.0.0.1:5005/metrics

curl -F "file=@recording.m4a" \
     -F "quality_profile=balanced" \
     -F "lang_hint=ru" \
     http://127.0.0.1:5005/v1/stt/transcribe
```
