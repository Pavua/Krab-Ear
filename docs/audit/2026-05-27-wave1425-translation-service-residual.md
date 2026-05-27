# Wave 1425 — Translation Service Residual Re-Audit

**Date:** 2026-05-27
**Scope:** `KrabEar/backend/translation_service.py`, `KrabEar/backend/translator.py`,
`KrabEar/backend/translation_cache.py`; post-W1175/W1190/W1192/W1318/W1394 re-audit.
**Status:** Read-only audit — 5 findings

---

## Merge State of Prior Waves

| Wave | Commit | Merged to `codex/krab-ear-v2`? |
|------|--------|-------------------------------|
| W1175 | c824eda1 | **YES** — `fix(wave1175): offline_only→offline_strict` (#1081) |
| W1190 | 163f71cd | **NO** — `feat(W1190): wire TranslationCache` never merged |
| W1192 | (no separate commit) | N/A — "W1192" appears only as a PR number alias for W1190 |
| W1318 | a3f18952 | **YES** — `fix(wave1318): TranslationCache key includes network_mode` (#1221) |
| W1319 | 58cee9a9 | **YES** — `fix(wave1319): Translator.clear_cache wipes persistent disk cache` (#1224) |
| W1394 | ff9f3b0a | **YES** — `fix(wave1394): translation_cache unified fsync + TOCTOU + v2 format` (#1299) |
| W1395 | 55845cc3 | **NO** — `fix(wave1395): translation_cache v1→v2 migration with backup` not merged |

**Effective production state on `codex/krab-ear-v2`:**

- W1175 fix is live: `translation_service.py` correctly forces `network_mode="offline_strict"` when `privacy_mode_enabled`.
- W1318 fix is live: `TranslationCache._make_key()` includes `network_mode`.
- W1394 fix is live: `TranslationCache` has fsync, TOCTOU protection, and v2 format.
- W1319 code is present in `translator.py` (lines 115–151) but is **dead code** — it is shadowed by duplicate method definitions added by a subsequent commit (see F1 below).
- W1190 was never merged, so `TranslationCache` remains unwired in production. The `_translation_cache` attribute is never injected into `Translator` by `BackendService`.

---

## Findings

### F1 HIGH — W1319 `clear_cache` and `_check_privacy_mode_changed` are dead code — shadowed by duplicate definitions

**File:** `KrabEar/backend/translator.py`, lines 115–151 (dead) vs lines 184–211 (live)

**Condition:** `translator.py` contains two definitions of each method inside the `Translator`
class body. Python resolves this by using the **last** definition. The first definitions
(lines 115–151), which were introduced by the W1319 fix, call `_translation_cache.clear()`
and accept a `privacy_mode_enabled: bool` parameter. The second definitions (lines 184–211)
silently shadow them:

```python
# Line 115–132: DEAD — W1319 version, clears disk cache and takes bool param
def clear_cache(self) -> None:
    self._cache.clear()
    translation_cache = getattr(self, "_translation_cache", None)
    if translation_cache is not None:
        translation_cache.clear()   # ← this never executes

# Line 184–187: LIVE — older version, no disk layer clear, no-param
def clear_cache(self) -> None:
    with self._cache_lock:
        self._cache.clear()         # ← this is the actual method called

# Line 134–151: DEAD — W1319 version takes privacy_mode_enabled param
def _check_privacy_mode_changed(self, privacy_mode_enabled: bool) -> None: ...

# Line 189–211: LIVE — old version, takes no param, depends on _settings_getter
def _check_privacy_mode_changed(self) -> None: ...
```

The W1319 fix's intent (wipe `_translation_cache` on `clear_cache()`) is fully negated. The
4 failing tests in `test_translator_clear_cache_W1319.py` confirm this at runtime:

```
FAILED TranslatorClearCacheDiskTestCase::test_clear_cache_wipes_disk_persistent
FAILED TranslatorPrivacyModeClearTestCase::test_privacy_mode_toggle_wipes_both_caches
FAILED TranslatorPrivacyModeClearTestCase::test_privacy_mode_true_to_false_also_wipes
FAILED TranslatorPrivacyModeClearTestCase::test_privacy_mode_no_transition_no_clear
```

**Root cause:** The W1319 commit added the new method versions at lines 115–151, but an
earlier (pre-existing) version of the same methods remained at lines 184–211. The duplicate
was introduced when the commit was cherry-picked onto a branch that already contained a
different version of these methods.

**Impact:** HIGH. When W1190 is eventually merged (wiring `_translation_cache`), the live
`clear_cache()` will silently fail to clear the persistent disk cache on privacy mode enable.
Additionally `_check_privacy_mode_changed()` at line 223 is called without arguments, which
matches the dead signature `(self, privacy_mode_enabled: bool)` if the shadow is removed —
i.e. the call site also needs updating. Currently the live version (line 189) accepts zero
args so the call site is compatible.

**Fix:** Remove lines 115–151 entirely (the first definitions) and update the live `clear_cache`
(lines 184–187) to incorporate the disk-cache clear from W1319:

```python
def clear_cache(self) -> None:
    """Clears in-memory LRU and persistent disk cache (W1313 F2 + W1319)."""
    with self._cache_lock:
        self._cache.clear()
    translation_cache = getattr(self, "_translation_cache", None)
    if translation_cache is not None:
        try:
            translation_cache.clear()
        except Exception as exc:
            logger.warning("Translator: disk translation_cache.clear() failed: %s", exc)
```

---

### F2 HIGH — W1190 (`TranslationCache` wiring) not merged — persistent cache is dead at runtime

**File:** `KrabEar/backend/service.py` — `TranslationCache` is not imported; `BackendService`
never instantiates it or injects `_translation_cache` into `self.translator`.

**Condition:** W1394 (merged, #1299) delivers a high-quality `TranslationCache` implementation
with fsync, TOCTOU protection, v2 on-disk format, and network_mode in the key. W1318 (merged,
#1221) adds `network_mode` to `_make_key()`. W1319 (merged, #1224) adds disk-clear on privacy
transition. However, **none of these fixes have any runtime effect** because the
`TranslationCache` object is never constructed and `Translator._translation_cache` is never
set.

The W1190 commit (163f71cd, not merged) would have wired all three by:
1. Adding `from backend.translation_cache import TranslationCache` to `service.py`.
2. Adding `self._translation_cache = TranslationCache(data_dir=store.data_dir)` in
   `BackendService.__init__`.
3. Late-injecting it into `self.translator._translation_cache`.
4. Registering a `clear_translation_cache` IPC handler.

**Impact:** HIGH. Three merged fixes (W1318, W1319, W1394) deliver zero operational value
until W1190 is shipped. All disk-cache privacy protections remain inactive in production.

**Fix:** Merge or re-implement W1190. The content of commit 163f71cd is the reference
implementation. Because W1319 adds a duplicate-method bug (F1 above), the merge order must be:
fix F1 first, then wire W1190.

---

### F3 MED — `translation_service.py` privacy audit log fires on every call when already `offline_strict`

**File:** `KrabEar/backend/translation_service.py`, `handle_translate_text` lines 97–109,
`handle_translate_selection` lines 201–214.

**Condition:** The W1175 fix correctly forces `network_mode = "offline_strict"` when
`privacy_mode_enabled`. However the condition guarding the privacy audit log entry is:

```python
if settings.get("privacy_mode_enabled"):
    if original_network_mode != "offline_strict":
        # log forced_offline event
        ...
    network_mode = "offline_strict"
```

The audit log fires only when `original_network_mode != "offline_strict"`. This is correct
for `handle_translate_text`, where the caller may supply `network_mode` in `params`. But in
`handle_translate_selection` (line 198) `original_network_mode` is read **only from settings**,
not from params:

```python
original_network_mode = str(settings.get("network_mode", "offline_default"))
```

If the user has `privacy_mode_enabled=True` AND `network_mode="offline_strict"` in settings
(the hardened configuration), the forced-offline log never fires — even though every single
translate call is operating under the W1175 protection. The audit log cannot distinguish
"forced offline because privacy is on" from "already offline".

**Impact:** MED. The privacy audit log (`privacy_audit.py`) is used for compliance reporting
(`PrivacyAuditLogger`). Absence of `forced_offline` entries for all calls in strict-offline
mode looks the same as "no protection applied" in an audit trail. Depending on compliance
posture, silent absence of audit entries may be worse than spurious entries.

**Fix:** Change the condition to log on every `privacy_mode_enabled` call regardless of
current `network_mode`, using a distinct `action` value:

```python
if settings.get("privacy_mode_enabled"):
    action = "already_offline_strict" if original_network_mode == "offline_strict" else "forced_offline"
    # log with action=action (rate-limited to avoid log spam)
    network_mode = "offline_strict"
```

---

### F4 MED — `_apply_glossary` uses bare `str.replace` — substring corruption (W935 not merged)

**File:** `KrabEar/backend/translator.py`, `_apply_glossary` line 806.

**Condition:** This finding was reported as W1313 F4 (PR #855 W935 fix open). It remains
live on `codex/krab-ear-v2` as of this audit. `str.replace` matches substring anywhere:

```python
result = result.replace(source, target)
```

Glossary entry `"el" → "the"` corrupts `"elecciones"` → `"theecciones"`. Entry
`"дом" → "house"` corrupts `"домой"` → `"houseой"`. The W935 fix uses
`re.sub(rf"\b{re.escape(src)}\b", target, result)` but the PR remains unmerged.

**Impact:** MED. Glossary entries containing common short words (articles, prepositions,
short nouns) silently corrupt translated output. Translation service delivers wrong text to
the user without any error signal.

**Fix:** Apply the W935 regex-based replacement:
```python
import re
result = re.sub(rf"\b{re.escape(source)}\b", target, result)
```
Note: word boundaries (`\b`) do not work reliably for Cyrillic text in Python's `re` module
(they only anchor on `[A-Za-z0-9_]`). For RU glossary entries, a space/punctuation-aware
pattern is needed instead.

---

### F5 LOW — `_check_privacy_mode_changed` (live version, line 189) silently no-ops when `_error_bus` absent — privacy cache clear fails at startup

**File:** `KrabEar/backend/translator.py`, `_check_privacy_mode_changed` lines 189–211
(the live version after shadowing).

**Condition:** The live `_check_privacy_mode_changed()` at line 195–198 returns early
without any privacy check when `_error_bus` is not attached:

```python
error_bus = getattr(self, "_error_bus", None)
if error_bus is None:
    # Без error_bus не можем получить runtime-настройки — пропускаем проверку.
    return
```

`_error_bus` is a late-injected attribute — it is not set until `BackendService.__init__`
wires it after `Translator()` construction. During the window between `Translator` creation
and `_error_bus` injection (and during any translation call from a test or integration
harness that does not inject `_error_bus`), every `translate()` call skips the
privacy-mode-change check entirely. A privacy mode transition that happens during this window
is silently missed.

The DEAD first definition (W1319 version, lines 134–151) accepted the privacy mode value as a
parameter directly from the caller (no dependency on `_error_bus`), eliminating this gap. The
live version's dependency on `_error_bus` to retrieve settings was introduced as a workaround
but creates a startup race.

**Impact:** LOW. On production startup with launchd (`BackendService.__init__` completes before
any IPC call is served), the gap is ~milliseconds. In integration tests and dev mode without
`_error_bus` injection, every `translate()` call skips the privacy check permanently.

**Fix:** Accept `privacy_mode_enabled` as an optional parameter (matching the dead W1319
signature), or read settings directly from the existing `_settings_getter` attribute
initialized unconditionally during construction (not dependent on error_bus injection).

---

## Test Coverage State

| Test File | Status |
|-----------|--------|
| `test_translation_service_offline_strict_W1175.py` | Passes (W1175 merged) |
| `test_translator_clear_cache_W1319.py` | 4 FAILING (F1 duplicate shadow) |
| `test_translation_cache.py` | Passes (W1394 merged) |
| `test_translator_glossary_deep.py` | Passes (but no word-boundary tests) |

4 tests are failing in `test_translator_clear_cache_W1319.py` due to F1 (duplicate method shadow):
- `test_clear_cache_wipes_disk_persistent`
- `test_privacy_mode_toggle_wipes_both_caches`
- `test_privacy_mode_true_to_false_also_wipes`
- `test_privacy_mode_no_transition_no_clear`

---

## Summary

| # | Severity | Finding | Blocked by |
|---|----------|---------|------------|
| F1 | HIGH | W1319 `clear_cache`/`_check_privacy_mode_changed` dead code — shadowed by duplicate definitions; 4 tests failing | New |
| F2 | HIGH | W1190 (TranslationCache wiring) not merged — W1318/W1319/W1394 have zero runtime effect | W1190 PR open |
| F3 | MED | Privacy audit log silent when already `offline_strict` in settings — compliance gap in `handle_translate_selection` | New |
| F4 | MED | `_apply_glossary` bare `str.replace` substring corruption — W935 fix not merged | W935 open |
| F5 | LOW | Live `_check_privacy_mode_changed` silently no-ops without `_error_bus` — privacy check skipped at startup / in tests | New |

**Recommended fix order:** F1 (remove duplicate shadows in translator.py) → F2 (merge W1190
wiring) → F3 (privacy audit log) → F4 (W935 glossary regex) → F5 (startup race).
