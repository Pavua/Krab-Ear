# W1252 Re-audit: `core/audio_chunker.py` — Residual Findings (post-W1130 boundary)

**Date:** 2026-05-26
**Branch:** `audit-audio-chunker-residual-W1252`
**Auditor:** W1252 sub-agent (read-only)
**Based on:** W1099 initial audit (5 findings), W1123 re-audit (5 new findings), W1130 fix

---

## W1099 / W1123 / W1130 Merge State

All three upstream PRs are **OPEN and unmerged** into `codex/krab-ear-v2` as of v2.0.5:

| PR | Wave | Description | Status |
|----|------|-------------|--------|
| #1006 | W1099 | Initial audit — 5 findings (docs-only) | OPEN |
| #1034 | W1123 | Re-audit — 5 new findings (docs-only) | OPEN |
| #1040 | W1130 | Fix: silence skip boundary `<` → `<=` (W1099-F1) | OPEN |

The W1130 one-line code fix (`region.start_sec < cursor` → `<=`) is queued but not yet merged.
All W1099 findings (F1–F5) and all W1123 findings (NF1–NF5) remain open in production.

**Correction of W1123-NF1:** W1123 stated that SmartSilenceSkipper was wired into `engine.py`
at step 2.6 (W1102). Code inspection of the current `codex/krab-ear-v2` tip confirms this is
**not the case**: `engine.py` contains zero references to `SmartSilenceSkipper` or
`SMART_SILENCE_SKIP_ENABLED`. The setting exists only in `core/config.py` line 200 and 826.
W1123-NF1 is therefore a **phantom finding** based on a future integration that was not yet
merged when the audit ran. The timestamp-shift risk it described is latent-only and currently
inert.

---

## New Findings (W1252)

### NF1 — MED · W1123-NF1 is a phantom: SmartSilenceSkipper not wired into engine.py

**Files:** `core/engine.py`, `core/smart_silence_skipper.py`, `core/config.py` line 200

`SmartSilenceSkipper` (`core/smart_silence_skipper.py`) exists as a standalone module with a
`process()` API and is registered in `DEFAULT_SETTINGS` as
`"smart_silence_skip_enabled": False`. However, as of v2.0.5 tip it is imported by **no
production code**: the only non-test references are the module itself and `config.py`.

```
$ grep -rn "SmartSilenceSkipper\|SMART_SILENCE_SKIP_ENABLED" KrabEar/ --include="*.py" \
  | grep -v test | grep -v smart_silence_skipper.py | grep -v config.py
# → (no output)
```

The W1123-NF1 finding assumed the module was invoked before the GigaAM chunker path,
creating timestamp drift. This entire risk is inert: the setting cannot be turned on via IPC
because no code path reads it or calls `SmartSilenceSkipper.process()`. The `AudioChunker`
chunk timestamps are therefore always relative to the original unmodified audio.

**Risk of this finding:** MED — not an active bug, but the dead-end setting
`smart_silence_skip_enabled` may mislead future developers or be partially wired incorrectly
in a future integration. The W1102 spec may have intended a different integration order than
what W1123 assumed.

**Recommendation:** Either (a) wire `SmartSilenceSkipper` into `engine.py` as documented in
the config/module docstring, being careful to pass pre-skip audio timestamps to
`AudioChunker` downstream, or (b) remove the `smart_silence_skip_enabled` setting from
`DEFAULT_SETTINGS` and `config.py` to eliminate the dead setting that implies a feature
exists. Document the decision.

---

### NF2 — MED · AudioChunker path in GigaAM has zero audio overlap; `transcribe_chunked` (Whisper path) has 2 s overlap — undocumented asymmetry

**Files:** `core/engine.py` lines 1391–1570, lines 2479–2502, `core/audio_chunker.py`

The Whisper long-audio path uses `engine.transcribe_chunked()` (line 773), which splits audio
into 15 s chunks with a **2 s overlap** (`overlap_sec=2.0`) and performs LCS-based seam
deduplication at chunk boundaries. This overlap guards against words being cut at chunk
boundaries and losing context.

