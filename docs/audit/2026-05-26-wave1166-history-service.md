# W1166 — HistoryService audit (2026-05-26)

Auditor: sub-agent W1166 (claude-sonnet-4-6).
Branch: `audit/history-service-W1166` off `codex/krab-ear-v2` (HEAD `6c900317`).
File audited: `KrabEar/backend/history_service.py` (2900 LOC).

---

## Named-fix merge state

| Fix | Commit | Status |
|-----|--------|--------|
| W869 — path-prefix-collision bypass (`is_relative_to`) | `872b4ff2` | **NOT MERGED into codex/krab-ear-v2** — exists only on audit branches |
| W844 — tz-aware timestamp comparison in `cleanup_old_history` | `82b22518` | **NOT MERGED into codex/krab-ear-v2** — exists only on fix branches |
| W1163 — semantic_search remove on `delete_history_item` | `54c0ea2c` | **NOT MERGED into codex/krab-ear-v2** — exists only on `fix-semantic-search-delete-W1163` |

All three fixes were authored and committed to feature/fix branches but never rebased/merged into the main branch (`codex/krab-ear-v2`). The production code on `codex/krab-ear-v2` still contains the unfixed versions of all three issues.

---

## Findings (5 new)

### F1 — SRT sequence numbers skip when empty turns are present (MEDIUM)

**File:** `history_service.py:756–770`

```python
for seq, turn in enumerate(turns, start=1):   # seq from enumerate
    turn_text = str(turn.get("text", "")).strip()
    if not turn_text:
        continue                               # skip but seq already incremented
    srt_lines.append(str(seq))                # gaps: 1, 3, 4, 6 …
```

`enumerate(turns, start=1)` advances `seq` even for skipped empty turns. SRT spec requires monotonically sequential subtitle numbers (1, 2, 3…); gaps cause many media players (VLC, ffmpeg) to drop or mis-order subtitles. Fix: use a separate `seq` counter that only increments when a subtitle is actually emitted.

**No test covers this.** Existing `test_export_validation.py::test_srt_sequential_numbers` passes because its fixture has no empty turns. A fixture with one empty turn between two real turns would expose the bug.

---

### F2 — Blank line in subtitle text corrupts SRT block separators (MEDIUM)

**File:** `history_service.py:957` (`_build_srt_single`) and `history_service.py:758–770` (multi-turn loop)

SRT format uses a blank line (`\n\n`) as the block separator between subtitle entries. `_build_srt_single` and `handle_export_history_srt` emit `turn_text` and `item.text` without stripping or collapsing embedded newlines. A transcript text containing `\n\n` (two consecutive newlines) splits the SRT block mid-subtitle, causing the rest of the text to be parsed as the sequence number of the *next* subtitle entry.

```python
# _build_srt_single produces:
"1\n00:00:00,000 --> 00:00:03,000\nLine one\n\nLine three\n"
# Reader sees:
# [block 1] text = "Line one"
# [block 2 header] "Line three" — parse error, not a sequence number
```

Fix: normalize `text` and `turn_text` with `" ".join(text.splitlines())` before writing into SRT output, or at minimum replace `\n\n` with `\n`.

---

### F3 — `handle_import_history_ndjson` path traversal via prefix collision (HIGH) — W869 not merged

**File:** `history_service.py:266–268`

```python
allowed_roots = [r.resolve() for r in (...)]
if not any(str(resolved).startswith(str(root)) for root in allowed_roots):
```

`str.startswith` is bypassable via directory-name prefix collision: e.g., if `data_dir = /tmp/data`, a path `/tmp/data-evil/evil.ndjson` passes the check because `"/tmp/data-evil/..."` starts with `"/tmp/data"`. The fix (`Path.is_relative_to(root)`, Python 3.9+) exists in commit `872b4ff2` (W869) but is **not merged** into `codex/krab-ear-v2`.

The same string-prefix guard also appears in `handle_export_obsidian`'s `output_dir_param` path (lines 2102–2106) where there is **no allowlist check at all** — any absolute path is accepted (see F4 below).

