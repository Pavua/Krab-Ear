# STT Engines Comparison — 2026-05-05 refresh

## Current production stack

| Layer | Component | Notes |
|-------|-----------|-------|
| Primary (RU) | GigaAM-RNNT | enabled via `stt_gigaam_enabled=True`, lang=ru |
| Primary (mixed) | Whisper Large v3 MLX | fallback when GigaAM disabled or lang≠ru |
| RU fine-tune | antony66/whisper-large-v3-russian | best pure-RU formal; slower load |
| Repetition guard | `core.utils.is_likely_repetition_loop` | fires on ANY engine output (C.4) |
| Brand normalizer | `core.utils._BRAND_WITH_HINTS` | fires on ANY engine output, hint fast-path |

Configuration entry points:
- `stt_gigaam_enabled` — bool setting, default True when venv_gigaam present
- `stt_*_primary_model` — selects Whisper model variant
- `GigaAM_HARD_LIMIT_SEC = 25` in `backend/gigaam_worker.py` (chunked for longer audio)

## Bench summary

Run via `scripts/stt_engine_bench.py` with real audio samples:

```bash
cd "/path/to/Krab Ear"
# dry-run (mock outputs, framework validation only)
python3 scripts/stt_engine_bench.py --suite default

# real audio, single sample
python3 scripts/stt_engine_bench.py \
    --audio ~/my_sample.wav \
    --reference "Эталонный текст здесь" \
    --no-mock
```

### Historical benchmark snapshot (2026-04-26, real RU call audio ~20 s)

| Engine | Cold load | Warm transcribe | RTF | Notes |
|--------|-----------|-----------------|-----|-------|
| GigaAM-RNNT | 37 s | 1.1 s | 0.041 (24× RT) | correct mat/Schweiz/Finland |
| Whisper Large v3 MLX | ~2.3 s | ~2.5 s | ~0.12 | hallucinated Италия/Света/филанюк |

> Source: `memory/reference_gigaam_vs_whisper_bench_2026-04-26.md`

**User empirical doubt (2026-05-05):** "что-то как-то незаметно, чтобы в 2 с 1/2 раза точнее
распознавалось" — re-run bench with current audio to validate or update this claim.
The 2026-04-26 snapshot used a single domain-specific clip; results may not generalize.

## Known per-engine traits

### GigaAM-RNNT

- **Strengths:** casual/colloquial Russian, mat-heavy speech, proper nouns in RU context
  (Швейцария, Швеция, Финляндия transcribed correctly on 2026-04-26 test).
- **Weaknesses:** brand names in Latin script (LM Studio → phonetic RU); mixed RU/EN utterances.
- **Hard limits:** 25 s audio chunk limit; longer audio chunked in `gigaam_worker.py`.
- **Memory:** runs in separate `venv_gigaam` (Python 3.12, torch 2.5.1); subprocess-isolated.
- **Import path:** `backend.gigaam_worker.GigaAMWorker`.

### Whisper Large v3 MLX

- **Strengths:** mixed RU+EN text, code-switching, hotword injection via `initial_prompt`.
- **Weaknesses:** prone to repetition hallucination loops on silence/noise; brand mishears
  (Швейцария → Италия on the 2026-04-26 test clip).
- **Thread-safety:** ALL MLX inference via `with mlx_lock()` (core.mlx_lock). Concurrent
  GPU access causes SIGSEGV in `__hash_table<MTL::Resource*>`. See PR #71.
- **Fallback chain:** balanced model → max candidates → remote STT.

### Whisper RU fine-tune (antony66/whisper-large-v3-russian)

- **Strengths:** formal Russian monologue, dictation, structured speech.
- **Weaknesses:** brand names, code-switching, slow cold load.
- **Use case:** long-form structured recordings where brand accuracy is not critical.

## Phase C C.4 impact

### Brand expansion (2026-05-04)

New entries added to `_BRAND_REPLACEMENTS_RAW` in `core/utils.py`:

| RU mishear | Normalized to | Engine most affected |
|------------|--------------|----------------------|
| Гемма / Джемма | Gemma | Whisper (phonetic RU) |
| Антропик | Anthropic | Whisper + GigaAM |
| Элэм Студио | LM Studio | GigaAM (new variant) |

**Effect:** reduces brand-error count on Russian speech mentioning Google/Anthropic model
names. Fires deterministically on all engine outputs — not engine-specific.

### Repetition loop detector (C.4)

`core.utils.is_likely_repetition_loop` added 2026-05-04. Heuristics:
1. ≥5 identical adjacent bigrams.
2. ≥3 identical sentences in a row.
3. Text > 60 chars AND unique-token ratio < 0.15.

**Effect on Whisper:** catches silence-induced loops before they reach paste layer.
**Effect on GigaAM:** rarely triggers (GigaAM less prone to repetition loops in practice).

## When to use which engine (heuristics)

| Scenario | Recommended engine | Reason |
|----------|--------------------|--------|
| Spoken commands / casual Russian | GigaAM-RNNT | Best colloquial RU accuracy |
| Technical Russian with English brand names | Whisper MLX + hotwords | Brand-aware with initial_prompt |
| Formal Russian without brands | Whisper RU fine-tune | Best formal accuracy |
| Mixed RU/EN utterance | Whisper MLX | Cross-lingual capability |
| Very long audio (>60 s) | Whisper MLX | GigaAM 25 s chunk limit |
| Low-latency warm path (<2 s) | GigaAM-RNNT | 1.1 s warm transcription |

## How to re-run and update this doc

1. Collect 3-5 representative audio clips in `KrabEar/tests/audio/` with known transcripts.
2. Run `python3 scripts/stt_engine_bench.py --suite default --no-mock`.
3. Paste the output table into "Historical benchmark snapshot" above.
4. Update the "Per-engine traits" section with any newly observed failure modes.

## Related files

- `KrabEar/core/utils.py` — `is_likely_repetition_loop`, `_BRAND_REPLACEMENTS_RAW`
- `KrabEar/backend/gigaam_worker.py` — `GigaAMWorker`, `HARD_LIMIT_SEC`
- `KrabEar/core/engine.py` — `AudioEngine`, MLX fallback chain, `mlx_lock` usage
- `scripts/stt_engine_bench.py` — benchmark runner (this refresh)
- `memory/reference_gigaam_vs_whisper_bench_2026-04-26.md` — prior benchmark snapshot
- `docs/audit/gigaam-longform-handling-2026-05-05.md` — GigaAM chunking audit