The GigaAM longform path (lines 2479–2502) uses `AudioChunker` with **zero overlap**. Each
chunk is passed directly to `adapter.transcribe(ch.audio, sample_rate=16000)` with no
preceding audio context and no cross-chunk initial-prompt feeding:

```python
for ch in chunks:
    ch_result = adapter.transcribe(ch.audio, sample_rate=16000)
    chunk_results.append({
        "text": ch_result.get("text", ""),
        ...
        "start_sec": ch.start_sec,
        "end_sec": ch.end_sec,
    })
merged = AudioChunker.merge_results(chunk_results)
```

GigaAM RNNT is designed to model long-range dependencies within a chunk, but with zero
overlap between 20 s chunks the last word(s) before a hard chunk boundary may be
misrecognised if the word straddles the boundary. `AudioChunker` prefers silence boundaries
(`_SPLIT_OFFSET_SEC=0.05 s` from the start of a silence region), so for audio with clear
pauses this is mostly safe. For dense speech with no usable silence the chunker falls back to
hard cuts (line 299–302 in `_compute_split_points`), where mid-word cuts are possible.

**Current impact:** LOW in practice — GigaAM uses `_MIN_ADVANCE_SEC = max_chunk_sec / 2.0`
guard which forces cursors to advance at least 10 s per iteration, so hard cuts are rare on
normal conversational audio. Still, the asymmetry vs `transcribe_chunked` is undocumented.

**Recommendation:** Add a docstring note in `_transcribe_gigaam` and in `AudioChunker` that
this path uses silence-preferring zero-overlap chunking (intentional, not an oversight). If
GigaAM accuracy at chunk boundaries becomes a reported problem, consider adding a small
`overlap_sec` (e.g. 0.5 s) to `AudioChunker` with a duplicate-word trim pass.

---

### NF3 — LOW · Silence threshold inconsistency: AudioChunker uses -40 dB; AudioQuality uses 0.001 RMS (-60 dB)

**Files:** `core/audio_chunker.py` line 79, `core/silence_detector.py` line 50,
`core/audio_quality.py` lines 23–24

Three co-located silence/energy analysis modules use different thresholds with no
cross-module documentation:

| Module | Silence threshold | Equivalent dB |
|--------|------------------|---------------|
| `SilenceDetector` (used by `AudioChunker`) | `-40.0 dB` default | -40 dB |
| `SmartSilenceSkipper` | `-40.0 dB` default | -40 dB |
| `AudioQualityAnalyzer._compute_silence_ratio` | `0.001 RMS` hardcoded | **-60 dB** |

`AudioQualityAnalyzer` classifies a frame as silent when RMS < 0.001, which is 20 dB quieter
than the `-40 dB` threshold used by `SilenceDetector`. This means:

- A frame with RMS = 0.005 (approximately -46 dB) is **speech** by `SilenceDetector` but
  **speech** by `AudioQuality` too — consistent.
- A frame with RMS = 0.005 is **silent** by `AudioQuality` only if below 0.001 — consistent.
- Actually: RMS = 0.002 (approx -54 dB) is considered **speech** by SilenceDetector
  (-40 dB threshold = 0.01) but also **speech** by AudioQuality (0.001 threshold)...

Re-checking: `_db_to_amplitude(-40) = 10^(-40/20) = 0.01`. So SilenceDetector considers
frames with RMS < 0.01 as silence. AudioQuality uses RMS < 0.001. The AudioQuality threshold
is **10× stricter** — only frames quieter than -60 dB are silence-classified, meaning
AudioQuality will report a lower `silence_ratio` than SilenceDetector for the same audio.

A recording assessed as "50% silent" by `SilenceDetector` (and therefore a good chunking
candidate) would be assessed as having significantly less silence by `AudioQuality`. If either
value is surfaced in diagnostics or IPC results, the two modules give semantically incompatible
numbers without any documentation of the difference.

Additionally, `SilenceDetector` uses `_FRAME_SIZE = 512` samples (32 ms @ 16 kHz) while
`AudioQuality` uses `_SILENCE_FRAME_SIZE = 1024` samples (64 ms @ 16 kHz). The coarser
AudioQuality frame misses short (<64 ms) speech events that SilenceDetector would detect.

