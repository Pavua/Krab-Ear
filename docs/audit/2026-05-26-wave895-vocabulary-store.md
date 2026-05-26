# Audit: backend/vocabulary_store.py — Wave 895

**Date:** 2026-05-26
**File:** `KrabEar/backend/vocabulary_store.py`
**Lines:** 162
**Auditor:** Claude (wave895/conflict-triage branch)

## Summary

`VocabularyStore` is a simple JSON-backed store for persisting user-defined STT hotwords.
Overall quality is good. Four findings — one medium (missing `_tmp` cleanup on save failure),
two low, one informational.

---

## Findings

### F1 — MEDIUM: Tmp file leaks on write failure (save)

**Location:** `save()` lines 122–131

```python
tmp_path = self.path.with_suffix(".json.tmp")
try:
    tmp_path.write_text(...)
    tmp_path.replace(self.path)
except OSError as exc:
    logger.error(...)
    raise
```

If `write_text` succeeds but `replace` raises (e.g., cross-device rename on some macOS
configurations), `vocabulary.json.tmp` is left on disk. The next successful save will
overwrite it, so data loss is not a concern, but the stale `.tmp` file can confuse
monitoring or backup tools.

**Fix:** Add cleanup in the `except` block:
```python
except OSError as exc:
    logger.error("Ошибка сохранения vocabulary.json: %s", exc)
    try:
        tmp_path.unlink(missing_ok=True)
    except OSError:
        pass
    raise
```

---

### F2 — LOW: No per-word length cap

**Location:** `save()` line 117, `add_words()` line 148

Words are filtered for non-empty strings after stripping, but there is no maximum length
check. Whisper `initial_prompt` has a 224-token ceiling; a single pathologically long
word (e.g., a URL accidentally added as vocabulary) could quietly consume a significant
fraction of that budget or cause downstream clipping without any warning.

**Recommendation:** Add a configurable `MAX_WORD_LENGTH` guard (e.g., 80 chars) in
`save()` with a `logger.warning` for rejected words. No functional block needed — just
filter-and-warn.

---

### F3 — LOW: No total vocabulary size cap

**Location:** `save()` line 117

The file accepts an unbounded list of words. At 10,000+ words the `sorted({...})` set
construction is still fast, but the JSON payload passed to Whisper `initial_prompt` will
be silently truncated by the tokenizer without any feedback to the user.

**Recommendation:** Log a warning when `len(unique) > 500` (configurable) and document
that Whisper's effective vocabulary bias is capped by the 224-token initial-prompt window.

---

### F4 — INFORMATIONAL: No threading lock; concurrent callers rely on atomic rename

**Location:** whole module

`VocabularyStore` has no `threading.Lock`. The save path is atomic (tmp→replace via
`Path.replace`) which is safe for single concurrent writer on POSIX. However,
`add_words()` and `remove_words()` both do a read-modify-write cycle:

```python
current = set(self.load())      # read
current.update(new_words)       # modify
self.save(merged)               # write
```

If two threads call `add_words()` simultaneously (e.g., `SmartVocabularyBuilder` and a
user IPC call), the second writer's `load()` can read stale data before the first writer's
`save()` completes, resulting in silently lost words.

**Observed callers:**
- `SmartVocabularyBuilder.auto_update_from_history()` — called from a background thread.
- `BackendService` IPC dispatch — single-threaded (GIL-protected loop), but could change.

**Current risk:** Low — `SmartVocabularyBuilder` runs infrequently and the IPC loop is
single-threaded today. But the invariant is fragile.

**Recommendation:** Add an `threading.RLock` instance attribute and wrap `add_words()` /
`remove_words()` with `with self._lock:` for future safety. Save itself is already atomic
at the filesystem level.

---

## What is working well

- **Atomic write:** `tmp_path.write_text` + `tmp_path.replace` is the correct POSIX
  pattern; no partial-read window.
- **Deduplication:** performed at every `save()` with `sorted({w.strip() ...})` — robust
  and deterministic.
- **Graceful degradation:** corrupted / missing file silently returns `[]` and pushes an
  error to `error_bus` (Phase B.2 wired). No crash path.
- **Type validation on load:** checks `isinstance(payload, dict)` and
  `isinstance(words, list)` before returning, preventing AttributeError on malformed data.
- **Unicode safe:** `ensure_ascii=False` in `json.dumps` and explicit `encoding="utf-8"`
  throughout — Cyrillic and Spanish words round-trip correctly.
- **Error bus integration:** `_push_error` with late-injection pattern matches Phase B
  standards; Sentry fallback on bus failure is correct.

---

## Test coverage assessment

Existing tests (`test_vocabulary_store.py`, `test_vocabulary_store_errors.py`) cover:
save, load, merge, add_words, remove_words, dedup, whitespace stripping, unicode, corrupt
JSON, missing file, wrong JSON shape.

**Not tested:**
- F1: tmp file leak on partial write failure (requires mocking `Path.replace` to raise).
- F2/F3: oversized words / vocabulary count > limit (no limits exist yet).
- F4: concurrent add_words race (not contractually safe today).

---

## Action items

| Priority | Item |
|----------|------|
| Medium | F1: cleanup `.tmp` in `except` block of `save()` |
| Low | F2: add per-word max-length filter with warning |
| Low | F3: log warning when vocabulary grows past ~500 words |
| Low | F4: add `threading.RLock` around read-modify-write in `add_words` / `remove_words` |
