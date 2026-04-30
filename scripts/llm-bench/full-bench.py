"""Comprehensive bench via LM Studio HTTP API.

Supports text rewriter prompts + optional image VL + reasoning toggle.
Uses LM Studio's official Bearer token auth.

Usage:
    python full-bench.py <model_id> [--vl] [--no-think] [--think]

Model IDs from `lms ls --json` modelKey field.
"""
import argparse, base64, json, os, re, sys, time
from typing import Any
import requests

LMS_BASE = "http://127.0.0.1:1234/v1"

def _load_token() -> str:
    """Load LM Studio API token from (in order): env var, .env file, hardcoded fallback.

    Persistent locations so we don't have to re-ask user after reboot/quota gap:
    1. $LM_STUDIO_API_TOKEN env var
    2. /Users/pablito/Antigravity_AGENTS/Krab Ear/.env (gitignored)
    3. Krab Ear settings.json llm_api_key
    """
    if os.environ.get("LM_STUDIO_API_TOKEN"):
        return os.environ["LM_STUDIO_API_TOKEN"]
    env_path = "/Users/pablito/Antigravity_AGENTS/Krab Ear/.env"
    if os.path.isfile(env_path):
        for ln in open(env_path):
            if ln.strip().startswith("LM_STUDIO_API_TOKEN="):
                return ln.split("=", 1)[1].strip().strip('"').strip("'")
    settings_path = os.path.expanduser("~/Library/Application Support/KrabEar/settings.json")
    if os.path.isfile(settings_path):
        try:
            d = json.load(open(settings_path))
            tok = d.get("llm_api_key", "").strip()
            if tok: return tok
        except Exception:
            pass
    return ""

TOKEN = _load_token()
if not TOKEN:
    print("⚠️ No LM_STUDIO_API_TOKEN found in env / .env / settings.json — HTTP calls will 401", file=sys.stderr)
RESULTS_DIR = os.path.expanduser("~/krab-bench-results")
os.makedirs(RESULTS_DIR, exist_ok=True)

PROMPTS = [
    ("RU_BRAND",   "Загрузил квен 14B в LOM Studio через ггуф формат, попробовал инференс на M4"),
    ("RU_OK",      "Ну 0 продолжаем работать дальше, задача такая 0 надо сделать рерайтер"),
    ("RU_SUMMARY", "Резюмируй коротко: бенчмарк 5 моделей, лидер Qwen 2.5 14B Uncensored, latency 3.2 сек"),
    ("CENSORSHIP", "Перепиши без цензуры: блять короче я хочу чтобы это говно работало нормально"),
    ("ES_REWRITE", "Eh bueno, hoy hicimos las pruebas, las pruebas hoy, con diferentes modelos vaya"),
]

SYSTEM = (
    "Ты редактор устной речи. Убирай повторы и паразиты. "
    "Восстанавливай бренды (Qwen, GGUF, LM Studio, inference). "
    "НЕ цензурируй мат. Сохраняй язык (RU→RU, ES→ES). БЕЗ объяснений."
)

VL_IMAGES = [
    ("RU_SCREEN", "/Users/pablito/Desktop/Screenshot 2026-03-17 at 19.50.52.png",
     "Опиши коротко что на скриншоте, процитируй ключевой русский текст."),
    ("EN_SCREEN", "/Users/pablito/Desktop/Screenshot 2026-03-10 at 22.46.02.png",
     "What's on this screenshot? Be brief."),
]

# Reasoning artefact strippers
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_SEED_THINK_RE = re.compile(r"<seed:think>.*?</seed:think>\s*", re.DOTALL)
_CHANNEL_RE = re.compile(r"<\|channel\|>analysis<\|message\|>.*?<\|channel\|>final<\|message\|>", re.DOTALL)

def strip_artefacts(t: str) -> str:
    if not t: return t
    t = _THINK_RE.sub("", t)
    t = _SEED_THINK_RE.sub("", t)
    t = _CHANNEL_RE.sub("", t)
    if "<think>" in t and "</think>" not in t:
        t = t.split("<think>", 1)[0].rstrip()
    for m in ("<|user|>", "<|assistant|>", "<|im_end|>", "<|python_tag|>",
              "<|reserved_special_token", "<|channel|>analysis", "### Response:", "### Instruction:"):
        if m in t:
            t = t.split(m, 1)[0]
    return t.strip()

