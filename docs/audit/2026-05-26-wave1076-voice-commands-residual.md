# Audit: voice_commands.py — Residual Issues (W1076)

**Date:** 2026-05-26  
**Scope:** `KrabEar/core/voice_commands.py` post-W994 (lookahead boundary + ES dedup)  
**Branch:** `audit-voice-commands-residual-W1076`  
**Method:** static read + dynamic probing (Python 3.x, worktree env)  
**Not re-reported:** W989 (lookahead boundary), W994 (ES nueva línea dedup first pass)

---

## Summary

5 confirmed residual findings. None are regressions from W994 — they pre-exist or are adjacent.

---

## F1 — ES `nueva línea` Duplicate Still Present (W994 Partial Fix)

**File:** `voice_commands.py:71-72`  
**Severity:** LOW — functional no-op (second entry never matched), but misleading

W994 was supposed to dedup the `nueva línea` duplicate in `_ES_COMMANDS`. The duplicate remains:

```python
(r"nueva línea", "insert", "\n"),   # [11]
(r"nueva línea", "insert", "\n"),   # [12]  ← still here
```

Verified by runtime probe: `_ES_COMMANDS[11] == _ES_COMMANDS[12]`. The second entry is dead (first always matches), but it inflates the compiled pattern list by 1 entry and signals that W994's ES cleanup was incomplete.

**Fix:** Remove line 72 (`KrabEar/core/voice_commands.py:72`).

---

## F2 — Capitalize/Uppercase Flag Not Reset on `delete_last`

**File:** `voice_commands.py:328-336` (`_apply_commands`)  
**Severity:** MEDIUM — state corruption produces unexpected capitalisation after delete

When `delete_last` fires, the local `capitalize_next` and `uppercase_next_sentence` flags are not cleared. The flags survive into subsequent text, producing unintended capitalisation:

```python
# Input: "большая буква удалить последнее слово мир"
# Expected: "мир"
# Actual:   " Мир"  ← flag persists through delete, capitalises "мир"
```

The `delete_last` branch (lines 328-336) modifies `output` but never touches the two boolean state variables. Only the `capitalize_next` branch at line 344 consumes the flag via `capitalize_next = False`.

**Fix:** Add `capitalize_next = False; uppercase_next_sentence = False` inside the `delete_last` branch at line 331 (after `output = [handler(current)]`).

**No test covers this interaction.** Existing `test_delete_then_continue` does not use a preceding capitalize command.

---

## F3 — Leading Space When Capitalize/Uppercase Command Is First Token

**File:** `voice_commands.py:321-326`  
**Severity:** LOW-MEDIUM — cosmetic corruption in edge case

When a capitalize/uppercase command appears at position 0 (no prior text), the code:
1. Tries to pop a trailing space from `output` (no-op, output is empty)
2. Advances `pos` past the command
3. Unconditionally appends `" "` to `output` if `pos < length` (line 325-326)

Result: the next word is prefixed with a leading space:

```python
proc.process("большая буква мир", language="ru")   # → " Мир"
proc.process("capitalize next hello", language="en") # → " Hello"
proc.process("caps lock important.", language="en")  # → " IMPORTANT."
```

The analogous `insert` branch (lines 309-310) already guards against leading spaces by only appending a space when `arg[-1] not in (" ", "\n", "\t")`, but the capitalize branch unconditionally appends.

**Fix:** Change the append at line 325-326 to only fire if `output` is non-empty:
```python
if pos < length and output:   # ← add `and output` guard
    output.append(" ")
```

---

## F4 — EN Single-Word Commands Are High-Frequency False Positives

**File:** `voice_commands.py:100-108`  
**Severity:** MEDIUM — silent data corruption in normal EN dictation

The EN command table includes common English words that have natural meaning in ordinary speech:

| Command | Word boundary safe? | False-positive example |
|---------|---------------------|----------------------|
| `tab`   | yes | `open tab in browser` → `open\tin browser` |
| `period` | yes | `the cretaceous period began` → `the cretaceous. began` |
| `colon` | yes | `clean your colon` → `clean your:` |
| `dash`  | yes | `she made a dash for it` → `she made a- for it` |
| `comma` | yes | `read the comma` → `read the,` |

All five convert correctly when used as dictation commands, but they produce silent corruption when spoken naturally. RU and ES equivalents (`запятая`, `запятой`, `coma`, `punto`) are borrowed punctuation vocabulary with far lower ambiguity in dictation context.

This is a design-level gap, not a W994 regression. No test covers false-positive avoidance for EN single-word commands. The feature is opt-in (`voice_commands_enabled`), but when EN is in `voice_commands_languages` (the default), all EN dictation goes through these patterns.

