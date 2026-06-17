#!/usr/bin/env python3
"""draft_audit.py — fan out an adversarial security audit of ONE module to a FREE
workhorse model (cerebras/groq/mistral/gemini/openrouter/zai), keeping the breadth
pass OFF the Claude quota. Claude then GATES the findings.

Key is read from ~/.openclaw/krab_runtime_state/lens_keys.env (never printed).

Usage:
    python scripts/draft_audit.py --provider cerebras --model gpt-oss-120b \
        --focus "osascript/AppleScript command injection" KrabEar/backend/apple_integration_service.py
"""
import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ENVF = Path.home() / ".openclaw" / "krab_runtime_state" / "lens_keys.env"
# (endpoint, key-source). Key-source is an env-var name in lens_keys.env, EXCEPT
# the sentinel "__GH_CLI__" which fetches a fresh token from `gh auth token`
# (GitHub Models — free, generous, no extra key needed beyond the gh login).
PROVIDERS = {
    "cerebras": ("https://api.cerebras.ai/v1/chat/completions", "CEREBRAS_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1/chat/completions", "MISTRAL_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "GEMINI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY"),
    "zai": ("https://api.z.ai/api/paas/v4/chat/completions", "ZAI_API_KEY"),
    "github": ("https://models.github.ai/inference/chat/completions", "__GH_CLI__"),
    "nvidia": ("https://integrate.api.nvidia.com/v1/chat/completions", "NVIDIA_API_KEY"),
    "hf": ("https://router.huggingface.co/v1/chat/completions", "__HF_SETTINGS__"),
}

# Krab Ear GUI settings hold the HuggingFace token (`hf_token`, write-scoped, used
# for pyannote + reusable for the HF inference router — many models, free/credit tier).
_KE_SETTINGS = Path.home() / "Library" / "Application Support" / "KrabEar" / "settings.json"

# Sensible default models per provider (override with --model):
#   github=openai/gpt-4o-mini (strong+precise), cerebras=gpt-oss-120b, groq=llama-3.3-70b-versatile,
#   mistral=codestral-latest, nvidia=deepseek-ai/deepseek-v4-pro (strong reviewer — caught a bug mistral missed).
# NOTE: cerebras/groq 403 (content-filter) on security prompts -> route audits to github/mistral/nvidia.
# 🔴 nvidia LOGS input/output (trial ToS) -> code-review of PUBLIC code ONLY, never private/transcript data.


def load_key(var: str) -> str:
    if var == "__GH_CLI__":
        try:
            r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
            return r.stdout.strip()
        except Exception:
            return ""
    if var == "__HF_SETTINGS__":
        try:
            return json.loads(_KE_SETTINGS.read_text(encoding="utf-8")).get("hf_token", "") or ""
        except Exception:
            return ""
    if not ENVF.exists():
        return ""
    for line in ENVF.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:]
        if line.startswith(var + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="cerebras", choices=list(PROVIDERS))
    ap.add_argument("--model", default="gpt-oss-120b")
    ap.add_argument("--focus", default="security + correctness")
    ap.add_argument("--max-tokens", type=int, default=5000)
    ap.add_argument("module")
    a = ap.parse_args()

    url, kv = PROVIDERS[a.provider]
    key = load_key(kv)
    if not key:
        print(f"### {a.module} ERROR: no key {kv}", flush=True)
        return 2
    try:
        code = Path(a.module).read_text(encoding="utf-8")[:48000]
    except Exception as exc:
        print(f"### {a.module} ERROR: read {exc}", flush=True)
        return 1

    prompt = (
        "You are a ruthless, precise security auditor. Adversarially audit this Python module for "
        "REAL, production-reachable bugs only. FOCUS: " + a.focus + ". Hunt: command/AppleScript/shell "
        "injection, ReDoS / catastrophic regex backtracking, path traversal, SSRF, auth/CORS bypass, "
        "markdown/template injection, unbounded loops or memory, resource leaks, concurrency races, "
        "silent failures. Trace which inputs are attacker-controlled to judge reachability. "
        "For EACH confirmed finding output EXACTLY one line:\n"
        "FINDING|<CRITICAL|HIGH|MED|LOW>|<line or range>|<one-sentence issue + why reachable>|<one-line fix>\n"
        "List ONLY findings you are confident are REAL and reachable (no theoretical noise). "
        "If the module is clean, output the single line: CLEAN\n\n"
        f"Module path: {a.module}\n\n```python\n{code}\n```"
    )
    body = json.dumps({
        "model": a.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": a.max_tokens,
        "temperature": 0.2,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        msg = d.get("choices", [{}])[0].get("message", {})
        out = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        if not out:
            out = "EMPTY_RESPONSE: " + json.dumps(d)[:300]
        print(f"### {a.module}  [{a.provider}/{a.model}]\n{out}", flush=True)
    except Exception as exc:
        print(f"### {a.module} ERROR: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
