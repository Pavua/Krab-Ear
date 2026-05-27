# W1213 — REST server file upload pipeline audit

**Date:** 2026-05-26
**Branch:** audit/audio-quality-residual-W1100
**Auditor:** W1213 sub-agent (read-only, no code changes)
**Scope:** `KrabEar/backend/rest_server.py` — file upload path (`POST /v1/stt/transcribe`) and all
downstream callers: `engine.normalize_audio()`, `transcriber.transcribe()`, `store.add_history_item()`,
`AudioConverter` ffmpeg subprocess.
**Previous wave:** W1207 (covered rate limiting, auth, SSRF surface).

---

## Summary

5 findings (1 HIGH, 2 MED, 2 LOW). No zip-bomb vector (no zip uploads accepted). No path traversal
from constructed temp path (uuid prefix + `secure_filename` output is ASCII-only). Concurrent
same-filename uploads are safe (12-hex uuid prefix guarantees uniqueness). The principal risks are
audio-decoder DoS via oversized/crafted files, privacy-mode bypass, and an extension-only allowlist
without magic-byte verification.

---

## F1 — HIGH: No magic-byte validation; extension-only allowlist bypassed

**File:** `rest_server.py` lines 891-894

```python
filename = secure_filename(file.filename)
ext = os.path.splitext(filename)[1].lower()
if ext not in ALLOWED_EXTENSIONS:
    return jsonify({"error": f"Unsupported file type: {ext}"}), 400
```

The server checks only the filename extension. A caller can upload an HTML, JavaScript, or
deliberately crafted file named `exploit.wav` and it passes straight to `soundfile.read()` and
then to `transcriber.transcribe()` (which eventually calls `mlx_whisper.transcribe`). Known
consequences:

- **soundfile / libsndfile CVEs** — crafted WAV/FLAC/OGG files have historically triggered
  heap overflows in libsndfile (e.g. CVE-2017-8361, CVE-2021-3246). Extension check alone
  provides zero protection.
- **ffmpeg CVEs** — `AudioConverter.convert()` passes the file path directly to ffmpeg
  (`subprocess.run([ffmpeg, "-i", str(src), ...])`). Malformed container files (MP4, MKV,
  WebM) have triggered remote code execution in older ffmpeg builds.
- **mlx-whisper / CoreML parser** — an adversarially crafted WAV header reaching the MLX
  audio encoder is an unexplored attack surface on the local machine.

**Recommended fix:** read the first 16 bytes of the uploaded stream before writing to disk and
validate against known audio magic bytes (RIFF/WAVE `52 49 46 46 … 57 41 56 45`, FLAC `66 4C
61 43`, OGG `4F 67 67 53`, MP3 `FF FB`/`49 44 33`, AAC ADTS `FF F1`/`FF F9`, M4A/MP4 `… 66
74 79 70`, OPUS inside OGG). Only proceed with the extension check after the magic matches.
The `python-magic` or `filetype` package can do this in one call.

---

## F2 — HIGH: Audio decoder DoS via 500 MB crafted file (unbounded RAM + no timeout)

**File:** `rest_server.py` line 42 + lines 899, 917-924

```python
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024   # 500 MB
# ...
engine.normalize_audio(str(temp_path))   # sf.read() — full file into RAM
result = transcriber.transcribe(str(temp_path), ...)    # mlx_whisper — no wall-clock limit
```

The 500 MB upload ceiling is generous for voice recordings (a 1-hour call at 128 kbps MP3 is
~56 MB). When `engine.normalize_audio()` calls `soundfile.read()` on a 500 MB WAV:

- numpy float64 representation is 4× the raw PCM size → **~2 GB RAM spike** on a single
  request.
- A compressed format (FLAC, OGG) can have much higher expansion ratios.
- `transcriber.transcribe()` has no per-request wall-clock timeout at the REST layer;
  an adversarial file that causes mlx-whisper to iterate forever will hold the GIL-adjacent
  MLX lock indefinitely, blocking all concurrent transcription.

This is locally exploitable (REST server binds to 127.0.0.1) but relevant in multi-user
environments and when the REST server is reverse-proxied externally.

**Recommended fixes:**
1. Reduce `MAX_CONTENT_LENGTH` to a sensible cap, e.g. 200 MB (still covers 2+ hour calls).
2. Add a `MAX_AUDIO_DURATION_SEC` guard: after writing to disk, use `soundfile.info()` to
   read only the header and reject if `frames / samplerate > MAX_AUDIO_DURATION_SEC` before
   calling `sf.read()`.
3. Wrap `transcriber.transcribe()` in a `concurrent.futures.ThreadPoolExecutor` with a
   `timeout=` so runaway inference is cancelled and the temp file is cleaned up.

---

## F3 — MED: privacy_mode bypass — REST endpoint writes history unconditionally

**File:** `rest_server.py` lines 928-933

```python
history_item = store.add_history_item(
    text=text,
    chat_id=chat_id or "",
    message_id=message_id or "",
    source_text=result.get("raw_text", text),
)
```

