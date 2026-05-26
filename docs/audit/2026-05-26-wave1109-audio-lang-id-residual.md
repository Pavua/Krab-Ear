# W1109 Re-audit: `core/audio_lang_id.py` — Residual Findings

**Date:** 2026-05-26  
**Auditor:** W1109 sub-agent  
**Branch audited:** `codex/krab-ear-v2` (HEAD `69f57bbc`)  
**File:** `KrabEar/core/audio_lang_id.py`

---

## W1090 Merge Status

**W1090 NOT merged.** PR #1004 (`fix-audio-lang-id-W1090`) is still **OPEN** as of 2026-05-26.

The `codex/krab-ear-v2` branch does NOT contain:
- `_ZERO_PEAK_THRESHOLD = 1e-4` (F1: zero-peak short-circuit)
- `MIN_CONFIDENCE = 0.35` (F2: confidence gate before returning language code)

Confirmed by:
```bash
grep "_ZERO_PEAK_THRESHOLD\|MIN_CONFIDENCE" KrabEar/core/audio_lang_id.py
# → no output
```

All findings below are residual issues **beyond** what W1090 addresses. They exist in
both the current `codex/krab-ear-v2` and the W1090 branch.

---

## Findings

### F1 — HIGH: `_model_cache` is a bare class-level `dict` with no thread lock

**Location:** `audio_lang_id.py:43`, `audio_lang_id.py:220-227`

```python
_model_cache: Dict[str, Any] = {}  # line 43

if model_path not in AudioLanguageID._model_cache:   # line 220
    if len(AudioLanguageID._model_cache) >= 1:        # line 222
        AudioLanguageID._model_cache.clear()          # line 224
    ...
    AudioLanguageID._model_cache[model_path] = model  # line 227
```

The cache is a plain `dict` on the class. `_detect_with_mlx()` is called **inside**
`mlx_lock()` context, which is a `threading.RLock`. This means two threads executing
`_run_detect()` are serialized at the `mlx_lock` level, **but only for the inference
step**. The problem surfaces in the check/clear/insert sequence: if two concurrent
`detect()` calls both find the cache miss and both wait on `mlx_lock`, the second one
enters `_detect_with_mlx` after the first exits and may see a stale view. More
critically, `mlx_lock` is a **shared reentrant lock** — any call-site that holds it and
then calls `AudioLanguageID.detect()` re-enters without contention. That means the
cache mutation at lines 222–227 can race if LID is ever called from a code path that
uses a separate thread not holding `mlx_lock`.

**Current W1036 fix** added `RLock` to `search_index.py` for the same pattern. The same
fix is needed here.

**Fix:** add `_cache_lock: threading.RLock = threading.RLock()` as a class attribute and
wrap the cache check/clear/insert block in `with AudioLanguageID._cache_lock:`.

---

### F2 — MEDIUM: No `mx.clear_cache()` call after LID inference

**Location:** `audio_lang_id.py:190-207` (`_run_detect`)

