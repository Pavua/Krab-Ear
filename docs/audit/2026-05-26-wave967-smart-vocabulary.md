# Wave 967 Audit — SmartVocabularyBuilder

**File:** `KrabEar/backend/smart_vocabulary.py`  
**Date:** 2026-05-26  
**Auditor:** W967 (read-only)  
**Findings:** 6 (1 HIGH, 3 MEDIUM, 2 LOW)

---

## Summary

`SmartVocabularyBuilder` is a pattern-based vocabulary extractor that analyses transcription history and proposes STT vocabulary hints for Whisper. The class is **wired in production** as a singleton (`self._smart_vocabulary`) in `BackendService.__init__` (line 436) and exposed via the `get_smart_vocabulary_suggestions` IPC handler (line 1117). The `auto_update` method exists but is **never called** from any production code path — it is dead code from the IPC perspective.

Test coverage is solid for the core logic (two test files: `test_smart_vocabulary.py` + `test_smart_vocabulary_extras.py`; concurrency tested). The IPC handler is covered by `test_dispatch_complete.py`.

---

## Findings

### F1 — HIGH: `auto_update` is unwired dead code

**Location:** `smart_vocabulary.py:167–229`, `service.py` (no call site)

`auto_update` reads history, builds a vocabulary update, and writes to `VocabularyStore` — a full persistence cycle. However, `grep` across the entire codebase (backend, native Swift, tests) returns zero production call sites for this method. It is never invoked from `BackendService`, any cron/scheduler, or the IPC dispatch table.

Impact: the incremental vocabulary-building feature is silently a no-op. Any vocabulary words derived from transcription patterns are never actually persisted automatically; the only live path is on-demand `get_smart_vocabulary_suggestions` (which does NOT persist).

**Recommendation:** Either wire `auto_update` to an existing cron/scheduled IPC handler (e.g., triggered post-transcription or via `RecapScheduler`), or document the design intent that persistence is always manual/user-initiated.

---

### F2 — MEDIUM: No vocabulary growth cap — unbounded accumulation

**Location:** `vocabulary_store.py:add_words()`, `smart_vocabulary.py:auto_update()`

`VocabularyStore.add_words()` performs an unlimited union of old + new words and saves back. There is no maximum vocabulary size check anywhere in `SmartVocabularyBuilder` or `VocabularyStore`. In theory, every `auto_update` cycle can only grow the vocabulary (no pruning logic exists).

Since Whisper's `initial_prompt` is length-limited (224 tokens), a large vocabulary is passed to `core/transcript_context.py::build_initial_prompt()` which must silently truncate. The vocabulary file itself can grow indefinitely on disk.

**Recommendation:** Add a `max_vocabulary_size` cap (e.g., 500 words) in `add_words()` or `auto_update()`. Implement a LRU or last-seen-timestamp pruning so stale words are eventually removed. `VocabularyStore` saves `updated_at` but per-word age is not tracked.

---

### F3 — MEDIUM: Partial PII extraction via proper-noun regex

**Location:** `smart_vocabulary.py:267–274` (`_RE_CAPITALIZED_MID`)

The regex `(?<=\s)([А-ЯA-Z][А-Яа-яA-Za-z]{2,})` captures capitalised words in mid-sentence position. When a transcript contains names like "Позвоните Ивану Петрову по адресу", first names and surnames are extracted as "proper noun" vocabulary candidates.

Tested: `"Please contact John.Doe@example.com"` → extracts `John`. Full email addresses are not extracted (the dot stops the match), but given names, surnames, and partial addresses (e.g., street names) that appear in ≥2 transcripts will be silently added to STT vocabulary suggestions.

This is low-severity when `auto_update` is unwired (F1), but becomes a real leak if F1 is fixed without a PII filter.

**Recommendation:** Before `auto_update` is wired, add a privacy-mode gate (see F4). Optionally pipe candidates through `core/text_anonymizer.py` to strip patterns like `[A-Z][a-z]+ [A-Z][a-z]+` that look like full names.

---

### F4 — MEDIUM: No privacy-mode gate

**Location:** `service.py:3547–3563` (`_handle_get_smart_vocabulary_suggestions`)

The IPC handler for `get_smart_vocabulary_suggestions` does not check `privacy_mode` before scanning history and returning candidate words. All other privacy-sensitive operations in `BackendService` check the `privacy` setting from `SettingsService` before exposing history content.

