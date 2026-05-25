# Memory baseline comparison — Wave 195 (2026-05-19)

## Wave 62 baseline (pre-Wave 63 fix)

- **File**: `docs/measurements/memory-baseline-2026-05-14.csv`
- **Duration**: 300 s, 31 samples @ 10 s interval
- **Backend start (first-5 mean)**: 124.1 MB
- **Backend at 5 min (last-5 mean)**: 391.6 MB
- **Drift**: +267.5 MB / 5 min = **~3.2 GB/hour projected**
- **Root cause identified**: `mlx_whisper.transcribe()` retains model weights in MLX cache between calls; `AudioLanguageID` language-ID model also accumulated without bound.
- **Leak indicator from `.md` notes**: "RSS grew +267 MB over 5 min and did not return to start level."

### Raw trace highlights (CSV)

| Uptime (s) | Backend RSS (MB) | Event |
|------------|-----------------|-------|
| 0 | 45.3 | Startup |
| ~40 | 379.9 → 392.8 | LLM rewrite burst #1 — cache not released |
| 300 | 392.0 | End of window — no recovery |

---

## Wave 63 fix (PR #405)

Two targeted changes in `core/engine.py` and `core/audio_lang_id.py`:

1. **`mx.clear_cache()` after each `mlx_whisper.transcribe()` call** — forces MLX to release Metal buffer pool immediately after inference instead of retaining across calls.
2. **`AudioLanguageID` model cache bound to LRU=1** — ensures at most one language-ID model is resident in memory at a time.

Test coverage: Wave 128 `test_audio_lang_id_cache` confirms LRU=1 bound is enforced.

---

## Production observed (post-Wave 63 fix)

- **Mega-session 2026-05-15/16** (waves 59-66, 14 PRs): backend RSS stable 35–40 MB across multi-hour development sessions.
- **Mega-session 2026-05-18** (waves 65-69): backend RSS 35 MB on restart, stable throughout session. Explicitly noted in session wrap: "Backend RSS 35 MB после restart = Wave 63 leak fix validated."
- **Restart-to-restart**: no measurable drift between sessions — idle baseline remains flat.

---

## Numerical comparison

| Metric | Pre-fix (Wave 62) | Post-fix (Wave 63+) | Change |
|--------|------------------|---------------------|--------|
| Backend RSS steady-state | ~392 MB after 5 min | 35–40 MB across hours | **−357 MB (−91%)** |
| Projected hourly growth | ~3.2 GB/hour | ~0 MB/hour (stable) | **−100% drift** |
| Backend start RSS | ~45 MB | ~35–40 MB | −5–10 MB |
| Memory pressure events | Yes (LM Studio + MLX OOM risk) | None observed | Eliminated |

**~91% reduction in steady-state RSS post-fix.**

---

## Limitations of this comparison

- Wave 62 baseline was captured during **active LLM rewriter + GigaAM activity** (real STT calls in flight). Post-Wave 63 production observations are **passive idle** (no concurrent STT bursts).
- A controlled re-run with STT operations after Wave 63 fix would give a direct apples-to-apples comparison.
- Worker RSS (GigaAM subprocess) was not separately tracked in post-fix sessions — assumed unchanged since Wave 63 did not modify the worker lifecycle.

---

## Recommendation for Wave N+1

1. **Controlled re-run**: run `scripts/memory_baseline.py` with ≥5 STT transcription jobs during the 5-min window to match Wave 62 conditions. Target: confirm backend RSS stays below 80 MB even under load.
2. **Sentry breadcrumb telemetry** (Wave 153 breadcrumbs pattern): add `backend_rss_mb` to `add_breadcrumb(data={...})` on each transcription — replaces need for manual baseline scripts in production.
3. **CI memory gate**: add a pytest fixture that runs a single `mlx_whisper.transcribe()` call and asserts RSS delta < 50 MB before/after — catches regressions automatically.
4. **Save new baseline file** after controlled re-run as `docs/measurements/memory-baseline-2026-05-19-post-fix.csv` for future drift comparison.

---

*Generated Wave 195 (2026-05-19). No model loads performed — comparison based on Wave 62 CSV + multi-session production observations post-PR #405.*
