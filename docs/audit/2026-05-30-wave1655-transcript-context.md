# Holistic Audit: `core/transcript_context.py` — `build_initial_prompt()` (W1655)

**Date:** 2026-05-30  
**Auditor:** W1655 (sub-agent)  
**File:** `KrabEar/core/transcript_context.py` (215 LOC)  
**Git history:** 6 commits touching this file (W913/W914/W1293 most recent)  
**Test coverage:** 34 tests in `test_transcript_context.py` + 11 in `test_auto_glossary.py` + 10 in `test_code_switching.py`

---

## Summary

`build_initial_prompt()` assembles the Whisper `initial_prompt` from recent history,
user hotwords, auto-glossary terms, and an optional code-switching hint. The function
has been patched multiple times but W914 (cherry-pick train) clobbered two earlier fixes
(W913 and W1293) and left 4 tests asserting behaviour the code no longer implements.
A separate issue: the caller in `recording_core_service.py` does not gate
`history_context` on `privacy_mode_enabled`, so past transcript text leaks into the
STT prompt even when the user has enabled privacy mode.

**6 findings total (2 HIGH, 2 MEDIUM, 2 LOW).**

---

## F1 HIGH — W914 cherry-pick clobbered W913 max_words=0 guard

**Location:** `KrabEar/core/transcript_context.py` lines 140–214 (function body)  
**Commit chain:**
- W913 (`6a9000de`, 2026-05-27 05:51) added `if max_words <= 0: return ""` guard — correctly fixing the `words[-0:]` Python slice bug that returns the full list instead of empty.  
- W914 (`77cde2f5`, 2026-05-26 05:36, merged *after* W913 via cherry-pick train) replaced the function body and **removed** the guard.

**Current state:** `max_words=0` silently bypasses the cap because `words[-0:]` returns the full list in Python. The test `test_max_words_zero_returns_no_previous_transcript` already documents this as a known quirk with a comment "So this just checks we don't crash" — but the comment was written for the broken state. The fix (W913) existed and was regressed.

**Impact:** Any caller passing `max_words=0` expecting an empty context gets the full 10-item history text injected into the Whisper prompt.

**Fix:** Restore `if max_words <= 0: return ""` at function entry (before `now = time.time()`).

---

## F2 HIGH — W914 cherry-pick clobbered W1293 char-cap (_MAX_PROMPT_CHARS) and logger

**Location:** `KrabEar/core/transcript_context.py`  
**Commit chain:**
- W1293 (`6fb82d5a`, 2026-05-27 03:38) added `_MAX_PROMPT_CHARS = 560`, a post-assembly truncation with rollback to the last clean comma/period, and `logger = logging.getLogger(__name__)`.  
- W914 (`77cde2f5`, 2026-05-26 05:36, merged after W1293) replaced the module and **dropped all three**: `_MAX_PROMPT_CHARS`, the truncation block, and the logger.

**Current state:** There is no character-level cap on the assembled prompt. The word-count caps (`_MAX_WORDS_CYRILLIC = 80`, `_MAX_WORDS_LATIN = 170`) guard only the "Previous transcript" section. The Glossary section (up to 250 terms, no character limit) can produce a prompt of several thousand characters — roughly 750+ BPE tokens — far exceeding the Whisper 224-token `initial_prompt` hard limit. Whisper truncates from the *beginning* of the prompt, so early hotwords are silently discarded. The 4 tests in `TestInitialPromptTokenCap` that assert `len(result) <= 560` **all fail** with the current code.

**Impact:**
- 4 tests in `test_transcript_context.py` (`test_initial_prompt_capped_at_560_chars_cyrillic`, `test_initial_prompt_under_cap_unchanged`, `test_truncation_strips_to_last_complete_term`, `test_truncation_logged`) assert code that does not exist.
- Production: large glossaries silently overflow Whisper's limit; early user hotwords are dropped by Whisper's internal truncation.

**Fix:** Restore the truncation block from W1293: `_MAX_PROMPT_CHARS = 560`, post-assembly truncation to last comma/period, and `logger = logging.getLogger(__name__)`.

---

## F3 MEDIUM — Privacy mode does not gate history injection into STT prompt

**Location:** `KrabEar/backend/recording_core_service.py`, `_stop_recording_phase_c()` (~lines 932–986)  
**Issue:** `_stop_recording_phase_c` unconditionally fetches the last 10 history items and passes them to `transcribe()` as `history_context` regardless of `privacy_mode_enabled`:

```python
_recent_history, _ = self.store.get_history_page(cursor=None, limit=10)
# ... no privacy check ...
transcribe_payload = self.transcriber.transcribe(
    audio,
    history_context=_recent_history if _recent_history else None,
    ...
)
```

`build_initial_prompt()` itself has no `privacy_mode` parameter. Privacy mode is respected in many other paths (SessionTracker, AutoDeduplicator, SentimentTrends, CalendarLinker) but the STT initial_prompt path was missed.

**Impact:** When `privacy_mode_enabled=True`, past transcript text from previous recordings is still injected into the Whisper `initial_prompt` ("Previous transcript: …"), leaking transcription content to the STT model's context window. This violates the user's explicit privacy intent.

**Fix:** In `_stop_recording_phase_c`, add:
```python
_privacy_mode = bool(_cached_settings_hw.get("privacy_mode_enabled", False))
_recent_history = [] if _privacy_mode else _recent_history
```
or pass `history_context=None if privacy_mode else _recent_history` to `transcribe()`.

---

## F4 MEDIUM — Glossary section ordering causes hotwords to be dropped first by Whisper truncation