**Mitigation options** (not implementing here, documenting for roadmap):
- Remove EN single-word commands from the default table and require explicit opt-in.
- Gate EN commands on a dedicated `voice_commands_en_aggressive` setting (default: false).
- Document known false positives in docstring.

---

## F5 — Hot-Path: New `VoiceCommandProcessor` Instance Per Transcription

**File:** `KrabEar/core/engine.py:933`  
**Severity:** LOW — minor overhead (~0.009 ms per call), not a bottleneck

Engine instantiates `VoiceCommandProcessor` on every transcription:

```python
_vc_processor = VoiceCommandProcessor(settings_get=self._settings_get)
```

The compiled regex patterns (`_COMPILED`) are module-level and persist across instances, so no regex recompilation occurs. However, object allocation + lambda closure creation + two `_settings_get` calls on every hot path add ~38% overhead vs a cached instance (measured: 0.034 ms new vs 0.025 ms reuse per 1000 calls; absolute difference is 9 µs).

At current STT throughput (one transcription per 2-30 seconds), this is not measurable in practice. The issue is architectural: the processor should be stored on `AudioEngine` (alongside `LLMRewriter`, `NumberNormalizer` pattern) and re-used across calls.

**Fix:** Move instantiation to `AudioEngine.__init__` as `self._vc_processor`, passing `self._settings_get`. The existing `settings_get` callback already reads runtime settings dynamically, so the instance remains settings-aware.

---

## F6 — `voice_commands_enabled` / `voice_commands_languages` Not in `DEFAULT_SETTINGS`

**File:** `KrabEar/backend/models.py` (`DEFAULT_SETTINGS` dict)  
**Severity:** LOW — silent gap in settings schema

`VoiceCommandProcessor._enabled()` reads `voice_commands_enabled` with a hardcoded default of `True`. `_allowed_languages()` reads `voice_commands_languages` with `["ru", "es", "en"]`. Neither key appears in `DEFAULT_SETTINGS`, so:

- The IPC `get_settings` response never shows these keys.
- The Swift settings panel cannot toggle voice commands via standard settings CRUD.
- `list_profile_presets` presets cannot override them.
- `settings_validator.py` has no schema entry to validate or migrate them.

The settings are runtime-effective if written directly to `settings.json`, but undiscoverable via the normal settings API.

**Fix:** Add both keys to `DEFAULT_SETTINGS` in `KrabEar/backend/models.py`:
```python
"voice_commands_enabled": True,
"voice_commands_languages": ["ru", "es", "en"],
```

---

## Wire Status

`VoiceCommandProcessor` is wired in `core/engine.py` (line 932-942) and fires for every transcription when enabled. There is no IPC handler to toggle `voice_commands_enabled` or `voice_commands_languages` at runtime beyond direct `set_settings` calls. No Swift UI surface exposes these settings (confirmed: no reference in `native/KrabEarAgent/`).

---

## Thread Safety

`VoiceCommandProcessor.process()` is stateless between calls (all mutable state is local to `_apply_commands`). `_COMPILED` module-level dict is written once per language under CPython's GIL — the check-then-write race is benign (idempotent rebuild). Concurrent `process()` calls are safe. The existing `TestConcurrentProcess` test covers this correctly.

---

## Test Coverage Gaps

| Gap | Covering test needed |
|-----|---------------------|
| F1 — ES nueva línea duplicate | `assertLen(_ES_COMMANDS, expected_count)` |
| F2 — capitalize_next survives delete_last | `"большая буква удалить последнее слово мир"` → `"мир"` |
| F3 — leading space when cmd is first token | `"capitalize next hello"` → `"Hello"` (no leading space) |
| F4 — EN false positives (tab, period, colon, dash) | document as known limitation or test with `enabled=False` guard |
| F6 — DEFAULT_SETTINGS contains both keys | integration test via `service.get_settings()` |

---

## Prioritised Action List

| # | Finding | Fix effort | Risk |
|---|---------|-----------|------|
| 1 | F1 — remove remaining ES nueva línea dupe | 1 line | none |
| 2 | F2 — reset capitalize/uppercase on delete_last | 2 lines + test | low |
| 3 | F3 — leading space on first-token capitalize | 1 line guard + test | low |
| 4 | F6 — add keys to DEFAULT_SETTINGS | 2 lines + test | low |
| 5 | F5 — cache processor on AudioEngine | refactor + test | low |
| 6 | F4 — EN false positives | design decision, not code fix | n/a |
