# LLM Conversion Guide — Krab Ear R21+ Benches

**Created:** 2026-05-12  
**Context:** R20 bench (Wave 44) showed `mlx-community/gemma-4-31B-it-assistant-bf16` fails to load in LM Studio with `No LM Runtime found for model format 'torchSafetensors'`.

---

## Background: Why the R20 Failure Happened

The LM Studio model catalog entry `mlx-community/gemma-4-31b-it-assistant` pointed to
`mlx-community/gemma-4-31B-it-assistant-bf16`, which is a **4-layer stub** (image backbone
only, 926 MB) in raw PyTorch SafeTensors format — not the full 31B model, not MLX-quantized.
LM Studio on Apple Silicon requires MLX or GGUF format for native inference.

The correct model ID for MLX 4-bit is: **`mlx-community/gemma-4-31b-it-4bit`**

---

## Quick Reference: MLX Models for Krab Ear Benches

| Model ID (HF) | LM Studio ID | Format | Size | Layers | Notes |
|---|---|---|---|---|---|
| `mlx-community/gemma-4-31b-it-4bit` | `mlx-community/gemma-4-31b-it-4bit` | MLX 4bit | ~17GB | 60 | Full 31B, proper MLX |
| `mlx-community/gemma-4-31b-it-6bit` | — | MLX 6bit | ~25GB | 60 | Higher quality |
| `mlx-community/gemma-4-31b-it-8bit` | — | MLX 8bit | ~32GB | 60 | Best quality, 36GB RAM tight |
| `mlx-community/gemma-4-26B-A4B-it-OptiQ-4bit` | `gemma-4-26b-a4b-it-optiq` | MLX 4bit | ~13GB | 46 | Current R19/R20 baseline |

---

## Method A: Download Pre-Built MLX Model (Recommended)

Use this when `mlx-community` already publishes an MLX-quantized version (4bit, 6bit, 8bit, mxfp4).

### Requirements

```bash
# Use .venv_vl — has mlx_lm 0.31.3 + huggingface_hub 1.12.2
/Users/pablito/.venv_vl/bin/python -c "import mlx_lm; print(mlx_lm.__version__)"
# Expected: 0.31.3
```

Disk space: at least 1.5× the model size free on the target volume.

### Download Script

```python
#!/usr/bin/env python3
"""Download a pre-built MLX model from HuggingFace Hub to LM Studio models directory."""

from huggingface_hub import snapshot_download

REPO_ID = "mlx-community/gemma-4-31b-it-4bit"   # change as needed
TARGET_DIR = "/Volumes/4TB SSD/LMStudio_models/mlx-community/gemma-4-31b-it-4bit"

path = snapshot_download(
    repo_id=REPO_ID,
    local_dir=TARGET_DIR,
    repo_type="model",
    ignore_patterns=["*.pt", "*.bin", "*.gguf"],  # skip non-MLX formats
)
print(f"Downloaded to: {path}")
```

Run with:

```bash
/Users/pablito/.venv_vl/bin/python download_mlx_model.py
```

Progress is tracked via `.cache/huggingface/download/*.incomplete` files in the target dir.
Files are moved to `TARGET_DIR/*.safetensors` upon completion of each shard.

### Verify After Download

```bash
# Confirm shard count and total size:
ls -lh "/Volumes/4TB SSD/LMStudio_models/mlx-community/gemma-4-31b-it-4bit/"*.safetensors

# Check config:
python3 -c "
import json
with open('/Volumes/4TB SSD/LMStudio_models/mlx-community/gemma-4-31b-it-4bit/config.json') as f:
    d = json.load(f)
tc = d.get('text_config', d)
print('layers:', tc.get('num_hidden_layers'))
print('quant bits:', d.get('quantization', {}).get('bits'))
"
```

Expected output:
```
layers: 60
quant bits: 4
```

### Add to LM Studio

LM Studio auto-discovers models in its configured models directory. After download completes:
1. Open LM Studio → Models
2. The new model appears as `mlx-community/gemma-4-31b-it-4bit`
3. Click Load to verify it loads with the MLX backend

---

## Method B: Convert torch SafeTensors → MLX 4bit

Use when only a torch/HF bf16 SafeTensors source exists (no pre-built MLX from mlx-community).

### Requirements

- `mlx_lm` 0.31.3+ in `.venv_vl` or LM Studio backend python
- Source model in HF format (NOT JANG/dealignai proprietary format)
- ~3× source size in free disk space (bf16 source ~62GB for 31B → need ~62+17=79GB free)

