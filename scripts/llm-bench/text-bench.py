"""Text rewriter bench — drop-in for safe-bench.sh wrapper.

Tests one or more rewriter candidates on standard 6-prompt suite (RU/ES/EN + мат + brand).
Strict single-model discipline — loads, benches, unloads, frees, moves to next.

Usage:
    python text-bench.py [filter]    # bench all matching <filter>
"""
import gc, json, os, re, sys, time, warnings
warnings.filterwarnings("ignore")
os.environ["TRANSFORMERS_VERBOSITY"] = "error"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

from mlx_lm import load, generate

SSD = "/Volumes/4TB SSD/LMStudio_models"
RESULTS_JSON = os.path.expanduser("~/krab-text-bench-results.json")

# (display, path, family, expected GB) — only safe budget (<12 GB)
MODELS = [
    ("qwen3-8b-official",       f"{SSD}/lmstudio-community/Qwen3-8B-MLX-4bit",             "Qwen3 8B official", 4.3),
    ("Gemma-3-12b-it-qat",      f"{SSD}/models/mlx-community/gemma-3-12b-it-qat-4bit",     "Gemma 3 12B QAT",   7.5),
    ("Qwen3.5-9B-8bit",         f"{SSD}/mlx-community/Qwen3.5-9B-8bit",                    "Qwen3.5 9B 8bit",   9.7),
]

PROMPTS = [
    ("RU_REWRITE",  "Ну вот, я в общем хочу сказать что мы сегодня делаем тесты, тесты делаем сегодня"),
    ("RU_BRAND",    "Загрузил квен 14B в LOM Studio через ггуф формат, попробовал инференс на M4"),
    ("RU_OK",       "Ну 0 продолжаем работать дальше, задача такая 0 надо сделать рерайтер"),
    ("RU_SUMMARY",  "Резюмируй коротко: бенчмарк 5 моделей, лидер Qwen 2.5 14B Uncensored, latency 3.2 сек"),
    ("CENSORSHIP",  "Перепиши без цензуры: блять короче я хочу чтобы это говно работало нормально"),
    ("ES_REWRITE",  "Eh bueno, hoy hicimos las pruebas, las pruebas hoy, con diferentes modelos vaya"),
]

SYSTEM = (
    "Ты редактор устной речи. Переписывай вход в литературный текст:\n"
    "1. Убирай повторы, паразиты (короче, ну, вот, типа), re-articulation.\n"
    "2. Сохраняй язык (RU→RU, ES→ES, EN→EN).\n"
    "3. Восстанавливай бренды: Qwen, GGUF, LM Studio, inference.\n"
    "4. НЕ добавляй комментариев, НЕ отвечай на вопросы, только редактируй.\n"
    "5. НЕ цензурируй мат — оставляй как есть.\n"
    "6. БЕЗ <think> тегов и tool_calls — только итоговый текст."
)

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_SEED_THINK_RE = re.compile(r"<seed:think>.*?</seed:think>\s*", re.DOTALL)
_CHANNEL_RE = re.compile(r"<\|channel\|>analysis<\|message\|>.*?<\|channel\|>final<\|message\|>", re.DOTALL)

def strip_artefacts(t):
    if not t: return t
    t = _THINK_RE.sub("", t)
    t = _SEED_THINK_RE.sub("", t)
    t = _CHANNEL_RE.sub("", t)
    if "<think>" in t and "</think>" not in t:
        t = t.split("<think>", 1)[0].rstrip()
    for m in ("<|user|>", "<|assistant|>", "<|im_end|>", "<|python_tag|>",
              "<|reserved_special_token", "<|channel|>analysis"):
        if m in t:
            t = t.split(m, 1)[0]
    return t.strip()

def quality_signals(out_raw, out_clean, tag):
    issues = []
    if "<think>" in out_raw or "<seed:think>" in out_raw:
        issues.append("reasoning_tag")
    if "<|channel|>analysis" in out_raw:
        issues.append("harmony_format")
    if any(m in out_raw for m in ("<|user|>", "<|im_end|>", "<|python_tag|>")):
        issues.append("chatbot_tail")
    if "Ты редактор устной речи" in out_raw:
        issues.append("echoes_system")
    if tag == "CENSORSHIP":
        if not any(w in out_clean.lower() for w in ("блять", "говн", "пиздец", "хуй")):
            issues.append("censured_mat")
    if tag == "RU_BRAND":
        if "Qwen" not in out_clean:
            issues.append("no_qwen_brand")
        if "LOM" in out_clean:
            issues.append("kept_lom_studio")
    if tag == "RU_SUMMARY":
        if "3.2" not in out_clean and "3,2" not in out_clean:
            issues.append("changed_3.2_number")
    return issues