**Location:** `KrabEar/core/transcript_context.py` lines 179–213 (assembly order)  
**Issue:** The assembled prompt has the structure:

```
"Glossary: term1, term2, ..., term250. Previous transcript: <recent text>"
```

Whisper truncates `initial_prompt` from the **beginning** (keeping the last 224 tokens). When total length exceeds the limit, the `Glossary:` prefix and early hotwords are dropped first, while the "Previous transcript" text (which is less important for boosting specific terminology) survives. Hotwords placed at the end of the glossary list (auto_glossary terms, not user-defined ones) survive; user-defined `stt_hotwords` (prepended, so earlier in the list) are more likely to be truncated.

**Impact:** The intended priority — user hotwords > auto_glossary > history text — is reversed by Whisper's truncation behaviour. Reliable terminology boosting fails silently with large glossaries.

**Fix (combined with F2):** The W1293 char-cap partially mitigated this by truncating from the *end* of the Glossary section before submitting. A complementary fix would reorder sections to put history text first and the Glossary (with important terms) last, so hotwords survive Whisper's keep-tail behaviour.

---

## F5 LOW — Module-level `logger` absent; truncation events are silent in production

**Location:** `KrabEar/core/transcript_context.py`  
**Issue:** W1293 added `logger = logging.getLogger(__name__)` and a `logger.info(...)` call when the char-cap fires. W914 removed both. The current module has no logger. Any future truncation or edge-case events (e.g., `_iso_to_epoch` parse errors returning 0.0) are not observable in production logs.

`_iso_to_epoch` already silently returns `0.0` on parse failure (line 94), causing the item to be treated as "too old" (age = `now - 0.0 ≈ 1.7 billion seconds`). There is no warning logged for this case, making broken timestamps invisible in production.

**Fix:** Restore `import logging; logger = logging.getLogger(__name__)` and add a `logger.debug` or `logger.warning` in `_iso_to_epoch` when it falls through to `return 0.0`.

---

## F6 LOW — No per-transcription caching; rebuilt on every call

**Location:** `KrabEar/core/transcript_context.py`, `KrabEar/backend/recording_core_service.py`  
**Issue:** `build_initial_prompt()` is called once per `stop_recording` invocation, which is acceptable. However there is no memoization even for identical inputs. The `CodeSwitchingDetector` instance is cached at module level (`_detector_cache`), which is correct, but `_get_detector` checks `_detector_cache._threshold != threshold` using a public attribute access on the internal `_threshold` field — this is a fragile coupling to `CodeSwitchingDetector`'s internals.

Additionally, the function is not cached across concurrent calls. For batch transcription jobs (`RecordingCoreService._transcribe_batch`), `build_initial_prompt` could be called many times with the same `history_items` argument; there is no deduplication.

**Impact:** Low — the function is O(n) in history items (n ≤ 10) and cheap. The `_threshold` coupling is a maintenance risk if `CodeSwitchingDetector` renames the attribute.

**Fix (optional):** Access the threshold via a public property or constructor default; add a `functools.lru_cache` on `_get_detector` or make the cache key explicit.

---

## Items verified as correct (no finding)

| Check | Status |
|---|---|
| 30-min window timezone handling | CORRECT — `_iso_to_epoch` uses `calendar.timegm` (UTC) for naive datetimes; handles `+00:00` and `Z` suffixes. |
| Glossary dedup (case-insensitive) | CORRECT — `seen_lower: set[str]` in `build_initial_prompt` lines 182–193. |
| `_MAX_COMBINED_TERMS = 250` enforced | CORRECT — `break` on line 193. |
| Code-switching hint not added when `code_switching_detect=False` | CORRECT — guarded at line 202. |
| History newest-first input, reversed to chronological | CORRECT — `recent.reverse()` line 144. |
| Empty history / empty hotwords edge cases | CORRECT — return `""` without error. |
| `auto_glossary` filler term bias (W1294) | CORRECT — filtering is in `auto_glossary.py`; `build_initial_prompt` receives already-filtered terms. |
| `_MAX_TEXT_BYTES` (W1547) interaction | N/A — text byte cap is in `auto_glossary.py`, not `transcript_context.py`; no double-truncation risk. |

---

## Test coverage summary

- `test_transcript_context.py`: 34 tests — good functional coverage. **4 tests currently assert non-existent char-cap behaviour (F2).**
- `test_auto_glossary.py`: 11 integration tests for `build_initial_prompt` with auto_glossary input — adequate.
- `test_code_switching.py`: 10 tests for the code-switching hook — adequate.
- Missing tests: privacy mode caller-side gating (F3), max_words=0 correct contract (F1 — existing test accepts broken behaviour), logger restored path (F5).

---

## Action items (priority order)

1. **F1 + F2 combined fix** — restore both the `max_words <= 0` early-return and the `_MAX_PROMPT_CHARS = 560` char-cap + logger in `transcript_context.py`. Update the 4 failing `TestInitialPromptTokenCap` tests to be accurate (they already describe the correct expected behaviour). This is a re-application of W913 + W1293 on top of the W914 word-count cap.
2. **F3 fix** — gate `history_context` on `privacy_mode_enabled` in `recording_core_service._stop_recording_phase_c`.
3. **F4 note** — once F2 char-cap is restored, consider moving the Glossary section after "Previous transcript" so user hotwords survive Whisper's keep-tail truncation.
4. **F5 fix** — restore `logger = logging.getLogger(__name__)` and add a `logger.debug` warn in `_iso_to_epoch` on parse failure.
