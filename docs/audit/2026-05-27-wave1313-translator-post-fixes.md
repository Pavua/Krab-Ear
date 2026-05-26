# Wave 1313 — Translator Post-Fix Re-audit

**Date:** 2026-05-27  
**Branch:** `audit/translator-post-W1313`  
**Scope:** `KrabEar/backend/translator.py` + `KrabEar/backend/translation_cache.py`  
**Prior waves audited:** W1145 (initial), W1161 (cache lock + privacy clear), W1175 (offline_strict), W1190 (TranslationCache wiring)

---

## Merge State of Prior Waves

| Wave | PR | Branch | Status |
|------|-----|--------|--------|
| W1145 | #1054 | `audit-translator-W1145` | **OPEN — NOT MERGED** |
| W1149 | #1064 | `fix-translator-cache-W1149` | **OPEN — NOT MERGED** |
| W1161 | #1071 | `fix-translator-cache-lock-W1161` | **OPEN — NOT MERGED** |
| W1175 | #1081 | `fix-translation-offline-strict-W1175` | **OPEN — NOT MERGED** |
| W1190 | #1102 | `wire-translation-cache-W1190` | **OPEN — NOT MERGED** |

**None of the prior wave fixes are present in `codex/krab-ear-v2`.**  
All findings in the current main-branch `translator.py` represent **live, unmitigated bugs**.

