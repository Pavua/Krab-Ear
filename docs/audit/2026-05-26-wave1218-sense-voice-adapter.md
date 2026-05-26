# Wave 1218 — SenseVoice STT Adapter Audit

**Date:** 2026-05-26  
**Branch:** audit/sense-voice-W1218  
**Scope:** `KrabEar/core/pipeline/stt_sensevoice.py` + `KrabEar/tests/test_sensevoice_adapter.py` + router integration  
**Auditor:** W1218 (sub-agent, read-only)

---

## Executive Summary

The SenseVoice adapter (`Phase D.2.2`) is structurally sound: it loads lazily,
handles import absence gracefully, strips emotion tags, and passes `disable_update=True`
to prevent funasr from phoning home. Five issues were found ranging from LOW to MEDIUM.
No critical-severity findings.

---

## Findings

### F1 — MEDIUM: Language set mismatch between adapter and service.py routing table

**File:** `KrabEar/backend/service.py:2424` vs `KrabEar/core/pipeline/stt_sensevoice.py:43`

`service.py` registers SenseVoice with `{"zh", "ja", "ko", "yue", "en", "ru"}` (includes `"ru"`).
The adapter's `_SUPPORTED_LANGUAGES` frozenset is `{"zh", "yue", "ja", "ko", "en"}` — **no Russian**.
`supports_language("ru")` returns `False`; `transcribe(..., language="ru")` silently falls back
to `lang_arg = "auto"`, meaning Russian audio is sent to a model not trained for it.

When `service.py` routing considers SenseVoice enabled and routes a Russian recording to it,
`generate(language="auto")` may produce garbage or empty output with no error, causing a
silent quality regression before the Whisper fallback is triggered.

**Recommended fix:** Remove `"ru"` from the `service.py` SenseVoice language set, aligning
it with the adapter contract.

---

### F2 — MEDIUM: No thread-safety guard on lazy model load

**File:** `KrabEar/core/pipeline/stt_sensevoice.py:154–155`

```python
if self._model is None and not self._load_failed:
    self._load_model(AutoModel)
```

The check-then-act sequence is not protected by a lock. If two threads call `transcribe()`
simultaneously on the same adapter instance (e.g., the REST server and the IPC service both
warm up concurrently), `_load_model()` may execute twice in parallel. The second call
re-assigns `self._model` mid-inference of the first, causing a use-after-free on the old
model object.

GigaAM uses a subprocess worker to avoid this; Parakeet notes "do not call from multiple
threads simultaneously". SenseVoice has no equivalent safeguard.

**Recommended fix:** Add a `threading.Lock` to serialise the lazy-load check:
```python
self._load_lock = threading.Lock()
# …
with self._load_lock:
    if self._model is None and not self._load_failed:
        self._load_model(AutoModel)
```

---

### F3 — LOW: `unload()` resets `_load_failed` — allows silent re-load after error

**File:** `KrabEar/core/pipeline/stt_sensevoice.py:302–305`

```python
def unload(self) -> None:
    """Release model from memory."""
    self._model = None
    self._load_failed = False   # ← resets error state
```

Resetting `_load_failed` on `unload()` means that if a model fails to load (e.g., corrupted
weights, disk full), calling `unload()` clears the failure flag and the next `transcribe()`
will retry the load. This is **intentional for a memory-pressure unload** (the user may
have freed space), but it is undocumented and creates a loop: repeated calls to `unload()`
followed by `transcribe()` will hammer the file system with repeated failing load attempts.

**Recommended fix:** Add a docstring note clarifying the reset behaviour, or expose a
separate `reset_failure()` method and keep `unload()` preserving `_load_failed`.

---

### F4 — LOW: `_emotion_tag_re` regex does not match numeric-mixed tags

**File:** `KrabEar/core/pipeline/stt_sensevoice.py:48`

```python
_EMOTION_TAG_RE = re.compile(r"<\|[A-Za-z_]+\|>")
```

FunASR/SenseVoice can emit tags with digits such as `<|S1|>` (speaker IDs) or `<|2HAPPY|>`
in some model variants. The current regex `[A-Za-z_]+` does not match digit-containing tags,
leaving them in the returned `clean_text` and in the `emotion_tags` metadata list.

This is low-severity because SenseVoiceSmall rarely emits numeric tags in practice, but the
fix is trivial.

**Recommended fix:** Broaden to `[A-Za-z0-9_]+`.

---

### F5 — LOW: Test class `TestSenseVoiceTranscribe` skipped wholesale on CI

**File:** `KrabEar/tests/test_sensevoice_adapter.py:14–18, 96`

```python
_SKIP_TORCH_TRITON_CI = os.environ.get("CI") == "true"
…
@unittest.skipIf(_SKIP_TORCH_TRITON_CI, _SKIP_TORCH_REASON)
class TestSenseVoiceTranscribe(unittest.TestCase):
```

All transcription tests (6 test methods, including the device-selection and emotion-tag
stripping tests) are skipped on CI. The skip reason cites a `torch+triton` duplicate
registration issue on xdist workers, but the tests use full `unittest.mock.patch` and
**never import torch directly** — the `torch.backends.mps.is_available` patch in
`test_transcribe_uses_mps_when_available` still patches at the module level, so the triton
symbol collision should not affect these tests. The blanket skip causes zero coverage of
the happy path on CI.

**Recommended fix:** Move the torch/triton CI workaround to only the two device-selection
tests (`test_transcribe_uses_mps_when_available`, `test_transcribe_uses_cpu_when_mps_unavailable`)
and remove the decorator from the remaining four tests that do not import torch.

---

## Non-findings (checked, OK)

| Check | Verdict |
|-------|---------|
| FunASR model loading security | OK — `disable_update=True` prevents phone-home; model path passed through unmodified but funasr's `AutoModel` validates it internally; no shell exec |
| Torch device selection (MPS/CPU) | OK — `_resolve_device()` cleanly falls back to `cpu` when `torch.backends.mps.is_available()` raises `ImportError` |
| ONNX model integrity | N/A — SenseVoice uses funasr's own format, not raw ONNX loading |
| Error handling on inference failure | OK — exception caught, re-raised as `RuntimeError` with context, `_load_failed` not set (correct: failure is inference-time, not load-time) |
| Fallback chain interaction (STTRouter) | OK — `is_available()` check in `build_router()` prevents dead adapter from joining the chain; adapter not in chain = Whisper takes over |
| Privacy mode | OK for network calls — `disable_update=True` is set; model weights are loaded from HuggingFace cache on first use (same as all other adapters, no privacy-mode-specific gap) |
| W1019 cross-language accuracy | Partially OK — the adapter correctly defaults to `lang_arg="auto"` for unsupported languages, which is the right behaviour; the routing mismatch in service.py (F1) is the real risk |

---

## Summary Table

| # | Severity | File | Line(s) | Title |
|---|----------|------|---------|-------|
| F1 | MEDIUM | `backend/service.py` | 2424 | Language set includes `"ru"` not supported by adapter |
| F2 | MEDIUM | `core/pipeline/stt_sensevoice.py` | 154–155 | No lock on lazy model load (thread safety) |
| F3 | LOW | `core/pipeline/stt_sensevoice.py` | 302–305 | `unload()` silently resets load-failure flag |
| F4 | LOW | `core/pipeline/stt_sensevoice.py` | 48 | Emotion-tag regex misses numeric-mixed tags |
| F5 | LOW | `tests/test_sensevoice_adapter.py` | 96 | Wholesale CI skip hides 6 transcription tests |

**Total findings: 5** (2 MEDIUM, 3 LOW). No CRITICAL or HIGH.
