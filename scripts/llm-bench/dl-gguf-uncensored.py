"""Download top GGUF uncensored models for llama.cpp runtime in LM Studio."""
from huggingface_hub import hf_hub_download
import os

SSD = "/Volumes/4TB SSD/LMStudio_models"

MODELS = [
    # DAN = "Do Anything Now" — zero guardrails jailbreak persona
    ("UnfilteredAI/DAN-L3-R1-8B-GGUF", "DAN-L3-R1-8B.Q4_K_M.gguf",
     f"{SSD}/UnfilteredAI/DAN-L3-R1-8B-GGUF"),
    # DarkIdol — dark creative writing, no moral compass
    ("bartowski/DarkIdol-Llama-3.1-8B-Instruct-1.2-Uncensored-GGUF",
     "DarkIdol-Llama-3.1-8B-Instruct-1.2-Uncensored-Q4_K_M.gguf",
     f"{SSD}/bartowski/DarkIdol-Llama-3.1-8B-Instruct-1.2-Uncensored-GGUF"),
    # Unholy v2 — 13B uncensored (classic)
    ("TheBloke/Unholy-v2-13B-GGUF", "unholy-v2-13b.Q4_K_M.gguf",
     f"{SSD}/TheBloke/Unholy-v2-13B-GGUF"),
]

for repo, filename, target_dir in MODELS:
    print(f"\n=== {repo} / {filename} ===", flush=True)
    os.makedirs(target_dir, exist_ok=True)
    try:
        path = hf_hub_download(
            repo_id=repo, filename=filename,
            local_dir=target_dir,
        )
        print(f"  ✅ {path}", flush=True)
    except Exception as e:
        # Try without specific filename (list files first)
        print(f"  ⚠️ {filename} not found, trying alternative quants...", flush=True)
        try:
            from huggingface_hub import list_repo_files
            files = [f for f in list_repo_files(repo) if f.endswith('.gguf')]
            q4 = [f for f in files if 'Q4_K_M' in f or 'q4_k_m' in f]
            if q4:
                path = hf_hub_download(repo_id=repo, filename=q4[0], local_dir=target_dir)
                print(f"  ✅ {path}", flush=True)
            elif files:
                smallest = sorted(files, key=lambda f: len(f))[:1]
                path = hf_hub_download(repo_id=repo, filename=smallest[0], local_dir=target_dir)
                print(f"  ✅ {path} (smallest available)", flush=True)
            else:
                print(f"  ❌ No GGUF files found in {repo}", flush=True)
        except Exception as e2:
            print(f"  ❌ {type(e2).__name__}: {str(e2)[:200]}", flush=True)

print("\n=== ALL DONE ===")
