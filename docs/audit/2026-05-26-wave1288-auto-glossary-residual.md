# Audit: core/auto_glossary.py — residual findings (W1288)

**Date:** 2026-05-26
**Auditor:** W1288 sub-agent (re-audit after W1024 + W1098 + W1104)
**Branch:** `audit-auto-glossary-residual-W1288`

---

## Merge state of prior waves

| Wave | Branch | Status |
|------|--------|--------|
| W1024 | `fix-auto-glossary-atomic-privacy-W1024` | **NOT merged** — only on remote branch |
| W1098 | `audit/auto-glossary-W1098` | **NOT merged** — docs only, no code fix |
| W1104 | `wire-auto-glossary-ipc-W1104` | **NOT merged** — IPC handlers unmerged |

All three waves exist as unmerged branches. The `codex/krab-ear-v2` HEAD (`6c900317`) contains none
of their changes. Consequently:

- **W1024 F1/F4 (non-atomic write, privacy disk-skip)**: still present in production code.
- **W1098 F1–F4**: still present (no fix was implemented; W1098 was docs-only).
- **W1104 IPC handlers** (`get_auto_glossary`, `refresh_auto_glossary`): absent from service.py.

The IPC stubs in `test_auto_glossary.py` test *local stub functions*, not actual wired handlers.

---

## Summary

Re-audit found **5 NEW residual issues** beyond W1098's 4 findings.
Highest severity: MEDIUM.

---

## Findings

### F1 — MEDIUM: No cache invalidation after `store.add_history_item()` — stale glossary for 6 hours

**File:** `KrabEar/backend/recording_core_service.py` lines 1105–1124 (phase_e); `KrabEar/core/auto_glossary.py` line 255 (`invalidate()`)

**Description:**
`_stop_recording_phase_c()` calls `self._auto_glossary.build()` (using cached terms) *before*
the new transcription is saved, then `_stop_recording_phase_e()` calls
`self.store.add_history_item(...)` to persist the new history entry.

`AutoGlossaryBuilder.invalidate()` is **never called** after adding the new item. The in-memory
and disk cache (TTL = 6 hours default) is never invalidated on a new transcription. This means:

1. A new proper-noun term spoken for the first time is not available in the glossary for the next
   **6 hours** (until the TTL expires).
2. A deleted/edited history item is never reflected in the glossary until TTL expiry.
3. The `invalidate()` method exists specifically for this use case but has zero production call
   sites — only test code calls it.

This is the primary cache-coherence gap the task description asks about.

**Evidence:**
```bash
grep -rn "\.invalidate()" KrabEar/backend/
# → zero results (only test files call it)
```

**Fix:** After `self.store.add_history_item(...)` in `_stop_recording_phase_e()`, add:
```python
try:
    self._auto_glossary.invalidate()
except Exception:
    pass
```
This forces the next `build()` call (at the next recording stop) to re-read from history,
picking up the just-saved item within one recording cycle instead of 6 hours.

---

### F2 — MEDIUM: Assembled `initial_prompt` string is never character/token-truncated before Whisper

**File:** `KrabEar/core/transcript_context.py` line 25 (`_MAX_COMBINED_TERMS = 250`);
`KrabEar/core/engine.py` line 741 (`dynamic_prompt = f"{context_suffix} {dynamic_prompt}"`);
`KrabEar/core/engine.py` line 1873 (`"initial_prompt": prompt`)

**Description:**
`build_initial_prompt()` caps combined glossary terms at `_MAX_COMBINED_TERMS = 250` entries
(count-based). However Whisper's `initial_prompt` has a hard character limit of ~224 tokens
(≈ 900–1000 characters for mixed RU/EN text). No character/token truncation is applied anywhere
in `engine.py` or `transcript_context.py` before passing `dynamic_prompt` to `mlx_whisper.transcribe()`.

With `auto_glossary_top_n=30` (default) + up to 100 user hotwords + "Previous transcript: N words" +
`TRANSCRIBE_PROMPT` prefix + code-switching hint + speaker-aware hint, the assembled `dynamic_prompt`
easily exceeds 1000+ characters. When it does:

- mlx-whisper silently truncates the prompt at the BPE tokenizer level, discarding the tail.
- The "Previous transcript" context (appended last) is discarded first, defeating its purpose.
- The order of assembly in `engine.py` line 741: `context_suffix` (Glossary + Previous transcript)
  is placed *before* `dynamic_prompt` (TRANSCRIBE_PROMPT), so the glossary terms are typically
  retained but the transcript history context is dropped without any warning.

