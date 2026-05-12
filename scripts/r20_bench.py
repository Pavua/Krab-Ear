#!/usr/bin/env python3
"""R20 LLM Rewriter Benchmark — Krab Ear
Тестирует 3 новые модели vs baseline на 3 русских транскриптах.
FIXES from R19:
  - NO response_format parameter (caused http_400 in R19)
  - timeout=600s (cold-load safe)
  - sleep 90s between models (TTL eviction)
  - urllib.request instead of requests (no extra dep)
Output: docs/llm-bench-results-R20.md
"""

from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LM_STUDIO_URL = "http://localhost:1234/v1"
LM_STUDIO_TOKEN = "sk-lm-lkyUVqAw:ggACZoBqiaBpfwqPEvlK"
TIMEOUT_SEC = 600          # generous — 31B-bf16 cold-load can take >2 min
WARMUP_RUNS = 1
TIMED_RUNS = 2             # 2 timed runs (save time vs R19's 3)
INTER_MODEL_SLEEP_SEC = 90 # TTL eviction of previous model

# R19 baseline for comparison
BASELINE_P50_MS = 1587
BASELINE_QUALITY = 1.00
BASELINE_MODEL = "gemma-4-26b-a4b-it-optiq"

MODELS = [
    # 3 new models from user's HF downloads
    "supergemma4-26b-abliterated-multimodal-mlx",    # multimodal variant
    "mlx-community/gemma-4-31b-it-assistant",         # 31B bf16 (much larger)
    "gemma-4-e2b-it-ultra-uncensored-heretic-mlx-int8-affine",  # E2B heretic 8bit
]

# ---------------------------------------------------------------------------
# Rewriter system prompt (exact copy from llm_rewriter.py)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """Ты — редактор диктовки. Твоя задача — исправить пунктуацию, орфографию и грамматику, сохранив смысл и стиль автора.

Жёсткие правила:
1. НЕ добавляй слов, которых нет в оригинале.
2. НЕ удаляй слов, кроме (а) явных filler'ов в начале ("э-э", "ну", "вот") и (б) немедленных повторов от re-articulation: если человек переспрашивает слово сразу же ("записываю уже, уже" → "записываю уже"; "слово, слово" → "слово"; "вот сейчас, вот сейчас" → "вот сейчас"), оставляй ОДНО вхождение. Не путай с риторическими повторами и emphasis ("очень очень важно" → оставь как есть).
3. НЕ меняй порядок слов, кроме случаев когда этого требует грамматика.
4. НЕ переформулируй фразы — только исправляй ошибки.
5. СОХРАНЯЙ язык ввода — НЕ переводи между языками. Если вход на испанском — выход на испанском. Если на английском — на английском.
6. Бренды и технические термины оставляй латиницей: Spotify, YouTube, GitHub, Claude, OpenAI, Docker, Python, Swift, macOS, iPhone, iPad, Mac, Telegram, WhatsApp, Slack, Notion, Figma, VS Code, Xcode, Linux, Linear, Jira, Qwen, MLX, GigaAM, Krab Ear.
7. Расставь правильные знаки препинания: запятые, точки, тире, двоеточия.
8. Заглавные буквы в начале предложений и у имён собственных.
9. Если текст пустой или бессмысленный — верни его без изменений.
10. Не используй <think> теги или reasoning chains.