When privacy mode is enabled, history items may contain sensitive transcripts. Returning vocabulary suggestions from those transcripts leaks implicit content (e.g., a proper name appears as a suggestion, revealing it was spoken during a private session).

**Recommendation:** Add a `privacy_mode` check at the top of `_handle_get_smart_vocabulary_suggestions` (pattern: `if self._get_runtime_setting("privacy_mode", False): return {"suggestions": [], "total": 0}`). Mirror this guard in `auto_update` if it gets wired.

---

### F5 — LOW: `\b` word boundary misses Cyrillic-adjacent Latin terms

**Location:** `smart_vocabulary.py:59` (`_RE_CAMEL_CASE`), `smart_vocabulary.py:60–63` (`_RE_TECH_WITH_DIGITS`)

Both regexes use `\b` anchors. In Python's `re` module, `\b` is defined as a transition between `\w` and `\W`. Cyrillic letters ARE `\w`, so `\b` does NOT trigger between a Cyrillic character and a Latin character. Result:

- `"МетодMachineLearning"` — `_RE_CAMEL_CASE` returns **no match** (expected: `MachineLearning`)
- `"ТестDataPipeline"` — same, no match

This is the same class of issue flagged in W926. In practice it only affects concatenated mixed-script tokens (rare in normal speech), but is a silent miss for technical terms typed together with Cyrillic prefixes.

**Recommendation:** Use `(?:^|(?<=[^А-Яа-яA-Za-z\d_]))` as left anchor instead of `\b` for the CamelCase and tech-digit patterns. (Same fix pattern applied in W926.)

---

### F6 — LOW: Thread safety — no lock on concurrent suggest + potential auto_update

**Location:** `smart_vocabulary.py:86–399`, `service.py:436`

`SmartVocabularyBuilder` is stateless (no instance-level mutable state beyond `self.min_word_length` and `self._extractor`). `TermExtractor` is also stateless. Concurrent calls to `get_vocabulary_suggestions` and `build_vocabulary` are therefore safe.

However, if `auto_update` were to be wired (F1), it would call `vocabulary_store.add_words()` which does a `load() → union → save()` read-modify-write cycle without a lock. `VocabularyStore` itself has no internal lock. Concurrent `auto_update` + manual `add_words` from another IPC call (e.g., user adds a word via `add_vocabulary_word`) could cause a lost-update race (last writer wins, earlier write discarded).

**Recommendation:** Before wiring `auto_update`, wrap the `load → merge → save` sequence in a file-lock (same pattern used in `StateStore` — `fcntl.flock`). The fix is contained within `VocabularyStore.add_words()` or a new `atomic_add_words()` helper.

---

## Checklist

| # | Item | Status |
|---|------|--------|
| 1 | Auto-update safety (per-transcript vs batched) | `auto_update` never called — N/A, but design intent unclear |
| 2 | PII in suggestions | Partial: first names extracted; full emails not (see F3) |
| 3 | Confidence threshold | Hardcoded `_LOW_CONFIDENCE_THRESHOLD = 0.65`; `min_frequency=3` default — adequate |
| 4 | Vocabulary growth cap | Missing — unbounded (F2) |
| 5 | Privacy mode gate | Missing in IPC handler (F4) |
| 6 | Thread safety | Stateless class — safe; `auto_update` path has race risk if wired (F6) |
| 7 | Persistence | `VocabularyStore` uses atomic `tmp→replace` — correct |
| 8 | Test coverage | Good: 2 test files, concurrency tested, IPC handler in dispatch suite |
| 9 | Wire status | `get_vocabulary_suggestions` → wired. `auto_update` → **dead code** (F1) |
| 10 | Locale / `\b` correctness | `\b` misses Cyrillic-adjacent Latin tokens (F5, same as W926) |

---

## Action Items

| Priority | Action | Owner |
|----------|--------|-------|
| HIGH | Decide: wire `auto_update` (post-transcription hook) or remove method + doc intent | Backend |
| MED | Add `max_vocabulary_size` cap + per-word timestamp for pruning | Backend |
| MED | Add `privacy_mode` gate to `_handle_get_smart_vocabulary_suggestions` | Backend |
| MED | Add PII filter (names/phones) before `auto_update` is wired | Backend |
| LOW | Fix `\b` anchor for Cyrillic-adjacent Latin CamelCase patterns | Backend |
| LOW | Add file-lock to `VocabularyStore.add_words()` before concurrent wiring | Backend |
