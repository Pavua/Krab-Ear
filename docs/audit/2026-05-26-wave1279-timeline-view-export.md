# Audit W1279 — timeline_view.py + timeline_export.py

**Date:** 2026-05-26  
**Branch:** audit/timeline-view-export-W1279  
**Files audited:**
- `KrabEar/backend/timeline_view.py` — `TimelineViewGenerator`
- `KrabEar/backend/timeline_export.py` — `TimelineExporter`

**Findings: 5 total (2 MED, 3 LOW)**

---

## F1 — MED: ICS RFC 5545 line-folding violation

**File:** `KrabEar/backend/timeline_export.py`, `export_ical()` lines 288–318

**Problem:** RFC 5545 §3.1 requires that iCalendar lines not exceed 75 octets, with longer lines folded by inserting CRLF + single LWSP (space/tab). The code truncates `summary_raw` to 75 chars but then prepends the `SUMMARY:` prefix (8 chars), yielding `SUMMARY:` + 75 chars = 83 octets. The `DESCRIPTION` line is not truncated at all and can be arbitrarily long (languages list × separator overhead).

**Evidence (confirmed):**
```
SUMMARY line length: 83 (RFC 5545 limit: 75 octets)
DESCRIPTION line length: 117 chars with 12 languages
```

**Impact:** Some calendar clients (Apple Calendar strict-mode, RFC-compliant parsers) reject or silently truncate unfolded lines. Calendar events may not import correctly.

**Fix:** Apply RFC 5545 folding before writing each line:
```python
def _ical_fold(line: str) -> str:
    """Fold iCal line at 75 octets per RFC 5545 §3.1."""
    if len(line.encode()) <= 75:
        return line
    result, cur = [], b""
    for char in line:
        enc = char.encode("utf-8")
        if len(cur) + len(enc) > 75:
            result.append(cur.decode("utf-8"))
            cur = b" " + enc
        else:
            cur += enc
    result.append(cur.decode("utf-8"))
    return "\r\n".join(result)
```
Apply to every appended line in `export_ical()`.

---

## F2 — MED: Raw transcript text leaks into ICS SUMMARY when exporting raw history items

**File:** `KrabEar/backend/timeline_export.py`, `export_ical()` lines 287–291

**Problem:** When `export_ical()` receives raw history items (not aggregated `TimelineBlock` dicts), the fallback chain at line 289 is:
```python
summary_raw = (
    item.get("summary_text")
    or item.get("text", "")[:80]   # <-- full transcript text, first 80 chars
    or "Recording"
)
```
If `summary_text` is absent (which it always is on raw `HistoryItem` dicts), up to 80 characters of the raw transcript are written verbatim into the ICS `SUMMARY` field. This includes PII: phone numbers, names, addresses captured in conversation.

**Evidence (confirmed):**
```
Raw text in ICS SUMMARY: True
SUMMARY:Personal: my phone is 555-1234\, SSN 123-45-6789\, ...
```

There is no `privacy_mode_enabled` check in `TimelineExporter` or in the (currently absent) IPC handler. The `_resolve_export_dir` / `sanitize_path` pattern from W1176 is also not applied since there is no file-writing IPC handler yet (see F3).

**Fix:** Replace the `text` fallback with `"Recording"`:
```python
summary_raw = item.get("summary_text") or "Recording"
```
Or, if text is desired for display, apply `TextAnonymizer` before writing to the export.

---

## F3 — LOW: `_timeline_exporter` instantiated but never reachable via IPC

**File:** `KrabEar/backend/service.py`, lines 21, 439; `KrabEar/backend/timeline_export.py`

**Problem:** `TimelineExporter` is imported and instantiated as `self._timeline_exporter` at service startup, but there is no registered IPC handler that calls any of its methods (`export_svg`, `export_json`, `export_ical`). The only timeline-related IPC handler is `get_timeline_view` which uses `_timeline_view` (the generator), not the exporter.

This means:
1. The export functionality is completely unreachable at runtime.
2. The privacy, path-traversal, and DoS concerns (F2, F4, F5) are currently theoretical — but become real once a handler is added.
3. Memory for the `TimelineExporter` object is allocated at startup for zero benefit.

**Fix:** Either add an IPC handler `export_timeline` that calls the exporter, or remove the instantiation until the handler is ready. If adding the handler, apply `sanitize_path` (from `input_sanitizer.py`) to any output path parameter.

---

## F4 — LOW: SVG tooltip leaks `summary_text` (keyword extract) with no size cap

**File:** `KrabEar/backend/timeline_export.py`, `export_svg()` lines 132–143