**Risk:** LOW — these two modules serve different purposes (SilenceDetector drives chunk
boundaries; AudioQuality drives quality scoring). No code path combines their outputs
directly. However, if diagnostic tools surface both `silence_ratio` values they will appear
inconsistent to users and developers.

**Recommendation:** Add comments in both modules documenting the design intent: "This
threshold is deliberately different from SilenceDetector's -40 dB — AudioQuality uses a
stricter floor for signal-presence scoring, not for split-point detection." Optionally
consolidate into a shared `_SILENCE_THRESHOLD_DB = -40.0` constant in a shared module.

---

### NF4 — LOW · `merge_results()` Whisper segment offset uses `chunk_start or 0.0` — silently ignores `chunk_start = 0.0`

**File:** `core/audio_chunker.py` line 224

```python
offset = chunk_start or 0.0
if "start" in adjusted:
    adjusted["start"] = adjusted["start"] + offset
```

`chunk_start` is `chunk.get("start_sec")` (line 214). For the very first chunk where
`start_sec = 0.0`, the expression `chunk_start or 0.0` evaluates `0.0 or 0.0 = 0.0`, which
is correct by accident. However, the idiom `x or default` is wrong for numeric values: if
`chunk_start` is explicitly `0.0`, it is falsy in Python, so the `or 0.0` path is taken —
which happens to give the same result, masking the bug.

The real problem appears if a caller ever passes a chunk dict where `start_sec` is `None`
(which can happen when constructing chunk results manually, e.g. `{"text": "...",
"confidence": 0.9}` with no `start_sec` key). In that case `chunk_start = None`, and
`offset = None or 0.0 = 0.0`, so segments are placed at time 0 — silently wrong for chunks
2+.

The existing code at line 214–215:
```python
chunk_start = chunk.get("start_sec")
chunk_end = chunk.get("end_sec")
```
already handles missing keys by returning `None`. But the downstream `offset = chunk_start or 0.0`
on line 224 re-uses the same `chunk_start` that may be `None` for inner chunks, corrupting
segment timestamps without any log warning.

The `engine.py` GigaAM path always includes `"start_sec"` in chunk results (line 2493),
so this is currently inert. But `merge_results` is a public static method that external
callers may invoke without `start_sec` on inner chunks.

**Risk:** LOW — current GigaAM call site populates `start_sec` correctly. Any future caller
that omits `start_sec` on non-zero-index chunks will silently get wrong Whisper segment
timestamps.

**Recommendation:** Change line 224 to:
```python
offset = chunk_start if chunk_start is not None else 0.0
```
This is semantically correct for all numeric values including `0.0`. Add a `logger.warning`
when `chunk_start is None` and `chunk.get("index", 0) > 0` to surface the mis-use.

---

### NF5 — LOW · No test for multi-channel (>2) audio input to `AudioChunker.chunk()`; `_to_mono` uses `mean(axis=1)` which fails for channel-first layout

**Files:** `core/silence_detector.py` line 211, `core/audio_chunker.py` line 124,
`KrabEar/tests/test_audio_chunker.py` lines 229–237

`SilenceDetector._to_mono()` converts stereo to mono via `audio.mean(axis=1)`, which assumes
`(n_samples, n_channels)` (sample-first, channel-last) layout — the standard `soundfile`
output format. However:

1. For a `(2, n_samples)` channel-first array (PyTorch `torchaudio` default), `axis=1` averages
   across the n_samples dimension, returning an array of shape `(2,)` — two values instead of
   a mono waveform. The chunker then calls `len(mono)` = 2, computes `total_sec = 2/16000 =
   0.000125 s`, and returns a single trivial chunk. **No exception is raised; the output is
   silently wrong.**

2. The existing stereo test (`test_stereo_audio_chunked`, line 229) uses
   `np.stack([mono, mono], axis=1)` which correctly produces `(n_samples, 2)` format. It
   does not test channel-first input, so this bug is not caught.

