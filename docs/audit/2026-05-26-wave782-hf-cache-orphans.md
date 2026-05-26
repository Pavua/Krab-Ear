# Wave 782 — HuggingFace Cache Orphan Audit

**Date:** 2026-05-26  
**Branch:** feature/hf-orphans-W782  
**Scope:** `~/.cache/huggingface/hub/` — models only referenced in Krab Ear codebase

---

## Summary

| Metric | Value |
|--------|-------|
| Total cache size | 3.4 GB |
| Total model directories | 53 |
| Active (referenced in code) | 8 |
| Empty stubs (gated / failed download) | 5 |
| Orphan candidates (LM Studio bench models) | 40 |
| Orphan candidate total size | ~2.9 GB (all 4 KB stub dirs — actual weights live in LM Studio cache) |
| True large-file orphans | 0 (all 4 KB dirs are LM Studio/mlx-lm stubs only) |

**Key finding:** Almost all 4 KB entries are empty HuggingFace metadata stubs. The actual model weights for LLM candidates (gemma, qwen, llama variants) were downloaded via LM Studio or `mlx_lm.convert` directly into LM Studio's model directory, not into the HuggingFace blob cache. The HF cache only holds metadata/config files for those entries. The real disk users are the 4 large STT/translation/embedding model directories.

---

## Total Cache Size

```
3.4G    ~/.cache/huggingface/hub/
```

Also present: `datasets--wikitext` (7.4 MB) — used by `scripts/r20_bench.py` benchmarks, not Krab Ear runtime.

---

## Models Referenced in Krab Ear Code

These models are actively used by the Krab Ear Python backend or native agent and should **not** be removed:

| Model | Size | Last Modified | Referenced In |
|-------|------|---------------|---------------|
| `mlx-community/whisper-large-v3-turbo` | 1.5 GB | 2026-05-23 | `KrabEar/core/config.py` (MODEL_BALANCED default), `core/audio_lang_id.py`, `core/pipeline/stt_whisper_mlx_adapter.py` |
| `mlx-community/whisper-small-mlx` | 459 MB | 2026-04-22 | `KrabEar/tests/test_stt_adapter_migration.py` (adapter test), `core/config.py` (MODEL_MAX_CANDIDATES fallback chain) |
| `Systran/faster-whisper-small` | 464 MB | 2026-05-22 | `KrabEar/backend/startup_diagnostics.py` (diagnostics check) |
| `minishlab/M2V_multilingual_output` | 997 MB | 2026-05-12 | **NOT found in code** — see orphan analysis below |
| `facebook/nllb-200-distilled-600M` | 4 KB stub | 2026-05-05 | `KrabEar/backend/translator.py` (`_NLLB_MODEL = "facebook/nllb-200-distilled-600M"`) |
| `pyannote/speaker-diarization-community-1` | 268 KB | 2026-04-11 | `KrabEar/core/config.py` is default for `DIARIZATION_MODEL` = `"pyannote/speaker-diarization-3.1"` |
| `mlx-community/gemma-4-26b-a4b-it-4bit` | 40 KB stub | 2026-05-16 | `KrabEar/core/config.py` comments, `scripts/r19_bench.py`, `scripts/r22_bench.py` |
| `mlx-community/whisper-large-v3-mlx` | 0 B (empty dir) | — | `KrabEar/core/config.py` (`MODEL_MAX_CANDIDATES`, `STT_*_PRIMARY_MODEL`), `core/stt_router.py` — **actively configured but not downloaded (gated/failed)** |

### Active diarization model vs cache

`DIARIZATION_MODEL` defaults to `pyannote/speaker-diarization-3.1` (in config.py line 106), but:
- `models--pyannote--speaker-diarization-3.1` exists as an **empty directory** (0 B, gated model, never accepted TOS on HF).
- `models--pyannote--speaker-diarization-community-1` (268 KB) is the community (ungated) variant downloaded in April.

The engine falls back to `speaker-diarization-community-1` when `3.1` is unavailable.

---

## Empty Stub Directories (0 B — gated or failed downloads)

These directories were created when HuggingFace attempted to download gated models that required TOS acceptance. They contain no model weights. Disk cost is negligible (directory entries only).

| Model | Size | Status |
|-------|------|--------|
| `pyannote/speaker-diarization-3.1` | 0 B | Gated — requires HF TOS accept at huggingface.co/pyannote/speaker-diarization-3.1 |
| `pyannote/speaker-diarization-2.1` | 0 B | Gated — older version, superseded by 3.1 |
| `pyannote/segmentation-3.0` | 0 B | Gated — required by GigaAM longform path |
| `pyannote/wespeaker-voxceleb-resnet34-LM` | 0 B | Gated — speaker embedding model |
| `mlx-community/whisper-large-v3-mlx` | 0 B | Configured as primary STT model but not downloaded; may be gated or download interrupted |