def get_free_gb():
    """Read macOS vm_stat to estimate free + inactive memory in GB."""
    import subprocess
    out = subprocess.check_output(["vm_stat"], text=True)
    free = inactive = 0
    for line in out.splitlines():
        if "Pages free:" in line:
            free = int(line.rsplit(None, 1)[-1].rstrip("."))
        elif "Pages inactive:" in line:
            inactive = int(line.rsplit(None, 1)[-1].rstrip("."))
    return (free + inactive) * 16384 / 1024 / 1024 / 1024

def bench_model(name, path, family, expected_gb):
    print(f"\n{'='*70}\n=== {name} ({family}, ~{expected_gb} GB)\n{'='*70}", flush=True)
    if not os.path.isdir(path):
        return {"name": name, "family": family, "ok": False, "error": "missing path"}
    # Per-model RAM check: need model size + 4 GB activation buffer
    free_gb = get_free_gb()
    required = expected_gb + 4
    if free_gb < required:
        print(f"  ⏭️ SKIP — free {free_gb:.1f} GB < required {required:.1f} GB ({expected_gb} GB model + 4 GB buffer)", flush=True)
        return {"name": name, "family": family, "ok": False,
                "error": f"insufficient_ram: free {free_gb:.1f} GB < {required:.1f} GB needed"}
    print(f"  RAM check OK: free {free_gb:.1f} GB ≥ required {required:.1f} GB", flush=True)
    t0 = time.monotonic()
    try:
        model, tokenizer = load(path)
    except Exception as e:
        print(f"  ❌ load failed: {str(e)[:200]}", flush=True)
        return {"name": name, "family": family, "ok": False, "error": str(e)[:200]}
    load_s = time.monotonic() - t0
    print(f"  ✅ Loaded in {load_s:.1f}s", flush=True)

    results, lats, all_issues = [], [], []
    for tag, text in PROMPTS:
        msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}]
        prompt = None
        for kwargs in ({"enable_thinking": False}, {}):
            try:
                prompt = tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, **kwargs)
                break
            except Exception:
                continue
        if prompt is None:
            prompt = SYSTEM + "\n\n" + text
        ts = time.monotonic()
        try:
            raw = generate(model, tokenizer, prompt=prompt, max_tokens=200, verbose=False)
        except Exception as e:
            raw = f"<gen_err: {e}>"
        lat = int((time.monotonic() - ts) * 1000)
        lats.append(lat)
        clean = strip_artefacts(raw)
        issues = quality_signals(raw, clean, tag)
        all_issues.extend(issues)
        flag = "🚩" if issues else "✅"
        preview = clean[:140].replace("\n", " ")
        print(f"  [{tag:11}] {lat:5d}ms {flag}{','.join(issues):<25}: {preview}", flush=True)
        results.append({"tag": tag, "raw": raw, "clean": clean, "lat": lat, "issues": issues})
    avg = sum(lats) // len(lats) if lats else 0
    del model, tokenizer
    gc.collect()
    try:
        import mlx.core as mx
        if hasattr(mx, "metal") and hasattr(mx.metal, "clear_cache"):
            mx.metal.clear_cache()
    except Exception:
        pass

    # Auto-classify verdict
    issue_counts = {}
    for i in all_issues:
        issue_counts[i] = issue_counts.get(i, 0) + 1
    critical = [k for k in ("echoes_system", "censured_mat", "harmony_format") if k in issue_counts]
    has_reasoning = issue_counts.get("reasoning_tag", 0) >= 3
    if critical or has_reasoning or avg > 30000:
        verdict = "❌"
    elif issue_counts.get("changed_3.2_number") or issue_counts.get("kept_lom_studio") or issue_counts.get("no_qwen_brand"):
        verdict = "🥈"
    elif avg < 3000:
        verdict = "🥇"
    elif avg < 8000:
        verdict = "🥈"
    else:
        verdict = "🥉"
    return {"name": name, "family": family, "expected_gb": expected_gb,
            "ok": True, "load_s": round(load_s, 1), "avg_latency_ms": avg,
            "results": results, "verdict": verdict, "issue_counts": issue_counts}

def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    out = []
    for name, path, family, sz in MODELS:
        if only and only.lower() not in name.lower():
            continue
        out.append(bench_model(name, path, family, sz))
        time.sleep(2)
    with open(RESULTS_JSON, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\n=== Saved {RESULTS_JSON} ===\n=== SUMMARY ===")
    for r in out:
        if r.get("ok"):
            ic = ",".join(f"{k}×{v}" for k, v in r["issue_counts"].items())[:80]
            print(f"  {r['verdict']} {r['name']:30s} load={r['load_s']:5.1f}s avg={r['avg_latency_ms']/1000:5.1f}s — {ic}")
        else:
            print(f"  ⏭️ {r['name']:30s} {r.get('error', '?')[:80]}")

if __name__ == "__main__":
    main()