**Current limit state:**
- `_MAX_COMBINED_TERMS = 250` is not a token budget — a 3-word term like "машинное обучение алгоритмы"
  contributes 3 tokens. With 250 such terms, the Glossary section alone = 750+ tokens = 3× Whisper limit.
- W914 comment in `stt_management_service.py` acknowledges the 224-token limit but only enforces it
  for user-added hotwords (capped at 100). Auto-glossary terms bypass this enforcement.
- `transcript_context.py` `_MAX_COMBINED_TERMS = 250` was set without accounting for multi-word terms.

**Fix:**
1. Add a character guard in `build_initial_prompt()`:
   ```python
   _MAX_PROMPT_CHARS: int = 900  # ~224 tokens for mixed RU/EN
   # After assembling prompt parts, truncate:
   result = " ".join(parts)
   if len(result) > _MAX_PROMPT_CHARS:
       result = result[:_MAX_PROMPT_CHARS].rsplit(" ", 1)[0]
   return result
   ```
2. Reduce `_MAX_COMBINED_TERMS` from 250 to 80 (accounting for multi-word terms averaging 2 tokens each).

---

### F3 — MEDIUM: `_is_capitalized_or_multiword()` false positives — sentence-start common words pass for multi-word n-grams

**File:** `KrabEar/core/auto_glossary.py` lines 30–53;
`KrabEar/core/term_extractor.py` lines 294–309 (`_extract_repeated_ngrams`)

**Description:**
`TermExtractor.extract_terms()` correctly skips position-0 (sentence-start) words for proper-noun
detection. However, the bigram extraction path (`_extract_repeated_ngrams`) does NOT skip
sentence-initial words — it filters only stop-words.

This means a repeated bigram starting with a common capitalized Russian word like "Это важно" or
"Всё правильно" (repeated ≥2 times across history) can enter the frequency counter as a multi-word
phrase. When it reaches `auto_glossary._build_from_history()`, the `_is_capitalized_or_multiword()`
check passes because `" " in term` is `True` for any multi-word phrase — no further validation.

Additionally, `_is_capitalized_or_multiword()` line 46 treats any term where `term[0].isupper()` as
valid. For multi-word bigrams, the term's first character is the first character of the bigram's
first word. If the original text had a sentence-starting word, the bigram inherits its capitalization
and slips through.

**Concrete example:**
History contains 3 transcripts each starting with "Хорошо, давайте обсудим" → bigram "Хорошо давайте"
passes `_is_capitalized_or_multiword()` (starts with capital "Х"), passes the stop-word check
(neither "хорошо" nor "давайте" is in `_STOP_WORDS_RU` — see `term_extractor.py` lines 34–35:
`"хорошо"` and `"давайте"` ARE in `_STOP_WORDS_RU` but `_extract_repeated_ngrams` filters using
`_is_stop_word(w)` which checks both tokens individually, not the bigram as a whole). However
"ладно слышу" (both in stop-words) would still form a bigram via lowercased matches.

**Actual gap:** `_INSTRUCTION_VERBS` in `_looks_like_hallucination()` is short and does not cover
common filler sentence-starters (Хорошо, Понятно, Отлично) that appear at sentence start. These
pass into the glossary and appear in the Whisper prompt, potentially biasing Whisper toward
outputting them.

**Fix:** Add a filter in `_build_from_history` that rejects terms where the first word (lowercased)
appears in a "common filler starters" set:
```python
_FILLER_STARTERS = frozenset({"хорошо", "понятно", "отлично", "значит", "вобщем", "ладно"})
first_word = term.split()[0].lower()
if first_word in _FILLER_STARTERS:
    continue
```

---

### F4 — LOW: W1024 `settings_provider` parameter NOT wired in production — privacy_mode guard is dead code

**File:** `KrabEar/backend/service.py` lines 461–466 (`AutoGlossaryBuilder(...)` constructor call);
`KrabEar/core/auto_glossary.py` line 197 (`settings_provider` parameter, W1024 branch only)

**Description:**
W1024 (unmerged) added a `settings_provider: Optional[Callable[[], dict]]` parameter to
`AutoGlossaryBuilder.__init__()` to allow runtime privacy_mode checking before disk writes.
However, even if W1024 were merged, the production instantiation in `service.py` lines 461–466
does NOT pass `settings_provider`:

```python
# service.py line 461 (current, no settings_provider):
self._auto_glossary = AutoGlossaryBuilder(
    store=self._store,
    data_dir=self._data_dir,
    refresh_hours=settings.AUTO_GLOSSARY_REFRESH_HOURS,
    # ← settings_provider not passed
)
```

