# Audit: core/auto_glossary.py — ninth-pass (W1569)

**Date:** 2026-05-29  
**Wave:** W1569  
**Branch:** `codex/krab-ear-v2` @ `83007afa`  
**Files audited:** `KrabEar/core/auto_glossary.py`, `KrabEar/backend/recording_core_service.py`, `KrabEar/backend/history_service.py`, `KrabEar/core/term_extractor.py`  
**Prior waves:** W1012 (first audit), W1024 (atomic write + privacy), W1098 (residual), W1288 (fifth-pass), W1294 (filler bigrams), W1402 (fifth-pass), W1417 (RLock), W1450 (sixth-pass), W1456 (dict-as-list), W1493 (seventh-pass), W1538 (regression scan), W1541 (filler restore), W1547 (byte-cap restore)

---

## W1541 / W1547 Merge State

**Both fixes are NOT in `codex/krab-ear-v2`.**

| Fix | Branch | Commit | Status |
|-----|--------|--------|--------|
| W1541 — restore `_FILLER_STARTERS` + `_starts_with_filler` | `fix/auto-glossary-filler-W1541` | `29647cf9` | NOT merged |
| W1547 — restore `_MAX_TEXT_BYTES` byte-cap | `fix/voice-commands-strict-W1547` | `b9f0a297` | NOT merged |

Verification:
```
$ python3 -c "from core.auto_glossary import _FILLER_STARTERS"
ImportError: cannot import name '_FILLER_STARTERS'
$ python3 -c "from core.auto_glossary import _MAX_TEXT_BYTES"
ImportError: cannot import name '_MAX_TEXT_BYTES'
```

Both regressions were introduced by the W1497 cherry-pick train reverting W1294 (filler bigrams) and an earlier byte-cap wave.

---

## New Findings (W1569)

### N1 — HIGH — `invalidate()` writes disk cache unconditionally, bypassing `privacy_mode`

**File:** `KrabEar/core/auto_glossary.py`, lines 265–270

```python
def invalidate(self) -> None:
    """Сбрасывает кэш — следующий вызов build() пересчитает глоссарий."""
    self._cache = []
    self._cache_built_at = 0.0
    if self._data_dir:
        self._save_cache_to_disk()   # ← no privacy_mode guard
```

`build()` (lines 252–257) correctly guards with `not self._is_privacy_mode_active()` before calling `_save_cache_to_disk()`. `invalidate()` writes an empty `{"terms": [], "built_at": 0.0}` payload unconditionally. While the terms list is empty, the file write itself violates the privacy_mode contract — it proves the glossary-cache mechanism is active, and signals the timestamp when it was invalidated. Fix: mirror the `build()` guard:

```python
def invalidate(self) -> None:
    self._cache = []
    self._cache_built_at = 0.0
    if self._data_dir and not self._is_privacy_mode_active():
        self._save_cache_to_disk()
```

**No test covers `invalidate()` + `privacy_mode=True`** — the existing `TestPrivacyMode*` suite only tests `build()`.

---

### N2 — HIGH — `_load_cache_from_disk()` serves stale pre-privacy data after `privacy_mode` is toggled on

**File:** `KrabEar/core/auto_glossary.py`, lines 214–216 and 368–385

`__init__` calls `_load_cache_from_disk()` unconditionally before `settings_provider` is even set (the provider is wired via `self._settings_provider = settings_provider` at line 208, which precedes the disk load at line 215, but the loaded terms stay live in `self._cache`). If a user transcribed with `privacy_mode=False`, a valid `auto_glossary.json` was written. When the user later toggles `privacy_mode=True` and restarts the backend, `__init__` loads the pre-privacy glossary from disk into `self._cache`. On the first call to `build()`, `_is_cache_valid()` returns `True` (the loaded cache is still fresh), so `build()` **returns the pre-privacy terms without rebuilding** and without triggering the privacy guard.

The correct fix is: in `_load_cache_from_disk`, check privacy_mode (or alternatively, clear `self._cache` at the start of `build()` when privacy_mode is on).

**No test covers this scenario.**

---

### N3 — MED — Cache not invalidated after `add_history_item` in `recording_core_service.py`

