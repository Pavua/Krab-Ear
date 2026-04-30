"""Download 5 RU-native uncensored GGUF models."""
from huggingface_hub import hf_hub_download, list_repo_files
import os

SSD = "/Volumes/4TB SSD/LMStudio_models"

MODELS = [
    # Qwen3-8B abliterated (bartowski, mlabonne abliteration)
    ("bartowski/mlabonne_Qwen3-8B-abliterated-GGUF", "Q4_K_M"),
    # Qwen2.5-14B abliterated v2 (mradermacher imatrix)
    ("mradermacher/Qwen2.5-14B-Instruct-abliterated-v2-GGUF", "Q4_K_M"),
    # Saiga Nemo 12B (IlyaGusev — RU fine-tune on abliterated Mistral Nemo)
    ("IlyaGusev/saiga_nemo_12b_gguf", "Q4_K_M"),
    # Saiga Gemma3 12B (IlyaGusev — RU fine-tune)
    ("IlyaGusev/saiga_gemma3_12b_gguf", "Q4_K_M"),
    # Qwen3-14B abliterated
    ("richardyoung/Qwen3-14B-abliterated-GGUF", "Q4_K_M"),
]

for repo, quant in MODELS:
    target = f"{SSD}/{repo}"
    os.makedirs(target, exist_ok=True)
    print(f"\n=== {repo} ({quant}) ===", flush=True)
    try:
        files = list_repo_files(repo)
        gguf = [f for f in files if f.endswith(".gguf")]
        match = [f for f in gguf if quant.lower() in f.lower()]
        if match:
            fname = match[0]
        elif gguf:
            fname = sorted(gguf)[0]
            print(f"  ⚠️ No {quant}, using {fname}", flush=True)
        else:
            print(f"  ❌ No GGUF files in repo", flush=True)
            continue
        path = hf_hub_download(repo_id=repo, filename=fname, local_dir=target)
        print(f"  ✅ {path}", flush=True)
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {str(e)[:200]}", flush=True)

print("\n=== ALL DONE ===")
