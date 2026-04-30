"""VL bench — Vision-Language model evaluation for Krab Ear / main Krab Telegram.

Tests a model on RU + EN screenshots, measures latency, prints output preview.

Usage:
    source ~/.venv_vl/bin/activate
    python scripts/llm-bench/vl-bench.py            # all models
    python scripts/llm-bench/vl-bench.py Pixtral    # only Pixtral
"""
import os, sys, time, json, gc, warnings
warnings.filterwarnings("ignore")
os.environ["TRANSFORMERS_VERBOSITY"] = "error"

from mlx_vlm import load, generate
from mlx_vlm.prompt_utils import apply_chat_template
from mlx_vlm.utils import load_config

SSD = "/Volumes/4TB SSD/LMStudio_models"
RESULTS_JSON = os.path.expanduser("~/krab-vl-bench-results.json")

MODELS = [
    ("Qwen2-VL-2B-Instruct-abl-8bit",   f"{SSD}/models/EZCon/Qwen2-VL-2B-Instruct-abliterated-8bit-mlx",   2.5),
    ("Qwen3.5-9B-VL-mxfp4",              f"{SSD}/RepublicOfKorokke/Qwen3.5-9B-mlx-vlm-mxfp4",              5.3),
    ("Pixtral-12B-4bit",                 f"{SSD}/mlx-community/Pixtral-12B-4bit",                          6.7),
    ("Qwen2-VL-7B-Instruct-abl",         f"{SSD}/models/mlx-community/Qwen2-VL-7B-Instruct-abliterated",  15.5),
    ("Qwen3.5-35B-A3B-VL-mxfp4",         f"{SSD}/RepublicOfKorokke/Qwen3.5-35B-A3B-mlx-vlm-mxfp4",        18.0),
    ("Llama-3.2-11B-Vision-abl",         f"{SSD}/mlx-community/Llama-3.2-11B-Vision-Instruct-abliterated",19.9),
    ("Qwen3.6-27B-UD-MLX-4bit",          f"{SSD}/unsloth/Qwen3.6-27B-UD-MLX-4bit",                        25.0),
]

TEST_IMAGES = [
    ("/Users/pablito/Desktop/Screenshot 2026-03-17 at 19.50.52.png",
     "Опиши что на этом скриншоте. Если есть русский текст — точно процитируй ключевые надписи."),
    ("/Users/pablito/Desktop/Screenshot 2026-03-10 at 22.46.02.png",
     "What's on this screenshot? Be brief."),
]

def extract_text(out):
    if isinstance(out, str):
        return out
    if hasattr(out, "text"):
        return out.text
    if hasattr(out, "generated_text"):
        return out.generated_text
    return str(out)

def bench_model(name, path, expected_gb):
    print(f"\n{'='*70}\n=== {name} (~{expected_gb} GB)\n{'='*70}", flush=True)
    if not os.path.isdir(path):
        return {"name": name, "ok": False, "error": "missing path"}
    t_load = time.monotonic()
    try:
        model, processor = load(path)
        config = load_config(path)
    except Exception as e:
        print(f"  ❌ load failed: {str(e)[:200]}", flush=True)
        return {"name": name, "ok": False, "error": f"load: {str(e)[:200]}"}
    load_s = time.monotonic() - t_load
    print(f"  ✅ Loaded in {load_s:.1f}s", flush=True)

    results, latencies = [], []
    for img_path, prompt_text in TEST_IMAGES:
        if not os.path.isfile(img_path):
            continue
        try:
            formatted = apply_chat_template(processor, config, prompt_text, num_images=1)
        except Exception:
            formatted = prompt_text
        t0 = time.monotonic()
        try:
            raw = generate(model, processor, formatted, image=img_path, max_tokens=200, verbose=False)
            out = extract_text(raw)
        except Exception as e:
            out = f"<gen error: {type(e).__name__}: {str(e)[:100]}>"
        latency = int((time.monotonic() - t0) * 1000)
        latencies.append(latency)
        preview = (out or "").strip()[:200].replace("\n", " ")
        tag = "RU" if "русский" in prompt_text.lower() else "EN"
        print(f"  [{tag}] {latency:5d}ms: {preview}", flush=True)
        results.append({"image": os.path.basename(img_path), "prompt": prompt_text,
                        "output": out, "latency_ms": latency})
    avg = sum(latencies) // len(latencies) if latencies else 0
    del model, processor
    gc.collect()
    try:
        import mlx.core as mx
        if hasattr(mx, "metal") and hasattr(mx.metal, "clear_cache"):
            mx.metal.clear_cache()
    except Exception:
        pass
    return {"name": name, "expected_gb": expected_gb, "ok": True,
            "load_s": round(load_s, 1), "avg_latency_ms": avg, "results": results}

def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    out = []
    for name, path, sz in MODELS:
        if only and only.lower() not in name.lower():
            continue
        out.append(bench_model(name, path, sz))
        time.sleep(3)
    with open(RESULTS_JSON, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\n=== Saved {RESULTS_JSON} ===\n=== SUMMARY ===")
    for r in out:
        if r.get("ok"):
            print(f"  {r['name']:42s} load={r['load_s']:5.1f}s avg={r['avg_latency_ms']/1000:5.1f}s ({r['expected_gb']} GB)")
        else:
            print(f"  ⏭️ {r['name']:42s} {r.get('error','?')[:80]}")

if __name__ == "__main__":
    main()
