# W1215 Audit — Silero TTS Adapter (`backend/tts_service.py`)

**Date:** 2026-05-26
**Branch:** audit-silero-adapter-W1215
**File audited:** `KrabEar/backend/tts_service.py` (347 lines)
**Status:** 5 findings (1 HIGH, 2 MEDIUM, 2 LOW)

No standalone `silero_adapter.py` exists. The Silero engine lives entirely inside
`tts_service.py`: module-level `_load_silero()` loader, `TTSService._synthesize_silero()`,
and the public `TTSService.synthesize_speech()` / `handle_synthesize_speech()` IPC handler.

---

## Finding 1 — HIGH: `torch.hub.load` without `trust_repo=True` hangs in headless daemon

**Location:** `tts_service.py:56-61`

Since PyTorch 1.12 (June 2022), `torch.hub.load()` from GitHub shows an
interactive consent prompt unless `trust_repo=True` is passed.  In a headless
daemon / launchd backend process there is no TTY — the prompt hangs silently
and the call never returns, causing a permanent load timeout.

The correct call is:

    torch.hub.load(
        repo_or_dir="snakers4/silero-models",
        model="silero_tts",
        language="ru",
        speaker=model_id,
        trust_repo=True,   # required since PyTorch 1.12
    )

`trust_repo=True` is the documented opt-in; it does not bypass model integrity
checks.  Without this flag the Silero branch is effectively broken in any
PyTorch >= 1.12 environment without an interactive terminal.

**Risk:** Silero TTS silently hangs on first load; RU synthesis dead until restart.

**Fix:** Add `trust_repo=True` to the `torch.hub.load()` call in `_load_silero()`.

---

## Finding 2 — MEDIUM: No Silero speaker/voice ID allowlist — arbitrary string passed to ML model

**Location:** `tts_service.py:183-190`, `tts_service.py:316-318`

The `voice` / `speaker` IPC parameter is sanitised only by `str(...).strip()` then
forwarded verbatim to Silero's `apply_tts(speaker=speaker)`.  Silero v4 has a
fixed set of known speakers (`"baya"`, `"kseniya"`, `"xenia"`, `"aidar"`,
`"eugene"`, `"random"`); an unknown value may raise an unhandled exception or
trigger undefined model behaviour.

By contrast, `_say_to_wav()` correctly validates the voice against
`r"^[a-zA-Z0-9 _\-]+"` — the Silero path has no analogous guard.

**Fix:** Add an explicit allowlist before passing `speaker` to `apply_tts()`:

    _SILERO_SPEAKERS = frozenset({"baya", "kseniya", "xenia", "aidar", "eugene", "random"})

    speaker = voice or settings.TTS_SILERO_VOICE
    if speaker not in _SILERO_SPEAKERS:
        logger.warning("Unknown Silero speaker %r, using default", speaker)
        speaker = settings.TTS_SILERO_VOICE

---

## Finding 3 — MEDIUM: No text-length cap — unbounded input to Silero and macOS say

**Location:** `tts_service.py:259`, `tts_service.py:310`

Neither `synthesize_speech()` nor `handle_synthesize_speech()` enforces a maximum
character count (only an empty-string guard exists).  Multi-megabyte text will:

- Exhaust CPU/RAM in Silero sentence batching.
- Produce very large WAV blobs base64-encoded in a single IPC JSON response —
  potentially blocking the IPC read loop.
- Trigger `E2BIG` / `ARG_MAX` OS errors in `macOS say` (macOS ARG_MAX ~256 KB).

**Fix:** Enforce a hard cap in `handle_synthesize_speech()`:

    MAX_TTS_CHARS = 2000
    text = text[:MAX_TTS_CHARS]

---

## Finding 4 — LOW: No `privacy_mode` gate on `synthesize_speech` IPC handler

**Location:** `backend/service.py:1183`

Krab Ear has a privacy-audit framework (`backend/privacy_audit.py`).
`synthesize_speech` performs no privacy-mode check: when privacy mode is active,
the handler will still synthesize audio from transcript text passed in, making
TTS a potential leak channel if a strict privacy-mode policy is introduced.

**Risk:** Low — TTS is off by default (`TTS_ENABLED=False`).  Severity increases
if a future strict privacy-mode policy is enforced.

**Fix:** Add a runtime-setting check in `handle_synthesize_speech()`:

    if self._get_runtime_setting("privacy_mode", False):
        return {"ok": False, "error": "TTS disabled in privacy mode"}

---

## Finding 5 — LOW: TOCTOU in double-checked locking for lazy model load

**Location:** `tts_service.py:153-161`, `tts_service.py:163-171`

The outer read of `self._silero_attempted` in `_get_silero()` (and the same
pattern in `_get_kokoro()`) occurs without holding the lock.  Under CPython's GIL
this is safe in practice, but:

1. A thread may observe `_silero_attempted = True` while `_silero` is still being
   assigned inside the lock — a genuine data race under free-threaded CPython
   3.13+ or non-CPython runtimes.
2. If `_load_silero()` raises an unhandled exception before setting
   `_silero_attempted = True`, the state may be left inconsistent.

**Fix:** Move the fast-path read inside the lock (simpler and unambiguously safe):

    def _get_silero(self) -> Any | None:
        with self._silero_lock:
            if not self._silero_attempted:
                self._silero = _load_silero(settings.TTS_SILERO_MODEL)
                self._silero_attempted = True
            return self._silero

---

## Test coverage note

`KrabEar/tests/test_tts_service.py` (446 lines) covers: language detection,
fallback chain, IPC handler parameter validation, and a 5-thread concurrent
stress test.  Missing: `_load_silero()` torch.hub path (never tested), the
`trust_repo` hang scenario, and speaker-allowlist validation.  A unit test
patching `torch.hub.load` should assert `trust_repo=True` is passed.

---

## Summary table

| # | Severity | Finding |
|---|----------|---------|
| 1 | HIGH     | `torch.hub.load` missing `trust_repo=True` — hangs in headless daemon |
| 2 | MEDIUM   | No Silero speaker/voice allowlist — arbitrary string to ML model |
| 3 | MEDIUM   | No text-length cap — E2BIG in macOS say / large IPC blobs |
| 4 | LOW      | No `privacy_mode` gate on `synthesize_speech` handler |
| 5 | LOW      | Double-checked locking TOCTOU in `_get_silero()` / `_get_kokoro()` |