### Conversion Command

```bash
/Users/pablito/.venv_vl/bin/python -m mlx_lm convert \
  --hf-path google/gemma-4-31b-it \
  --mlx-path "/Volumes/4TB SSD/LMStudio_models/google/gemma-4-31b-it-mlx-4bit" \
  --quantize \
  --q-bits 4 \
  2>&1 | tee /tmp/mlx_convert.log
```

Note: `mlx_lm.convert` downloads the source from HF Hub if not cached locally. For a 31B bf16
model this is ~62GB download + conversion time (~30-60 min on M4 Max).

### Quantization Options

| Flag | Result | Disk size (31B) | Quality |
|---|---|---|---|
| `--q-bits 4` | Standard 4bit | ~17GB | Good |
| `--q-bits 6` | 6bit | ~25GB | Better |
| `--q-bits 8` | 8bit | ~32GB | Best (fits 36GB RAM tight) |
| `--q-bits 4 --quant-predicate mixed_4_6` | Mixed 4/6bit | ~20GB | Better quality/size tradeoff |

### Supported Source Formats

mlx_lm.convert accepts:
- Standard HF SafeTensors (bf16, fp16, fp32)
- HF model IDs (auto-downloads from Hub)
- Local paths to HF-format model dirs

**Not supported:**
- JANG format (dealignai/JANGQ-AI proprietary — requires LM Studio JANG runtime)
- GGUF (use llama.cpp or LM Studio directly for GGUF)

---

## Method C: Check for Existing MLX Models Before Downloading

Before any download or conversion, check what already exists:

```bash
# Find all 60-layer (31B) models on 4TB SSD:
python3 -c "
import os, json
base = '/Volumes/4TB SSD/LMStudio_models'
for root, dirs, files in os.walk(base):
    if 'config.json' in files:
        try:
            with open(os.path.join(root, 'config.json')) as f:
                d = json.load(f)
            tc = d.get('text_config', d)
            layers = tc.get('num_hidden_layers', 0)
            q = d.get('quantization', {})
            if layers >= 58:
                parts = len([f for f in os.listdir(root) if f.endswith('.part')])
                complete = len([f for f in os.listdir(root) if f.endswith('.safetensors') and '.part' not in f])
                print(f'{layers}L quant={q.get(\"bits\",\"none\")} parts={parts} complete={complete}: {root}')
        except: pass
"
```

---

## Troubleshooting

### "No LM Runtime found for model format 'torchSafetensors'"

The model in LM Studio's catalog is raw PyTorch SafeTensors (not MLX).
Fix: Download the proper MLX-quantized version (Method A) or convert (Method B).

### "mlx-lm not found" in Krab Ear venv

The Krab Ear `.venv_krab_ear` does NOT include mlx_lm (no conversion deps).
Use `.venv_vl` instead (has mlx_lm 0.31.3):

```bash
/Users/pablito/.venv_vl/bin/python -m mlx_lm convert --help
```

LM Studio's own mlx backend also has mlx_lm 0.31.3:
```
/Users/pablito/.lmstudio/extensions/backends/vendor/_amphibian/app-mlx-generate-mac14-arm64@23/bin/python3
```

### Download stalls / .part files frozen

LM Studio downloads can pause when switching between models. To resume:
1. Open LM Studio → Models → click the model → Resume Download
2. Or re-run the huggingface_hub script (it resumes from where it left off via `.incomplete` cache)

### JANG format models (dealignai, JANGQ-AI)

Cannot be used as source for mlx_lm.convert. JANG is LM Studio's proprietary quantization.
These load only via LM Studio's JANG runtime backend — not via mlx_lm.

---

## R21 Bench Candidates (Gemma-4-31B)

After `mlx-community/gemma-4-31b-it-4bit` is downloaded and loaded:

```bash
TOKEN="sk-lm-lkyUVqAw:ggACZoBqiaBpfwqPEvlK"
curl -m 600 -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -X POST http://localhost:1234/v1/chat/completions \
  -d '{
    "model": "mlx-community/gemma-4-31b-it-4bit",
    "messages": [{"role": "user", "content": "тест"}],
    "max_tokens": 20,
    "temperature": 0
  }'
```

Bench script: `scripts/r19_bench.py` (copy + update MODELS list for R21).
Add `"mlx-community/gemma-4-31b-it-4bit"` to candidates alongside current baseline.