Additional context: W935 (`fix/translator-glossary-regex-W935`, PR #855) which fixes
`_apply_glossary` substring corruption via `re.sub(\b…\b)` is also **NOT MERGED**.

---

## Findings

### F1 — HIGH — Persistent Cache Key Omits `network_mode` (W1190 design defect)

**File:** `KrabEar/backend/translator.py` (W1190 branch, lines 201–224)  
**Present on main:** No (W1190 not merged), but the defect is in the proposed W1190 code.

The W1190 persistent cache uses a key of `(text, mode, style, "persistent")`, deliberately
omitting `network_mode`. This means a successful online translation (`online_opt_in`) is
persisted under the same key as an offline request. On the next call with
`network_mode="offline_default"` or `"offline_strict"`, the persistent cache returns the
previously-computed online result — **bypassing the offline network policy silently**.

```python
# W1190 translator.py lines 202–207 — network_mode absent from key:
persistent_hit = self._translation_cache.get(
    text=clean_text,
    source=normalized_mode,   # "ru_to_es"
    target=normalized_style,  # "neutral"
    engine="persistent",      # network_mode NOT included
)
```

**Scenario:** User translates text once with `online_opt_in`, then enables `privacy_mode`.
Next translate call hits persistent cache with a result obtained from a network model,
even though privacy/offline policy forbids network. In-memory cache correctly includes
`network_mode` in its tuple key (line 183–188 on main).

**Fix:** Add `normalized_network_mode` to the persistent cache key by passing it as part
of `engine` parameter or using a composite source field:

```python
persistent_engine = f"persistent:{normalized_network_mode}"
persistent_hit = self._translation_cache.get(
    text=clean_text, source=normalized_mode, target=normalized_style, engine=persistent_engine
)
```

---

### F2 — HIGH — `_translation_cache.clear()` Not Called on Privacy Mode Enable (W1190)

**File:** `KrabEar/backend/translator.py` (W1190 branch) and main branch  
**Present on main:** Yes (W1190 not merged, privacy_mode cache clear not in main either)

W1161 adds `clear_cache()` on `Translator` that clears the in-memory `self._cache` when
`privacy_mode_enabled` transitions `False→True`. However W1190's `_translation_cache`
(the persistent on-disk `TranslationCache`) is **never cleared** when privacy mode is
enabled. Pre-privacy translations remain readable from disk and are served on future calls.

Neither W1161's `clear_cache()` (which only clears `self._cache`, not `_translation_cache`)
nor W1190's integration code clears the persistent store on privacy mode change.

**Fix:** In `clear_cache()` (W1161 branch), add:

```python
def clear_cache(self) -> None:
    with self._cache_lock:
        self._cache.clear()
    if self._translation_cache is not None:
        self._translation_cache.clear()  # also wipe persistent store
```

---

### F3 — MED — `auto_to_ru` with German Input Produces Wrong `mode` in Result

**File:** `KrabEar/backend/translator.py`, `_translate_single_mode` (line 252–258)  
**Present on main:** Yes (live on `codex/krab-ear-v2`)

When `mode="auto_to_ru"` and the source is German, `_resolve_mode` returns `"de_to_en"`
(an intermediate step because no direct DE→RU Helsinki model exists). The result is then
stored in the in-memory cache under the `auto_to_ru` cache_key, but `_translate_with_model`
is called with `return_mode=resolved_mode` (line 257), so `result.mode` == `"de_to_en"`.

The result is cached at key `("auto_to_ru", style, network_mode, text)` but carries
`mode="de_to_en"`. A consumer reading `result.mode` sees `"de_to_en"` — inconsistent with
the requested `"auto_to_ru"`. Additionally, the result is English (not Russian), since
the chain only does DE→EN; there is no second EN→RU step. The caller receiving an EN
result when they requested `auto_to_ru` is a silent semantic bug.

```python
# Line 252-258 — return_mode should be the original caller mode:
return self._translate_with_model(
    text=text,
    resolved_mode=resolved_mode,      # "de_to_en"
    network_mode=network_mode,
    translation_style=translation_style,
    return_mode=resolved_mode,        # BUG: should be "auto_to_ru" (or trigger two-step chain)
)
```

**Fix (minimal):** Pass `return_mode=mode` (i.e. `"auto_to_ru"`) so the result label is
consistent, and document the DE→RU gap. Full fix requires adding a two-step DE→EN→RU chain.

---

### F4 — MED — `_apply_glossary` Uses Bare `str.replace` — Substring Corruption (W935 open)

**File:** `KrabEar/backend/translator.py`, `_apply_glossary` (line 687–692)  
**Present on main:** Yes (W935 fix PR #855 not merged)

```python
@staticmethod
def _apply_glossary(text: str, glossary: dict[str, str]) -> str:
    result = text
    for source, target in glossary.items():
        result = result.replace(source, target)  # no word boundary
    return result
```

`str.replace` matches anywhere in the string. Glossary entry `"el"→"the"` rewrites
`"elecciones"→"theecciones"`. Cyrillic entry `"дом"→"house"` rewrites
`"домой"→"houseой"`. This corrupts translated output silently. W935 fixes it with
`re.sub(rf"\b{re.escape(src)}\b", target, result)` but the PR is still open.

---

### F5 — LOW — `TranslationCache._persist()` Double-Lock Is a Re-acquisition Pattern

**File:** `KrabEar/backend/translation_cache.py`, `_persist()` (line 109–119)  
**Present on main:** Yes (live on `codex/krab-ear-v2`)

`put()` releases `self._lock` before calling `_persist()`, which then re-acquires
`self._lock` to take a snapshot. Between the unlock and re-lock, another thread can
insert additional entries. The persisted snapshot will include those entries even though
the snapshot was triggered by the `put()` caller only expecting to persist their own entry.
This is a minor logical inconsistency (not a data-loss bug, but the comment "без lock —
вызывается после release" is misleading — it does acquire the lock internally).

More concretely, if two threads call `put()` concurrently, both may call `_persist()`
sequentially, each writing the full current state. This doubles disk I/O on concurrent
translation under load (e.g. live subtitles + manual translate simultaneously).

**Fix:** Use a dedicated `threading.Event` or queue to debounce/coalesce `_persist()`
calls. For now, at minimum update the comment to clarify the lock is re-acquired inside.

---

## Test Coverage Gaps (post-W1161/W1175/W1190)

| Gap | Severity |
|-----|---------|
| No test for persistent cache with `online_opt_in` then `offline_strict` same text (F1) | HIGH |
| No test that `_translation_cache.clear()` is called on privacy mode transition (F2) | HIGH |
| No test for `auto_to_ru` + German input — result lang and `mode` field (F3) | MED |
| Glossary word-boundary tests exist in W935 test file but not merged; no coverage in main | MED |
| No test for concurrent `put()` / `_persist()` interleaving (F5) | LOW |

---

## Summary

5 findings: 2 HIGH, 2 MED, 1 LOW.

- **F1 (HIGH):** W1190 persistent cache key omits `network_mode` — online result served for offline request.
- **F2 (HIGH):** Privacy mode transition clears in-memory cache (W1161) but not persistent `TranslationCache` (W1190).
- **F3 (MED):** `auto_to_ru` + German produces DE→EN result with wrong `.mode` label; no two-step chain.
- **F4 (MED):** `_apply_glossary` uses bare `str.replace` (substring corruption) — W935 fix open but not merged.
- **F5 (LOW):** `TranslationCache._persist()` double-lock re-acquisition causes doubled disk I/O under concurrent load.

Prior wave PRs (#1054, #1064, #1071, #1081, #1102) all remain open and unmerged; none of
their fixes are present in `codex/krab-ear-v2`.