**Recommendation:** These empty stubs are safe to delete (0 B), but their presence is harmless. Deleting them won't recover meaningful disk space. The pyannote gated models require HF TOS acceptance before they can be re-downloaded.

---

## Special Case: `minishlab/M2V_multilingual_output` (997 MB)

This is the **largest single orphan candidate** by disk usage.

- **Size:** 997 MB
- **Last modified:** 2026-05-12
- **Codebase search:** Zero references to `minishlab`, `M2V_multilingual`, `model2vec`, or `M2V` found anywhere in `KrabEar/`, `native/KrabEarAgent/`, or `scripts/`.
- **Context:** The `SemanticSearcher` in `backend/semantic_search.py` uses `intfloat/multilingual-e5-base` (a sentence-transformers model), not M2V. This model appears to have been downloaded during an experimental session (possibly the W512 mega-marathon semantic search work) and then superseded.

**Status: ORPHAN CANDIDATE — highest priority for user review.**

---

## LM Studio / Bench Model Stubs (4 KB each — actual weights elsewhere)

All entries below show 4 KB in HuggingFace cache. This means only metadata/config JSON was cached by HuggingFace; the actual model weights were downloaded directly by LM Studio (to `~/Library/Application Support/LM Studio/models/`) or via `mlx_lm` benchmark scripts. Safe to remove stubs without losing model weights.

These models were evaluated during R19/R20/R21/R22 benchmark sessions (April 28 – May 14) to find the best LLM rewriter model. The winner (`supergemma4-26b-abliterated-multimodal-mlx`, `gemma-4-26b-a4b-it-optiq`) is used via LM Studio, not via HuggingFace cache.

