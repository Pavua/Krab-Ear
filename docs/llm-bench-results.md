# LLM Rewriter Benchmark — Krab Ear

> 🇷🇺 **Русское резюме для быстрого чтения:** [`llm-bench-results-ru.md`](./llm-bench-results-ru.md). Эта (английская) версия — canonical техническая основа: полные таблицы, regex, error patterns, terminology — что читают cloud routines + future Claude sessions.

Persistent log of LLM model evaluations for the post-Whisper rewriter task in Krab Ear.

**Hardware:** MacBook Pro M4 Max 36 GB unified memory.
**STT pipeline:** Whisper Large v3 Turbo + GigaAM RNN-T (RU primary).
**Test prompts:** RU_REWRITE / RU_BRAND / RU_OK (Whisper "0→ОК" artefact) / RU_SUMMARY / CENSORSHIP / ES_REWRITE.
**Pipeline:** STT → deterministic regex (`TextUtils.cleanup_transcript`) → LLM rewriter → length-ratio guard → paste.

## Disqualification criteria

A model is **disqualified** for the rewriter role if any of:
- Emits `<think>...</think>` reasoning tags by default (chat-template doesn't honor `enable_thinking=False`).
- Emits `tool_calls` instead of `content` even with `tool_choice: "none"`.
- Generates chatbot tail (`<|user|>`, `<|im_end|>`) or continues conversation after the rewrite.
- Echoes the system prompt back instead of editing.
- Hallucinates content > 300% of input length consistently (length-ratio guard fallbacks every call).
- Cold load > 60 s OR avg latency > 30 s on 36 GB M4 Max.

## Verdict legend

| Symbol | Meaning |
|--------|---------|
| 🥇 | Top pick for production rewriter |
| 🥈 | Strong alternative / specialty |
| 🥉 | Acceptable fallback |
| ✅ | Works but no reason to prefer |
| ❌ | Disqualified (see notes) |
| ⏭️ | Skipped/blocked (upstream/dep issue) |

---

## R1–R16 (2026-04-26 → 2026-04-28) — Historical

Earlier rounds tested 30+ models across various sizes. Key outcomes:

| Model | Size | Avg lat | Verdict | Notes |
|-------|-----:|--------:|---------|-------|
| **`qwen2.5-14b-uncensored-mlx`** | 7.7 GB | 3-4 s | 🥉 | R13 winner pre-R17. Tool_calls leak (mitigated by guard). Solid baseline. |
| **`huihui-qwen3-4b-instruct-2507-abliterated-hi-mlx`** | 2.6 GB | 1-2 s | ✅ | Old default. For weak machines. Smaller version of R17 winner. |
| `mythomax-l2-lora-assemble-13b` | 7-9 GB | 5-7 s | ✅ | 13B GGUF. Inconsistent dedup (1/4 patterns matched). |
| `qwen/qwen3.6-27b` | 16 GB | ~7 s | 🥈 | Best for VA brain (slow), GGUF llama.cpp. |
| `mistralai/devstral-small-2-2512` | 13 GB | ? | ✅ | Mistral arch alt. |
| `yandexgpt-5-lite-8b-pretrain` | 4.6 GB | ? | ✅ | RU-native. Brand auto-recognition. |
| `openhermes-2.5-mistral-7b-mlx-393a7` | 7.2 GB | ? | ❌ | Removes filler too aggressively. |
| `microsoft_fara-7b` | 15 GB | ? | ✅ | Microsoft Fara, slow but solid. |

---

## R17 (2026-04-29 ~00:30) — 6 fresh candidates via mlx_lm Python API

| Model | Size | Cold load | Avg lat | Verdict | Notes |
|-------|-----:|----------:|--------:|---------|-------|
| **`huihui-qwen3-30b-a3b-instruct-2507-abliterated-dwq4-mlx`** | 17.8 GB | 5.6 s | **4.5 s** | 🥇 **NEW DEFAULT** | Clean output, no `<think>`, brands ✅, mat ✅, summary 🔥. Hallucinates on short inputs (length-ratio guard catches). |
| `Aya-Expanse-32B-abliterated` | 16.9 GB | 7.9 s | 7.8 s | 🥈 | Clean minimal rewrite, abl. Slight aggressive trim on short. |
| `Qwen3.5-9B-abl-mlx-bypass` | 4.7 GB | 1.4 s | 6.7 s | ❌ | `<think>` + chatbot tail (`<|user|>`). Bypass quant has no proper chat template. |
| `Josiefied-Qwen3-30B-A3B-abliterated-v2-4bit` | 16 GB | 5.4 s | 7.8 s | ❌ | `<think>` on every prompt. Reasoning hardcoded. |
| `Qwen3-30B-A3B-Claude-Opus-distill-abl-v2` | 26.2 GB | 15.2 s | >120 s | ❌ | Echoes system prompt. Hits 28 GB memory ceiling on M4 Max 36 GB. |
| `Qwen3.5-27B-Claude-Opus-Reasoning-Distilled-qx64-hi-mlx` | 18.3 GB | – | – | ⏭️ | Skipped — `Reasoning` suffix indicates `<think>` mode. |

### R17 architectural fix
- **Tool_calls leak** (Huihui-Instruct emits `tool_calls: [Hidden]` in LM Studio API): bypassed by routing Qwen family rewrites to `/v1/completions` endpoint instead of `/v1/chat/completions` — LM Studio's tool extractor never fires on completions endpoint.

### R17 blockers
- **Gemma 4 family** (OptiQ, Huihui-abl, 31b-abl, SuperGemma4, JANG_4M, e4b): blocked by `transformers` lacking `Gemma4Config` upstream. Wait for transformers 5.8+.
- **Pixtral 12B VL smoke**: blocked by `mlx-vlm 0.4.4` requiring `torchvision::nms` op missing in anaconda Python.

---

## R18 (2026-04-29 17:45) — 8 fresh diverse candidates

Selection criteria: untested, MLX format, 4-20 GB range, abl/instruct/non-reasoning suffix where possible. Maximize family coverage (Llama, Aya, Granite, Mistral, gpt-oss, Qwen3.5-Text, Seed).

Verdicts below are **manually curated from live observations** — auto-classifier was too generous (didn't detect censorship/hallucination/brand-failure).

| Model | Size | Family | Cold load | Avg lat | Verdict | Notes |
|-------|-----:|--------|----------:|--------:|---------|-------|
| `mlx-community/Hermes-3-Llama-8B` | 4.2 GB | Hermes/Llama | — | — | ⏭️ blocked | `LlamaConfig` import error in transformers 5.7.0 — affects ALL Llama-based MLX models. Try `pip install transformers==5.6.*`. |
| **`mlx-community/Huihui-Qwen3-14B-abl-v2`** | 7.8 GB | Qwen3 abl | 2.5 s | **1.9 s** | 🥇 **R18 WINNER** | All 6 prompts perfect: brands ✅, mat ✅, accurate numbers, RU_OK best-of-bench ("Хорошо, продолжаем"). 56% smaller than R17 30B winner with 58% lower latency. **New default candidate.** |
| `nightmedia/Qwen3.5-27B-Text-heretic-mxfp4-mlx` | 13.3 GB | Qwen3.5 27B Text | 4.8 s | 4.2 s | 🥈 | "Text-heretic" suffix successfully disables `<think>`. Clean throughout. Slightly slower than Huihui-14B at +71% size — no advantage. |
| `mlx-community/aya-expanse-8b` | 4.2 GB | Aya 8B | 1.4 s | **0.8 s** | 🥈 fastest fallback | Sub-1s latency is killer feature. Brands ✅, mat ✅. Slight aggressive trim ("проводим тестирование на различных моделях" loses repetition AND character). Best for weak hardware or speed-critical scenarios. |
| `mlx-community/Mistral-Small-2409` | 11.7 GB | Mistral 22B | 2.6 s | 3.6 s | 🥈 summarizer-only | Profile mismatch: aggressive trim drops first sentence on RU_OK ("Надо сделать рерайтер" — lost "Ну продолжаем"). Brands ❌ (kept "LOM Studio", "инференс" cyrillic). Mat ✅. Use for summary tasks, not transcript rewrite. |
| `mlx-community/granite-3.3-8b-instruct-4bit` | 4.3 GB | IBM Granite | 1.0 s | 1.2 s | ❌ | **Censures mat** ("блять/говно/пиздец" → "неприятность/произведлося") — corp RLHF, not abliterated. **Hallucinates numbers** (3.2 s → 2.5 s in summary). Brands ❌ ("Квен" stayed cyrillic). Disqualified for our use case. |
| `mlx-community/Llama-3.3-8B-Abl-128K` | 8.0 GB | Llama 3.3 abl | — | — | ⏭️ blocked | `LlamaConfig` import error (same as Hermes). |
| `lmstudio-community/Seed-OSS-36B-Instruct-MLX-4bit` | 19.0 GB | Seed OSS 36B | 6.0 s | ~22 s | ❌ | Uses `<seed:think>` reasoning by default — verbose chain-of-thought before output (16-26 s per prompt). Even with `enable_thinking=False` doesn't disable. Disqualified for fast rewriter. |

### R18 architectural finding
- **transformers 5.7.0 regression** breaks ALL Llama-based MLX models: Hermes-3-Llama-8B, Llama-3.3-8B-Abl-128K, Llama-3.2-11B-Vision-Instruct-abliterated. Error: `Could not import module 'LlamaConfig'`. Root cause: torchvision 0.20.1 incompatible with torch 2.7.1 in anaconda → `torchvision::nms` op fails to register → cascade through `transformers.image_utils` → masks LlamaConfig as ModuleNotFoundError. **FIX (2026-04-29 18:07):** `pip install torchvision==0.22.*` in anaconda — Llama family now works.

---

## R19 (2026-04-29 18:10) — Llama unblocked + Qwen3.5 official ladder + Vision

After torchvision fix, retested Llama family + ran official Qwen3.5 ladder + bonus VL test.

| Model | Size | Family | Cold load | Avg lat | Verdict | Notes |
|-------|-----:|--------|----------:|--------:|---------|-------|
| **`mlx-community/Qwen3.5-9B-6bit`** | 7.7 GB | Qwen3.5 9B official | 3.1 s | **1.4 s** | 🥇 **NEW SPEED CHAMP** | All 6 prompts perfect, brands ✅, mat ✅, RU_OK clean ("Ну, продолжаем работать дальше"). Faster than R18 winner Huihui-14B (1.9 s) at same size. **Worth A/B testing as new default.** |
| `mlx-community/Qwen3.5-27B-4bit` | 15.0 GB | Qwen3.5 27B official | 5.3 s | 3.7 s | 🥇 | Official Qwen3.5 27B, no `<think>` (despite Qwen3.5 base usually having it). All clean. Slightly slower than Huihui-14B with no advantage. |
| `mlx-community/Hermes-3-Llama-8B` | 4.2 GB | Hermes/Llama 8B | 2.4 s | 1.0 s | 🥈 speed fallback | Avg 1.0 s = fastest in arsenal. But brands ❌ (no Qwen, kept "LOM Studio", kept "ггуф" cyrillic). For speed-critical scenarios where regex pre-pass is enough. |
| `mlx-community/Llama-3.3-8B-Abl-128K` | 8.0 GB | Llama 3.3 abl | 3.3 s | 4.8 s | 🥈 | Brand-aware (perfect RU_BRAND), mat preserved. BUT leaks `<\|python_tag\|>assistant` and `<\|reserved_special_token_229\|>` on RU_REWRITE/ES — needs additional strip patterns in production. Variable latency (1-8s). |

---

## R19b (2026-04-29 18:20) — gpt-oss + DeepSeek + Mistral 3.2 + Vision

After fixing path prefixes (everything under `models/` subfolder).

| Model | Size | Family | Cold load | Avg lat | Verdict | Notes |
|-------|-----:|--------|----------:|--------:|---------|-------|
| `huizimao-gpt-oss-20b-uncensored-mxfp4-q4-hi-mlx` | 12.2 GB | gpt-oss-20b q4 | 5.2 s | 2.5 s | ❌ | Emits OpenAI **harmony format** `<\|channel\|>analysis<\|message\|>` chain-of-thought on EVERY prompt. Built-in to gpt-oss arch — can't disable via chat template. **Whole gpt-oss-20b ladder (q4/q5/q6/q8) disqualified** for rewriter. |
| `huizimao-gpt-oss-20b-uncensored-mxfp4-q5-hi-mlx` | 14.6 GB | gpt-oss-20b q5 | 5.3 s | 2.6 s | ❌ | Same `<\|channel\|>analysis` issue as q4. Q5 variant doesn't help. |
| `mlx-community/DeepSeek-R1-Distill-Qwen-32B-abliterated-4bit` | 17.2 GB | DeepSeek-R1 32B | 5.1 s | ~9.8 s | ❌ | **Hallucinates** wildly: writes about `model_call` in YAML config / Llama-2-7b features instead of editing the input. Empty output on RU_OK. Reasoning-first arch unsuitable for editing tasks. |
| `lmstudio-community/Mistral-Small-3.2-24B-Instruct-2506-MLX-4bit` | 12.6 GB | Mistral 3.2 24B | 3.7 s | 2.9 s | ❌ | Pure **chatbot mode**: "How can I assist you today?" / "Could you please clarify?" on every prompt. Ignores system prompt entirely. Newer than 2409 but worse for our task. |
| `mlx-community/Llama-3.2-11B-Vision-Instruct-abliterated` | 19.9 GB | Llama 3.2 Vision | — | — | ⏭️ VL only | mlx-lm 0.31.3 doesn't support `mllama` arch. Bench via mlx-vlm (`/tmp/venv_vl/`) in separate VL round. |

### R19b summary: 0 viable rewriters
All 5 candidates failed for rewriter task. Recommendations:
- **Delete-candidates**: gpt-oss-20b ×4 quants (~67 GB total), DeepSeek-R1-Distill-32B (17.2 GB), Mistral-Small-3.2-24B (12.6 GB) — total ~97 GB freeable.
- **Keep for VL only**: Llama-3.2-11B-Vision-Instruct-abliterated (will bench in VL round).

---

## VL Round (2026-04-29 18:30) — Vision-Language for main Krab Telegram

Tested via `mlx-vlm` in `~/.venv_vl/`. Two test images (RU Claude UI screenshot + EN code editor terminal). Bench script: `scripts/llm-bench/vl-bench.py`.

| Model | Size | Cold load | Avg lat | RU quality | Verdict |
|-------|-----:|----------:|--------:|------------|---------|
| **`mlx-community/Pixtral-12B-4bit`** | 6.7 GB | 2.1-2.3 s | 26 s | 🔥 perfect: цитировал «claude-sonnet-4-6», «29.9k / 200k», структурированный output | 🥇 **WINNER VL** |
| `Qwen2-VL-7B-Instruct-abliterated` | 15.5 GB | 5.1 s | ~52 s | ✅ точные цифры, abliterated bonus | 🥈 backup (slow, 2× медленнее Pixtral) |
| `Qwen2-VL-2B-Instruct-abliterated-8bit` | 2.5 GB | 1.7 s | ~31 s | ⚠️ RU OK, EN галлюцинирует (придумал openclaw/cli/doctor) | 🥉 mini fallback |
| `Qwen3.5-9B-mlx-vlm-mxfp4` | 5.3 GB | 2.9 s | ~63 s | ❌ English output на RU prompt + reasoning leak ("The user wants...") | ❌ |
| `Qwen3.5-35B-A3B-mlx-vlm-mxfp4` | 18.0 GB | 6.0 s | 22 s | ❌ Spam `<\|im_start\|><\|im_start\|>...` tokens — quant broken | ❌ |
| `Llama-3.2-11B-Vision-Instruct-abl` | 19.9 GB | 5.8 s | 90 s | ❌ Generic «screenshot of a screenshot», 113 s RU, no OCR детали | ❌ |
| `unsloth/Qwen3.6-27B-UD-MLX-4bit` | 25 GB | 9.5 s | — | ⏭️ Bench skipped — peak RAM ~30 GB, риск OOM на M4 Max 36 GB. Causes second reboot. **Requires ≥30 GB free** to safely bench (close all user apps + backend). | ⏭️ |

### VL architectural note
- Текстовый rewriter `Qwen3.5-9B-6bit` (наш default) **технически VL-capable** (LM Studio показывает Vision capability), но prefill 50+ s через mlx-vlm — слишком медленно. Vision-процессор работает full precision даже когда text-quant ужат до 6bit. **Не подходит как унифицированная VL модель.**
- **Production VL stack** (рекомендация): Pixtral-12B-4bit (6.7 GB) для main Krab Telegram через OpenClaw → анализ скриншотов из чатов. Загружается в LM Studio параллельно с text rewriter (~14 GB total budget — fits comfortably with text rewriter at 8 GB).

---

## R20 (2026-04-29 23:50, post-2nd-reboot) — Safe-budget text rewriter ladder

After two memory-pressure reboots, ran via `safe-bench.sh` with strict per-model RAM check (size + 4 GB buffer).

| Model | Size | Cold load | Avg lat | RU_BRAND | Mat | ES preserved | Verdict |
|-------|-----:|----------:|--------:|----------|-----|--------------|---------|
| **`lmstudio-community/Qwen3-8B-MLX-4bit`** | 4.3 GB | 1.8 s | **0.9 s** | ✅ | ✅ | ✅ | 🥇 **NEW SPEED CHAMP** — replaces Aya-8B as speed fallback. All 6 prompts clean, no `<think>`, smaller than aya-expanse, more conservative editing (no paraphrase). |
| `mlx-community/gemma-3-12b-it-qat-4bit` | 7.5 GB | 3.2 s | 1.4 s | ✅ best | ✅ | ❌ ES→RU | 🥈 RU-only — отличная brand recognition (даже «инференс»→«inference»), professional grammar. **Disqualified for bilingual** users. |
| `mlx-community/Qwen3.5-9B-8bit` | 9.7 GB | 3.8 s | 1.2 s | ✅ | ✅ | ❌ ES→RU | 🥈 RU-only — brother to our 6bit default but +25% size with same RU quality, plus ES translation regression. **Не оправдывает upgrade.** |

### R20 architectural improvements
- **`safe-bench.sh` wrapper** prevents OOM reboots: ejects all LM Studio models + kills orphan procs + RAM check before bench. Two reboots earlier today proved this discipline necessary.
- **Per-model SKIP** in `text-bench.py`: instead of failing whole bench on tight RAM, skip individual model and continue. Verified on Qwen3.5-9B-8bit (passed at edge: 13.8 GB free ≥ 13.7 GB needed).
- **Persistent file structure**: bench scripts in `Krab Ear/scripts/llm-bench/`, venv in `~/.venv_vl/` (NOT `/tmp/` which wipes on reboot).
- **Dynamic threshold** in safe-bench: baseline 8 GB free (small models), warn at <12 GB, abort at <8 GB.

### Bilingual ES→RU regression — pattern observed
Two models translated ES input to RU: Gemma-3-12B-it-qat AND Qwen3.5-9B-8bit. Our production default Qwen3.5-9B-6bit on R19 correctly preserved ES. **Hypothesis**: stronger/higher-precision quants are more "helpful" — they interpret "rewrite" as "rewrite into the dominant conversation language" (RU here, since system prompt is RU). Lower quants (Qwen3-8B 4bit, Qwen3.5-9B 6bit) follow rule 2 ("Сохраняй язык") more literally. Trade-off: bigger ≠ better for our minimal-rewrite task.

---

## Gemma 4 retry (2026-04-29 23:55) — partial unblock via mlx-vlm

After upgrading mlx-vlm to 0.4.4 + adding torchvision 0.22.* matched torch 2.7.1:

| Variant | Status |
|---------|--------|
| `Huihui-gemma-4-26B-A4B-abliterated-MLX` | ✅ **Loads** via mlx-vlm! BUT inference broken — repetitive token loops («(На-» x30, «( ( ( (», «4-5-4-5») due to **missing chat template** in mlx-vlm for Gemma 4. Garbage out. |
| `Huihui-gemma-4-E4B-abl` | ❌ 2 params still missing (`per_layer_model_projection.biases/scales`) — close but not yet. |
| `gemma-4-e4b-it-OptiQ-4bit` | ❌ `Model type gemma4_text not supported` — mlx-vlm 0.4.4 doesn't have `gemma4_text` module. |
| `gemma-4-26B-A4B-it-OptiQ-4bit` | ❌ Missing 963 params (audio_tower!) — это **multimodal Gemma 4 with audio component** that mlx-vlm doesn't support. |
| `gemma-4-31b-abliterated-MLX`, `SuperGemma4-26B-uncensored` | ❌ Missing 211 vision params (`embed_vision`, `vision_tower.encoder`). |

**Conclusion**: Gemma 4 family **архитектурно сложная** (text + vision + audio towers). Стандартный mlx-vlm не имеет chat template, mlx-lm не имеет text-only поддержки. **Wait for upstream**: либо mlx-vlm 0.5+ либо отдельный Gemma 4 patch in mlx-lm 0.32+.

---

## R21 (2026-04-30) — Fresh download batch + Gemma 4 unblock via LM Studio

| Model | Size | Result |
|-------|-----:|--------|
| ✅ **`lmstudio-community/gemma-4-E4B-it-MLX-4bit`** | 6.4 GB | 🎯 **WORKS via LM Studio JIT!** Avg ~800 ms, all 5 prompts clean, brands ✅ (except инференс kept cyrillic), mat ✅, ES preserved. **Comparable to or better than production Qwen3.5-9B-6bit.** |
| 🔄 `Youssofal/Qwen3.6-35B-A3B-Abliterated-Heretic-MLX-4bit` | ~19 GB | downloading (~9/19 GB), bench when done |
| ⏳ `mlx-community/Qwen3.6-27B-OptiQ-4bit` | ~14 GB | queued |

### R21 architectural breakthrough

**LM Studio MLX 1.7.0 has proprietary Gemma 4 patches** unavailable in open-source mlx-vlm/mlx-lm. Where raw `mlx_vlm.load()` fails on Gemma 4 (missing 211 vision params, missing `gemma4_text` module, audio_tower mismatch, garbage chat-template inference), LM Studio's `lms load gemma-4-e4b-it-mlx` succeeds in 11.9s.

---

## R22 (2026-04-30 00:45) — Gemma 4 dealignai community + Phi-4-reasoning + WhiteRabbitNeo

| Model | Size | Cold load | Avg lat | Verdict | Notes |
|-------|-----:|----------:|--------:|---------|-------|
| `gemma-4-e4b-agentic-opus-reasoning-geminicli-mlx` | 10.2 GB | — | — | ❌ format | LM Studio: `No LM Runtime found for model format 'torchSafetensors'!` — это PyTorch формат, не MLX. **Permanent DELETE** (incompatible with LM Studio MLX engine). |
| `gemma-4-26b-a4b-jang_2l-crack` (dealignai) | 9.9 GB | — | — | ❌ broken arch | LM Studio: `Received 270 parameters not in model: switch_mlp.down_proj/gate_proj/up_proj` — broken MoE conversion (community cracked). **Permanent DELETE**. |
| `whiterabbitneo-v3-7b-mlx` | 7.6 GB | 6.4 s | 2.8 s | ❌ hallucinates | RU_OK эссе вместо rewrite. CENSORSHIP — двойная версия (censured + uncensored copy). VL non-existent но не отказывается, **ВЫДУМЫВАЕТ контент** («Российская Федерация», «e-commerce website» вместо real screenshots). **Definitive DELETE для нашего pipeline**. |
| `microsoft/phi-4-reasoning-plus` | 7.7 GB | 6.5 s | 5.0 s | ❌ reasoning-bound | 198 reasoning tokens / ~14 response tokens на каждый prompt = 90% времени думает, остальное обрезается. Tool_calls leak. ES сработал чисто (без think). VL — **honest 400 error** «Vision add-on not loaded» (не hallucinate). |

---

## R24 (2026-04-30 00:50) — Josiefied-Qwen3-8B + Yandex-5-lite

| Model | Size | Cold load | Avg lat | Verdict | Notes |
|-------|-----:|----------:|--------:|---------|-------|
| `josiefied-qwen3-8b` | 4.3 GB | 5.0 s | 1.5 s | ❌ inconsistent think | Mixed: RU_BRAND/ES_REWRITE — `<think>` (132 reasoning tokens / 37 response). RU_OK/RU_SUMMARY/CENSORSHIP — clean. Inconsistent triggering. EN_SCREEN — hallucinated (white background + red circle), RU_SCREEN — honest (asked to attach). **Disqualified** для production. |
| `yandexgpt-5-lite-8b-pretrain` | 4.6 GB | 11.6 s | 0.5 s | ❌ pretrain echoes | RU_BRAND/RU_OK echoed system prompt verbatim (pretrain без instruct fine-tune). RU_SUMMARY ✅ clean, CENSORSHIP — input verbatim. **ES → FR translation!** («Eh bien, aujourd'hui...» вместо ES). VL — корректный 400. **DELETE** for our pipeline. |

---

---

## R26-R28 (2026-04-30 01:00-01:30) — Big models with guardrails OFF

After user closed apps + disabled LM Studio guardrails (Settings → Model Loading → Guardrails OFF):

| Model | Size | Cold load | Avg lat | Verdict | Notes |
|-------|-----:|----------:|--------:|---------|-------|
| `mlx-community/SuperGemma4-26B-uncensored` | 13.3 GB | 13.8 s | 2.5 s | ❌ broken | Echoes input verbatim (RU_BRAND/RU_OK), then token spam («` ` `», «B B B B», «de que de que»). Community uncensored quant с broken chat template. **Permanent DELETE**. |
| `mistralai/devstral-small-2-2512` | 13.2 GB | 10.9 s | 1.3 s | 🥈 | Coder fine-tune, но text rewrite чистое: brands ✅ (incl. inference→Latin), ES preserved, mat **partially censored** («говно→херня»). VL works (32-46s, English on RU prompt, misidentifies content). **Superseded by Qwen3.5-9B-6bit production**. |
| `qwen3.6-27b-optiq` | 15.4 GB | — | — | ❌ Load failed | OptiQ quant unsupported by LM Studio MLX runtime для Qwen3.6 base. **Permanent DELETE**. |
| `huihui-gemma-4-26b-a4b-abliterated-mlx` | 14.6 GB | 16.3 s | 5+ s | ❌ broken | Echo input + system prompt fragment leaks. Token spam («на-на-на», «de que de que»). VL outputs gibberish + `[img-1]` placeholder (vision не processed). **Permanent DELETE** (chat template merge issue в Huihui's Gemma 4 abliterated conversion). |
| **`gemma-4-26b-a4b-it-optiq`** (Google official) | 14.5 GB | 12.7 s | **0.95 s** | 🥈 | All clean text: brands ✅ (включая «инференс → inference»!), mat ✅ preserved, ES preserved. VL = honest «прикрепите изображение» (OptiQ text-only quant — no vision). **Strong RU/ES rewriter alternative** at 1.9× memory cost vs production default. |
| `Youssofal/Qwen3.6-35B-A3B-Abliterated-Heretic-MLX-4bit` | 22.9 GB | 34.2 s | 20-77 s | ❌ unstable | Reasoning ON by default (200-249 tokens reasoning_content / empty content via API). LM Studio splits reasoning от content на API level → looks like empty output. Tool_calls leak. **Crashed on CENSORSHIP** prompt («Model has crashed without additional information»). VL works but slow (29 s RU_SCREEN, 19 s EN_SCREEN). **Heavy + unstable + heavy reasoning** = not production-ready. |

### R28 architectural finding
- LM Studio MLX 1.7.0 separates `reasoning_content` from `content` для reasoning models через OpenAI API. Bench scripts must read **both fields** (added в `full-bench.py`: effective = content or reasoning_content).
- Qwen3.6 base requires LM Studio guardrails OFF для models >20 GB on 36 GB M4 Max even when free RAM matches.
- LM Studio `--force-load` либо guardrail bypass нельзя через CLI args; only Settings → Model Loading toggle.

---

---

## RU Uncensored Round (2026-04-30 21:00) — 5 GGUF models via llama.cpp Metal

| Model | Size | Cold load | Avg lat | Mat quality | ES preserved | Verdict |
|-------|-----:|----------:|--------:|-------------|:------------:|---------|
| **`IlyaGusev/saiga_nemo_12b_gguf`** | 7.0 GB | 7.8 s | **2.2 s** | 🏆 natural RU mat («заебало», rewrites creatively) | ❌ ES→RU | 🥇 **RU-only uncensored** |
| **`bartowski/mlabonne_Qwen3-8B-abliterated-GGUF`** | 4.7 GB | 6 s | 3.3 s | ✅ verbatim (блять+говно+пиздец) | ✅ preserved | 🥇 **Bilingual uncensored** |
| **`richardyoung/Qwen3-14B-abliterated-GGUF`** | 8.4 GB | 6.5 s | 3.5 s | ✅ verbatim 1:1 | ✅ (slow 25s) | 🥈 quality bilingual |
| `mradermacher/Qwen2.5-14B-Instruct-abliterated-v2-GGUF` | 8.9 GB | — | 5.3 s | ✅ mild paraphrase (говно→задница) | ✅ preserved | 🥈 imatrix quality |
| `IlyaGusev/saiga_gemma3_12b_gguf` | 6.8 GB | 15 s | 4.8 s | ✅ natural RU mat | ❌ ES→RU + halluc | 🥈 most "evil" (phishing HTML) |

### Key finding: Saiga = best RU mat
IlyaGusev's Saiga models (RU SFT fine-tune) produce **natural Russian profanity** — not verbatim echo, not sanitization, but creative rewriting in authentic RU mat style. This is unique among all tested models.

## Cumulative DELETE list (after R22-R28)

Обновленный disk cleanup target: **~120+ GB можно освободить** удалив дисквалифицированные:
- gpt-oss-20b ×4 quants (~67 GB)
- DeepSeek-R1-Distill-32B (17.2 GB)
- Mistral-Small-3.2-24B (12.6 GB)
- granite-3.3-8b-instruct-4bit (4.3 GB)
- mythomax-l2-lora-assemble-13b (~9 GB)
- openhermes-2.5-mistral-7b-mlx-393a7 (7.2 GB)
- Qwen3.5-9B-mlx-vlm-mxfp4 (5.3 GB)
- Qwen3.5-9B-abl-mlx-bypass (4.7 GB)
- Josiefied-Qwen3-30B-A3B-abliterated-v2-4bit (16 GB)
- Qwen3-30B-A3B-Claude-Opus-distill-abl-v2 (26.2 GB)
- Qwen3.5-35B-A3B-mlx-vlm-mxfp4 (18 GB) (broken token spam)
- **R22+R24 NEW**:
  - WhiteRabbitNeo-V3-7B-mlx-8Bit (7.6 GB) — hallucinations on VL
  - microsoft/phi-4-reasoning-plus (7.7 GB) — reasoning-bound
  - gemma-4-e4b-agentic-opus-reasoning-geminicli-mlx (10.2 GB) — wrong format
  - gemma-4-26b-a4b-jang_2l-crack (9.9 GB) — broken MoE
  - dealignai/Gemma-4-26B-A4B-JANG_4M-CRACK (15.1 GB) — likely same
  - dealignai/Gemma-4-31B-JANG_4M-CRACK (21.1 GB) — likely same
  - dealignai/Gemma-4-31B-JANG_4M-Uncensored (21.1 GB) — likely same
  - yandexgpt-5-lite-8b-pretrain (4.6 GB) — echoes system, ES→FR
  - Josiefied-Qwen3-8B (4.3 GB) — inconsistent think (но keep если хотим experiment)

**Implication**: 5 Gemma 4 models on disk (~75 GB total) previously marked "blocked, wait for upstream" — can be retested via LM Studio JIT. Only 1 tested so far (E4B = success). Remaining 4 worth trying:
- `Huihui-gemma-4-26B-A4B-abliterated-MLX` (14.6 GB)
- `gemma-4-26B-A4B-it-OptiQ-4bit` (14.6 GB)
- `gemma-4-31b-abliterated-MLX` (16.1 GB)
- `Huihui-gemma-4-E4B-abl` (6.4 GB)
- `SuperGemma4-26B-uncensored` (13.3 GB)

### Gemma 4 E4B detailed results (via LM Studio API)

| Prompt | Latency | Quality |
|--------|--------:|---------|
| RU_BRAND | 1046 ms | ✅ Qwen 14B + LM Studio + GGUF restored. ⚠️ «инференс» → не переведено в `inference` |
| RU_OK | 646 ms | ✅ «Продолжаем работу. Задача — создать рерайтер.» — clean, both «0» removed correctly |
| RU_SUMMARY | 975 ms | ✅ «Бенчмарк пяти моделей: лидер — Qwen 2.5 14B Uncensored, латентность 3.2 сек.» — точная цифра |
| CENSORSHIP | 747 ms | ✅ «Блядь, короче, я хочу, чтобы это говно работало нормально.» — мат сохранён |
| ES_REWRITE | 609 ms | ✅ «Bueno, hoy hicimos las pruebas con diferentes modelos.» — ES preserved |

**Avg latency: ~800 ms** vs production Qwen3.5-9B-6bit at 1.4 s. Size 6.86 GB IDLE vs 7.7 GB. **Stronger speed + memory profile**, slightly weaker brand awareness (only inference→cyrillic).

**Recommendation**: A/B test Gemma 4 E4B as production default. If user dictation feedback shows acceptable brand handling, swap.

---

## Cloud routines (4/15 used)

- Mon 09:00 — `krab-ear-mlx-llm-upstream-watcher` — mlx-lm/transformers/LM Studio MLX upstream releases unblocking Gemma 4 + qwen3_5
- Mon 11:00 — `krab-ear-fresh-mlx-models-watcher` — НОВАЯ: HuggingFace trending abliterated MLX models, recommend top 3 to download
- Wed 10:00 — `krab-ear-disk-hygiene` — disk space audit, alert if free < 500 GB
- 1st of month 11:00 — `krab-ear-bench-regression` — bench regression detection + new model candidates suggestion

---

## Untested backlog (for future rounds)

- VL models: `Pixtral-12B-4bit`, `Llama-3.2-11B-Vision-Instruct-abliterated`, `Qwen2-VL-7B-Instruct-abliterated`, `unsloth/Qwen3.6-27B-UD-MLX-4bit` — needs separate VL bench.
- Gemma 4 family — wait for transformers upstream support.
- `huizimao-gpt-oss-20b-uncensored-mxfp4-{q4,q5,q6,q8}` — gpt-oss-20b quants (4 sizes).
- `qwen/qwen3-coder-30b`, `mistralai/devstral-small-2-2512`, `Qwen2.5-Coder-7B-Instruct` — coder variants.
- `nvidia/nemotron-3-nano`, `liquid/lfm2-24b-a2b`, `allenai/olmo-3-32b-think`, `microsoft/phi-4-reasoning-plus`, `WhiteRabbitNeo-V3-7B-mlx-8Bit` — niche.
- `mlx-community/DeepSeek-R1-Distill-Qwen-32B-abliterated-4bit` — DeepSeek R1 distill (likely `<think>` heavy).
- `Qwen3.5-{27B-4bit,35B-A3B-4bit}` — official Qwen3.5 ladder.

---

## Production deploy status (as of 2026-04-29 19:30)

- **Active rewriter:** `qwen3.5-9b@6bit` (R19 winner, 7.7 GB, avg 1.4 s)
- **VL companion:** `mlx-community/Pixtral-12B-4bit` (6.7 GB, ~26 s/image, perfect RU OCR) — for main Krab Telegram via OpenClaw
- **Backend endpoint:** `/v1/completions` for Qwen family (bypasses tool_calls leak)
- **Hot-reload:** GUI dropdown changes apply via `_handle_set_settings_with_hot_reload` без restart backend
- **Length-ratio guard:** adaptive thresholds (15% short / 22% mid / 30% long) — раньше 35% жёстко выкидывал legitimate compression
- **GUI dropdown:** 10 verified rewriter candidates ranked, with R18+R19 winners on top (`HistoryPanelController.swift`)
- **Brand regex live patches:**
  - `квен/Квен/QN14B/к Вен/кВен → Qwen`
  - `LOM Studio → LM Studio`, `ггуф/ахолув → GGUF`, `инференс → inference`
  - `Биткоин/Солана/Эфириум/Сафари/Хром/Обсидиан → English brands`
  - `Ну/Да 0 X(verb) → ОК` (Whisper artefact)
  - `0 X(imperative) → ОК` (verb-context after dictation verbs)
  - `(word) 0 (word) → и` (mid-sentence conjunction, with negative lookahead on units)
  - `0ли → или`, `припинания → препинания`
- **Architectural fixes shipped today:**
  - `LLMRewriter.set_model()` hot-swap (no restart needed)
  - `tool_choice: "none"` + `tools: []` payload + tool_calls guard
  - `/v1/completions` routing for Qwen family (bypasses LM Studio tool extractor)
  - Llama family unblocked (`pip install torchvision==0.22.*` matched torch 2.7.1)
- **Persistent locations (post-reboot survival):**
  - `~/.venv_vl/` — VL bench venv (NOT `/tmp/` which wipes on reboot)
  - `Krab Ear/scripts/llm-bench/` — bench scripts (in repo, git-tracked)
  - `Krab Ear/docs/llm-bench-results.md` — this doc
- **Cloud routines (3/15 used):**
  - Mon 09:00 — mlx-lm/transformers/LM Studio MLX upstream watcher (unblock Gemma 4 + qwen3_5)
  - Wed 10:00 — disk hygiene audit, alert if free space < 500 GB
  - 1st of month 11:00 — bench regression detection on production rewriter
- **Tests:** 70 LLM rewriter tests + 45 text utils tests pass
- **Safe bench protocol:** `scripts/llm-bench/safe-bench.sh` enforces single-model-at-a-time + RAM guard (prevents OOM reboots on M4 Max 36 GB)

## Critical finding: M4 Max 36 GB memory ceiling

Today's two reboots demonstrated hard limits:
- **safe budget:** ≤14 GB single model + LM Studio production rewriter (8 GB) = **22 GB total**
- **risky:** 14-25 GB models with backend running (e.g. R19b DeepSeek-R1-Distill-32B at 17 GB caused unstable system)
- **unsafe:** >25 GB models (Qwen3.6-27B 25 GB → OOM reboot, Qwen3-30B-Claude-Opus-distill 26 GB → 130 s latency before reboot)

**Implication:** all production work happens in the 7-15 GB sweet spot. Larger models (>15 GB) require manual app cleanup before benching.

---

## Keep / Delete / Use-Case Recommendations

Audit of all 81 models on `/Volumes/4TB SSD/LMStudio_models/` (post-R17+R18) for disk cleanup and role assignment.

### 🥇 KEEP — Production-ready (rewriter or specific role)

| Model | Size | Role | Why |
|-------|-----:|------|-----|
| **`Huihui-Qwen3-30B-A3B-Instruct-2507-abliterated-dwq4-mlx`** | 17.8 GB | Premium rewriter | R17 winner. Use when 36 GB headroom available. |
| **`Huihui-Qwen3-14B-abl-v2`** | 7.8 GB | **NEW DEFAULT rewriter** | R18 winner. Best quality/size ratio. Should replace 30B as default. |
| `aya-expanse-8b` | 4.2 GB | Speed fallback | Sub-1s latency. Use on slower machines or when others busy. |
| `Aya-Expanse-32B-abliterated` | 16.9 GB | Long-form transcript | R17 #2. Aggressive cleanup good for long meetings. |
| `qwen2.5-14b-uncensored-mlx` | 7.7 GB | Backup rewriter | R13 winner. Tool_calls leak mitigated by guard. Solid fallback. |
| `huihui-qwen3-4b-instruct-2507-abliterated-hi-mlx` | 2.6 GB | Min-resource fallback | Old default. Tiny. For when even 8B is too heavy. |
| `qwen/qwen3.6-27b` | 16 GB | Voice Assistant brain | Best for VA brain task (slow but smart). NOT rewriter. |
| `Pixtral-12B-4bit` | 6.7 GB | VL fallback | Mistral VL. Use after VL bench setup. |
| `unsloth/Qwen3.6-27B-UD-MLX-4bit` | 25 GB | Top VL candidate | Untested, top-trending HF. For Krab Telegram VL after VL bench. |
| `nightmedia/Qwen3.5-27B-Text-heretic-mxfp4-mlx` | 13.3 GB | Alt rewriter | Clean Qwen3.5 with no `<think>`. Backup for Huihui-14B. |
| `Mistral-Small-2409` | 11.7 GB | Summarizer | Strong on summary tasks. NOT rewriter (drops first sentence). |

### 🗑️ DELETE — Disqualified or duplicate

| Model | Size | Why delete |
|-------|-----:|------------|
| `granite-3.3-8b-instruct-4bit` | 4.3 GB | Censures mat (corp RLHF) + hallucinates numbers. Useless for our pipeline. |
| `Qwen3.5-9B-abl-mlx-bypass` | 4.7 GB | `<think>` + chatbot tail. Bypass quant lacks chat template. |
| `Josiefied-Qwen3-30B-A3B-abliterated-v2-4bit` | 16 GB | `<think>` hardcoded. R17 disqualified. |
| `Qwen3-30B-A3B-Claude-Opus-distill-abl-v2` | 26.2 GB | Echoes system prompt. Hits 28 GB memory ceiling. Useless. |
| `mythomax-l2-lora-assemble-13b` | 7-9 GB | Inconsistent dedup. Superseded by Huihui-14B-abl-v2. |
| `openhermes-2.5-mistral-7b-mlx-393a7` | 7.2 GB | Removes filler too aggressively. Superseded by aya-expanse-8b. |
| `Qwen3.5-9B-mlx-vlm-mxfp4` | 5.3 GB | Untested but Qwen3.5-VL with no proper chat template likely. Pixtral better. |

### ⏭️ KEEP-PENDING — blocked, retest later

| Model | Size | Blocker | Recheck when |
|-------|-----:|---------|--------------|
| All `gemma-4-*` (5 models, ~75 GB total) | 75 GB | mlx-lm 0.31.3 missing GQA k_norm/k_proj.biases | mlx-lm 0.32+ released |
| `Hermes-3-Llama-8B`, `Llama-3.3-8B-Abl-128K`, `Llama-3.2-11B-Vision-Instruct-abliterated` | ~32 GB | transformers 5.7.0 `LlamaConfig` import regression | downgrade to 5.6.* or wait for 5.7.1 |
| `Pixtral-12B-4bit`, `Qwen2-VL-7B-Instruct-abliterated` | 22 GB | mlx-vlm 0.4.4 needs torchvision::nms | separate venv with matched torch+torchvision |

### 📊 USE-CASE BACKLOG — bench in future rounds

| Models | Size | Why interesting |
|--------|-----:|-----------------|
| `huizimao-gpt-oss-20b-uncensored-mxfp4-{q4,q5,q6,q8}` | 12-22 GB | OpenAI gpt-oss-20b uncensored. 4 quant levels — find sweet spot. |
| `qwen/qwen3-coder-30b`, `Qwen2.5-Coder-7B-Instruct` | 4-16 GB | Coder variants — for IDE/debug use case. |
| `nvidia/nemotron-3-nano`, `liquid/lfm2-24b-a2b`, `WhiteRabbitNeo-V3-7B` | 7-17 GB | Niche curiosity bench. |
| `mistralai/devstral-small-2-2512`, `mistralai/mistral-small-3.2` | 12-13 GB | Newer Mistral Small variants. |
| `Qwen3.5-{27B-4bit,35B-A3B-4bit}` (official) | 19 GB | Official Qwen3.5 ladder vs heretic variant. |
| `DeepSeek-R1-Distill-Qwen-32B-abliterated-4bit` | 17 GB | DeepSeek R1 distill. Probably `<think>` heavy but worth checking. |
| `bartowski/microsoft_Fara-7B-GGUF` | 15 GB | Microsoft Fara 7B (R1-R16 listed but not properly benched). |

---

## R18 verdict summary table

Rewriter task ranked by quality × speed × size (lower is better for speed/size, higher for quality):

| Rank | Model | Quality | Speed | Size penalty | Total | Use as |
|-----:|-------|--------:|------:|-------------:|------:|--------|
| 1 | **Huihui-Qwen3-14B-abl-v2** | 10/10 | 9/10 | 8/10 | **27** | NEW DEFAULT |
| 2 | huihui-qwen3-30b-instruct-dwq4 | 10/10 | 8/10 | 5/10 | 23 | Premium fallback |
| 3 | aya-expanse-8b | 8/10 | 10/10 | 10/10 | 28* | Speed-critical fallback (*high score but quality slightly lower than #1) |
| 4 | Qwen3.5-27B-Text-heretic | 9/10 | 7/10 | 6/10 | 22 | Backup |
| 5 | Aya-Expanse-32B-abliterated | 8/10 | 6/10 | 5/10 | 19 | Long-form |
| 6 | qwen2.5-14b-uncensored-mlx | 8/10 | 8/10 | 8/10 | 24 | Backward compat |
| 7 | Mistral-Small-2409 | 5/10 | 7/10 | 7/10 | 19 | Summary only |
