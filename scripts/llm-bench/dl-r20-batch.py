"""Download 3 fresh MLX models for R21 bench (after R20 results).

Targets:
1. Youssofal/Qwen3.6-35B-A3B-Abliterated-Heretic-MLX-4bit (~19 GB) — text-only abl heretic Qwen3.6 MoE
2. mlx-community/Qwen3.6-27B-OptiQ-4bit (~14 GB) — text-only Qwen3.6 27B
3. lmstudio-community/gemma-4-E4B-it-MLX-4bit (~4 GB) — small popular Gemma 4 (340k DL)

Total ~37 GB on /Volumes/4TB SSD/
"""
from huggingface_hub import snapshot_download

CANDIDATES = [
    ("Youssofal/Qwen3.6-35B-A3B-Abliterated-Heretic-MLX-4bit",
     "Youssofal/Qwen3.6-35B-A3B-Abliterated-Heretic-MLX-4bit"),
    ("mlx-community/Qwen3.6-27B-OptiQ-4bit",
     "mlx-community/Qwen3.6-27B-OptiQ-4bit"),
    ("lmstudio-community/gemma-4-E4B-it-MLX-4bit",
     "lmstudio-community/gemma-4-E4B-it-MLX-4bit"),
]

SSD_BASE = "/Volumes/4TB SSD/LMStudio_models"
PATTERNS = ["*.safetensors", "*.json", "*.txt", "*.md", "tokenizer*", "*.model", "*.py", "*.jinja"]

for repo, dirname in CANDIDATES:
    target = f"{SSD_BASE}/{dirname}"
    print(f"\n=== {repo} → {target} ===", flush=True)
    try:
        path = snapshot_download(
            repo_id=repo,
            local_dir=target,
            allow_patterns=PATTERNS,
        )
        print(f"  ✅ {path}", flush=True)
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {str(e)[:300]}", flush=True)

print("\n=== ALL DONE ===")
