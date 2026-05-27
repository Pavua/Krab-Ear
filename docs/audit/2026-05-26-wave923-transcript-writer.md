# Wave 923 Audit: TranscriptWriter — Atomic Write, Filename Collision, YAML Escape

**Date:** 2026-05-26  
**File audited:** `KrabEar/backend/transcript_writer.py` (146 lines)  
**Tests checked:** `KrabEar/tests/test_transcript_writer.py`, `KrabEar/tests/test_transcript_writer_coverage.py`  
**Reference:** `KrabEar/backend/obsidian_sync.py` (YAML escape pattern comparison)

---

## Summary

5 findings (2 HIGH, 2 MEDIUM, 1 LOW). The module has solid test coverage for happy paths
but is missing atomic write, a tight filename collision window, no YAML frontmatter
(different from `obsidian_sync.py`), no path-traversal sanitisation for injected data,
and no disk-full guard.

---

## Findings

### [HIGH-1] Non-atomic write — partial files on crash

**Lines:** 143  
**Code:**
```python
file_path.write_text(content, encoding="utf-8")
```

`Path.write_text()` opens the file, writes, and closes in one call — **no tmp+fsync+rename**.
If the process crashes (SIGKILL, power loss, Metal GPU hang) mid-write the `.md` file is
left partially written. Obsidian will silently load a truncated/corrupt document.

`obsidian_sync.py` has the same vulnerability but is a known accepted risk there; here it
is unreviewed and untested.

**Fix pattern:**
```python
tmp = file_path.with_suffix(".tmp")
tmp.write_text(content, encoding="utf-8")
tmp_fd = tmp.open("r")
os.fsync(tmp_fd.fileno())
tmp_fd.close()
tmp.replace(file_path)   # atomic on POSIX
```

---

### [HIGH-2] Filename collision race — two recordings at the same second overwrite each other

**Lines:** 130–143  
**Code:**
```python
filename = f"{date_str}-Транскрибация.md"
file_path = output_dir / filename
if file_path.exists():
    filename = f"{date_str}-Транскрибация-{time_str}.md"
    file_path = output_dir / filename
content = cls.build_content(item)
file_path.write_text(content, encoding="utf-8")
```

The collision guard is a check-then-act race (TOCTOU). If two recordings complete within
the same second and are written concurrently (e.g. from the REST server + IPC server
simultaneously, or from `TestConcurrentWritesSafe` with identical timestamps), both see
`file_path.exists() == False` and the second write silently clobbers the first.

Additionally, if **two recordings happen at the same second**, both produce the same
`time_str` suffix → the second still clobbers the first.

The coverage test `TestConcurrentWritesSafe` avoids this by using distinct hour-spaced
timestamps — the race is untested at sub-second granularity.

**Fix:** add microsecond or UUID4 suffix; use `open(path, "x")` (exclusive create) to
atomically detect collision and retry.

---

### [MEDIUM-3] No path-traversal sanitisation for user-controlled data embedded in filename

**Lines:** 126–140  

The filename is built from `ts` only (fixed `%Y-%m-%d` format), so `ts` itself cannot
inject path separators. However, `output_dir` is passed in from `BackendService` — if
a caller passes a path derived from `recording_id` or `title` (both fully user-controlled),
those values reach `output_dir / filename` with no `resolve()`/`is_relative_to()` guard.

In `service.py`, callers currently pass the fixed `transcripts_dir`:
```python
TranscriptWriter.write_transcript(item, self._transcripts_dir)
```
This is currently safe, but `build_content()` embeds `item["text"]`, `item["translated_text"]`,
and speaker names verbatim into the file body. A speaker name containing `../../` would appear
in the `.md` body but **not** escape the directory. The actual path-traversal risk is LOW
given current callers, but the API has no defensive contract.

**Fix:** add `output_dir = Path(output_dir).resolve()` at the top of `write_transcript`,
and document that `item` values are written verbatim into file content.

---

### [MEDIUM-4] No YAML frontmatter — differs silently from `obsidian_sync.py`

**Lines:** 81–109  

`TranscriptWriter.build_content()` produces **Obsidian-style bold metadata** (e.g.
`**Дата:** …`) but **no YAML frontmatter block** (`---` / `---`). The module docstring
says "Obsidian-совместимый формат" yet Obsidian's property indexing, Dataview plugin,
and templating all rely on actual YAML frontmatter.

Compare `obsidian_sync.py` lines 334–349 which correctly emits:
```yaml
---
title: Транскрипция 2026-05-18
date: 2026-05-18 12:00:00
id: <uuid>
tags:
  - krab-ear
  - transcript
source: krab-ear
---
```

`TranscriptWriter` was apparently written independently and never aligned with the pattern
established in `obsidian_sync.py`. Files from both writers coexist in the same vault
directory, producing inconsistent Obsidian property coverage.

`obsidian_sync.py` also has no `_yaml_scalar` escaping for `title:` or `id:` (values
containing `:`, `"`, or newlines would break YAML parsing); `TranscriptWriter` inherits
the same gap by omission.

**Fix:** add a `---` frontmatter block mirroring `obsidian_sync.py`, and escape string
values (wrap in `"..."`, doubling internal `"`).

---

### [LOW-5] No disk-full guard; `OSError: [Errno 28] No space left on device` surfaces as unhandled exception

**Lines:** 122, 143  

`output_dir.mkdir(parents=True, exist_ok=True)` and `file_path.write_text(...)` both
raise `OSError` on full disk. `write_transcript` has no try/except and no pre-check
(`shutil.disk_usage`). The exception propagates to `BackendService._handle_write_transcript`
caller which logs it as an IPC error — the user sees a generic failure rather than a
"disk full" message.

`DiskSpaceMonitor` (`backend/disk_monitor.py`) runs a background thread that warns at
2 GB free, but that warning fires asynchronously and does not block writes; a quick-fill
scenario (large batch import) can still hit ENOSPC before the monitor fires.

**Fix:** wrap `write_text` in `try/except OSError as exc` and re-raise a typed error or
emit an `error_bus` code (`disk.critical` — already registered as Wave 82).

---

## Test Coverage Assessment

| Area | Covered | Gap |
|------|---------|-----|
| `build_content` happy paths | Yes (8 cases) | No special chars in `title`/speaker name with `../`, `\n`, RTL |
| `write_transcript` file creation | Yes | — |
| Collision guard (different timestamps) | Yes | Same-second race untested |
| Concurrent writes (distinct timestamps) | Yes | Concurrent same-second race not tested |
| Atomic write / crash safety | **No** | — |
| Disk-full ENOSPC | **No** | — |
| Path traversal via `output_dir` | **No** | — |
| YAML frontmatter structure | **No** | Tests check bold `**Дата:**` only |
| Encoding (UTF-8 no BOM) | Implicit | No explicit BOM test |

**Total test methods across both files:** 30 (24 in coverage file + 6 format helpers in
primary test file). Coverage is good for functional behaviour; durability and edge-case
security paths are absent.

---

## Recommended Priority

1. **HIGH-1** (atomic write) — one-liner fix, prevents data loss on crash/SIGKILL
2. **HIGH-2** (collision race) — use `open(..., "x")` with microsecond suffix retry
3. **MEDIUM-4** (YAML frontmatter) — align with `obsidian_sync.py` pattern
4. **MEDIUM-3** (path traversal contract) — add `resolve()` + docstring
5. **LOW-5** (disk-full error) — emit `disk.critical` error code on ENOSPC