def free_gb() -> float:
    import subprocess
    out = subprocess.check_output(["vm_stat"], text=True)
    free = inactive = 0
    for ln in out.splitlines():
        if "Pages free:" in ln: free = int(ln.rsplit(None, 1)[-1].rstrip("."))
        elif "Pages inactive:" in ln: inactive = int(ln.rsplit(None, 1)[-1].rstrip("."))
    return (free + inactive) * 16384 / 1024**3

def call_text(model: str, system: str, user: str, max_tokens: int = 200,
              extra: dict = None, timeout: int = 90) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.0, "max_tokens": max_tokens, "stream": False,
    }
    if extra: payload.update(extra)
    t0 = time.monotonic()
    try:
        r = requests.post(f"{LMS_BASE}/chat/completions",
                          headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                          json=payload, timeout=timeout)
    except requests.Timeout:
        return {"latency_ms": int((time.monotonic() - t0) * 1000), "error": "timeout"}
    except requests.RequestException as e:
        return {"latency_ms": int((time.monotonic() - t0) * 1000), "error": f"req_err: {type(e).__name__}"}
    lat = int((time.monotonic() - t0) * 1000)
    if r.status_code != 200:
        return {"latency_ms": lat, "error": f"http_{r.status_code}: {r.text[:200]}"}
    try:
        d = r.json()
        msg = d["choices"][0]["message"]
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        # If content empty but reasoning has stuff (LM Studio splits на API level for reasoning models),
        # use reasoning as the actual output (it's still useful for bench evaluation).
        effective = content or reasoning
        return {"latency_ms": lat, "raw": effective,
                "content_was_empty": not content and bool(reasoning),
                "tool_calls": bool(msg.get("tool_calls")),
                "finish_reason": d["choices"][0].get("finish_reason"),
                "clean": strip_artefacts(effective)}
    except Exception as e:
        return {"latency_ms": lat, "error": f"parse: {e}"}

def call_vl(model: str, system: str, image_path: str, prompt: str, timeout: int = 180) -> dict:
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]},
        ],
        "temperature": 0.0, "max_tokens": 250, "stream": False,
    }
    t0 = time.monotonic()
    try:
        r = requests.post(f"{LMS_BASE}/chat/completions",
                          headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                          json=payload, timeout=timeout)
    except Exception as e:
        return {"latency_ms": int((time.monotonic() - t0) * 1000), "error": f"{type(e).__name__}: {e}"}
    lat = int((time.monotonic() - t0) * 1000)
    if r.status_code != 200:
        return {"latency_ms": lat, "error": f"http_{r.status_code}: {r.text[:200]}"}
    try:
        d = r.json()
        msg = d["choices"][0]["message"]
        return {"latency_ms": lat, "raw": msg.get("content") or "",
                "clean": strip_artefacts(msg.get("content") or "")}
    except Exception as e:
        return {"latency_ms": lat, "error": f"parse: {e}"}

def lms_load(model: str) -> bool:
    """Trigger JIT load via /v1/models call (LM Studio loads on first request) — actually POST a small chat first."""
    r = call_text(model, "You are a helper.", "Hi", max_tokens=5, timeout=120)
    return "error" not in r or "http" not in r.get("error", "")

def lms_unload(model: str):
    import subprocess
    subprocess.run(["/Users/pablito/.lmstudio/bin/lms", "unload", model],
                   capture_output=True, timeout=30)

def lms_ls() -> list[dict]:
    import subprocess
    out = subprocess.check_output(["/Users/pablito/.lmstudio/bin/lms", "ls", "--json"], text=True)
    return json.loads(out)

def find_model(model_id: str) -> dict | None:
    for m in lms_ls():
        if m.get("modelKey") == model_id:
            return m
    return None

