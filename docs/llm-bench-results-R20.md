# LLM Rewriter Benchmark R20 — Krab Ear

**Date:** 2026-05-12 05:11  
**Hardware:** MacBook Pro M4 Max, 36 GB RAM  
**LM Studio:** http://localhost:1234/v1  
**Runs per prompt:** 1 warmup + 2 timed × 3 prompts  
**R19 baseline (reference):** `gemma-4-26b-a4b-it-optiq` p50=1587ms quality=1.00  

## Summary Table

| Model | p50_ms | p95_ms | quality_avg | ok_ratio | vs_baseline_p50 |
|-------|--------|--------|-------------|----------|----------------|
| `mlx-community/gemma-4-31b-it-assistant` | 3 | 6 | 0.000 | 0.00 | -1584ms |
| `supergemma4-26b-abliterated-multimodal-mlx` | 4808 | 6606 | 1.000 | 1.00 | +3221ms |
| `gemma-4-e2b-it-ultra-uncensored-heretic-mlx-int8-affine` | 14960 | 17577 | 0.200 | 1.00 | +13373ms |
| `gemma-4-26b-a4b-it-optiq` *(R19 ref)* | 1587 | — | 1.000 | 1.00 | baseline |

## Per-Model Analysis

### `mlx-community/gemma-4-31b-it-assistant`

- p50=3ms (1584ms faster than baseline)
- p95=6ms
- quality_avg=0.000
- ok_ratio=0.00

| Prompt | p50_ms | p95_ms | quality | ok/runs | issues |
|--------|--------|--------|---------|---------|--------|
| meeting_short | 4 | 4 | 0.00 | 0/2 | failed:http_400 |
| tech_short | 4 | 6 | 0.00 | 0/2 | failed:http_400 |
| dictation_note | 3 | 3 | 0.00 | 0/2 | failed:http_400 |

### `supergemma4-26b-abliterated-multimodal-mlx`

- p50=4808ms (+3221ms slower than baseline)
- p95=6606ms
- quality_avg=1.000
- ok_ratio=1.00

| Prompt | p50_ms | p95_ms | quality | ok/runs | issues |
|--------|--------|--------|---------|---------|--------|
| meeting_short | 3977 | 5402 | 1.00 | 2/2 | — |
| tech_short | 4204 | 4215 | 1.00 | 2/2 | — |
| dictation_note | 6490 | 6606 | 1.00 | 2/2 | — |

### `gemma-4-e2b-it-ultra-uncensored-heretic-mlx-int8-affine`

- p50=14960ms (+13373ms slower than baseline)
- p95=17577ms
- quality_avg=0.200
- ok_ratio=1.00

| Prompt | p50_ms | p95_ms | quality | ok/runs | issues |
|--------|--------|--------|---------|---------|--------|
| meeting_short | 16053 | 17340 | 0.20 | 2/2 | too_short(0.10), no_cyrillic |
| tech_short | 14654 | 15154 | 0.20 | 2/2 | too_short(0.11), no_cyrillic |
| dictation_note | 13540 | 17577 | 0.20 | 2/2 | too_short(0.11), no_cyrillic |

## Recommendation

R19 baseline `gemma-4-26b-a4b-it-optiq`: p50=1587ms, quality=1.000

> **HOLD** — no model beats baseline on both latency (<1587ms) and quality (≥0.95). Keep `gemma-4-26b-a4b-it-optiq`.
>
> Best quality candidate: `supergemma4-26b-abliterated-multimodal-mlx` (p50=4808ms, quality=1.000)

## Model Notes

### supergemma4-26b-abliterated-multimodal-mlx
Multimodal variant (vision encoder loaded by default). May show extra latency due to
vision components initialized even for text-only inference.

### mlx-community/gemma-4-31b-it-assistant
**STATUS: INCOMPATIBLE with LM Studio runtime.**
LM Studio error: `No LM Runtime found for model format 'torchSafetensors'!`
The `mlx-community/gemma-4-31b-it-assistant-bf16` model is stored in PyTorch SafeTensors format,
not converted to MLX format. LM Studio on Apple Silicon requires MLX or GGUF format — raw bf16
SafeTensors cannot be loaded via LM Studio. To bench this model, convert with `mlx_lm.convert`
or download a proper GGUF/MLX-quantized version. Results excluded from comparison.

### gemma-4-e2b-it-ultra-uncensored-heretic-mlx-int8-affine
**STATUS: DISQUALIFIED — output format broken.**
Model responds but outputs internal channel tokens (`<|channel>thought Thinking Process:...`)
instead of Russian text. The E2B heretic variant appears to have its chat template or output
formatter corrupted — it dumps raw internal state rather than completing the text. This is
unrelated to quantization level; the model itself is non-functional for text generation tasks.
Also crashed during post-bench spot-check (`Exit code: null`). Do not use for rewriting.

## Raw JSON

```json
{
  "supergemma4-26b-abliterated-multimodal-mlx": {
    "latency_p50_ms": 4808,
    "latency_p95_ms": 6606,
    "quality_avg": 1.0,
    "ok_ratio": 1.0
  },
  "mlx-community/gemma-4-31b-it-assistant": {
    "latency_p50_ms": 3,
    "latency_p95_ms": 6,
    "quality_avg": 0.0,
    "ok_ratio": 0.0
  },
  "gemma-4-e2b-it-ultra-uncensored-heretic-mlx-int8-affine": {
    "latency_p50_ms": 14960,
    "latency_p95_ms": 17577,
    "quality_avg": 0.19999999999999996,
    "ok_ratio": 1.0
  }
}
```
