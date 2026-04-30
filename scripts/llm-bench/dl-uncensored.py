"""Download 5 most uncensored MLX models for zero-guardrails testing."""
from huggingface_hub import snapshot_download

SSD = "/Volumes/4TB SSD/LMStudio_models"
PATTERNS = ["*.safetensors", "*.json", "*.txt", "*.md", "tokenizer*", "*.model", "*.py", "*.jinja"]

CANDIDATES = [
    # 1. Dolphin classic — Eric Hartford, zero guardrails, follows literally
    ("mlx-community/dolphin-2.9-llama3-8b-4bit",
     f"{SSD}/mlx-community/dolphin-2.9-llama3-8b-4bit"),
    # 2. GLM 4.7 Flash abliterated MLX — Huihui's abliteration of ZhipuAI GLM 4.7
    ("huihui-ai/Huihui-GLM-4.7-Flash-abliterated-mlx-4bit",
     f"{SSD}/huihui-ai/Huihui-GLM-4.7-Flash-abliterated-mlx-4bit"),
    # 3. MiniMax M2.5 Uncensored MLX — large uncensored (456B MoE)
    ("mlx-community/MiniMax-M2.5-Uncensored-4bit",
     f"{SSD}/mlx-community/MiniMax-M2.5-Uncensored-4bit"),
    # 4. Gemma 4 E4B heretic nvfp4 — Gemma 4 heretic (different quant from our it-mlx)
    ("vanch007/gemma-4-E4B-it-heretic-mlx-nvfp4",
     f"{SSD}/vanch007/gemma-4-E4B-it-heretic-mlx-nvfp4"),
    # 5. SuperGemma4 v2 fixed (Jiunsong) — if exists
    ("Jiunsong/supergemma4-26b-uncensored-mlx-4bit-v2",
     f"{SSD}/Jiunsong/supergemma4-26b-uncensored-mlx-4bit-v2"),
]

for repo, target in CANDIDATES:
    print(f"\n=== {repo} ===", flush=True)
    try:
        path = snapshot_download(repo_id=repo, local_dir=target, allow_patterns=PATTERNS)
        print(f"  ✅ {path}", flush=True)
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {str(e)[:300]}", flush=True)

print("\n=== ALL DONE ===")