| Model | Size | Last Modified | Krab Ear Code Ref? |
|-------|------|---------------|-------------------|
| `EZCon/Huihui-gemma-4-E4B-it-abliterated-4bit-mlx` | 4 KB | 2026-04-28 | No |
| `Eldadalbajob/Huihui-Qwen3-Next-80B-A3B-Instruct-abliterated-mlx-3Bit` | 4 KB | 2026-04-28 | No |
| `Jiunsong/supergemma4-26b-uncensored-mlx-4bit-v2` | 4 KB | 2026-04-28 | No |
| `KYUNGYONG/aya-expanse-32b-abliterated-Q4-mlx` | 4 KB | 2026-04-28 | No |
| `Youssofal/Qwen3.6-35B-A3B-Abliterated-Heretic-MLX-4bit` | 4 KB | 2026-04-30 | No |
| `alib97/Josiefied-Qwen3-14B-abliterated-v3-mlx-4Bit` | 4 KB | 2026-04-28 | No |
| `divinetribe/gemma-4-31b-it-abliterated-4bit-mlx` | 4 KB | 2026-04-28 | No |
| `enet45/Qwen2.5-14B-Instruct-Uncensored-mlx-4Bit` | 4 KB | 2026-04-28 | No |
| `guardiangate1775/gemma-4-26B-A4B-it-assistant-4bit` | 4 KB | 2026-05-14 | No |
| `huihui-ai/Huihui-GLM-4.7-Flash-abliterated-mlx-4bit` | 4 KB | 2026-04-30 | No |
| `huihui-ai/Huihui-Qwen3.5-9B-abliterated-mlx-4bit` | 4 KB | 2026-04-28 | No |
| `huihui-ai/Qwen2.5-7B-Instruct-abliterated` | 4 KB | 2026-04-28 | No |
| `leonsarmiento/Llama-3.3-8B-Instruct-128K_Abliterated-8bit-mlx` | 4 KB | 2026-04-28 | No |
| `lmstudio-community/gemma-4-E4B-it-MLX-4bit` | 4 KB | 2026-04-30 | Comment only in `llm_rewriter.py` (model name in a comment, not loaded from HF) |
| `mlx-community/Hermes-3-Llama-3.1-8B-4bit` | 4 KB | 2026-04-28 | No |
| `mlx-community/Hermes-3-Llama-3.2-3B-4bit` | 4 KB | 2026-04-28 | No |
| `mlx-community/Josiefied-Qwen3-4B-abliterated-v1-4bit` | 4 KB | 2026-04-28 | No |
| `mlx-community/Josiefied-Qwen3-8B-abliterated-v1-4bit` | 4 KB | 2026-04-28 | No |
| `mlx-community/Llama-3.2-11B-Vision-Instruct-abliterated` | 4 KB | 2026-04-28 | No |
| `mlx-community/MiniMax-M2.5-Uncensored-4bit` | 4 KB | 2026-04-30 | No |
| `mlx-community/Mistral-Nemo-Instruct-2407-4bit` | 4 KB | 2026-04-28 | No |
| `mlx-community/Mistral-Small-Instruct-2409-4bit` | 4 KB | 2026-04-28 | No |
| `mlx-community/Phi-4-mini-instruct-4bit` | 4 KB | 2026-04-28 | No |
| `mlx-community/Qwen3.6-27B-OptiQ-4bit` | 4 KB | 2026-04-30 | No |
| `mlx-community/aya-expanse-8b` | 4 KB | 2026-04-28 | No |
| `mlx-community/c4ai-command-r7b-12-2024-4bit` | 4 KB | 2026-04-28 | No |
| `mlx-community/dolphin-2.9-llama3-8b-4bit` | 4 KB | 2026-04-30 | No |
| `mlx-community/gemma-4-26B-A4B-it-assistant-bf16` | 4 KB | 2026-05-11 | Comment only in `scripts/r20_bench.py` (31B bf16 stub — confirmed in MEMORY.md as 4-layer stub) |
| `mlx-community/gemma-4-26b-a4b-it-4bit` | 40 KB | 2026-05-16 | Config/script comments only; LM Studio `gemma-4-26b-a4b-it-optiq` is the real production variant |
| `mlx-community/gemma-4-26b-a4b-it-mxfp4` | 4 KB | 2026-05-14 | No |
| `mlx-community/gemma-4-26b-a4b-it-nvfp4` | 4 KB | 2026-05-14 | No |
| `mlx-community/gemma-4-31b-it-4bit` | 4 KB | 2026-05-12 | `scripts/r21_bench.py` only (benchmark script, not production) |
| `mlx-community/gemma-4-e4b-it-OptiQ-4bit` | 4 KB | 2026-05-11 | No |
| `mlx-community/granite-3.3-8b-instruct-4bit` | 4 KB | 2026-04-28 | No |
| `mlx-community/internlm3-8b-instruct-4bit` | 4 KB | 2026-04-28 | No |
| `mlx-community/pixtral-12b-4bit` | 4 KB | 2026-04-28 | No |
| `nightmedia/Qwen3-30B-A3B-...-mlx` | 4 KB | 2026-04-28 | No |
| `pj1983/Huihui-Qwen3-14B-abliterated-v2-mlx-4bit` | 4 KB | 2026-04-28 | No |
| `unsloth/Qwen3.6-27B-UD-MLX-4bit` | 4 KB | 2026-04-29 | No |
| `vanch007/Huihui-gemma-4-26B-A4B-it-abliterated-mlx-4bit` | 4 KB | 2026-04-28 | No |
| `vanch007/gemma-4-E4B-it-heretic-mlx-nvfp4` | 4 KB | 2026-04-30 | No |
| `z-lab/gemma-4-26B-A4B-it-DFlash` | 4 KB | 2026-05-14 | No |

---

## Recommendations

**Do not auto-delete.** Review each category:

### Priority 1 — Review before deleting

| Model | Size | Action |
|-------|------|--------|
| `minishlab/M2V_multilingual_output` | **997 MB** | Likely from experimental Model2Vec semantic search evaluation. Zero code references. Safe to delete if no external script uses it. Confirm with `grep -r M2V ~/Antigravity_AGENTS/` |
| `mlx-community/whisper-small-mlx` | 459 MB | Used in test stubs only (`test_stt_adapter_migration.py`). Not a default production model. Keep if running adapter tests; can delete if tests are mocked. |
| `Systran/faster-whisper-small` | 464 MB | Referenced in `startup_diagnostics.py`. Delete only if diagnostics check is removed or mocked. |

### Priority 2 — Safe to delete (stubs only, 0 B)

The 5 empty-directory stubs (`pyannote/speaker-diarization-3.1`, `speaker-diarization-2.1`, `segmentation-3.0`, `wespeaker-voxceleb-resnet34-LM`, `whisper-large-v3-mlx`) recover 0 bytes but reduce clutter. Run:
```bash
rm -rf ~/.cache/huggingface/hub/models--pyannote--speaker-diarization-2.1
rm -rf ~/.cache/huggingface/hub/models--pyannote--speaker-diarization-3.1
rm -rf ~/.cache/huggingface/hub/models--pyannote--segmentation-3.0
rm -rf ~/.cache/huggingface/hub/models--pyannote--wespeaker-voxceleb-resnet34-LM
rm -rf ~/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-mlx
```
Note: deleting `whisper-large-v3-mlx` means the next time Krab Ear tries to use it as primary STT model, HuggingFace will re-create the directory stub during the download attempt.