3. There is no test for 3+ channel audio (e.g. surround). For `(n_samples, 4)` format,
   `mean(axis=1)` returns `(n_samples,)` mono — correct. For `(4, n_samples)` channel-first,
   `mean(axis=1)` returns `(4,)` — four values, silently wrong.

**The current GigaAM path in `engine.py` line 2460 does mono-conversion before calling
`AudioChunker`:**
```python
if audio_data_np.ndim == 2:
    audio_data_np = audio_data_np.mean(axis=1)
```
This `mean(axis=1)` has the same channel-first risk, but since the GigaAM adapter receives
audio from `soundfile`/`sounddevice` which uses sample-first layout, it is safe in production.

**Risk:** LOW — production audio paths use `soundfile` (always sample-first). The risk is
a future integration that imports `AudioChunker` with PyTorch/torchaudio audio tensors.

**Recommendation:** Add an assertion at the top of `SilenceDetector._to_mono()`:
```python
if audio.ndim == 2:
    assert audio.shape[0] > audio.shape[1] or audio.shape[1] <= 8, \
        "Audio appears channel-first (shape[0] <= 8); expected (n_samples, n_channels)"
```
Also add a test case with channel-first stereo `np.stack([mono, mono], axis=0)` asserting
that chunking raises or produces correct output.

---

## Open W1099 / W1123 Finding Status (unchanged)

| ID | Severity | Description | Status |
|----|----------|-------------|--------|
| W1099-F1 | HIGH | `start_sec < cursor` off-by-one skip guard | OPEN — fix in PR #1040, not merged |
| W1099-F2 | LOW | No max_chunk_sec cap / GigaAM constraint not in docstring | OPEN |
| W1099-F3 | LOW | No warning for sub-400-sample tail chunks | OPEN |
| W1099-F4 | INFO | Stereo memory copy on long audio | OPEN — INFO, no action required |
| W1099-F5 | INFO | AudioChunker not in RecordingCoreService (by design) | CONFIRMED OK |
| W1123-NF1 | MED | SmartSilenceSkipper timestamp shift in GigaAM path | PHANTOM — SSS not wired |
| W1123-NF2 | MED | Denoiser latent per-chunk boundary artifact risk | OPEN |
| W1123-NF3 | LOW | No bounds validation on constructor params | OPEN |
| W1123-NF4 | LOW | Dead no-op conditional line 133; no channel-layout assertion | OPEN |
| W1123-NF5 | INFO | Idempotency confirmed — no issue | CONFIRMED OK |

---

## Summary Table (W1252 new findings only)

| ID | Severity | Description |
|----|----------|-------------|
| NF1 | MED | W1123-NF1 is phantom: SmartSilenceSkipper not wired into engine.py |
| NF2 | MED | GigaAM AudioChunker path has zero audio overlap vs Whisper 2 s overlap — undocumented asymmetry |
| NF3 | LOW | Silence threshold inconsistency: SilenceDetector -40 dB vs AudioQuality 0.001 RMS (-60 dB) |
| NF4 | LOW | `merge_results()` uses `chunk_start or 0.0` idiom — wrong for `None` on inner chunks |
| NF5 | LOW | No test for channel-first stereo; `_to_mono(axis=1)` silently wrong on `(2, n)` input |

---

## Verdict

`AudioChunker` remains production-safe. The most actionable new finding is NF1 (correcting
a phantom in the previous audit), which clarifies that the SmartSilenceSkipper timestamp-drift
risk is currently zero. NF4 is a one-liner correctness fix for a public API. NF5 highlights
a latent correctness bug in `_to_mono` that is safe today but could break on PyTorch inputs.

**Priority order for follow-up fixes:**
1. Merge PR #1040 (W1130 F1 fix — already queued).
2. NF4: fix `chunk_start or 0.0` → `chunk_start if chunk_start is not None else 0.0`.
3. NF1: either wire SmartSilenceSkipper properly or remove the dead setting.
4. NF5: add channel-layout assertion to `_to_mono` + test for channel-first input.
5. NF3: add docstring comment documenting the intentional threshold difference.
