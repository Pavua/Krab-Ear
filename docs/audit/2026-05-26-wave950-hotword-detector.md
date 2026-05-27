# Wave 950 Audit — HotwordDetector

**File:** `KrabEar/backend/hotword_detector.py` (193 lines)
**Date:** 2026-05-26
**Auditor:** W950 sub-agent

---

## Summary

`HotwordDetector` is a lean, well-structured module. Patterns are compiled once on registration (`_register`), `re.escape` is applied to all user input, and `re.IGNORECASE` handles Cyrillic and Spanish accents correctly via Python 3's Unicode-aware regex engine. The word-boundary trap (W926 F1) does NOT apply here — `\b` is used correctly and works for Cyrillic/Spanish. No callbacks, no auto-trigger on transcription, no persistence of matched content. The main real findings are: (1) a silent no-match condition for non-word-char hotwords, (2) no cap on hotwords count/patterns, (3) a minor remove edge case for case-sensitive hotwords.

---

## Findings

### F1 — MEDIUM: Hotwords starting/ending with non-word chars silently never match

**Lines:** `_register()` L80–88

`\b` is a zero-width assertion between a `\w` and a `\W` character. When a hotword begins or ends with a non-word character (punctuation `!`, `@`, `#`, emoji, etc.), the `\b` anchor requires a word character immediately before/after that position — which never occurs when the hotword is surrounded by spaces in a transcript.

**Demonstration:**
```python
import re
p = re.compile(r'\b' + re.escape('!alert') + r'\b', re.IGNORECASE)
p.search(' !alert here')   # None — silent no-match
p.search('x!alert here')   # matches — only if word char precedes '!'
```

Users who add hotwords like `!срочно`, `#важно`, or emoji-prefixed triggers will get zero matches with no error or warning.

**Recommendation:** In `add_hotword()`, detect if the word starts/ends with `\W` and either (a) warn via logger, or (b) conditionally skip leading/trailing `\b` anchors:
```python
leading = r'\b' if re.match(r'\w', word) else r'(?<!\w)'
trailing = r'\b' if re.match(r'\w', word[-1]) else r'(?!\w)'
self._patterns[key] = re.compile(leading + re.escape(word) + trailing, flags)
```

---

### F2 — LOW: No cap on registered hotwords / compiled pattern count

**Lines:** `_register()` L80–88, `handle_add_hotword()` L166–173

`self._hotwords` and `self._patterns` grow unboundedly. Each registered hotword adds one `re.Pattern` object to memory. There is no maximum limit enforced (contrast with `_handle_add_stt_hotword` in `service.py` L3283 which caps at 100 entries with explicit truncation logic).

In practice, hotwords are user-managed and unlikely to reach pathological counts via normal use. But an IPC client could call `add_hotword` in a loop to cause unbounded memory growth.

**Recommendation:** Add a cap (e.g. 500 entries) in `add_hotword()` with a logged warning, consistent with the STT hotwords cap pattern in `service.py`.

---

### F3 — LOW: remove_hotword cannot remove case-sensitive hotwords by alternate case

**Lines:** `remove_hotword()` L109–126

When `add_hotword('Alert', case_sensitive=True)` is called, the storage key is `'Alert'` (original case). `remove_hotword('alert')` tries `key_lower='alert'` then `key_exact='alert'` — neither matches. The hotword cannot be removed without passing the exact same case.

This is not a crash risk but can confuse IPC callers who store the original word string and attempt removal by a lowercased copy.

**Recommendation:** Document this behavior in the docstring, or add a case-insensitive fallback scan over all keys before returning `False`.

---

## Confirmed-Clean Items

| Check | Result |
|---|---|
| **Regex compilation** — hot-path patterns compiled once per registration, not per call | CLEAN — `_patterns` dict with pre-compiled `re.Pattern` |
| **Word boundary (W926 F1 trap)** — `\b` used, not substring match | CLEAN — `\b...\b` applied; verified OK for Cyrillic, Spanish accents, multi-word phrases |
| **Case-folding** — Cyrillic uppercase, Spanish accents | CLEAN — `re.IGNORECASE` with Python 3 Unicode engine handles both correctly |
| **Pattern injection** — user hotwords passed through `re.escape` | CLEAN — `re.escape(word)` at L88 prevents all regex injection |
| **Trigger callback safety** — no callback mechanism | N/A — no callbacks exist; detection is purely on-demand via IPC `check_hotwords` |
| **Privacy mode** — matched transcript content logged/persisted | CLEAN — `HotwordMatch.context` (40-char snippet) is returned to caller only, never logged or persisted |
| **Memory leak** — matched events cached | CLEAN — `check_text()` returns a fresh list per call, no internal cache of results |
| **Wire status** — called in production | CONFIRMED WIRED — `service.py` L459 instantiates it, L1169–1172 registers 4 IPC handlers; but `check_text()` is NOT auto-triggered post-transcription (on-demand only via IPC) |
| **Concurrency** — thread-safe add/remove during active detection | MOSTLY SAFE — `_lock` covers add/remove/get operations; `check_text()` takes a patterns snapshot under lock then reads `_hotwords` outside lock (TOCTOU), but the `None` guard at L142 makes this benign (missed match at worst) |
| **Test coverage** | GOOD — 33 test cases across 4 test classes covering basic CRUD, Unicode, boundaries, IPC handlers, persistence reload |

---

## Wire Status

`HotwordDetector` is wired into `BackendService` as 4 IPC methods:
- `add_hotword` / `remove_hotword` / `get_hotwords` / `check_hotwords`

**Gap:** `check_text()` is never called automatically after a transcription completes. The design is purely reactive (caller must invoke `check_hotwords` IPC manually). No EventBus integration, no post-transcription hook. This means real-time hotword alerting is not implemented in the backend — a caller (Swift agent or external tool) must poll or call explicitly.

---

## Finding Count: 3 (0 HIGH, 1 MEDIUM, 2 LOW)