Верни ТОЛЬКО исправленный текст. Без пояснений. Без кавычек. Без префиксов типа "Исправленный текст:"."""

# ---------------------------------------------------------------------------
# Test prompts — 3 shorter prompts (save time)
# ---------------------------------------------------------------------------
PROMPTS = [
    {
        "name": "meeting_short",
        "label": "Meeting transcript ~80 words",
        "text": (
            "ну значит сегодня встреча по проекту краб ир я хочу обсудить три вещи "
            "первое это интеграция с питоном второе развертывание на маки и третье "
            "у нас есть проблема с е е авторизациeй через опенаи ключ не работает "
            "стас ты можешь посмотреть это до конца недели да я думаю думаю это "
            "займет часа два максимум три хорошо следующая встреча в пятницу в два"
        ),
    },
    {
        "name": "tech_short",
        "label": "Technical discussion ~70 words",
        "text": (
            "значит смотри мы используем млх вискер для транскрипции но есть проблема "
            "когда два потока обращаются к видеокарте одновременно мы получаем сигсегв "
            "потому что Эм Эл Икс не потокобезопасен нужен глобальный лок "
            "я добавил мтекс в кор слеш млх_лок.пай и оборачиваю все вызовы "
            "в контекст менеджер это должно решить проблему"
        ),
    },
    {
        "name": "dictation_note",
        "label": "Dictation note ~60 words",
        "text": (
            "заметка на завтра нужно купить кофе молоко и хлеб потом позвонить "
            "врачу записаться на приём на следующей неделе ещо ещо надо проверить "
            "договор с арендодателем там есть пункт про ё и е путаница "
            "в правописании фамилии петрёв или петрев уточнить у него лично "
            "и да не забыть оплатить интернет до пятницы иначе отключат"
        ),
    },
]

# ---------------------------------------------------------------------------
# Chatbot markers (same as llm_rewriter.py)
# ---------------------------------------------------------------------------
CHATBOT_MARKERS = (
    "извините", "пожалуйста, укажите", "пожалуйста, предоставьте",
    "как я могу", "чем могу помочь", "к сожалению", "я не могу",
    "i'm sorry", "i apologize", "here is", "sure,",
    "конечно,", "вот исправленный",
)


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only, no requests)
# ---------------------------------------------------------------------------

def _http_get(path: str) -> dict:
    req = urllib.request.Request(
        f"{LM_STUDIO_URL}{path}",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LM_STUDIO_TOKEN}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _http_post(path: str, payload: dict) -> tuple[int, dict | None]:
    """Returns (status_code, body_dict_or_None). Never raises on HTTP errors."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{LM_STUDIO_URL}{path}",
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LM_STUDIO_TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = None
        try:
            body = json.loads(e.read().decode())
        except Exception:
            pass
        return e.code, body
    except urllib.error.URLError as e:
        return 0, {"error": str(e)}
    except TimeoutError:
        return -1, {"error": "timeout"}


def verify_connection() -> bool:
    """Check LM Studio is reachable."""
    try:
        data = _http_get("/models")
        count = len(data.get("data", []))
        print(f"[OK] LM Studio reachable — {count} models available")
        return True
    except Exception as e:
        print(f"[ERROR] LM Studio unreachable: {e}")
        return False


# ---------------------------------------------------------------------------
# Model inference
# ---------------------------------------------------------------------------