**File:** `KrabEar/backend/recording_core_service.py`, line 1129  
**Related:** `KrabEar/core/auto_glossary.py`, `invalidate()` at line 265

After a transcription completes and a new history item is persisted via `self.store.add_history_item(...)` (line 1129), `self._auto_glossary` is never invalidated. The feedback loop is:

```
audio → transcribe → history item saved → auto_glossary should pick up new terms
```

But without an `invalidate()` call post-save, the glossary stays stale for up to `refresh_hours` (default 6 hours). In a long recording session, terms from transcription N won't influence the STT prompt for transcriptions N+1 through N+k (up to 6 hours later). The fix is to call `self._auto_glossary.invalidate()` after `add_history_item` (or use `force=True` in the next `build()` call). Similarly, `handle_delete_history_item` in `history_service.py` does not invalidate the glossary cache after a tombstone delete.

**No test verifies cache invalidation after new items are persisted.**

---

### N4 — MED — `_FILLER_STARTERS` / `_starts_with_filler` gap leaks non-stop-word filler bigrams (W1541 UNMERGED, confirmed live)

**File:** `KrabEar/core/auto_glossary.py` (missing W1541 symbols)

Confirmed live on `codex/krab-ear-v2`:

```python
>>> from core.term_extractor import TermExtractor, _is_stop_word
>>> _is_stop_word('знаешь')   # False — not in stop_words
>>> _is_stop_word('слушай')   # False
>>> _is_stop_word('кстати')   # False
>>> _is_stop_word('значит')   # False
```

`TermExtractor._extract_repeated_ngrams()` only filters stop-words. Since `знаешь`, `слушай`, `кстати`, `значит` are NOT in the unified stop-words set, bigrams like `"знаешь что"` and `"значит нужно"` pass through and reach `_is_capitalized_or_multiword()`, which returns `True` for any phrase with a space. Result: conversational filler bigrams pollute the Whisper `initial_prompt`.

The `_FILLER_STARTERS` / `_starts_with_filler` guard in W1541 specifically targets first-token fillers to block these. Until W1541 is merged, this gap remains open.

**Test files `test_auto_glossary_filler_W1541.py` are on the fix branch, not in main.**

---

### N5 — LOW — `_MAX_TEXT_BYTES` truncation emits a `logger.warning` on every oversized item, not once per session

**File:** `KrabEar/core/auto_glossary.py` (as it will appear after W1547 merges), specifically the truncation block that W1547 adds at `_build_from_history`.

The W1547 implementation logs:
```python
logger.warning(
    "auto_glossary: text truncated to _MAX_TEXT_BYTES (%d bytes)",
    _MAX_TEXT_BYTES,
)
```

This fires once per oversized history item. In a pathological scenario (many very long transcriptions from bulk-imported audio), this floods the log. A typical long recording session importing 50 audio files each > 1 MB produces 50 WARNING lines per `build()` call — and `build()` is called on every transcription start. Fix: use `logger.warning` only on the first truncation per `build()` call and `logger.debug` for subsequent ones (or aggregate with a count: `"Truncated %d/%d items"`).

**No test verifies the warning rate.**

---

## Summary Table

| # | Severity | Component | Description |
|---|----------|-----------|-------------|
| N1 | HIGH | `auto_glossary.py` `invalidate()` | Privacy_mode guard missing — disk write on invalidate violates privacy contract |
| N2 | HIGH | `auto_glossary.py` `__init__` | Stale pre-privacy cache loaded from disk when privacy_mode is toggled on |
| N3 | MED | `recording_core_service.py` | Cache not invalidated after `add_history_item` — feedback loop broken for 6h |
| N4 | MED | `auto_glossary.py` (W1541 unmerged) | Filler bigrams leak through because W1541 symbols are absent from main |
| N5 | LOW | `auto_glossary.py` (W1547 future) | Per-item truncation warning floods log on bulk-import sessions |

**W1541 merge state:** NOT merged (regression from W1497 cherry-pick train)  
**W1547 merge state:** NOT merged (regression from W1497 cherry-pick train)  
**New findings:** 5 (N1–N5)