Wave 63 (PR #405) established the rule: call `mx.clear_cache()` after every
`mlx_whisper.transcribe()` invocation to prevent Metal buffer accumulation. The same
applies to `detect_language()` — it also exercises the MLX GPU path.

`engine.py` calls `mx.clear_cache()` at lines 545 and 920 after its own transcription.
But `_run_detect` in `audio_lang_id.py` has **no** `mx.clear_cache()` call after
`mlx_whisper.decoding.detect_language()` completes.

In a session with many short recordings, LID is called once per recording. Without
`mx.clear_cache()`, each call accumulates Metal buffers from the 30-second padded mel
spectrogram computation, potentially contributing to the RAM growth that Wave 63 fixed
in engine.py.

**Fix:** add a `try/except ImportError` block after `_detect_with_mlx` returns to call
`mx.clear_cache()`:
```python
try:
    import mlx.core as _mx
    _mx.clear_cache()
except Exception:
    pass
```

---

### F3 — MEDIUM: FR/TR/PT false positives pass through without filtering (W1019 interaction)

**Location:** `stt_router.py:260-265` (`_LANG_TO_CONFIG_ATTR`), `audio_lang_id.py:279-281`

`AudioLanguageID.detect()` returns the raw ISO 639-1 code from `detect_language()` with
no allowlist filtering. Whisper's language head can return any of 99+ languages —
including `fr`, `tr`, `pt`, `de`, `it`, `nl`, etc. — with non-trivial confidence for
short or ambiguous audio (e.g., Spanish-accented Russian, or a recording starting with
music).

`STTRouter._LANG_TO_CONFIG_ATTR` only maps `ru`, `uk`, `en`, `es`. Any other code
falls through to `STT_OTHER_PRIMARY_MODEL` (line 564). This is a graceful degradation,
**not a crash**, but it causes a silent penalty: a recording that is actually Russian but
detected as `fr` (0.37 confidence) gets routed to the generalist Whisper-large model
instead of GigaAM-RNNT or the RU-specialized model.

W1090's `MIN_CONFIDENCE=0.35` gate reduces but does not eliminate this: a `fr` detection
at 0.36 still passes. The fix here is independent of W1090 — an allowlist of accepted
language codes should be applied before returning from `detect()`.

**Fix:** add a `_ACCEPTED_LANGS = frozenset({"ru", "uk", "en", "es"})` constant and
return `None` (triggering `stt_router` fallback to `"ru"`) when `lang_code` is not in
the allowlist. This is a behavioral contract between LID and router — the router only
knows 4 codes, so returning anything else is noise. Log a debug message for observability.

---

### F4 — MEDIUM: First-call load latency is invisible to callers (no warm-up API)

**Location:** `audio_lang_id.py:220-234` (lazy load inside `_detect_with_mlx`)

The model is loaded lazily on the first `detect()` call. Loading `whisper-large-v3-turbo`
via `mlx_whisper.load_models.load_model()` takes ~1–3 seconds cold. This delay happens
**inside `mlx_lock()`**, blocking all other MLX inference (engine, LLM probe) for the
duration.

There is no `warm_up()` or `preload()` method, and no hook in `BackendService` startup
to pre-warm LID. `BackendService` pre-warms the STT engine (Wave 58 `stt_warmup`) but
not LID. The first recording in a session therefore incurs a silent +1–3s latency spike
that gets attributed to STT rather than LID.

**Fix:** add a `warm_up()` public method that calls `_detect_with_mlx` on a 1-second
silent array and swallow the result. Call it from `BackendService._warmup_stt()` when
`STT_AUDIO_LANG_ID_ENABLED=True`.

---

### F5 — LOW: RU+ES code-switching returns single-language verdict (W1074 interaction)

**Location:** `audio_lang_id.py:258-281`

For mixed RU+ES audio (the project's primary use case), `detect_language()` returns a
single language code — typically the dominant one. When RU and ES are roughly balanced
(e.g., 50/50 bilingual call), Whisper may detect `es` even though RU is also present,
causing the router to select the ES-specialized model. GigaAM (the RU-specialized
adapter) is then skipped.

The tuple result `(lang_code, probs_dict)` contains per-language probabilities, but
`audio_lang_id.py` currently discards `probs_dict` (the second tuple element is only
used for confidence extraction in W1090's pending fix). Even after W1090 merges, the
second-language signal is thrown away.

This is an enhancement gap rather than a bug. The correct fix is out of scope for a
quick patch but should be tracked: for `probs["ru"] > 0.3 AND probs["es"] > 0.3`,
return a special value (or `None`) to let the router pick the multilingual fallback
rather than a specialized model. This prevents wrongly routing bilingual audio to a
single-language adapter.

**Fix (tracked, not urgent):** after W1090 merges, check if both `probs.get("ru", 0)`
and `probs.get("es", 0)` exceed a `_BILINGUAL_THRESHOLD = 0.25` and return `None` to
trigger the `STT_OTHER_PRIMARY_MODEL` generalist path.

---

## Test Coverage Gaps

The existing tests in `test_audio_lang_id.py` and `test_audio_lang_id_cache_limit.py`
cover:
- ✅ Empty / short audio → None
- ✅ Tuple / dict / str result formats
- ✅ Cache hit / miss
- ✅ MLX lock usage
- ✅ Concurrent detect (no crash)
- ✅ Cache bounded to 1 entry

**Missing tests (for new findings):**
- ❌ F1: concurrent cache mutation with two threads both missing cache simultaneously
- ❌ F2: `mx.clear_cache()` called after inference (once F2 fix is applied)
- ❌ F3: language code not in `_ACCEPTED_LANGS` → returns `None` (once F3 fix applied)
- ❌ F4: `warm_up()` method pre-loads model into cache
- ❌ F5: bilingual audio (high probs on both "ru" and "es") → `None` sentinel

---

## Summary Table

| # | Severity | Description | Requires W1090? |
|---|----------|-------------|-----------------|
| F1 | HIGH | `_model_cache` class dict has no RLock — race in check/clear/insert | No |
| F2 | MEDIUM | No `mx.clear_cache()` after LID inference — Metal buffer leak | No |
| F3 | MEDIUM | No allowlist filter; FR/TR/PT codes route to wrong STT adapter | W1090 partially mitigates |
| F4 | MEDIUM | First-call load latency blocks MLX lock, no preload hook | No |
| F5 | LOW | RU+ES code-switching returns single-lang verdict, drops bilingual signal | Post-W1090 enhancement |

**W1090 merge is prerequisite** for F3 to be fully resolved (confidence gate reduces but
does not eliminate false positives). F1, F2, F4 are independent of W1090.