---

### F4 — `handle_export_obsidian` and `handle_export_bundle` accept arbitrary `output_dir` (HIGH)

**Files:** `history_service.py:2102–2106`, `history_service.py:2612–2616`

Both methods accept a caller-supplied `output_dir` parameter and call `Path(output_dir_param).expanduser().resolve()` followed by `mkdir(parents=True, exist_ok=True)` and file writes with **no allowlist check**. An IPC caller can write files to any filesystem location accessible to the backend process (e.g., `~/.ssh/`, `/Library/LaunchAgents/`, `/tmp/`, home directory dotfiles).

`handle_import_history_ndjson` (line 267) has a partial guard (the vulnerable `startswith` form), but `export_obsidian` and `export_bundle` have no guard at all. The W869 fix covered only `import_history_ndjson` and `recording_core_service.py`.

Fix: add `is_relative_to` allowlist check in both export methods (same pattern as the W869 fix).

---

### F5 — `handle_cleanup_old_history` leaves semantic-search index stale (LOW)

**File:** `history_service.py:1285–1290`

`handle_cleanup_old_history` tombstone-deletes potentially thousands of items in a single lock window but never calls `self._semantic_searcher.remove(item_id)` for any of them. By contrast, `handle_delete_history_item` gained this call via W1163 (not yet merged, but correct when it lands). The bulk cleanup path is a separate code path that skips semantic cleanup.

After a large cleanup, semantic search returns stale IDs that no longer exist in history, causing callers to receive empty result enrichment or 404-like states when resolving those IDs.

Fix: after the tombstone loop, call `_semantic_searcher.remove(item.id)` for each deleted item (guarded by `if self._semantic_searcher is not None`, same pattern as W1163 uses in `handle_delete_history_item`).

---

## Test coverage gaps

| Area | Status |
|------|--------|
| SRT empty-turn sequence skip (F1) | No test |
| SRT blank-line-in-text (F2) | No test |
| `handle_export_obsidian` path traversal (F4) | No test |
| `handle_export_bundle` path traversal (F4) | No test |
| `handle_cleanup_old_history` semantic index stale (F5) | No test |
| `handle_cleanup_old_history` tz-aware comparison (W844 regression) | Tests exist in `test_history_service_edges.py` but fix not merged |

---

## Items confirmed correct

- **Tombstone compaction correctness**: `_compact_unlocked` correctly holds the `_lock()`, rewrites history to a tmp file then atomically renames, then truncates all delta files. No TOCTOU window.
- **Clipboard history bounds**: `recording_core_service.py:1130–1131` enforces the 20-item cap with `del self._clipboard_history[:-20]`. `handle_get_clipboard_history` further bounds the response to `max=20` via `_coerce_bounded`. Correct.
- **`compact_with_stats` lock correctness**: fully inside `with self._lock()`, no read/compact race.
- **W1163 design**: correct — `handle_delete_history_item` gains `_semantic_searcher.remove(item_id)` after a successful store delete, with exception guard for graceful degradation. Only pending merge into main branch.
- **`handle_cleanup_old_history` lock correctness**: tombstone appends and active count calculation happen inside a single `with self.store._lock()` block — consistent view.

---

## Merge priority

1. **W869** (`872b4ff2`) — path prefix collision fix — HIGH, merge immediately
2. **W1163** (`54c0ea2c`) — semantic delete wiring — MEDIUM, merge after W869
3. **W844** (`82b22518`) — tz-aware timestamp comparison — HIGH (silent cleanup failure), merge immediately
4. **F4** (new) — `export_obsidian`/`export_bundle` no path guard — HIGH, needs new fix
5. **F1** (new) — SRT sequence number skip — MEDIUM, needs new fix
6. **F5** (new) — cleanup_old_history semantic stale — LOW, can batch with F1
7. **F2** (new) — blank line in SRT text — MEDIUM, can batch with F1
