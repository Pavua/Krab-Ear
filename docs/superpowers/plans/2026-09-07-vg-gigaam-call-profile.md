# Voice Gateway GigaAM call profile — implementation plan

**Goal:** дать коротким русским телефонным ходам доступ к уже загруженному
GigaAM-владельцу Krab Ear, сохранив честный auto-language на первом ходе и не
создавая второй ML worker.

**Base:** `origin/codex/krab-ear-v2` at
`7cdb38561ae62d2fca0febf57858db89c28bf199`.

**Runtime boundary:** source/tests only. No `REST_IN_PROCESS_ENABLED`, launchd
restart, live call, process termination, credentials, or production state.

## Confirmed defect

Standalone REST owns `AudioEngine(skip_gigaam_warmup=True)` and calls its own
`Transcriber`, so both omitted and literal `auto` can fall back to global
`TRANSCRIBE_LANGUAGE=ru`, while later `language=ru` cannot reach the owner
GigaAM adapter at all. The comment claiming REST already proxies STT through
BackendService IPC is stale.

## Contract

The existing `POST /v1/stt/transcribe` gains an additive
`request_profile=voice_gateway_call` form value.

- First unknown-language turn: preserve explicit `language=auto` through REST,
  `Transcriber`, and Whisper as `language=None`; do not route it to GigaAM.
- Latched `language=ru`: standalone REST sends one bounded, ephemeral IPC
  request to the live `BackendService`; only its already-loaded GigaAM adapter
  may serve it.
- Non-Russian languages and absent call profile keep current behavior.
- A call-profile result returns factual backend metadata: transport, adapter,
  mode, model, and reason. `gigaam-rnnt` alone is insufficient to prove v3.
- `busy`, `not_ready`, timeout, empty audio, and empty transcription are
  distinct outcomes. They never write history and never cold-load GigaAM.
- Privacy is checked again in the owner process before admission and before
  returning text. Any privacy refusal is HTTP 403 at REST and forbids cloud or
  local fallback in Voice Gateway.

## Task 1: Preserve explicit auto

**Modify:** `KrabEar/backend/rest_server.py`, the narrow language-resolution
seam in `KrabEar/core/engine.py` only if the RED proves it necessary.

1. Add RED tests for omitted/default language versus explicit literal `auto`
   while global `TRANSCRIBE_LANGUAGE=ru`.
2. Carry a request-local explicit-auto sentinel to the Whisper model call and
   assert its final `language` argument is `None`.
3. Keep ordinary omitted-language requests backward compatible.
4. Verify returned `language` is model output, not the forced request hint.

## Task 2: Add a narrow owner-side IPC method

**Modify:** `KrabEar/backend/service.py`.

1. Register `transcribe_ephemeral_call` in the cached dispatch table.
2. Validate a bounded base64 WAV payload below the existing 1 MiB IPC cap,
   `language=ru`, a finite remaining deadline, and a request id included inside
   signed params. Cap the complete JSON/HMAC envelope before connecting.
3. Put atomic loaded-only admission in the router/adapter/session that owns the
   worker, not in `BackendService`: it must serialize with dictation, memory
   conductor and `close_if_idle`, eliminating service-check TOCTOU and lazy
   spawn. Reject absent, cold, closing, or occupied state. Admission waits only
   for a measured bounded slice; no executor queue and no model load.
4. Decode 8 kHz phone WAV and let the existing owner engine perform its
   established 8→16 kHz preparation. Do not use `transcribe_paths`, history,
   translation, diarization, cleanup rewrite, or batch budget.
5. Invoke only the existing GigaAM adapter/session in single-pass mode. Do not
   instantiate `AudioEngine`, `Transcriber`, or a GigaAM worker.
6. Check owner-side privacy before admission and again before releasing text;
   force ephemeral/no-history regardless of REST form input.
7. Keep the admission token and temp audio owned until the real inference
   finishes. If the caller deadline expires, return timeout but drain the late
   result in a tracked owner task and clean up there before accepting another
   request. `begin_shutdown` atomically closes admission before drain starts.
   Drain is bounded; if owner inference is still running at the deadline, close
   reports incomplete and must not close its pipes/transcriber underneath it.
   A repeated close remains idempotent and may finish after the tracked drain.

## Task 3: Add a standalone REST IPC client

**Add:** a small backend module dedicated to one newline-delimited Unix-socket
request, using the canonical socket path and existing `RequestSigner` when IPC
signing is enabled.

1. Apply one absolute deadline to connect, admission, send, receive, and decode.
2. Validate mono PCM WAV, declared and actual sample rate/channels, duration
   `<=25s`, decoded byte count and maximum output samples. Enforce the IPC
   payload limit before connecting and cap the response.
3. Map `busy`, `not_ready`, and timeout to bounded HTTP responses that let Voice
   Gateway fall through to its existing Whisper chain.
4. Do not terminate the standalone REST process for an owner-side GigaAM
   timeout. Its current hard-exit policy remains only for a poisoned local REST
   inference.
5. Keep the existing API-key/privacy gates and force `persist_history=false`
   for the whole call profile even if a client submits `true`.
6. Build the exact signed envelope from the authoritative IPC config using
   `RequestSigner`; include `request_id` in signed params because top-level
   envelope `id` is not covered by HMAC. When signing is enabled, missing or
   invalid signing state fails closed with no unsigned retry.

## Task 4: Deadline and fallback budget

1. Add RED demonstrating the current double wait: singleflight deadline plus a
   second full Future deadline.
2. Compute one monotonic deadline at HTTP entry and pass only its remainder to
   every wait.
3. Reserve time for Voice Gateway's existing fallback. Numerical admission and
   inference budgets remain constants under test until fresh qualification;
   do not invent latency claims from process health.
4. A timed-out owner request remains `busy` until its late result is drained;
   a second phone turn must fail fast instead of starting hidden work.

## Task 5: Evidence

Add focused hermetic tests for:

- omitted language versus explicit auto with global RU;
- `auto` bypasses GigaAM, latched RU uses owner GigaAM;
- 8 kHz WAV accepted and prepared for the adapter;
- invalid/oversize/empty payload;
- worker absent, cold, busy, success, empty result, exception, timeout;
- timeout late-drain and temp cleanup;
- timeout→shutdown, shutdown→admission, and repeated close: admission closes
  atomically, drain is bounded, and an incomplete close preserves live
  pipes/transcriber until the tracked owner work finishes;
- concurrent dictation/memory-conductor/close-if-idle owns or closes the worker
  and call profile fails fast without lazy spawn;
- owner privacy before admission and before returning text, HTTP 403 mapping,
  and forced no-history even when the caller asks to persist;
- IPC signing enabled and disabled, tampered signed params, missing signing
  secret, and proof that no unsigned retry occurs;
- malformed stereo/rate/duration/output-size and full-envelope overflow;
- response metadata identifies actual adapter/mode/model;
- REST never constructs or closes a second GigaAM worker.

Run focused tests first, then the repository's required `make audit-all` and
Python 3.12 parity gate from a clean committed SHA. Separately, Voice Gateway
must test its exact multipart form, HTTP fallback mapping, metadata propagation,
and keep `KRAB_SCREENING_AUTO_LANGUAGE_ENABLED=0` until both SHAs pass.

## Qualification after source acceptance

Requires a separate explicit runtime step. Use owner-approved synthetic or
recorded phone-grade audio, never a new PSTN call by default. Measure warm
p50/p95, 8→16 kHz RU quality, busy/timeout behavior during a real dictation,
and sequential `GigaAM STT → Qwen clone TTS`. Only then choose the production
deadline and enable the Gateway flag.