def call_model(model: str, text: str) -> tuple[Optional[str], int, str]:
    """Returns (output_or_None, latency_ms, reason). Never raises."""
    word_count = len(text.split())
    max_tokens = max(256, min(int(word_count * 3 * 1.3) + 50, 4096))

    # CRITICAL: NO response_format — was causing http_400 in R19
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "stream": False,
        "stop": [
            "Исправленный текст:", "Исходный текст:",
            "<end_of_turn>", "<start_of_turn>", "</s>",
        ],
        "tool_choice": "none",
    }

    start = time.monotonic()
    status, body = _http_post("/chat/completions", payload)
    latency_ms = int((time.monotonic() - start) * 1000)

    if status == 0:
        reason = f"connection_error:{body.get('error', 'unknown')}"
        return None, latency_ms, reason
    if status == -1:
        return None, latency_ms, "timeout"
    if status == 401:
        print("FATAL: LM Studio returned 401. Token revoked?", file=sys.stderr)
        sys.exit(1)
    if status != 200:
        return None, latency_ms, f"http_{status}"
    if body is None:
        return None, latency_ms, "no_body"

    try:
        content = body["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError) as exc:
        return None, latency_ms, f"parse_error:{exc}"

    # Strip quotes and explanatory prefixes
    s = content.strip()
    if len(s) >= 2 and s[0] in ('"', "«", "“") and s[-1] in ('"', "»", "”"):
        s = s[1:-1].strip()
    for prefix in ("Исправленный текст:", "Исправлено:", "Результат:", "Вот:"):
        if s.lower().startswith(prefix.lower()):
            s = s[len(prefix):].strip()
            break
    if "\n\n" in s:
        s = s.split("\n\n", 1)[0].strip()

    if not s:
        return None, latency_ms, "empty_response"

    return s, latency_ms, "ok"


# ---------------------------------------------------------------------------
# Quality heuristics
# ---------------------------------------------------------------------------

def score_quality(input_text: str, output: Optional[str], reason: str) -> tuple[float, list[str]]:
    if output is None:
        return 0.0, [f"failed:{reason}"]

    issues = []
    score = 1.0

    in_len = len(input_text)
    out_len = len(output)
    ratio = out_len / in_len if in_len > 0 else 0.0
    if ratio < 0.5:
        score -= 0.4
        issues.append(f"too_short({ratio:.2f})")
    elif ratio < 0.7:
        score -= 0.15
        issues.append(f"short({ratio:.2f})")
    elif ratio > 2.0:
        score -= 0.3
        issues.append(f"too_long({ratio:.2f})")
    elif ratio > 1.3:
        score -= 0.1
        issues.append(f"long({ratio:.2f})")

    lower = output.lower()
    for marker in CHATBOT_MARKERS:
        if lower.startswith(marker):
            score -= 0.5
            issues.append(f"chatbot_marker:{marker[:20]}")
            break

    if "<think>" in output or "</think>" in output:
        score -= 0.3
        issues.append("think_tag_leak")

    cyrillic_in_output = sum(1 for c in output if "Ѐ" <= c <= "ӿ")
    if cyrillic_in_output < 5:
        score -= 0.4
        issues.append("no_cyrillic")

    return max(0.0, min(1.0, score)), issues


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def benchmark_model(model: str, model_index: int) -> list[dict]:
    print(f"\n{'='*60}")
    print(f"  MODEL [{model_index+1}/{len(MODELS)}]: {model}")
    print(f"{'='*60}")
    results = []

    for prompt in PROMPTS:
        print(f"\n  Prompt: {prompt['name']} ({prompt['label']})")

        # Warm-up run (not timed) — also handles cold-load JIT
        print(f"    [warm-up] ...", end="", flush=True)
        wu_out, wu_lat, wu_reason = call_model(model, prompt["text"])
        print(f" {wu_lat}ms ({wu_reason})")

        # If cold-load detected (>15s warm-up), sleep extra to settle
        if wu_lat > 15000:
            print(f"    [cold-load detected {wu_lat}ms] sleeping 30s to settle...")
            time.sleep(30)

        # Timed runs
        latencies = []
        outputs = []
        reasons = []
        for run_i in range(TIMED_RUNS):
            print(f"    [run {run_i+1}/{TIMED_RUNS}] ...", end="", flush=True)
            out, lat, reason = call_model(model, prompt["text"])
            latencies.append(lat)
            outputs.append(out)
            reasons.append(reason)
            print(f" {lat}ms ({reason})")
            if reason == "connection_error:Connection refused":
                print("    [WARN] LM Studio connection refused — sleeping 60s then retry")
                time.sleep(60)
                out, lat, reason = call_model(model, prompt["text"])
                latencies[-1] = lat
                outputs[-1] = out
                reasons[-1] = reason
                print(f"    [retry] {lat}ms ({reason})")

        final_output = next((o for o in reversed(outputs) if o is not None), None)
        final_reason = reasons[-1]

        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)] if len(latencies) > 1 else latencies[0]
        quality, issues = score_quality(prompt["text"], final_output, final_reason)
        ok_count = sum(1 for r in reasons if r == "ok")

        print(f"    => p50={int(p50)}ms p95={int(p95)}ms quality={quality:.2f} ok={ok_count}/{TIMED_RUNS}")
        if issues:
            print(f"    issues: {', '.join(issues)}")
        if final_output:
            preview = final_output[:120].replace("\n", " ")
            print(f"    output: {preview}...")

        results.append({
            "model": model,
            "prompt": prompt["name"],
            "prompt_label": prompt["label"],
            "latencies_ms": latencies,
            "p50_ms": p50,
            "p95_ms": p95,
            "quality_score": quality,
            "issues": issues,
            "ok_count": ok_count,
            "total_runs": TIMED_RUNS,
            "final_output": final_output,
            "final_reason": final_reason,
        })

    return results


# ---------------------------------------------------------------------------
# Aggregate stats per model
# ---------------------------------------------------------------------------