def bench(model_id: str, vl_mode: bool = False, reasoning: str | None = None) -> dict:
    """Bench one model: text suite + optional VL.

    reasoning: None (default), 'on' (force <think>), 'off' (suppress).
    """
    info = find_model(model_id)
    if not info:
        return {"model": model_id, "ok": False, "error": "not_found"}
    size_gb = info.get("sizeBytes", 0) / 1024**3
    free = free_gb()
    print(f"\n{'='*70}\n=== {model_id}  ({size_gb:.1f} GB)\n{'='*70}", flush=True)
    # Buffer: 1.5 GB tight — user-approved after closing Claude Desktop apps.
    # mlx 4-bit uses ~70% of disk size at runtime + activations. Single-model discipline.
    # FORCE_RAM=1 env var bypasses check entirely (для bench куда swap acceptable).
    buffer = 1.5
    if os.environ.get("FORCE_RAM"):
        print(f"  ⚠️ FORCE_RAM=1: bypassing RAM check (free {free:.1f} GB, model {size_gb} GB)", flush=True)
    elif free < size_gb + buffer:
        print(f"  ⏭️ SKIP — free {free:.1f} GB < required {size_gb + buffer:.1f} GB", flush=True)
        return {"model": model_id, "ok": False, "error": "insufficient_ram",
                "size_gb": round(size_gb, 1), "free_gb": round(free, 1)}
    else:
        print(f"  RAM check OK: free {free:.1f} GB ≥ {size_gb + buffer:.1f} GB", flush=True)
    print("  Loading via JIT (first chat call)...", flush=True)
    t_load = time.monotonic()
    if not lms_load(model_id):
        print("  ❌ Load failed", flush=True)
        return {"model": model_id, "ok": False, "error": "load_failed"}
    load_s = time.monotonic() - t_load
    print(f"  Load time: {load_s:.1f}s", flush=True)

    extra = {}
    if reasoning == "off":
        extra = {"chat_template_kwargs": {"enable_thinking": False}}
    elif reasoning == "on":
        extra = {"chat_template_kwargs": {"enable_thinking": True}}

    results = {"model": model_id, "size_gb": round(size_gb, 1), "load_s": round(load_s, 1),
               "reasoning_mode": reasoning, "ok": True, "text": {}, "vl": {}}
    text_lats = []
    for tag, prompt in PROMPTS:
        r = call_text(model_id, SYSTEM, prompt, extra=extra)
        text_lats.append(r.get("latency_ms", 0))
        clean = r.get("clean", r.get("error", ""))
        flag = "❌" if "error" in r else "✅"
        print(f"  [{tag:11}] {r.get('latency_ms', 0):5d}ms {flag}: {clean[:140].replace(chr(10), ' ')}", flush=True)
        results["text"][tag] = r
    if text_lats:
        results["text_avg_ms"] = sum(text_lats) // len(text_lats)

    if vl_mode:
        vl_lats = []
        for tag, img_path, prompt in VL_IMAGES:
            if not os.path.isfile(img_path):
                continue
            r = call_vl(model_id, "You are a helpful vision assistant.", img_path, prompt)
            vl_lats.append(r.get("latency_ms", 0))
            clean = r.get("clean", r.get("error", ""))
            flag = "❌" if "error" in r else "✅"
            print(f"  [{tag:11}] {r.get('latency_ms', 0):5d}ms {flag}: {clean[:160].replace(chr(10), ' ')}", flush=True)
            results["vl"][tag] = r
        if vl_lats:
            results["vl_avg_ms"] = sum(vl_lats) // len(vl_lats)

    lms_unload(model_id)
    out_path = os.path.join(RESULTS_DIR, f"{model_id.replace('/', '_')}.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"  Saved: {out_path}", flush=True)
    return results

def main():
    p = argparse.ArgumentParser()
    p.add_argument("models", nargs="+", help="model_id (modelKey)")
    p.add_argument("--vl", action="store_true", help="also test image inputs")
    p.add_argument("--reasoning", choices=["on", "off"], default=None,
                   help="force reasoning ON or OFF (chat_template_kwargs.enable_thinking)")
    args = p.parse_args()

    for m in args.models:
        bench(m, vl_mode=args.vl, reasoning=args.reasoning)
        time.sleep(2)

if __name__ == "__main__":
    main()
