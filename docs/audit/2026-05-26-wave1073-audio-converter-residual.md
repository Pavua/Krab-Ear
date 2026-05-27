# Wave 1073 — AudioConverter Residual Audit (post-W893)

**Date:** 2026-05-26  
**Auditor:** Sub-agent W1073  
**File audited:** `KrabEar/core/audio_converter.py`  
**Scope:** Residual issues NOT addressed by W893 (timeout=60 + tmp cleanup). New findings only.

---

## Summary

6 residual findings across security (1), robustness (3), correctness (1), and test-coverage (1) dimensions. None are critical blockers, but F1 is the most urgent: an ffmpeg hang causes a permanent thread block in production.

---

## F1 — `subprocess.run` Has No Timeout (HIGH)

**Location:** `audio_converter.py:168`

```python
result = subprocess.run(cmd, capture_output=True, text=True, check=False)
```

W893 is described as adding `timeout=60`, but the live code has **no `timeout` parameter**. If ffmpeg hangs (malformed file, codec deadlock, GPU resource contention), the call blocks indefinitely, locking the IPC thread and stalling all queued requests behind it.

`engine.py`'s `brctl download` call at line 269 does have `timeout=5`, but that is a separate subprocess for iCloud download triggering — not the main conversion call.

**Fix:** Add `timeout=300` (5 minutes, consistent with the largest allowed file at ~1 GB) and catch `subprocess.TimeoutExpired`.

```python
try:
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=300
    )
except subprocess.TimeoutExpired:
    Path(dst).unlink(missing_ok=True)
    raise RuntimeError(
        f"ffmpeg превысил лимит времени (300 с) для файла: {input_path}"
    ) from None
```

---

## F2 — `get_audio_info` Crashes with Opaque Error When `soundfile` is Unavailable (MEDIUM)

**Location:** `audio_converter.py:100`

```python
info = sf.info(str(p))
```

`sf` is set to `None` if `import soundfile` fails (lines 19–22). When `sf is None`, the call raises `AttributeError: 'NoneType' object has no attribute 'info'`, which the `except Exception` block on line 109 re-wraps as:

```
RuntimeError: Не удалось получить информацию о файле /path: 'NoneType' object has no attribute 'info'
```

This is an opaque error: the operator sees a cryptic message instead of a clear "soundfile (libsndfile) is not installed" diagnosis.

**Fix:** Add an explicit guard at the top of `get_audio_info`:

```python
if sf is None:
    raise RuntimeError(
        "soundfile (libsndfile) недоступен — установите: pip install soundfile"
    )
```

Same guard should be applied if `convert()` is ever extended to use `sf` directly.

---

## F3 — `output_format` Not Validated: Arbitrary Suffix in `NamedTemporaryFile` (LOW)

**Location:** `audio_converter.py:149–152`

```python
fmt = output_format.lower().lstrip(".")
# ...
handle = tempfile.NamedTemporaryFile(
    prefix="krab_ear_conv_", suffix=f".{fmt}", delete=False
)
```

If `output_format` contains a path separator (e.g. `"../etc/passwd"`), `fmt` becomes `/etc/passwd` and `suffix=f".{fmt}"` becomes `"../etc/passwd"`. `NamedTemporaryFile` will attempt to create the file at a nonsensical path, causing an `OSError` with a confusing message about a missing directory — not a `ValueError` that would make the bad input obvious.

Since `cmd` is a **list** (not `shell=True`), there is no shell injection risk. The risk is purely an uninformative error path.

**Fix:** Validate `fmt` against an allowlist:

```python
SUPPORTED_OUTPUT_FORMATS = {"wav", "mp3", "ogg", "flac", "m4a"}
fmt = output_format.lower().lstrip(".")
if fmt not in SUPPORTED_OUTPUT_FORMATS:
    raise ValueError(
        f"Неподдерживаемый output_format: {output_format!r}. "
        f"Допустимые: {sorted(SUPPORTED_OUTPUT_FORMATS)}"
    )
```

---

## F4 — No `MAX_AUDIO_MB` Enforcement Inside `AudioConverter` (MEDIUM)

**Location:** `KrabEar/core/engine.py:789–791` (guard lives here), `audio_converter.py` (no guard)

The file-size gate (`size_mb > settings.MAX_AUDIO_MB → ValueError`) exists exclusively in `engine.py`. Direct callers of `AudioConverter.convert()` — including `audio_analytics_service.handle_get_audio_info` and any future callers — bypass this check entirely.

