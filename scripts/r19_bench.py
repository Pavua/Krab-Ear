#!/usr/bin/env python3
"""R19 LLM Rewriter Benchmark — Krab Ear
Тестирует 7 моделей (baseline + 6 candidates) на 5 русских транскриптах.
Output: docs/llm-bench-results-R19.md
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LM_STUDIO_URL = "http://localhost:1234/v1"


def _load_lm_studio_token() -> str:
    """Read LM Studio token from environment or shared `.env` file.

    Never hardcode the token here — Wave 47 security audit (CRIT-1) flagged
    a leaked token in this file. Now we read at runtime from:
    1. LM_STUDIO_TOKEN environment variable (preferred for CI/scripts)
    2. `~/Antigravity_AGENTS/Краб/.env` SENTRY_AUTH_TOKEN-style line
    """
    import os
    import re
    from pathlib import Path

    env_val = os.environ.get("LM_STUDIO_TOKEN", "").strip()
    if env_val:
        return env_val

    env_file = Path.home() / "Antigravity_AGENTS" / "Краб" / ".env"
    if env_file.exists():
        match = re.search(r"sk-lm-[A-Za-z0-9:_-]+", env_file.read_text())
        if match:
            return match.group(0)

    raise RuntimeError(
        "LM_STUDIO_TOKEN not found. Set env var or add to Krab/.env"
    )


LM_STUDIO_TOKEN = _load_lm_studio_token()
TIMEOUT_SEC = 120.0  # generous — first call may cold-load model
WARMUP_RUNS = 1      # warm-up calls before timed runs
TIMED_RUNS = 3       # timed calls per model per prompt

MODELS = [
    # baseline first
    "gemma-4-26b-a4b-it-optiq",
    # candidates
    "supergemma4-26b-uncensored-mlx-v2",
    "qwen3-14b-abliterated",
    "huihui-glm-4.7-flash-abliterated-mlx",
    "qwen3.6-27b-ud-mlx",
    "gemma-4-26b-a4b-it-assistant",
    "mlabonne_qwen3-8b-abliterated",
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
# Test prompts — 5 типовых русских STT транскриптов с типичными ошибками
# ---------------------------------------------------------------------------
PROMPTS = [
    {
        "name": "meeting_150w",
        "label": "Meeting transcript ~150 words",
        "text": (
            "ну значит сегодня встреча по проекту краб ир я хочу обсудить три вещи "
            "первое это интеграция с питоном второе развертывание на маки и третье "
            "у нас есть проблема с е е авторизациeй через опенаи ключ не работает "
            "стас ты можешь посмотреть это до конца недели да я думаю думаю это "
            "займет часа два максимум три хорошо а по поводу документации кто берет "
            "на себя Паша ты можешь написать технический раздел я напишу я напишу "
            "окей отлично следующая встреча в пятницу в два часа дня не забудьте "
            "обновить трело до встречи прикрепите скрины из xcode и гитхаб если есть "
            "вопросы пишите в слак канал разработки всем спасибо за внимание"
        ),
    },
    {
        "name": "phone_call_mat",
        "label": "Phone call ~100 words with mat",
        "text": (
            "алло ну блять опять не слышно подожди подожди я перезвоню нет нет "
            "слышу слышу говори ну короче там ситуация такая ё-моё заказ снова "
            "задержали на складе в швейцарии не в швеции в швейцарии понял "
            "так значит что делаем я звоню логистам прямо сейчас или ты "
            "блин я же говорил что нельзя через финляндию везти надо было "
            "через польшу ладно хватит ругаться что делаем звони им прямо "
            "сейчас скажи что мы теряем деньги каждый день ок понял позвоню"
        ),
    },
    {
        "name": "dictation_note",
        "label": "Dictation note ~80 words",
        "text": (
            "заметка на завтра нужно купить кофе молоко и хлеб потом позвонить "
            "врачу записаться на приём на следующей неделе ещо ещо надо проверить "
            "договор с арендодателем там есть пункт про ё и е путаница "
            "в правописании фамилии петрёв или петрев уточнить у него лично "
            "и да не забыть оплатить интернет до пятницы иначе отключат"
        ),
    },
    {
        "name": "tech_discussion",
        "label": "Technical discussion ~120 words (AI/programming)",
        "text": (
            "значит смотри мы используем млх вискер для транскрипции но есть проблема "
            "когда два потока обращаются к видеокарте одновременно мы получаем сигсегв "
            "потому что Эм Эл Икс не потокобезопасен нужен глобальный лок "
            "я добавил мтекс в кор слеш млх_лок.пай и оборачиваю все вызовы "
            "в контекст менеджер это должно решить проблему также хочу попробовать "
            "гигаам для русских транскриптов он лучше распознаёт мат и разговорную речь "
            "мы тестировали на реальном звонке скорость похожая но качество лучше "
            "особенно для слов типа швейцария финляндия которые вискер галлюцинирует"
        ),
    },
    {
        "name": "mixed_ru_en",
        "label": "Mixed Russian/English ~100 words",
        "text": (
            "окей давай обсудим деплой на прод я уже пушнул изменения в гитхаб "
            "ветка фиче слеш рефактор логгера пул реквест открыт можешь порявьювить "
            "там я поменял формат логов на джейсон как мы договаривались "
            "ещё добавил метрики через дата дог агент настроен на порт восемь тысяч "
            "и двадцать пять кстати у нас в слаке было сообщение что хуки в гитхаб "
            "экшнс упали на джоб билд свифт нужно проверить xcode настройки "
            "там может быть проблема с сертификатом ладно погнали"
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
# LM Studio API helpers
# ---------------------------------------------------------------------------

def get_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LM_STUDIO_TOKEN}",
    }


def verify_token() -> None:
    """Fail loud if LM Studio rejects the token."""
    resp = requests.get(f"{LM_STUDIO_URL}/models", headers=get_headers(), timeout=10)
    if resp.status_code == 401:
        print("FATAL: LM Studio rejected the API token (HTTP 401). Aborting.", file=sys.stderr)
        sys.exit(1)
    if resp.status_code != 200:
        print(f"FATAL: LM Studio /v1/models returned HTTP {resp.status_code}. Aborting.", file=sys.stderr)
        sys.exit(1)
    print(f"[OK] LM Studio reachable, token valid. {len(resp.json().get('data', []))} models loaded.")


def call_model(model: str, text: str) -> tuple[Optional[str], int, str]:
    """Returns (output_text_or_None, latency_ms, failure_reason).
    Never raises.
    """
    word_count = len(text.split())
    max_tokens = max(256, min(int(word_count * 3 * 1.3) + 50, 4096))
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
    try:
        resp = requests.post(
            f"{LM_STUDIO_URL}/chat/completions",
            json=payload,
            headers=get_headers(),
            timeout=TIMEOUT_SEC,
        )
    except requests.Timeout:
        return None, int((time.monotonic() - start) * 1000), "timeout"
    except requests.ConnectionError as exc:
        return None, int((time.monotonic() - start) * 1000), f"connection_error:{exc}"

    latency_ms = int((time.monotonic() - start) * 1000)

    if resp.status_code == 401:
        print("FATAL: LM Studio returned 401 mid-bench. Token revoked?", file=sys.stderr)
        sys.exit(1)
    if resp.status_code != 200:
        return None, latency_ms, f"http_{resp.status_code}"

    try:
        data = resp.json()
        content = data["choices"][0]["message"].get("content") or ""
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        return None, latency_ms, f"parse_error:{exc}"

    # postprocess: strip quotes and explanatory prefixes
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
    """Returns (0.0-1.0 score, list of issue flags)."""
    if output is None:
        return 0.0, [f"failed:{reason}"]

    issues = []
    score = 1.0

    # Length ratio: ideal 0.7–1.3
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

    # Chatbot leak check
    lower = output.lower()
    for marker in CHATBOT_MARKERS:
        if lower.startswith(marker):
            score -= 0.5
            issues.append(f"chatbot_marker:{marker[:20]}")
            break

    # Mat preservation: if input has mat words, output should too
    mat_words = ["блять", "блин", "ё-моё", "ёмоё", "хватит"]
    for w in mat_words:
        if w in input_text.lower() and w not in output.lower():
            score -= 0.05
            issues.append(f"mat_dropped:{w}")

    # <think> tag leak (reasoning model bleed)
    if "<think>" in output or "</think>" in output:
        score -= 0.3
        issues.append("think_tag_leak")

    # Language preserved: input is Russian, output should have Cyrillic
    cyrillic_in_output = sum(1 for c in output if "Ѐ" <= c <= "ӿ")
    if cyrillic_in_output < 5:
        score -= 0.4
        issues.append("no_cyrillic")

    return max(0.0, min(1.0, score)), issues


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def benchmark_model(model: str) -> list[dict]:
    """Run all prompts for one model. Returns list of result dicts."""
    print(f"\n{'='*60}")
    print(f"  MODEL: {model}")
    print(f"{'='*60}")
    results = []

    for prompt in PROMPTS:
        print(f"  Prompt: {prompt['name']} ... ", end="", flush=True)

        # Warm-up runs (not timed)
        for _ in range(WARMUP_RUNS):
            call_model(model, prompt["text"])

        # Timed runs
        latencies = []
        outputs = []
        reasons = []
        for _ in range(TIMED_RUNS):
            out, lat, reason = call_model(model, prompt["text"])
            latencies.append(lat)
            outputs.append(out)
            reasons.append(reason)

        # Use last successful output for quality scoring
        final_output = next((o for o in reversed(outputs) if o is not None), None)
        final_reason = reasons[-1]

        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)] if len(latencies) > 1 else latencies[0]
        quality, issues = score_quality(prompt["text"], final_output, final_reason)
        ok_count = sum(1 for r in reasons if r == "ok")

        print(f"p50={int(p50)}ms p95={int(p95)}ms quality={quality:.2f} ok={ok_count}/{TIMED_RUNS}")
        if issues:
            print(f"    issues: {', '.join(issues)}")
        if final_output:
            preview = final_output[:100].replace("\n", " ")
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
    """Aggregate per-prompt results into per-model summary."""
    by_model: dict[str, list] = {}
    for r in results:
        by_model.setdefault(r["model"], []).append(r)

    summary = {}
    for model, rows in by_model.items():
        all_lats = [lat for row in rows for lat in row["latencies_ms"]]
        all_q = [row["quality_score"] for row in rows]
        total_ok = sum(row["ok_count"] for row in rows)
        total_runs = sum(row["total_runs"] for row in rows)
        json_valid_ratio = total_ok / total_runs if total_runs > 0 else 0.0

        summary[model] = {
            "latency_p50_ms": int(statistics.median(all_lats)),
            "latency_p95_ms": int(sorted(all_lats)[int(len(all_lats) * 0.95)]),
            "quality_avg": sum(all_q) / len(all_q),
            "json_valid_ratio": json_valid_ratio,
            "rows": rows,
        }
    return summary


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

def build_markdown(summary: dict, all_results: list[dict]) -> str:
    baseline = "gemma-4-26b-a4b-it-optiq"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        "# LLM Rewriter Benchmark R19 — Krab Ear",
        "",
        f"**Date:** {ts}  ",
        f"**Hardware:** MacBook Pro M4 Max, 36 GB RAM  ",
        f"**LM Studio:** {LM_STUDIO_URL}  ",
        f"**Runs:** {WARMUP_RUNS} warmup + {TIMED_RUNS} timed × {len(PROMPTS)} prompts  ",
        "",
        "## Summary Table",
        "",
        "| Model | Role | latency_p50_ms | latency_p95_ms | quality_avg | json_valid_ratio |",
        "|-------|------|---------------|---------------|-------------|-----------------|",
    ]

    # Sort by p50 latency
    ordered = sorted(summary.items(), key=lambda x: x[1]["latency_p50_ms"])
    for model, stats in ordered:
        role = "**baseline**" if model == baseline else "candidate"
        lines.append(
            f"| `{model}` | {role} "
            f"| {stats['latency_p50_ms']} "
            f"| {stats['latency_p95_ms']} "
            f"| {stats['quality_avg']:.3f} "
            f"| {stats['json_valid_ratio']:.2f} |"
        )

    # Best picks
    best_latency_model = min(summary, key=lambda m: summary[m]["latency_p50_ms"])
    best_quality_model = max(summary, key=lambda m: summary[m]["quality_avg"])

    lines += [
        "",
        "## Analysis",
        "",
        f"- **Fastest (p50):** `{best_latency_model}` — {summary[best_latency_model]['latency_p50_ms']}ms",
        f"- **Best quality:** `{best_quality_model}` — {summary[best_quality_model]['quality_avg']:.3f}",
        f"- **Baseline:** `{baseline}` — p50={summary[baseline]['latency_p50_ms']}ms, quality={summary[baseline]['quality_avg']:.3f}",
        "",
        "## Per-Prompt Detail",
        "",
    ]

    for model, stats in ordered:
        lines.append(f"### `{model}`")
        lines.append("")
        lines.append("| Prompt | p50_ms | p95_ms | quality | ok/runs | issues |")
        lines.append("|--------|--------|--------|---------|---------|--------|")
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
    baseline_stats = summary[baseline]
    best_overall = max(
        summary,
        key=lambda m: summary[m]["quality_avg"] - (summary[m]["latency_p50_ms"] / 10000)
    )
    lines += [
        "## Recommendation",
        "",
        f"Baseline `{baseline}`: p50={baseline_stats['latency_p50_ms']}ms, quality={baseline_stats['quality_avg']:.3f}, json_valid={baseline_stats['json_valid_ratio']:.2f}",
        "",
        f"Best overall composite (quality − latency_penalty): `{best_overall}`",
        f"- p50={summary[best_overall]['latency_p50_ms']}ms",
        f"- quality={summary[best_overall]['quality_avg']:.3f}",
        f"- json_valid_ratio={summary[best_overall]['json_valid_ratio']:.2f}",
        "",
        "### Switch recommendation",
    ]

    if best_overall == baseline:
        lines.append(
            f"> **HOLD** — baseline `{baseline}` remains the best option. No switch recommended."
        )
    else:
        bq = summary[best_overall]["quality_avg"]
        bq_base = baseline_stats["quality_avg"]
        bl = summary[best_overall]["latency_p50_ms"]
        bl_base = baseline_stats["latency_p50_ms"]
        delta_q = (bq - bq_base) * 100
        delta_l = bl_base - bl
        lines.append(
            f"> **UPGRADE** to `{best_overall}` — "
            f"quality +{delta_q:.1f}pp vs baseline, "
            f"latency {'−' if delta_l > 0 else '+'}{abs(delta_l)}ms. "
            f"Set `lm_rewriter_model` in Krab Ear settings."
        )

    # Raw JSON appendix
    lines += [
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
    print("R19 LLM Rewriter Benchmark — Krab Ear")
    print(f"Models: {len(MODELS)} | Prompts: {len(PROMPTS)} | Runs: {WARMUP_RUNS}+{TIMED_RUNS}")
    print()

    verify_token()

    all_results: list[dict] = []
    for model in MODELS:
        results = benchmark_model(model)
        all_results.extend(results)

    summary = aggregate(all_results)

    # Write markdown report
    repo_root = Path(__file__).parent.parent
    out_path = repo_root / "docs" / "llm-bench-results-R19.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    md = build_markdown(summary, all_results)
    out_path.write_text(md, encoding="utf-8")
    print(f"\n{'='*60}")
    print(f"Report saved: {out_path}")
    print(f"{'='*60}")

    # Print summary table to stdout
    print()
    baseline = "gemma-4-26b-a4b-it-optiq"
    ordered = sorted(summary.items(), key=lambda x: x[1]["latency_p50_ms"])
    print(f"{'Model':<45} {'p50_ms':>8} {'p95_ms':>8} {'quality':>8} {'ok_ratio':>9}")
    print("-" * 85)
    for model, stats in ordered:
        tag = " ← baseline" if model == baseline else ""
        print(
            f"{model:<45} {stats['latency_p50_ms']:>8} {stats['latency_p95_ms']:>8} "
            f"{stats['quality_avg']:>8.3f} {stats['json_valid_ratio']:>9.2f}{tag}"
        )


if __name__ == "__main__":
    main()