### Priority 3 — Bulk delete bench stubs (all 4 KB, disk savings ~160 KB)

All 40 LM Studio benchmark model stubs (4 KB each, April 28–May 14 bench sessions). These are metadata stubs only — the actual model files are in LM Studio's model dir. Script:
```bash
for m in \
  EZCon--Huihui-gemma-4-E4B-it-abliterated-4bit-mlx \
  Eldadalbajob--Huihui-Qwen3-Next-80B-A3B-Instruct-abliterated-mlx-3Bit \
  Jiunsong--supergemma4-26b-uncensored-mlx-4bit-v2 \
  KYUNGYONG--aya-expanse-32b-abliterated-Q4-mlx \
  Youssofal--Qwen3.6-35B-A3B-Abliterated-Heretic-MLX-4bit \
  alib97--Josiefied-Qwen3-14B-abliterated-v3-mlx-4Bit \
  divinetribe--gemma-4-31b-it-abliterated-4bit-mlx \
  enet45--Qwen2.5-14B-Instruct-Uncensored-mlx-4Bit \
  guardiangate1775--gemma-4-26B-A4B-it-assistant-4bit \
  huihui-ai--Huihui-GLM-4.7-Flash-abliterated-mlx-4bit \
  huihui-ai--Huihui-Qwen3.5-9B-abliterated-mlx-4bit \
  huihui-ai--Qwen2.5-7B-Instruct-abliterated \
  leonsarmiento--Llama-3.3-8B-Instruct-128K_Abliterated-8bit-mlx \
  lmstudio-community--gemma-4-E4B-it-MLX-4bit \
  mlx-community--Hermes-3-Llama-3.1-8B-4bit \
  mlx-community--Hermes-3-Llama-3.2-3B-4bit \
  mlx-community--Josiefied-Qwen3-4B-abliterated-v1-4bit \
  mlx-community--Josiefied-Qwen3-8B-abliterated-v1-4bit \
  mlx-community--Llama-3.2-11B-Vision-Instruct-abliterated \
  mlx-community--MiniMax-M2.5-Uncensored-4bit \
  mlx-community--Mistral-Nemo-Instruct-2407-4bit \
  mlx-community--Mistral-Small-Instruct-2409-4bit \
  mlx-community--Phi-4-mini-instruct-4bit \
  mlx-community--Qwen3.6-27B-OptiQ-4bit \
  mlx-community--aya-expanse-8b \
  mlx-community--c4ai-command-r7b-12-2024-4bit \
  mlx-community--dolphin-2.9-llama3-8b-4bit \
  mlx-community--gemma-4-26B-A4B-it-assistant-bf16 \
  mlx-community--gemma-4-26b-a4b-it-mxfp4 \
  mlx-community--gemma-4-26b-a4b-it-nvfp4 \
  mlx-community--gemma-4-31b-it-4bit \
  mlx-community--gemma-4-e4b-it-OptiQ-4bit \
  mlx-community--granite-3.3-8b-instruct-4bit \
  mlx-community--internlm3-8b-instruct-4bit \
  mlx-community--pixtral-12b-4bit \
  nightmedia--Qwen3-30B-A3B-Claude-4.5-Opus-High-Reasoning-2507-ABLITERATED-UNCENSORED-V2-qx86-hi-mlx \
  pj1983--Huihui-Qwen3-14B-abliterated-v2-mlx-4bit \
  unsloth--Qwen3.6-27B-UD-MLX-4bit \
  vanch007--Huihui-gemma-4-26B-A4B-it-abliterated-mlx-4bit \
  vanch007--gemma-4-E4B-it-heretic-mlx-nvfp4 \
  z-lab--gemma-4-26B-A4B-it-DFlash
do
  rm -rf "$HOME/.cache/huggingface/hub/models--$m"
done
```

### Keep (production active)

- `mlx-community/whisper-large-v3-turbo` (1.5 GB) — MODEL_BALANCED default
- `facebook/nllb-200-distilled-600M` (4 KB stub, real weights downloaded when first used) — Translator
- `pyannote/speaker-diarization-community-1` (268 KB) — active diarization fallback

---

## Methodology

1. Listed all `models--*` dirs in `~/.cache/huggingface/hub/` (53 total).
2. Normalized dir names from `models--<org>--<name>` to `<org>/<name>`.
3. Searched `KrabEar/`, `native/KrabEarAgent/`, `scripts/` with `grep -r` for model name as string literal across `.py` and `.swift` files.
4. Verified code context (production use vs test stub vs comment vs benchmark script).
5. Used `du -sh` for sizes; noted 4 KB entries are HF metadata stubs (actual weights in LM Studio).