**Problem:** The SVG tooltip is:
```python
tooltip = (
    f"{start_ts} | {count} items"
    + (f" | {lang_str}" if lang_str else "")
    + (f" | {duration:.0f}s" if duration else "")
    + (f" | {summary}" if summary else "")  # summary = summary_text[:40]
)
```
`summary_text` is capped at 40 chars, which is reasonable. However, `lang_str` and `start_ts` are uncapped. A crafted `start_time` value from the IPC layer (if the exporter ever gets an IPC handler) or `lang_str` list could produce arbitrarily long tooltips, yielding SVG files of unexpected size. More importantly, `summary_text` in `TimelineBlock` is the top-5 keyword extraction from transcript text — these keywords are often names, proper nouns, or location terms that constitute PII.

**Impact:** LOW (keywords are less sensitive than full text; 40-char cap limits exposure; SVG is already bounded by the block list size which in `get_timeline_view` is capped at 5000 items). Becomes MED if an export-to-file IPC handler is added without privacy guard.

**Fix:** Add a `privacy_mode_enabled` check in any future export handler; strip `summary_text` from SVG tooltips when privacy mode is active.

---

## F5 — LOW: `generate_timeline` / `_build_block` tokenizes all text per item — O(N×T) RAM with long texts

**File:** `KrabEar/backend/timeline_view.py`, `_build_block()` lines 295–299

**Problem:** For every item in a block, `_tokenize(text)` is called and the token list is appended to `all_tokens`:
```python
tokens = [w for w in _tokenize(text) if w not in _STOP_WORDS and len(w) > 2]
all_tokens.extend(tokens)
```
`all_tokens` accumulates across all items in the block before `Counter(all_tokens).most_common(5)` is called. With the service-level cap of 5000 items and realistic texts (~100 words each), this is ~500k tokens in memory, acceptable. However, the `TimelineViewGenerator` API is also exposed as a pure library (`generate_timeline` accepts any `items` list without the cap). If called directly (e.g., in future from a batch export path without the service cap), with long texts per item, RAM usage grows linearly and can reach hundreds of MB (tested: 100k items × 1000-word texts = 210 MB, 74 s).

**Impact:** LOW (service enforces 5000-item cap today; direct library calls are uncommon). The service cap is not documented on the method signature.

**Fix:** Accept a `max_text_tokens_per_item` parameter in `_build_block` and truncate token extraction:
```python
tokens = _tokenize(text)[:500]  # bound per item
```
Or document the cap requirement in the `generate_timeline` docstring.

---

## Topic-shift detection algorithm

`TimelineViewGenerator` does not perform topic-shift detection in the traditional NLP sense. It groups recordings by time window (hour/day/week) and extracts top-5 keywords as `summary_text`. The "topic shift" is implicit: distinct `summary_text` values across consecutive blocks signal a change in dominant vocabulary. This is correct for the declared purpose (CLAUDE.md describes it as "topic-shift timeline from history items"). The algorithm is sound for a heuristic keyword approach.

The `generate_activity_heatmap` matrix logic is correct: UTC-aware cutoff filtering, proper `weekday()` (Mon=0), and string-keyed output for JSON compatibility.

## Path traversal (W1176 pattern)

Neither `timeline_view.py` nor `timeline_export.py` writes to disk — they return strings. There is no IPC handler that writes export output to a file path (see F3). When such a handler is added, `input_sanitizer.sanitize_path` must be applied to any `output_path` IPC parameter before passing to `open()`.

## Privacy mode

No `privacy_mode_enabled` check exists in either file or in the `_handle_get_timeline_view` handler. The timeline view returns keyword summaries (not full text), which is acceptable for the view endpoint. For any future file-export handler, a `privacy_mode_enabled` guard must be added (pattern: `translation_service.py` lines 96, 201).

## Export file size DoS

SVG with 10,000 blocks: ~2 MB, 0.04 s — negligible. The `get_timeline_view` service handler caps input at 5000 items, which bounds downstream SVG/JSON/ICS size to safe levels. No additional guard is needed at the exporter level as long as the service cap is maintained.

## Test coverage

Both files have dedicated test suites (`test_timeline_view.py`, `test_timeline_export.py`) with good coverage: grouping logic, aggregates, heatmap matrix, SVG structure, JSON schema, ICS RFC fields, XML/iCal escaping, concurrent export, and unicode. Missing tests:
- ICS line folding (RFC 5545 §3.1 75-octet limit)
- Raw `text` field leaking into ICS SUMMARY (F2)
- `_timeline_exporter` wiring to IPC (F3)