`DEFAULT_SETTINGS` in `core/config.py` defines `privacy_mode_enabled = False` as an
opt-in flag. When a user enables privacy mode through the IPC interface (e.g. `set_settings
{"privacy_mode_enabled": true}`), the intent is that transcripts are **not persisted**
to `history.ndjson`. The IPC path (`service.py`) respects this setting before writing
history. The REST endpoint does **not** check `privacy_mode_enabled` at all — it calls
`store.add_history_item()` unconditionally regardless of the setting value.

A user who believes privacy mode prevents transcript storage will have audio uploaded via
REST silently persisted to disk, violating the privacy contract.

**Recommended fix:** in `transcribe_audio()`, check
`settings_value = store.get_setting("privacy_mode_enabled", False)` (or read from
`settings.DATA_DIR / "settings.json"`) before calling `store.add_history_item()`. If
privacy mode is active, skip the history write and return `history_id: ""`.

---

## F4 — MED: Unicode filename truncated to bare extension, causing cryptic 400 errors

**File:** `rest_server.py` lines 891-894

`werkzeug.utils.secure_filename()` strips all non-ASCII characters. A filename like
`"тест.wav"` (Cyrillic, common for Russian users) becomes `"wav"` — a bare extension with
no stem. `os.path.splitext("wav")[1]` returns `""` (empty), which fails the
`ALLOWED_EXTENSIONS` check and returns HTTP 400 with the message
`"Unsupported file type: "` (empty string in the error body).

The client gets a confusing error that does not indicate the real problem (non-ASCII
filename). The audio is valid and would transcribe correctly if saved.

```
# Verified in venv:
>>> secure_filename("тест.wav")    -> 'wav'
>>> secure_filename("正常.mp3")    -> 'mp3'
>>> os.path.splitext("wav")[1]     -> ''
```

**Recommended fix:** extract the extension from the **original** `file.filename` before
calling `secure_filename()`, then use only `secure_filename()` for the saved path:

```python
original_ext = os.path.splitext(file.filename)[1].lower()
if original_ext not in ALLOWED_EXTENSIONS:
    return jsonify({"error": f"Unsupported file type: {original_ext}"}), 400
filename = secure_filename(file.filename) or f"upload{original_ext}"
temp_path = TEMP_DIR / f"{uuid.uuid4().hex[:12]}_{filename}"
```

---

## F5 — LOW: Temp upload files accumulate on hard process kill (no startup sweep)

**File:** `rest_server.py` lines 286-287, 958-963

```python
TEMP_DIR = settings.DATA_DIR / "temp_uploads"
TEMP_DIR.mkdir(parents=True, exist_ok=True)
# ...
finally:
    if temp_path.exists():
        temp_path.unlink()
```

The `finally` block correctly cleans up temp files on normal completion and on Python
exceptions. However, if the process is killed with `SIGKILL` (e.g., macOS OOM killer,
supervisor restart after hang) while a file is being saved or processed, the partial or
complete temp file is left in `TEMP_DIR` with no cleanup mechanism.

- A single 500 MB upload that crashes the process leaves 500 MB on disk.
- After many such events `TEMP_DIR` can accumulate gigabytes of stranded uploads.
- There is no startup sweep, no cron, and no `DiskSpaceMonitor` integration that covers
  `temp_uploads/` specifically (the existing `DiskSpaceMonitor` watches the data dir free
  space but does not enumerate and remove stale temp files).

**Recommended fix:** add a startup sweep in the module-level init (after `TEMP_DIR.mkdir()`):

```python
# Sweep stale uploads from previous runs
_now = time.time()
for _f in TEMP_DIR.iterdir():
    try:
        if _now - _f.stat().st_mtime > 3600:  # older than 1 hour
            _f.unlink()
    except OSError:
        pass
```

---

## Not-a-finding: items confirmed safe

| Concern | Verdict |
|---|---|
| Path traversal via filename | Safe — `secure_filename()` output is ASCII-only; uuid prefix prevents collisions |
| Concurrent same-filename uploads | Safe — 12-hex uuid prefix in temp path guarantees uniqueness |
| Zip bomb | Not applicable — `.zip` is not in `ALLOWED_EXTENSIONS` |
| ffmpeg shell injection | Safe — subprocess called with list args, no `shell=True` |
| Content-Length spoofing | Mitigated — Flask enforces `MAX_CONTENT_LENGTH` before stream read |
| Partial-upload cleanup | Safe on Python exception — `finally` block with `temp_path.exists()` guard |
| Idempotency key injection | Safe — `str().strip()` normalization; empty strings return `False` |

---

## Cross-reference to W1207

W1207 identified auth bypass (DISABLED by default) and SSRF in `probe_llm_http`. Those
findings are orthogonal to the upload pipeline. F1 (magic bytes) and F2 (decoder DoS) are
new vectors that only apply to the `/v1/stt/transcribe` multipart upload path not covered
by W1207.