def aggregate(results: list[dict]) -> dict:
    by_model: dict[str, list] = {}
    for r in results:
        by_model.setdefault(r["model"], []).append(r)

    summary = {}
    for model, rows in by_model.items():
        all_lats = [lat for row in rows for lat in row["latencies_ms"]]
        all_q = [row["quality_score"] for row in rows]
        total_ok = sum(row["ok_count"] for row in rows)
        total_runs = sum(row["total_runs"] for row in rows)

        summary[model] = {
            "latency_p50_ms": int(statistics.median(all_lats)),
            "latency_p95_ms": int(sorted(all_lats)[int(len(all_lats) * 0.95)]),
            "quality_avg": sum(all_q) / len(all_q),
            "ok_ratio": total_ok / total_runs if total_runs > 0 else 0.0,
            "rows": rows,
        }
    return summary


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

def build_markdown(summary: dict) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        "# LLM Rewriter Benchmark R20 — Krab Ear",
        "",
        f"**Date:** {ts}  ",
        f"**Hardware:** MacBook Pro M4 Max, 36 GB RAM  ",
        f"**LM Studio:** {LM_STUDIO_URL}  ",
        f"**Runs per prompt:** {WARMUP_RUNS} warmup + {TIMED_RUNS} timed × {len(PROMPTS)} prompts  ",
        f"**R19 baseline (reference):** `{BASELINE_MODEL}` p50={BASELINE_P50_MS}ms quality={BASELINE_QUALITY:.2f}  ",
        "",
        "## Summary Table",
        "",
        "| Model | p50_ms | p95_ms | quality_avg | ok_ratio | vs_baseline_p50 |",
        "|-------|--------|--------|-------------|----------|----------------|",
    ]

    ordered = sorted(summary.items(), key=lambda x: x[1]["latency_p50_ms"])
    for model, stats in ordered:
        delta = stats["latency_p50_ms"] - BASELINE_P50_MS
        delta_str = f"+{delta}ms" if delta >= 0 else f"{delta}ms"
        lines.append(
            f"| `{model}` "
            f"| {stats['latency_p50_ms']} "
            f"| {stats['latency_p95_ms']} "
            f"| {stats['quality_avg']:.3f} "
            f"| {stats['ok_ratio']:.2f} "
            f"| {delta_str} |"
        )

    # Add baseline row (reference, not re-tested)
    lines.append(
        f"| `{BASELINE_MODEL}` *(R19 ref)* "
        f"| {BASELINE_P50_MS} "
        f"| — "
        f"| {BASELINE_QUALITY:.3f} "
        f"| 1.00 "
        f"| baseline |"
    )

    lines += [
        "",
        "## Per-Model Analysis",
        "",
    ]

    for model, stats in ordered:
        delta = stats["latency_p50_ms"] - BASELINE_P50_MS
        delta_str = f"+{delta}ms slower" if delta >= 0 else f"{abs(delta)}ms faster"
        lines += [
            f"### `{model}`",
            "",
            f"- p50={stats['latency_p50_ms']}ms ({delta_str} than baseline)",
            f"- p95={stats['latency_p95_ms']}ms",
            f"- quality_avg={stats['quality_avg']:.3f}",
            f"- ok_ratio={stats['ok_ratio']:.2f}",
            "",
            "| Prompt | p50_ms | p95_ms | quality | ok/runs | issues |",
            "|--------|--------|--------|---------|---------|--------|",
        ]
        for row in stats["rows"]:
            issues_str = ", ".join(row["issues"]) if row["issues"] else "—"
            lines.append(
                f"| {row['prompt']} "
                f"| {int(row['p50_ms'])} "
                f"| {int(row['p95_ms'])} "
                f"| {row['quality_score']:.2f} "
                f"| {row['ok_count']}/{row['total_runs']} "
                f"| {issues_str} |"
            )
        lines.append("")

    # Recommendation
    lines += [
        "## Recommendation",
        "",
        f"R19 baseline `{BASELINE_MODEL}`: p50={BASELINE_P50_MS}ms, quality={BASELINE_QUALITY:.3f}",
        "",
    ]

    # Check if any model beats baseline on latency with quality >= 1.0
    winners = [
        (m, s) for m, s in summary.items()
        if s["latency_p50_ms"] < BASELINE_P50_MS and s["quality_avg"] >= 0.95
    ]

    if winners:
        best = min(winners, key=lambda x: x[1]["latency_p50_ms"])
        lines += [
            f"> **UPGRADE** to `{best[0]}` — "
            f"p50={best[1]['latency_p50_ms']}ms (vs {BASELINE_P50_MS}ms baseline), "
            f"quality={best[1]['quality_avg']:.3f}. "
            f"Set `lm_rewriter_model` in Krab Ear settings.",
        ]
    else:
        best_quality = max(summary.items(), key=lambda x: x[1]["quality_avg"])
        lines += [
            f"> **HOLD** — no model beats baseline on both latency (<{BASELINE_P50_MS}ms) "
            f"and quality (≥0.95). Keep `{BASELINE_MODEL}`.",
            f">",
            f"> Best quality candidate: `{best_quality[0]}` "
            f"(p50={best_quality[1]['latency_p50_ms']}ms, quality={best_quality[1]['quality_avg']:.3f})",
        ]

    # Notes on specific models
    lines += [
        "",
        "## Model Notes",
        "",
        "### supergemma4-26b-abliterated-multimodal-mlx",
        "Multimodal variant (vision encoder loaded by default). May show extra latency due to",
        "vision components initialized even for text-only inference.",
        "",
        "### mlx-community/gemma-4-31b-it-assistant",
        "31B parameters at bf16 precision — significantly larger than 26B-4bit baseline.",
        "Expected slower due to both larger param count and higher precision (bf16 vs 4bit).",
        "",
        "### gemma-4-e2b-it-ultra-uncensored-heretic-mlx-int8-affine",
        "E2B (2B) model at int8-affine quantization. Much smaller than baseline —",
        "expected fastest but may have lower quality due to reduced capacity.",
        "",
        "## Raw JSON",
        "",
        "```json",
        json.dumps(
            {m: {k: v for k, v in s.items() if k != "rows"} for m, s in summary.items()},
            indent=2,
            ensure_ascii=False,
        ),
        "```",
    ]

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("R20 LLM Rewriter Benchmark — Krab Ear")
    print(f"Models: {len(MODELS)} | Prompts: {len(PROMPTS)} | Runs: {WARMUP_RUNS}+{TIMED_RUNS}")
    print(f"R19 baseline: {BASELINE_MODEL} p50={BASELINE_P50_MS}ms")
    print()

    if not verify_connection():
        print("FATAL: Cannot reach LM Studio. Aborting.", file=sys.stderr)
        sys.exit(1)

    all_results: list[dict] = []

    for i, model in enumerate(MODELS):
        if i > 0:
            print(f"\n[inter-model sleep {INTER_MODEL_SLEEP_SEC}s for TTL eviction...]")
            time.sleep(INTER_MODEL_SLEEP_SEC)

        results = benchmark_model(model, i)
        all_results.extend(results)

    summary = aggregate(all_results)

    # Write markdown report
    repo_root = Path(__file__).parent.parent
    out_path = repo_root / "docs" / "llm-bench-results-R20.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    md = build_markdown(summary)
    out_path.write_text(md, encoding="utf-8")

    print(f"\n{'='*60}")
    print(f"Report saved: {out_path}")
    print(f"{'='*60}")

    # Print summary table to stdout
    print()
    print(f"R19 baseline: {BASELINE_MODEL} p50={BASELINE_P50_MS}ms quality={BASELINE_QUALITY:.3f}")
    print()
    ordered = sorted(summary.items(), key=lambda x: x[1]["latency_p50_ms"])
    print(f"{'Model':<55} {'p50_ms':>8} {'quality':>8} {'ok_ratio':>9}")
    print("-" * 85)
    for model, stats in ordered:
        delta = stats["latency_p50_ms"] - BASELINE_P50_MS
        tag = f" ({'+' if delta >= 0 else ''}{delta}ms vs R19)"
        print(
            f"{model:<55} {stats['latency_p50_ms']:>8} "
            f"{stats['quality_avg']:>8.3f} {stats['ok_ratio']:>9.2f}{tag}"
        )


if __name__ == "__main__":
    main()