Concretely: a user with direct IPC access can call `get_audio_info` or `convert` on a 50 GB file; `AudioConverter` will pass it straight to ffmpeg without objection, potentially exhausting disk and memory.

**Options:**

- **Option A (preferred):** Add a `max_size_mb` optional parameter to `AudioConverter.convert()` and `AudioConverter.get_audio_info()`, defaulted to `None` (no enforcement). `BackendService` / `AudioAnalyticsService` pass `settings.MAX_AUDIO_MB`.
- **Option B:** Read `settings.MAX_AUDIO_MB` directly inside `AudioConverter` (couples core module to config singleton — not recommended).

---

## F5 — iCloud Path Workaround Not Triggered for `get_audio_info` IPC Caller (MEDIUM)

**Location:** `KrabEar/backend/audio_analytics_service.py:133`, `KrabEar/core/engine.py:796–799`

The iCloud copy-to-tmp workaround (`_is_icloud_path` + `_copy_to_tmp_with_icloud_download`) is triggered in `engine.py` before any transcription call. However, `AudioAnalyticsService.handle_get_audio_info` calls `self._audio_converter.get_audio_info(path)` directly, bypassing `engine.py` entirely.

If a user calls `get_audio_info` IPC method on an iCloud path that is an undownloaded placeholder (0-byte stub), `sf.info()` receives a 0-byte file and raises `RuntimeError` — but **the more subtle failure** is when the file is a downloaded-but-locked iCloud file that triggers `errno 11 (EDEADLK)`: `libsndfile` will receive a locked file descriptor and the error message will not mention iCloud at all.

The test `TestICloudPathCopiestoTmp.test_icloud_path_subprocess_eagain_raises_runtime_error` covers the `convert()` path but not the `get_audio_info()` path.

**Fix:** In `handle_get_audio_info`, check `_is_icloud_path(path)` and surface a user-friendly error directing the user to trigger a download first, or perform the brctl+copy before passing to `get_audio_info`.

---

## F6 — No Test Coverage for `sf=None` Degraded Mode (LOW / TEST GAP)

**Location:** `KrabEar/tests/test_audio_converter.py`

The `TestMaxSizeLimit`, `TestAudioInfo`, and `TestExtractMetadata` test classes all rely on `soundfile` being available. There is no test that patches `core.audio_converter.sf = None` and verifies that `get_audio_info` raises a `RuntimeError` with a useful message (rather than a cryptic `AttributeError`).

Until F2 is fixed, this test would document current (bad) behaviour. After F2 is fixed, this test verifies the explicit guard.

**Missing test:**
```python
def test_get_audio_info_soundfile_unavailable(self):
    import core.audio_converter as m
    original = m.sf
    try:
        m.sf = None
        with self.assertRaises(RuntimeError) as ctx:
            self.converter.get_audio_info("/tmp/any.wav")
        self.assertIn("soundfile", str(ctx.exception).lower())
    finally:
        m.sf = original
```

---

## Finding Index

| # | Title | Severity | File | Actionable |
|---|-------|----------|------|-----------|
| F1 | No `timeout` in `subprocess.run` | HIGH | `audio_converter.py:168` | Add `timeout=300` + `TimeoutExpired` handler |
| F2 | Opaque error when `soundfile` is None | MEDIUM | `audio_converter.py:100` | Add explicit `sf is None` guard |
| F3 | `output_format` not validated (arbitrary suffix) | LOW | `audio_converter.py:149` | Validate against allowlist |
| F4 | No `MAX_AUDIO_MB` enforcement in converter | MEDIUM | `audio_converter.py`, `engine.py:789` | Add `max_size_mb` param |
| F5 | iCloud workaround skipped in `get_audio_info` IPC path | MEDIUM | `audio_analytics_service.py:133` | Check iCloud path before sf.info() |
| F6 | No test for `sf=None` degraded mode | LOW | `tests/test_audio_converter.py` | Add mock-patch test |

---

## Not Re-reported from W893

- ffmpeg temp file cleanup on error (`Path(dst).unlink(missing_ok=True)`) — present and correct.
- ffmpeg binary path validation (`os.path.isfile` + `os.access(X_OK)`) — present and correct.
- `_find_ffmpeg` candidate list and `shutil.which` fallback — present and correct.

---

*Generated by Sub-agent W1073. Read-only audit. No source files modified.*