This means W1024's privacy_mode disk-skip guard would be permanently `False` (fallback in
`_is_privacy_mode_active()`) even after merging. The privacy guard is dead code without the
instantiation fix. The W1104 IPC handlers do implement a privacy check via
`self._settings_svc.cached_settings().get("privacy_mode_enabled")` but only for the IPC
response — they don't affect the on-disk write path.

**Impact:** When privacy_mode is enabled by the user, `auto_glossary.json` is still written to
disk at the end of each recording, leaking glossary terms (proper nouns extracted from history)
to disk in violation of the privacy mode intent.

**Fix:**
1. In service.py, pass `settings_provider=self._settings_svc.cached_settings` to
   `AutoGlossaryBuilder(...)`.
2. Merge W1024 first.

---

### F5 — LOW: No test for cache-invalidation-after-add flow (production path untested)

**File:** `KrabEar/tests/test_auto_glossary.py`

**Description:**
The test suite (57 tests in `test_auto_glossary.py`) does not have a test that simulates the
production flow:

1. Build glossary (cache hit for 6 hours)
2. Add a new history item to the store (simulating `stop_recording`)
3. Call `build()` again → assert new term appears immediately

The closest test (`test_invalidate_clears_cache`, line 245) tests `invalidate()` in isolation but
does NOT test the `stop_recording` → `add_history_item` → next `build()` pipeline.

The `test_concurrent_build` test (line 566) tests exception safety but not value correctness under
concurrent access — it does not verify the cache contains consistent results after 16 concurrent
`force=True` builds (which overwrites `_cache` without a lock; see W1098 F2 RLock gap, still
unmerged).

This means the F1 production bug (stale glossary after new transcription) has no regression test
preventing reintroduction after fixing.

**Fix:** Add tests:
```python
def test_no_stale_cache_after_add(self):
    store = _FakeStore(items=[_make_item("TensorFlow популярен")])
    builder = AutoGlossaryBuilder(store=store, refresh_hours=24.0)
    result1 = builder.build()  # cache built
    # Simulate add_history_item: new term not in cache yet
    store._items.append(_make_item("NewFramework новый фреймворк"))
    builder.invalidate()  # this is what phase_e should call
    result2 = builder.build()  # must rebuild
    # cache should not be stale
    self.assertNotEqual(result1, result2)  # or verify NewFramework present

def test_concurrent_build_value_consistency(self):
    # After N concurrent builds, all reads return same-length list
    # (no torn write).
```

---

## W1098 findings — still unmerged, all 4 remain open

Per merge state above: W1098 was docs-only; W1024 and W1104 are unmerged. All 4 W1098 findings
are still present in `codex/krab-ear-v2`:

| W1098 Finding | Status |
|---------------|--------|
| F1 — newline/control-char sanitization | Open (no fix merged) |
| F2 — no RLock thread-safety | Open (no fix merged) |
| F3 — source_text PII preference | Open (no fix merged) |
| F4 — no top_n/window_days clamp | Open (no fix merged) |

---

## Interaction summary

| Area | Interaction |
|------|-------------|
| W1024 (unmerged) | Privacy disk-skip is dead without `settings_provider` in service.py (F4 above) |
| W1098 F2 (RLock) | F1 above (concurrent invalidate after add) amplifies the TOCTOU — both are open |
| W914 (hotwords token budget) | Auto-glossary bypasses the hotword 100-entry enforcement entirely (F2 above) |
| `_MAX_COMBINED_TERMS = 250` | Over-generous count ceiling enables silent Whisper prompt overflow (F2 above) |

---

## Recommendations (priority order)

1. **F1** (cache invalidation after add): one-line fix in `recording_core_service._stop_recording_phase_e()`. Highest production impact.
2. **F2** (prompt character truncation): add `_MAX_PROMPT_CHARS = 900` guard in `build_initial_prompt()`; reduce `_MAX_COMBINED_TERMS` from 250 → 80.
3. **W1098 F2** (RLock): add `threading.RLock()` — prerequisite for F1 fix safety. Low effort.
4. **F4** (settings_provider wiring): wire `settings_provider=self._settings_svc.cached_settings` in service.py when merging W1024.
5. **F3** (filler starters): add `_FILLER_STARTERS` filter in `_build_from_history()`.
6. **F5** (test gap): add regression test for cache-invalidation-after-add flow.

---

## Test coverage gaps (W1288 new)

Not covered by existing tests:
- New term appears in glossary within one recording cycle (F1)
- Prompt string length exceeds Whisper token budget (F2)
- Bigram with common filler word passes `_is_capitalized_or_multiword` (F3)
- `settings_provider=None` in production means privacy guard is bypassed (F4)
- Concurrent `build()` + `invalidate()` value correctness (F5 / W1098-F2)
